"""The list route: no browser, the human's words kept exactly, bad requests refused."""

import io
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from linkedin_automation import browser_lock
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import review_list as rl
from linkedin_automation import review_run as rr
from linkedin_automation import review_status as rs

PROFILE = "lister"
URL = "https://www.linkedin.com/feed/update/urn:li:activity:1000000000000000001/"
URL2 = "https://www.linkedin.com/feed/update/urn:li:activity:1000000000000000010/"
DRAFT = "A plain made-up draft comment."
# Long, with line breaks, every kind of quote and non-ASCII: it must survive untouched.
FINAL = ('A made-up final "version", it\'s longer.\n\n  Second paragraph, café \U0001F389 ’\n'
         + "More words. " * 80 + "\n")


@pytest.fixture
def profile():
    os.makedirs(pm.get_data_dir(PROFILE), exist_ok=True)
    with open(pm.get_config_path(PROFILE), "w", encoding="utf-8") as f:
        json.dump({"review": {"max_draft_age_days": 3}}, f)
    return PROFILE


def _seed(url=URL, comment=DRAFT, score=50, **fields):
    store = post_store.load_synced_store(PROFILE)
    store.upsert_scraped({"url": url, "text": "A made-up post, é.", "author_name": "An Author",
                          "relevance_score": score}, post_store.NEW)
    store.mark_generated(url, comment, save=True)
    rec = store._resolve(url)
    rec.update(fields)
    store.save()
    return rec


def _rec(url=URL):
    return post_store.load_synced_store(PROFILE)._resolve(url)


def _log():
    path = os.path.join(pm.get_data_dir(PROFILE), rr.LOG_FILE)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _run(capsys, *args, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    code = rl.main(["--profile", PROFILE, *args])
    out, err = capsys.readouterr()
    return code, out, err


# ─── --list ───────────────────────────────────────────────────────────────────

def test_list_is_one_json_object_in_ready_order(profile, capsys):
    _seed(URL, score=10)
    _seed(URL2, comment="Another made-up draft.", score=90)
    code, out, err = _run(capsys, "--list")
    assert code == 0
    data = json.loads(out)
    assert [d["url"] for d in data["drafts"]] == [URL2, URL]
    assert set(data["drafts"][0]) == {"key", "url", "author", "text", "comment", "generated_at",
                                      "relevance_score", "reviewed_at"}
    assert data["drafts"][1]["text"] == "A made-up post, é."
    assert out.isascii()


def test_list_gives_every_skip_reason_and_says_which_needs_a_note(profile, capsys):
    data = json.loads(_run(capsys, "--list")[1])
    assert [r["code"] for r in data["skip_reasons"]] == [c for c, _, _ in rr.SKIP_REASONS]
    assert [r["code"] for r in data["skip_reasons"] if r["needs_note"]] == [rr.OTHER_REASON]


def test_list_leaves_out_held_and_settled_drafts(profile, capsys):
    _seed(URL, needs_check="unsure")
    _seed(URL2, status=post_store.COMMENTED)
    assert json.loads(_run(capsys, "--list")[1])["drafts"] == []


def test_list_changes_nothing_and_works_while_a_run_holds_the_lock(profile, capsys):
    _seed()
    path = post_store.PostStore._default_path(PROFILE)
    before = open(path, "rb").read()
    other = browser_lock.ProfileBrowserLock(PROFILE, owner="review_run")
    assert other.acquire()
    try:
        code, out, _ = _run(capsys, "--list")
    finally:
        other.release()
    assert code == 0 and len(json.loads(out)["drafts"]) == 1
    assert open(path, "rb").read() == before


def test_an_unreadable_store_is_refused_not_read_as_nothing_ready(profile, capsys):
    _seed()
    path = post_store.PostStore._default_path(PROFILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json " * 40)
    code, out, err = _run(capsys, "--list")
    assert code == 1 and out == "" and "could not be read" in err
    assert len(err.strip().splitlines()) == 1


def test_a_mistyped_profile_is_refused_and_not_created(capsys):
    assert rl.main(["--profile", "no_such_profile", "--list"]) == 1
    assert "unknown profile" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(pm.DATA_ROOT, "no_such_profile"))


# ─── --posted ─────────────────────────────────────────────────────────────────

def test_posted_with_final_text_keeps_every_character_and_the_original(profile, capsys, monkeypatch):
    _seed()
    code, out, err = _run(capsys, "--posted", URL, "--final-text-stdin",
                          stdin=FINAL.encode("utf-8"), monkeypatch=monkeypatch)
    assert code == 0 and err == ""
    assert len(out.strip().splitlines()) == 1 and "posted" in out

    rec = _rec()
    assert rec["status"] == post_store.COMMENTED
    assert rec["comment"] == FINAL
    assert rec["draft_original"] == DRAFT

    entry = _log()[-1]
    assert entry["final_text"] == FINAL
    assert entry["draft_original"] == DRAFT
    assert entry["loaded_text"] == DRAFT


def test_the_log_says_it_was_their_word_and_not_read_back(profile, capsys):
    _seed()
    _run(capsys, "--posted", URL)
    entry = _log()[-1]
    assert entry["outcome"] == rl.OUTCOME_POSTED
    assert entry["said_posted"] is True
    assert entry["route"] == rl.ROUTE
    assert entry["thread_match"] is None and entry["page_excerpt"] is None


def test_posted_with_no_final_text_does_not_claim_the_draft_went_out(profile, capsys):
    """An edit made only in LinkedIn's box cannot be seen here, so it must not be asserted."""
    _seed()
    assert _run(capsys, "--posted", URL)[0] == 0
    entry = _log()[-1]
    assert entry["final_text"] is None
    assert entry["final_text_source"] == rl.SOURCE_NOT_SUPPLIED
    assert _rec()["comment"] == DRAFT


def test_posted_goes_through_the_same_ledger_as_a_run(profile, capsys):
    _seed()
    _run(capsys, "--posted", URL)
    poster = rr.LinkedInCommentPoster(profile_name=PROFILE)
    assert URL in poster.progress["posted_comments"]


def test_posted_updates_ready_and_keeps_the_last_scan_verdict(profile, capsys):
    _seed()
    rs.write_status(PROFILE, verdict=rs.HEALTHY, reasons=[])
    _run(capsys, "--posted", URL)
    status = rs.read_status(PROFILE)
    assert status["ready"] == 0 and status["verdict"] == rs.HEALTHY


def test_posting_settles_a_held_draft(profile, capsys):
    _seed(needs_check="unsure")
    assert _run(capsys, "--posted", URL)[0] == 0
    rec = _rec()
    assert rec["status"] == post_store.COMMENTED and "needs_check" not in rec


def test_an_old_record_keeps_its_original_too(profile, capsys, monkeypatch):
    rec = _seed()
    store = post_store.load_synced_store(PROFILE)
    store._resolve(URL).pop("draft_original")
    store.save()
    assert rec
    _run(capsys, "--posted", URL, "--final-text-stdin", stdin=b"Changed.", monkeypatch=monkeypatch)
    assert _rec()["draft_original"] == DRAFT


@pytest.mark.parametrize("args, stdin, expected", [
    (["--posted", "no-such-key"], None, "unknown key"),
    (["--posted", URL, "--final-text-stdin"], b"   \n", "empty"),
    (["--posted", URL, "--final-text-stdin"], b"\xff\xfe bad", "UTF-8"),
])
def test_bad_posted_requests_are_refused_and_change_nothing(profile, capsys, monkeypatch, args,
                                                            stdin, expected):
    _seed()
    code, out, err = _run(capsys, *args, stdin=stdin, monkeypatch=monkeypatch)
    assert code == 1 and out == "" and expected in err
    assert len(err.strip().splitlines()) == 1
    assert _rec()["status"] == post_store.GENERATED and _log() == []


def test_a_settled_post_cannot_be_settled_again(profile, capsys):
    _seed()
    assert _run(capsys, "--posted", URL)[0] == 0
    code, _, err = _run(capsys, "--posted", URL)
    assert code == 1 and "already settled" in err
    assert len(_log()) == 1


def test_a_post_with_no_draft_is_refused(profile, capsys):
    store = post_store.load_synced_store(PROFILE)
    store.upsert_scraped({"url": URL, "text": "A made-up post.", "author_name": "An Author"},
                         post_store.NEW, save=True)
    code, _, err = _run(capsys, "--posted", URL)
    assert code == 1 and "not a drafted comment" in err


# ─── --skip ───────────────────────────────────────────────────────────────────

def test_skip_with_a_reason_logs_it_and_moves_to_trash(profile, capsys):
    _seed()
    code, out, _ = _run(capsys, "--skip", URL, "--reason", "draft_not_me")
    assert code == 0 and "Trash" in out and len(out.strip().splitlines()) == 1
    entry = _log()[-1]
    assert entry["outcome"] == rr.SKIPPED and entry["skip_reason"] == "draft_not_me"
    assert entry["route"] == rl.ROUTE
    rec = _rec()
    assert rec["status"] == post_store.TRASH and rec["comment"] == DRAFT
    assert rs.read_status(PROFILE)["ready"] == 0


def test_not_now_stays_ready_and_can_be_skipped_again(profile, capsys):
    _seed()
    assert _run(capsys, "--skip", URL, "--reason", "not_now")[0] == 0
    assert _rec()["status"] == post_store.GENERATED
    assert rs.read_status(PROFILE)["ready"] == 1
    assert _run(capsys, "--skip", URL, "--reason", "not_now")[0] == 0


def test_other_needs_a_note_and_keeps_it_exactly(profile, capsys):
    _seed()
    code, _, err = _run(capsys, "--skip", URL, "--reason", "other")
    assert code == 1 and _rec()["status"] == post_store.GENERATED
    note = '  "Exact" words,\nkept as typed ’ '
    assert _run(capsys, "--skip", URL, "--reason", "other", "--note", note)[0] == 0
    assert _log()[-1]["skip_note"] == note


@pytest.mark.parametrize("args, expected", [
    (["--skip", URL, "--reason", "made_up"], "bad skip reason"),
    (["--skip", URL], "needs --reason"),
    (["--skip", "no-such-key", "--reason", "not_now"], "unknown key"),
])
def test_bad_skip_requests_are_refused_and_change_nothing(profile, capsys, args, expected):
    _seed()
    code, out, err = _run(capsys, *args)
    assert code == 1 and out == "" and expected in err
    assert _rec()["status"] == post_store.GENERATED and _log() == []


# ─── Safe alongside a scan or a run ───────────────────────────────────────────

@pytest.mark.parametrize("args", [["--posted", URL], ["--skip", URL, "--reason", "draft_not_me"]])
def test_writes_are_refused_while_a_scan_or_run_holds_the_lock(profile, capsys, args):
    _seed()
    other = browser_lock.ProfileBrowserLock(PROFILE, owner="morning_scan")
    assert other.acquire()
    try:
        code, out, err = _run(capsys, *args)
    finally:
        other.release()
    assert code == 1 and out == "" and "morning_scan" in err
    assert _rec()["status"] == post_store.GENERATED and _log() == []
    assert _run(capsys, *args)[0] == 0         # and it works once the lock is free


def test_the_lock_is_released_after_a_refusal_inside_it(profile, capsys):
    _run(capsys, "--posted", "no-such-key")
    assert browser_lock.is_held(PROFILE) is False


def test_nothing_here_can_open_a_browser_or_click():
    import ast
    source = os.path.join(os.path.dirname(rl.__file__), "review_list.py")
    with open(source, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    banned = {"setup_driver", "create_driver", "login", "navigate_to_post", "post_comment",
              "post_single_comment", "click", "send_keys", "webdriver"}
    assert not banned & names, banned & names


# Through a real process, because stray output on stdout would break the caller's JSON
# parse, and the in-process tests above replace stdin and stdout.
def _real_process(args, stdin=b""):
    script = (f"import sys; from linkedin_automation import profile_manager as pm, review_list as rl; "
              f"pm.DATA_ROOT = {pm.DATA_ROOT!r}; sys.exit(rl.main({['--profile', PROFILE, *args]!r}))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(rl.__file__)))
    return subprocess.run([sys.executable, "-c", script], cwd=root, input=stdin,
                          capture_output=True, timeout=120,
                          env={**os.environ, "PYTHONUTF8": "1"})


def test_a_real_process_prints_only_json_and_keeps_stdin_exact(profile):
    _seed()
    listed = _real_process(["--list"])
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["drafts"][0]["url"] == URL

    posted = _real_process(["--posted", URL, "--final-text-stdin"], FINAL.encode("utf-8"))
    assert posted.returncode == 0, posted.stderr
    assert len(posted.stdout.decode().strip().splitlines()) == 1
    assert _rec()["comment"] == FINAL and _log()[-1]["final_text"] == FINAL

    again = _real_process(["--posted", URL])
    assert again.returncode == 1 and again.stdout == b""
    assert b"already settled" in again.stderr


def test_unedited_text_sent_every_time_reads_as_is(profile, capsys, monkeypatch):
    """The screen always sends final text, so sending it must not read as an edit."""
    _seed()
    _run(capsys, "--posted", URL, "--final-text-stdin", stdin=(DRAFT + "\n").encode("utf-8"),
         monkeypatch=monkeypatch)
    entry = _log()[-1]
    assert entry["final_vs_draft"] == rl.AS_IS
    assert entry["final_text"] == DRAFT + "\n"              # still stored exactly


def test_changed_text_reads_edited(profile, capsys, monkeypatch):
    _seed()
    _run(capsys, "--posted", URL, "--final-text-stdin", stdin=b"Different words.",
         monkeypatch=monkeypatch)
    assert _log()[-1]["final_vs_draft"] == rl.EDITED


def test_no_final_text_makes_no_claim_either_way(profile, capsys):
    _seed()
    _run(capsys, "--posted", URL)
    assert _log()[-1]["final_vs_draft"] is None


def test_as_is_compares_with_the_original_draft_not_an_approved_edit(profile, capsys, monkeypatch):
    _seed()
    store = post_store.load_synced_store(PROFILE)
    store.mark_reviewed(URL, comment="Changed on the screen.", save=True)
    _run(capsys, "--posted", URL, "--final-text-stdin", stdin=b"Changed on the screen.",
         monkeypatch=monkeypatch)
    assert _log()[-1]["final_vs_draft"] == rl.EDITED


@pytest.mark.parametrize("note", [
    "--starts with dashes",
    "--reason=not_now",
    "-x",
    '"Quoted" words, it\'s\nkept = exactly \u2019 ',
])
def test_note_in_the_equals_form_survives_exactly_through_a_real_process(profile, note):
    _seed()
    done = _real_process(["--skip", URL, "--reason", "other", f"--note={note}"])
    assert done.returncode == 0, done.stderr
    entry = _log()[-1]
    assert entry["skip_note"] == note and entry["skip_reason"] == "other"


# ─── Another writer overwriting the store ─────────────────────────────────────

def _overwrite_store_with(content):
    with open(post_store.PostStore._default_path(PROFILE), "wb") as f:
        f.write(content)


def test_a_posted_record_lost_to_another_writer_is_never_offered_again(profile, capsys):
    """The duplicate-comment risk: the store goes back to its old contents afterwards."""
    _seed()
    path = post_store.PostStore._default_path(PROFILE)
    stale = open(path, "rb").read()
    assert _run(capsys, "--posted", URL)[0] == 0
    _overwrite_store_with(stale)
    assert post_store.PostStore(PROFILE)._resolve(URL)["status"] == post_store.GENERATED

    assert json.loads(_run(capsys, "--list")[1])["drafts"] == []
    code, _, err = _run(capsys, "--posted", URL)
    assert code == 1 and "already settled" in err
    assert len(_log()) == 1


def test_listing_applies_the_ledger_without_saving(profile, capsys):
    _seed()
    path = post_store.PostStore._default_path(PROFILE)
    stale = open(path, "rb").read()
    _run(capsys, "--posted", URL)
    _overwrite_store_with(stale)
    _run(capsys, "--list")
    assert open(path, "rb").read() == stale


# ─── Dashboard jobs that write the store ──────────────────────────────────────

def _wait_for(job_id, dashboard):
    deadline = time.time() + 20
    while dashboard.jobs[job_id]["status"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    return dashboard.jobs[job_id]


def test_a_job_that_writes_the_store_makes_review_list_refuse(profile, capsys):
    from linkedin_automation import dashboard
    _seed()
    started, finish = threading.Event(), threading.Event()

    def job(job_id):
        started.set()
        finish.wait(20)
        return "done"

    dashboard.run_job("lock_job", job, profile=PROFILE, category="scrape", takes_profile_lock=True)
    assert started.wait(20)
    try:
        code, _, err = _run(capsys, "--posted", URL)
        assert code == 1 and "busy" in err and "scrape" in err
        assert _rec()["status"] == post_store.GENERATED
    finally:
        finish.set()
    assert _wait_for("lock_job", dashboard)["status"] == "completed"
    assert browser_lock.is_held(PROFILE) is False
    assert _run(capsys, "--posted", URL)[0] == 0


def test_a_job_refused_by_the_lock_says_who_has_it_and_does_not_run(profile):
    from linkedin_automation import dashboard
    ran = []
    other = browser_lock.ProfileBrowserLock(PROFILE, owner="review_run")
    assert other.acquire()
    try:
        dashboard.run_job("busy_job", lambda job_id: ran.append(1), profile=PROFILE,
                          category="generate_comments", takes_profile_lock=True)
        job = _wait_for("busy_job", dashboard)
    finally:
        other.release()
    assert job["status"] == "failed" and "review_run" in job["error"]
    assert ran == []


def test_a_failing_job_still_releases_the_lock(profile):
    from linkedin_automation import dashboard

    def boom(job_id):
        raise RuntimeError("failed on purpose")

    dashboard.run_job("boom_job", boom, profile=PROFILE, category="scrape", takes_profile_lock=True)
    assert _wait_for("boom_job", dashboard)["status"] == "failed"
    assert browser_lock.is_held(PROFILE) is False


def test_scrape_and_generate_all_take_the_lock():
    from linkedin_automation import dashboard
    with open(dashboard.__file__, encoding="utf-8") as f:
        source = f.read()
    for category in ('category="scrape"', 'category="scheduled_scrape"',
                     'category="generate_comments"'):
        at = source.index(category)
        assert "takes_profile_lock=True" in source[at:at + 80], category
