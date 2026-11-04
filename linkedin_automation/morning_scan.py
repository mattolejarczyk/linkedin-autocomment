"""One short read-only scan, drafts for what it found, and a verdict.

Meant for a daily scheduled task. It tops the review queue up to the daily limit
and stops: if the queue is already full there is no scan, so the account is not
touched for nothing. Nothing here posts. The scan is the existing post finder and
the drafts are the existing comment generator, each run as its own process so a
crash in either becomes a verdict here rather than a lost run.

Ends with HEALTHY / DEGRADED / FAILED printed, in the exit code (0 / 2 / 1) and in
``data/<profile>/review_status.json``. Every failure names its evidence file.
"""

import argparse
import glob
import logging
import os
import signal
import subprocess
import sys
import traceback
from datetime import datetime

from . import browser_lock
from . import post_store
from . import profile_manager as pm
from . import review_status as rs

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Hard stop for each child process. A scan that runs this long is stuck.
STEP_TIMEOUT_SECONDS = 20 * 60

_FINDER_EXIT_REASONS = {
    pm.EXIT_LOGIN_REQUIRED: "login_required",
    pm.EXIT_PRODUCTION_REFUSED: "production_access_refused",
}


def _kill_tree(pid):
    """End a process and everything it started, Chrome included.

    Killing only the child is not enough: on Windows a venv's python.exe is a
    launcher, so the real Python and its browser would carry on without it.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            logger.debug("process group %s already gone", pid, exc_info=True)


def _run_killing_tree(args, timeout=None, **kwargs):
    """subprocess.run, except a timeout ends the child's whole process tree."""
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    try:
        return subprocess.CompletedProcess(args, proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        proc.wait()
        raise


def _run_step(args, log_path, runner=_run_killing_tree):
    """Run one child process, appending its output to ``log_path``.

    Returns its exit code, or None if it hit the timeout.
    """
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with open(log_path, "a", encoding="utf-8") as log:
        log.write("\n$ %s\n" % " ".join(args))
        log.flush()
        try:
            return runner(args, cwd=PROJECT_ROOT, env=env, stdout=log,
                          stderr=subprocess.STDOUT,
                          timeout=STEP_TIMEOUT_SECONDS).returncode
        except subprocess.TimeoutExpired:
            return None


def _newest_scan_file(profile_name, since):
    """The post finder's output from this run, or None if it wrote none."""
    pattern = os.path.join(pm.get_timeline_dir(profile_name), "ai_posts_*.json")
    files = [f for f in glob.glob(pattern) if os.path.getmtime(f) >= since]
    return max(files, key=os.path.getmtime) if files else None


def _draftable(store, max_age_days, now):
    """NEW posts from recent scans, so an old leftover is never drafted."""
    fresh = []
    for rec in store.by_status(post_store.NEW):
        try:
            scraped = datetime.fromisoformat(rec.get("scraped_at") or "")
        except ValueError:
            continue
        if rec.get("url") and (now - scraped).total_seconds() <= max_age_days * 86400:
            fresh.append(rec)
    return fresh


def run_scan(profile_name, allow_production=False, runner=_run_killing_tree,
             now=None) -> dict:
    """Run the morning job and return the status it wrote."""
    lock = browser_lock.ProfileBrowserLock(profile_name, owner="morning_scan")
    if not lock.acquire():
        who = browser_lock.holder(profile_name)
        logger.warning("Browser in use by %s; no scan was run", who or "another process")
        return rs.write_status(
            profile_name, verdict=rs.DEGRADED, scan_skipped=True,
            reasons=["browser_in_use"], posts_seen=None, qualified=None,
            new_drafts=0, evidence=None,
            scan_started_at=datetime.now().isoformat(),
            scan_finished_at=datetime.now().isoformat())
    try:
        return _run_locked(profile_name, allow_production, runner, now)
    finally:
        lock.release()


def _run_locked(profile_name, allow_production, runner, now):
    started = datetime.now()
    settings = pm.get_review_settings(profile_name)
    limit = int(settings["daily_limit"])
    max_age = settings["max_draft_age_days"]

    def finish(verdict, reasons, **fields):
        fields.setdefault("scan_skipped", False)
        fields.setdefault("evidence", None)
        return rs.write_status(profile_name, verdict=verdict, reasons=reasons,
                               scan_started_at=started.isoformat(),
                               scan_finished_at=datetime.now().isoformat(),
                               **fields)

    store = post_store.load_synced_store(profile_name)
    ready_before = len(store.review_ready(max_age_days=max_age))
    wanted = limit - ready_before
    if wanted <= 0:
        return finish(rs.HEALTHY, ["queue_full"], scan_skipped=True, posts_seen=None,
                      qualified=None, new_drafts=0, store=store)

    log_dir = pm.get_data_dir(profile_name, "morning_scan")
    log_path = os.path.join(log_dir, f"scan_{started:%Y%m%d_%H%M%S}.log")

    finder = [sys.executable, "-m", "linkedin_automation.post_finder",
              "--profile", profile_name,
              "--max-posts", str(settings["scan_max_posts"]),
              "--min-quality", str(wanted)]
    if allow_production:
        finder.append("--allow-production")
    code = _run_step(finder, log_path, runner)
    if code != pm.EXIT_OK:
        reason = ("scan_timed_out" if code is None else
                  _FINDER_EXIT_REASONS.get(code, f"post_finder_exit_{code}"))
        logger.error("Scan FAILED (%s); evidence: %s", reason, log_path)
        return finish(rs.FAILED, [reason], evidence=log_path, posts_seen=None,
                      qualified=None, new_drafts=0)

    scan_file = _newest_scan_file(profile_name, started.timestamp() - 1)
    if not scan_file:
        logger.error("The scan exited cleanly but saved no results; evidence: %s", log_path)
        return finish(rs.FAILED, ["no_scan_output"], evidence=log_path, posts_seen=None,
                      qualified=None, new_drafts=0)
    scan = post_store._read_json(scan_file) or {}
    seen = scan.get("total_scanned")
    qualified = scan.get("quality_found")
    # A post the scan wanted but could not get a link for cannot be drafted, and it
    # goes to the Trash. The run still has drafts, so this would otherwise be silent.
    lost = sum(1 for p in scan.get("all_posts") or []
               if p.get("should_engage") and not p.get("url"))
    lost_reasons = [f"qualified_posts_with_no_link_{lost}"] if lost else []
    if lost:
        logger.warning("%d qualifying post(s) lost: no link could be copied; evidence: "
                       "data/<profile>/failures and %s", lost, log_path)
    lost_evidence = log_path if lost else None
    clean_verdict = rs.DEGRADED if lost else rs.HEALTHY
    if not seen:
        logger.warning("The feed showed no posts; evidence: %s", log_path)
        return finish(rs.DEGRADED, ["feed_showed_no_posts"], evidence=log_path,
                      posts_seen=seen, qualified=qualified, new_drafts=0)

    store = post_store.load_synced_store(profile_name)
    records = _draftable(store, max_age, now or datetime.now())[:wanted]
    if not records:
        return finish(clean_verdict, lost_reasons + ["nothing_new_to_draft"],
                      evidence=lost_evidence, posts_seen=seen, qualified=qualified,
                      new_drafts=0, store=store, qualified_lost=lost)

    input_path, _ = post_store.write_generator_input(profile_name, records)
    generator = [sys.executable, "-m", "linkedin_automation.comment_generator",
                 input_path, "--profile", profile_name, "--platform", "linkedin",
                 "--limit", str(wanted)]
    code = _run_step(generator, log_path, runner)

    store = post_store.load_synced_store(profile_name)
    new_drafts = len(store.review_ready(max_age_days=max_age)) - ready_before
    if code != pm.EXIT_OK:
        reason = "drafting_timed_out" if code is None else f"comment_generator_exit_{code}"
        logger.warning("Drafting DEGRADED (%s); evidence: %s", reason, log_path)
        return finish(rs.DEGRADED, lost_reasons + [reason], evidence=log_path, posts_seen=seen,
                      qualified=qualified, new_drafts=new_drafts, store=store,
                      qualified_lost=lost)
    return finish(clean_verdict, lost_reasons, evidence=lost_evidence, posts_seen=seen,
                  qualified=qualified, new_drafts=new_drafts, store=store, qualified_lost=lost)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--allow-production", action="store_true",
                        help="Allow the scan against a declared PRODUCTION identity")
    parser.add_argument("--status-only", action="store_true",
                        help="Refresh review_status.json from the store. No browser.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")

    if args.status_only:
        status = rs.write_status(args.profile)
        print(f"ready={status['ready']} last_verdict={status['verdict']} "
              f"file={rs.status_path(args.profile)}")
        return pm.EXIT_OK

    try:
        status = run_scan(args.profile, allow_production=args.allow_production)
    except Exception:
        crash = os.path.join(pm.get_data_dir(args.profile, "morning_scan"),
                             f"crash_{datetime.now():%Y%m%d_%H%M%S}.log")
        with open(crash, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        logger.exception("Morning scan crashed; evidence: %s", crash)
        status = rs.write_status(args.profile, verdict=rs.FAILED, reasons=["crashed"],
                                 evidence=crash, new_drafts=0,
                                 scan_finished_at=datetime.now().isoformat())

    print(f"VERDICT: {status['verdict']} ready={status['ready']} "
          f"new_drafts={status.get('new_drafts')} reasons={status['reasons']} "
          f"evidence={status.get('evidence')}")
    return rs.EXIT_CODES[status["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
