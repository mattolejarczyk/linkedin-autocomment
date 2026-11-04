"""The morning scan: verdicts, the status file, and "could not look" never reading as 0."""

import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from linkedin_automation import browser_lock
from linkedin_automation import morning_scan as ms
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import review_status as rs

PROFILE = "scanner"
URL = "https://www.linkedin.com/feed/update/urn:li:activity:1000000000000000{:03d}/"


@pytest.fixture
def profile():
    os.makedirs(pm.get_data_dir(PROFILE), exist_ok=True)
    with open(pm.get_config_path(PROFILE), "w", encoding="utf-8") as f:
        json.dump({"review": {"daily_limit": 2, "max_draft_age_days": 3,
                              "scan_max_posts": 30}}, f)
    return PROFILE


def _new_post(store, i, **fields):
    store.upsert_scraped({"url": URL.format(i), "text": "A post", "author_name": "Someone",
                          "relevance_score": 50}, post_store.NEW)
    store._resolve(URL.format(i)).update(fields)


def _drafted(i):
    store = post_store.load_synced_store(PROFILE)
    _new_post(store, i)
    store.mark_generated(URL.format(i), f"Draft {i}", save=True)


class Runner:
    """Stands in for the two child processes."""

    def __init__(self, finder_code=0, generator_code=0, scanned=5, qualified=2,
                 writes_file=True, posts=(1, 2)):
        self.calls = []
        self.finder_code = finder_code
        self.generator_code = generator_code
        self.scanned = scanned
        self.qualified = qualified
        self.writes_file = writes_file
        self.posts = posts

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if "linkedin_automation.post_finder" in args:
            return SimpleNamespace(returncode=self._finder())
        return SimpleNamespace(returncode=self._generator(args))

    def _finder(self):
        if self.finder_code == 0 and self.writes_file:
            store = post_store.load_synced_store(PROFILE)
            for i in self.posts:
                _new_post(store, i)
            store.save()
            path = os.path.join(pm.get_timeline_dir(PROFILE),
                                f"ai_posts_{datetime.now():%Y%m%d_%H%M%S}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"total_scanned": self.scanned,
                           "quality_found": self.qualified}, f)
        return self.finder_code

    def _generator(self, args):
        if self.generator_code == 0:
            with open(args[args.index("linkedin_automation.comment_generator") + 1],
                      encoding="utf-8") as f:
                posts = json.load(f)["quality_posts"]
            store = post_store.load_synced_store(PROFILE)
            for post in posts:
                store.mark_generated(post["url"], "A draft", save=True)
        return self.generator_code

    def ran(self, module):
        return any(module in call for call in self.calls)


def test_healthy_scan_tops_the_queue_up(profile):
    runner = Runner()
    status = ms.run_scan(PROFILE, runner=runner)
    assert status["verdict"] == rs.HEALTHY
    assert status["ready"] == 2 and status["new_drafts"] == 2
    assert status["posts_seen"] == 5 and status["qualified"] == 2
    assert status["scan_started_at"] and status["scan_finished_at"]
    assert rs.read_status(PROFILE)["ready"] == 2


def test_full_queue_means_no_scan_at_all(profile):
    _drafted(1)
    _drafted(2)
    runner = Runner()
    status = ms.run_scan(PROFILE, runner=runner)
    assert runner.calls == []
    assert status["verdict"] == rs.HEALTHY
    assert status["scan_skipped"] is True and status["reasons"] == ["queue_full"]


def test_scan_asks_only_for_what_is_missing(profile):
    _drafted(1)
    runner = Runner(posts=(2, 3, 4))
    status = ms.run_scan(PROFILE, runner=runner)
    finder = runner.calls[0]
    assert finder[finder.index("--min-quality") + 1] == "1"
    generator = runner.calls[1]
    assert generator[generator.index("--limit") + 1] == "1"
    assert status["ready"] == 2


@pytest.mark.parametrize("code, reason", [
    (pm.EXIT_LOGIN_REQUIRED, "login_required"),
    (pm.EXIT_PRODUCTION_REFUSED, "production_access_refused"),
    (pm.EXIT_ERROR, "post_finder_exit_1"),
])
def test_failed_scan_is_failed_not_zero_ready(profile, code, reason):
    _drafted(1)
    status = ms.run_scan(PROFILE, runner=Runner(finder_code=code))
    assert status["verdict"] == rs.FAILED and status["reasons"] == [reason]
    assert status["evidence"] and os.path.exists(status["evidence"])
    assert status["ready"] == 1          # real count, from the store


def test_production_flag_is_only_passed_when_asked_for(profile):
    runner = Runner()
    ms.run_scan(PROFILE, runner=runner)
    assert "--allow-production" not in runner.calls[0]

    os.remove(rs.status_path(PROFILE))
    store = post_store.load_synced_store(PROFILE)
    for rec in store.by_status(post_store.GENERATED):
        store.reject(rec["key"])
    store.save()
    runner = Runner(posts=(7, 8))
    ms.run_scan(PROFILE, allow_production=True, runner=runner)
    assert "--allow-production" in runner.calls[0]


def test_clean_exit_with_no_results_file_is_failed(profile):
    status = ms.run_scan(PROFILE, runner=Runner(writes_file=False))
    assert status["verdict"] == rs.FAILED and status["reasons"] == ["no_scan_output"]
    assert os.path.exists(status["evidence"])


def test_empty_feed_is_degraded(profile):
    status = ms.run_scan(PROFILE, runner=Runner(scanned=0, qualified=0, posts=()))
    assert status["verdict"] == rs.DEGRADED
    assert status["reasons"] == ["feed_showed_no_posts"]


def test_nothing_qualifying_is_healthy(profile):
    runner = Runner(qualified=0, posts=())
    status = ms.run_scan(PROFILE, runner=runner)
    assert status["verdict"] == rs.HEALTHY
    assert status["reasons"] == ["nothing_new_to_draft"]
    assert not runner.ran("linkedin_automation.comment_generator")


def test_drafting_failure_is_degraded_with_evidence(profile):
    status = ms.run_scan(PROFILE, runner=Runner(generator_code=1))
    assert status["verdict"] == rs.DEGRADED
    assert status["reasons"] == ["comment_generator_exit_1"]
    assert os.path.exists(status["evidence"])


def test_old_leftover_posts_are_not_drafted(profile):
    store = post_store.load_synced_store(PROFILE)
    _new_post(store, 9, scraped_at=(datetime.now() - timedelta(days=9)).isoformat())
    store.save()
    runner = Runner(qualified=0, posts=())
    status = ms.run_scan(PROFILE, runner=runner)
    assert not runner.ran("linkedin_automation.comment_generator")
    assert status["new_drafts"] == 0


def test_browser_in_use_is_degraded_and_runs_nothing(profile):
    other = browser_lock.ProfileBrowserLock(PROFILE, owner="review_run")
    assert other.acquire()
    try:
        _drafted(1)
        runner = Runner()
        status = ms.run_scan(PROFILE, runner=runner)
        assert runner.calls == []
        assert status["verdict"] == rs.DEGRADED
        assert status["reasons"] == ["browser_in_use"]
        assert status["ready"] == 1
    finally:
        other.release()


def test_lock_is_released_after_a_scan(profile):
    ms.run_scan(PROFILE, runner=Runner())
    assert browser_lock.is_held(PROFILE) is False


def test_timeout_is_failed(profile):
    import subprocess

    def hangs(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    status = ms.run_scan(PROFILE, runner=hangs)
    assert status["verdict"] == rs.FAILED and status["reasons"] == ["scan_timed_out"]


def test_timeout_really_ends_the_child(tmp_path):
    import subprocess
    import sys

    log = tmp_path / "step.log"
    started = datetime.now()
    with pytest.raises(subprocess.TimeoutExpired):
        with open(log, "w", encoding="utf-8") as out:
            ms._run_killing_tree([sys.executable, "-c", "import time; time.sleep(60)"],
                                 timeout=1, stdout=out, stderr=subprocess.STDOUT)
    assert (datetime.now() - started).total_seconds() < 30


def test_exit_codes_match_verdicts(profile, monkeypatch):
    for verdict, code in ((rs.HEALTHY, 0), (rs.FAILED, 1), (rs.DEGRADED, 2)):
        monkeypatch.setattr(ms, "run_scan", lambda *a, verdict=verdict, **k: {
            "verdict": verdict, "ready": 0, "new_drafts": 0, "reasons": [], "evidence": None})
        assert ms.main(["--profile", PROFILE]) == code


def test_a_crash_is_failed_with_evidence(profile, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(ms, "run_scan", boom)
    assert ms.main(["--profile", PROFILE]) == 1
    status = rs.read_status(PROFILE)
    assert status["verdict"] == rs.FAILED and status["reasons"] == ["crashed"]
    with open(status["evidence"], encoding="utf-8") as f:
        assert "unexpected" in f.read()


def test_status_only_says_no_scan_yet_and_runs_nothing(profile, monkeypatch):
    def no_scan(*a, **k):
        raise AssertionError("ran a scan")
    monkeypatch.setattr(ms, "run_scan", no_scan)
    _drafted(1)
    assert ms.main(["--profile", PROFILE, "--status-only"]) == 0
    status = rs.read_status(PROFILE)
    assert status["ready"] == 1
    assert status["verdict"] == rs.DEGRADED and status["reasons"] == ["no_scan_yet"]


def test_status_refresh_keeps_the_last_scan_result(profile):
    ms.run_scan(PROFILE, runner=Runner())
    rs.write_status(PROFILE)
    status = rs.read_status(PROFILE)
    assert status["verdict"] == rs.HEALTHY and status["posts_seen"] == 5


def test_old_drafts_do_not_count_as_ready(profile):
    _drafted(1)
    store = post_store.load_synced_store(PROFILE)
    store._resolve(URL.format(1))["generated_at"] = (
        datetime.now() - timedelta(days=4)).isoformat()
    store.save()
    assert rs.write_status(PROFILE)["ready"] == 0
