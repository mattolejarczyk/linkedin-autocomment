"""The review run: the human clicks Comment, and the code provably cannot.

No browser and no LinkedIn: a small fake page stands in for the post, and a
scripted "human" acts on it between the run's looks at the page.
"""

import ast
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from linkedin_automation import browser_lock
from linkedin_automation import comment_poster as cpm
from linkedin_automation import dashboard
from linkedin_automation import post_store
from linkedin_automation import profile_manager as pm
from linkedin_automation import review_run as rr
from linkedin_automation import review_status as rs
from linkedin_automation import scheduler as sched

PROFILE = "reviewer"
URL = "https://www.linkedin.com/feed/update/urn:li:activity:1000000000000000001/"
URL2 = "https://www.linkedin.com/feed/update/urn:li:activity:1000000000000000010/"
DRAFT = "A plain draft comment that is long enough to find again in the thread."

# Selenium's key codes for Enter, Return, Control and Meta.
SUBMIT_KEYS = ("", "", "", "")


class FakeElement:
    def __init__(self, text="", aria=None, displayed=True):
        self.text = text
        self.aria = aria
        self.displayed = displayed
        self.clicks = 0
        self.keys = []

    def is_displayed(self):
        return self.displayed

    def get_attribute(self, name):
        return self.aria if name == "aria-label" else None

    def click(self):
        self.clicks += 1

    def send_keys(self, keys):
        self.keys.append(keys)
        self.text += keys


class FakePage:
    """A post page. The comment box exists only once it has been opened."""

    title = "fake post"
    page_source = "<html></html>"

    def __init__(self, box_open=False):
        self.current_url = ""
        self.box = FakeElement()
        self.box_open = box_open
        self.submit = FakeElement("Comment")            # no aria-label, as on LinkedIn
        self.opener = FakeElement("12", aria="Comment")
        self.thread = FakeElement("Someone 2h ago An earlier comment")
        self.quit_called = False
        self.closed = False
        self.human = []                                 # steps run between looks
        self.idle_looks = 0
        self.insert_fails = False
        self.only_on_page = ""
        self.nav_outcome = None
        self.post_on_screen = True
        self.entity_comments = None
        self.scripts = []

    def find_elements(self, by, selector):
        if self.entity_comments is not None:
            # The page some accounts get: neither older thread hook matches at all.
            if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_ENTITY_SELECTOR:
                return self.entity_comments
            if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_CONTAINER_SELECTOR:
                return []
        if selector == rr.OPENER_SELECTOR:
            return [self.opener]
        if selector == cpm.LinkedInCommentPoster.SUBMIT_BUTTON_XPATH:
            return [self.submit]
        if selector == cpm.LinkedInCommentPoster.POSTED_COMMENT_CONTAINER_SELECTOR:
            return [self.thread] if self.thread.text else []
        if selector == "div[role='textbox'][contenteditable='true']":
            return [self.box] if self.box_open else []
        return []

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if script == cpm.LinkedInCommentPoster._FIND_COMPOSER_SUBMIT_JS:
            return self.submit
        if "execCommand('delete'" in script:
            self.box.text = ""
        if script == "return document.body.innerText":
            return "The post text " + self.box.text + " " + self.thread.text + self.only_on_page
        return None

    def execute_cdp_cmd(self, command, params):
        """Text inserted whole by the browser, not typed key by key."""
        assert command == "Input.insertText"
        if not self.insert_fails:
            self.box.text += params["text"]

    def save_screenshot(self, path):
        with open(path, "wb") as f:
            f.write(b"png")
        return True

    def quit(self):
        self.quit_called = True

    def tick(self):
        """Time passing: the next scripted thing the human does.

        The wait has no time limit by design, so a test whose human has finished
        fails here rather than hanging the suite.
        """
        if self.human:
            self.human.pop(0)(self)
            return
        self.idle_looks += 1
        if self.idle_looks > 200:
            raise KeyboardInterrupt("the run is waiting for a human who has finished")

    # ---- what the human does

    @staticmethod
    def clicks_comment(page):
        """Clicking Comment by hand: the box empties and the text joins the thread."""
        page.thread.text += " You now " + page.box.text
        page.box.text = ""

    @staticmethod
    def edits_to(text):
        def edit(page):
            page.box.text = text
        return edit


def _keys_typed(page):
    return "".join(page.box.keys)


@pytest.fixture
def env(monkeypatch):
    """A profile with quiet settings, and a poster wired to a fake page."""
    os.makedirs(pm.get_data_dir(PROFILE), exist_ok=True)
    with open(pm.get_config_path(PROFILE), "w", encoding="utf-8") as f:
        json.dump({"review": {"human_click_only": True, "min_seconds_between_posts": 0,
                              "posted_confirm_seconds": 0, "poll_seconds": 0,
                              "daily_limit": 5, "max_draft_age_days": 3}}, f)

    page = FakePage()

    class Poster(cpm.LinkedInCommentPoster):
        def setup_driver(self):
            self.driver = page

        def login(self):
            return True

        def navigate_to_post(self, url):
            self.last_navigation = {"url": url, "outcome": "ok", "reason": None}
            page.current_url = url
            page.box.text = ""          # a page load starts with an empty box
            if page.nav_outcome:
                self.last_navigation = {"url": url, "outcome": page.nav_outcome,
                                        "reason": "page load timed out"}
                return False
            return True

        def post_content_present(self):
            return page.post_on_screen

        @staticmethod
        def submit_button_is_enabled(button):
            return True

        def await_comment_input(self, timeout=None):
            return super().await_comment_input(timeout=0)

        def submit_button_candidates(self):
            return [page.submit]

    monkeypatch.setattr(rr, "LinkedInCommentPoster", Poster)
    monkeypatch.setattr(cpm.hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(cpm.hb, "scroll_to_element", lambda d, e: None)

    def human_click(driver, element):
        element.click()
        if element is page.opener:
            page.box_open = True

    monkeypatch.setattr(cpm.hb, "human_click", human_click)

    def type_like_human(driver, element, text):
        for char in text:
            element.send_keys(char)

    monkeypatch.setattr(cpm.hb, "type_like_human", type_like_human)
    # Only the review module's own clock. The poster's page-load polling must
    # not use up the human's steps before the draft has even been typed.
    monkeypatch.setattr(rr, "time", SimpleNamespace(time=time.time,
                                                    sleep=lambda s: page.tick()))
    return page


def _seed(url=URL, comment=DRAFT, **fields):
    store = post_store.load_synced_store(PROFILE)
    store.upsert_scraped({"url": url, "text": "The post", "author_name": "An Author",
                          "relevance_score": fields.pop("score", 50)}, post_store.NEW)
    store.mark_generated(url, comment, save=True)
    rec = store._resolve(url)
    rec.update(fields)
    store.save()
    return rec


def _log():
    path = os.path.join(pm.get_data_dir(PROFILE), rr.LOG_FILE)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _rec(url=URL):
    return post_store.load_synced_store(PROFILE)._resolve(url)


# ─── The submit path cannot be reached ────────────────────────────────────────

@pytest.mark.parametrize("module", ["review_run.py", "review_window.py"])
def test_source_has_no_way_to_submit(module):
    """Read from the source: none of these exist in the module at all."""
    path = os.path.join(os.path.dirname(cpm.__file__), module)
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add(node.name)
    banned = {"post_comment", "post_comment_ctrl_enter", "post_single_comment",
              "post_comment_ctrl", "submit_modifier", "Keys", "ENTER", "RETURN",
              "CONTROL", "find_composer_submit", "await_composer_submit",
              "send_keys", "click", "open_comment_box", "ActionChains"}
    assert not banned & names, banned & names


def test_the_only_click_helper_is_the_opener():
    """human_click appears once, in open_composer, on find_opener's result."""
    path = os.path.join(os.path.dirname(cpm.__file__), "review_run.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    calls = []
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "human_click"):
                calls.append(func.name)
    assert calls == ["open_composer"]


def test_a_whole_run_never_touches_the_submit(env):
    env.human = [FakePage.clicks_comment]
    _seed(comment="First line\nsecond line of a draft comment that has a newline in it")
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    assert env.submit.clicks == 0
    assert env.opener.clicks == 1
    typed = _keys_typed(env)
    assert "\n" not in typed
    assert not any(key in typed for key in SUBMIT_KEYS)
    assert env.quit_called


def test_the_runs_poster_refuses_to_submit_even_if_asked(env):
    run = rr.ReviewRun(PROFILE)
    env.human = [lambda page: _try_submit(run), FakePage.clicks_comment]
    _seed()
    run.run()
    assert env.submit.clicks == 0


def _try_submit(run):
    for attempt in (lambda: run.poster.post_comment("x"),
                    lambda: run.poster.post_comment_ctrl_enter(FakeElement(), "x"),
                    lambda: run.poster.post_single_comment({"url": URL, "comment": "x"})):
        with pytest.raises(pm.HumanClickOnlyRefused):
            attempt()


# ─── The guard on every other posting path ────────────────────────────────────

@pytest.fixture
def guarded_poster(env):
    return cpm.LinkedInCommentPoster(profile_name=PROFILE)


def test_guard_reads_on_from_the_config(guarded_poster):
    assert guarded_poster.human_click_only is True


def test_post_comment_refused_before_touching_the_page(guarded_poster):
    guarded_poster.driver = None        # any page access would raise AttributeError
    with pytest.raises(pm.HumanClickOnlyRefused):
        guarded_poster.post_comment("hello")


def test_keyboard_submit_refused_with_no_key_sent(guarded_poster, env):
    guarded_poster.driver = env
    box = FakeElement()
    with pytest.raises(pm.HumanClickOnlyRefused):
        guarded_poster.post_comment_ctrl_enter(box, "hello")
    assert box.keys == []


def test_posting_run_refused_before_any_browser(guarded_poster, monkeypatch):
    def no_browser():
        raise AssertionError("opened a browser")
    monkeypatch.setattr(guarded_poster, "setup_driver", no_browser)
    with pytest.raises(pm.HumanClickOnlyRefused):
        guarded_poster.run("anything.txt", 1)


def test_guard_off_by_default_keeps_the_poster_working(tmp_path):
    assert pm.human_click_only("no_such_profile_config") is False
    assert cpm.LinkedInCommentPoster(profile_name="other").human_click_only is False


def test_unreadable_config_fails_closed(env):
    with open(pm.get_config_path(PROFILE), "w", encoding="utf-8") as f:
        f.write("{ not json")
    assert pm.human_click_only(PROFILE) is True


def test_scheduler_refuses_and_writes_nothing(env):
    _seed()
    calls = []
    result = sched.Scheduler._fire_post_comments(
        SimpleNamespace(_submit_post_job=lambda *a: calls.append(a)),
        PROFILE, {}, datetime.now())
    assert result["action"] == sched.ACTION_REFUSED
    assert calls == []


def test_dashboard_post_route_refuses(env, monkeypatch):
    _seed()
    def no_job(*a, **k):
        raise AssertionError("started a posting job")
    monkeypatch.setattr(dashboard, "run_job", no_job)
    res = dashboard.app.test_client().post(f"/api/post/{PROFILE}", json={"count": 1})
    assert res.status_code == 409
    assert res.get_json()["human_click_only"] is True


# ─── Opening the box safely ───────────────────────────────────────────────────

@pytest.fixture
def poster(env):
    p = rr.LinkedInCommentPoster(profile_name=PROFILE)
    p.setup_driver()
    return p


def test_opener_clicks_only_the_labelled_button(env, poster):
    box, problem = rr.open_composer(poster)
    assert box is env.box and problem is None
    assert env.opener.clicks == 1 and env.submit.clicks == 0


def test_open_box_needs_no_click(env, poster):
    env.box_open = True
    box, problem = rr.open_composer(poster)
    assert box is env.box and problem is None
    assert env.opener.clicks == 0 and env.submit.clicks == 0


def test_box_that_already_holds_text_is_refused(env, poster):
    env.box_open = True
    env.box.text = "something already here"
    box, problem = rr.open_composer(poster)
    assert box is None and problem == "comment_box_already_has_text"
    assert env.opener.clicks == 0 and env.submit.clicks == 0


def test_a_submit_is_never_taken_for_the_opener(env, poster):
    """Even a submit carrying the opener's label stays unclicked."""
    env.opener = env.submit
    env.submit.aria = "Comment"
    box, problem = rr.open_composer(poster)
    assert box is None and problem == "comment_button_not_found"
    assert env.submit.clicks == 0


def test_no_opener_is_a_failure_not_a_guess(env, poster, monkeypatch):
    monkeypatch.setattr(env, "find_elements", lambda by, sel: [])
    box, problem = rr.open_composer(poster)
    assert box is None and problem == "comment_button_not_found"


# ─── Seeing the click ─────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


THREAD = "Someone 2h ago An earlier comment"


def _watcher(draft=DRAFT, confirm=20):
    clock = Clock()
    return rr.ClickWatcher(draft, confirm_seconds=confirm, clock=clock), clock


def test_posted_when_box_empties_and_text_is_in_the_thread():
    w, _ = _watcher()
    assert w.observe(DRAFT, THREAD) is None
    result = w.observe("", THREAD + " You now " + DRAFT)
    assert result["posted"] is True
    assert result["final_text"] == DRAFT and result["match"] == "full"


def test_edited_text_is_what_gets_reported():
    w, _ = _watcher()
    w.observe(DRAFT, THREAD)
    w.observe("My own wording of this comment.", THREAD)
    result = w.observe("", THREAD + " You now My own wording of this comment.")
    assert result["final_text"] == "My own wording of this comment."


def test_box_gone_counts_like_box_empty():
    w, _ = _watcher()
    w.observe(DRAFT, THREAD)
    assert w.observe(None, THREAD + " You now " + DRAFT)["posted"] is True


def test_a_collapsed_comment_matches_on_its_start():
    long_draft = "word " * 60
    w, _ = _watcher(long_draft)
    w.observe(long_draft, THREAD)
    shown = THREAD + " You now " + rr.normalize(long_draft)[:100] + " see more"
    result = w.observe("", shown)
    assert result["posted"] is True and result["match"] == "prefix"


def test_a_keyword_added_on_its_own_line_is_matched():
    """Real use: adding a keyword to get the free thing. Line break included."""
    with_keyword = DRAFT + "\nSkills"
    w, _ = _watcher()
    w.observe(DRAFT, THREAD)
    w.observe(with_keyword, THREAD)
    result = w.observe("", THREAD + " You now " + DRAFT + "\nSkills".replace("\n", " "))
    assert result["posted"] is True and result["match"] == "full"
    assert result["final_text"] == with_keyword


def test_a_keyword_added_after_the_last_look_still_counts_and_the_excerpt_shows_it():
    """The box was last seen without it, so last_text is short. The excerpt is not."""
    w, _ = _watcher()
    w.observe(DRAFT, THREAD)
    result = w.observe("", THREAD + " You now " + DRAFT + " Skills Like Reply")
    assert result["posted"] is True and result["match"] == "full"
    assert DRAFT + " Skills" in result["excerpt"]


def test_an_edit_near_the_start_after_the_last_look_still_counts():
    """Neither the whole text nor its first 80 characters are on the page now."""
    w, _ = _watcher()
    w.observe(DRAFT, THREAD)
    posted = "Totally changed the opening here, and then " + DRAFT[30:]
    result = w.observe("", THREAD + " You now " + posted)
    assert result["posted"] is True and result["match"] == "part"
    assert posted[:20] not in DRAFT and DRAFT[30:] in result["excerpt"]


def test_a_chance_short_overlap_is_not_a_match():
    w, clock = _watcher(confirm=20)
    w.observe(DRAFT, THREAD)
    assert w.observe("", THREAD + " Someone now A plain draft of something else") is None


def test_a_piece_already_on_the_page_before_does_not_count():
    w, _ = _watcher()
    with_piece = THREAD + " Earlier " + DRAFT[20:]
    w.observe(DRAFT, with_piece)
    assert w.observe("", with_piece) is None


def test_keyword_run_logs_the_edit_and_the_excerpt(env):
    with_keyword = DRAFT + "\nSkills"
    env.human = [FakePage.edits_to(with_keyword), FakePage.clicks_comment]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_EDITED
    assert entry["final_text"] == with_keyword and entry["draft_original"] == DRAFT
    assert "Skills" in entry["page_excerpt"]


def test_empty_box_alone_is_not_posted():
    """They cleared it to retype. However long that takes, keep waiting."""
    w, clock = _watcher(confirm=20)
    w.observe(DRAFT, THREAD)
    clock.now += 500
    assert w.observe("", THREAD) is None


def test_thread_changing_while_they_read_is_not_a_click():
    """Timestamps and other people's comments move while the box holds text."""
    w, clock = _watcher(confirm=20)
    w.observe(DRAFT, THREAD)
    clock.now += 500
    moved = "Someone 3h ago An earlier comment A stranger now Nice post"
    assert w.observe(DRAFT, moved) is None
    clock.now += 30
    assert w.observe("", moved) is None


def test_thread_changed_but_text_missing_is_unknown_never_a_guess():
    w, clock = _watcher(confirm=20)
    w.observe(DRAFT, THREAD)
    changed = THREAD + " You now something else entirely"
    assert w.observe("", changed) is None       # still inside the confirm window
    clock.now += 21
    result = w.observe("", changed)
    assert result["posted"] is None and result["reason"]


def test_first_comment_on_a_post_with_no_thread_yet():
    w, _ = _watcher()
    assert w.observe(DRAFT, None) is None
    assert w.observe("", "You now " + DRAFT)["posted"] is True


def test_same_text_already_in_the_thread_does_not_count():
    w, _ = _watcher()
    with_copy = THREAD + " Earlier " + DRAFT
    w.observe(DRAFT, with_copy)
    assert w.observe("", with_copy) is None


def test_unreadable_thread_is_not_read_as_posted():
    w, clock = _watcher(confirm=20)
    w.observe(DRAFT, THREAD)
    clock.now += 500
    assert w.observe("", None) is None


# ─── Waiting ──────────────────────────────────────────────────────────────────

class Reader:
    """A page the wait loop can look at, scripted one look at a time."""

    def __init__(self, looks, alive=True, left=None):
        self.looks = list(looks)
        self.alive = alive
        self.left = left

    def check_alive(self):
        if not self.alive:
            raise rr.BrowserClosed("gone")

    def box_text(self):
        return self.looks[0][0]

    def thread(self):
        look = self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]
        return look[1]

    def page_text(self):
        return None

    def comment_items(self):
        return None

    def left_the_post(self):
        return self.left


def test_wait_returns_on_skip():
    control = rr.ReviewControl()
    control.request_skip("draft_not_me")
    result = rr.wait_for_human(Reader([(DRAFT, THREAD)]), control, DRAFT, poll_seconds=0)
    assert result["outcome"] == rr.SKIPPED and result["reason"] == "skipped"
    assert result["skip_reason"] == "draft_not_me"
    assert control.take_skip() is None           # one press, one skip


def test_wait_returns_on_stop():
    control = rr.ReviewControl()
    control.request_stop()
    result = rr.wait_for_human(Reader([(DRAFT, THREAD)]), control, DRAFT, poll_seconds=0)
    assert result["outcome"] == rr.UNKNOWN and result["reason"] == "stopped_with_no_decision"


def test_closing_the_window_is_unknown():
    result = rr.wait_for_human(Reader([(DRAFT, THREAD)], alive=False),
                               rr.ReviewControl(), DRAFT, poll_seconds=0)
    assert result["outcome"] == rr.UNKNOWN and result["reason"] == "browser_closed"


def test_leaving_the_post_is_unknown():
    result = rr.wait_for_human(Reader([(DRAFT, THREAD)], left="redirected"),
                               rr.ReviewControl(), DRAFT, poll_seconds=0)
    assert result["outcome"] == rr.UNKNOWN and result["reason"] == "left_the_post"


# ─── A whole run ──────────────────────────────────────────────────────────────

def test_posted_as_is_is_logged_and_leaves_the_queue(env):
    env.human = [FakePage.clicks_comment]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY

    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_AS_IS
    assert entry["draft_original"] == DRAFT and entry["final_text"] == DRAFT
    assert _rec()["status"] == post_store.COMMENTED

    poster = rr.LinkedInCommentPoster(profile_name=PROFILE)
    assert URL in poster.progress["posted_comments"]
    assert rs.read_status(PROFILE)["ready"] == 0


def test_edit_in_the_box_is_logged_and_the_original_kept(env):
    edited = "My own wording, changed in the box before I clicked."
    env.human = [FakePage.edits_to(edited), FakePage.clicks_comment]
    _seed()
    rr.ReviewRun(PROFILE).run()

    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_EDITED
    assert entry["draft_original"] == DRAFT
    assert entry["loaded_text"] == DRAFT
    assert entry["final_text"] == edited
    rec = _rec()
    assert rec["comment"] == edited and rec["draft_original"] == DRAFT


def test_approved_text_is_what_loads_and_is_logged_apart(env):
    approved = "The wording I approved on the review screen."
    _seed()
    store = post_store.load_synced_store(PROFILE)
    store.mark_reviewed(URL, comment=approved, save=True)
    env.human = [FakePage.clicks_comment]
    rr.ReviewRun(PROFILE, approved_only=True).run()

    assert _keys_typed(env) == approved
    entry = _log()[-1]
    assert entry["draft_original"] == DRAFT
    assert entry["approved_text"] == approved
    assert entry["final_text"] == approved


def test_approved_only_ignores_unapproved_drafts(env):
    _seed()
    assert rr.ReviewRun(PROFILE, approved_only=True).run() == rs.HEALTHY
    assert env.quit_called is False           # nothing to do, no browser


def test_not_now_stays_queued_clears_the_box_and_moves_on(env):
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_skip("not_now"), FakePage.clicks_comment]
    _seed(URL, score=90)
    _seed(URL2, comment="A different draft comment, long enough to find in the thread.",
          score=10)
    rr.ReviewRun(PROFILE, control=control).run()

    outcomes = [e["outcome"] for e in _log()]
    assert outcomes == [rr.SKIPPED, rr.POSTED_AS_IS]
    assert _rec(URL)["status"] == post_store.GENERATED
    assert _rec(URL2)["status"] == post_store.COMMENTED
    assert env.submit.clicks == 0


def test_a_skip_cannot_be_requested_without_a_reason():
    control = rr.ReviewControl()
    with pytest.raises(TypeError):
        control.request_skip()
    for code, note in ((None, None), ("", None), ("made_up", None), ("other", None),
                       ("other", "   ")):
        with pytest.raises(ValueError):
            control.request_skip(code, note)
    assert control.take_skip() is None


def test_every_listed_reason_but_other_is_enough_alone():
    for code, _, _ in rr.SKIP_REASONS:
        expected = None if code != rr.OTHER_REASON else "Type your reason."
        assert rr.skip_reason_problem(code) == expected
    assert rr.skip_reason_problem(rr.OTHER_REASON, "My own words") is None


def test_free_text_reason_is_logged_word_for_word(env):
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_skip("other", "  Already replied by DM\n")]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    entry = _log()[-1]
    assert entry["outcome"] == rr.SKIPPED
    assert entry["skip_reason"] == "other"
    assert entry["skip_note"] == "  Already replied by DM\n"
    assert env.submit.clicks == 0


def test_a_reason_other_than_not_now_takes_it_out_of_the_queue(env):
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_skip("draft_not_me")]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["skip_reason"] == "draft_not_me"
    rec = _rec()
    assert rec["status"] == post_store.TRASH
    assert rec["draft_original"] == DRAFT
    assert rs.read_status(PROFILE)["ready"] == 0


def test_not_now_is_logged_and_offered_again(env):
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_skip("not_now")]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["skip_reason"] == "not_now"
    assert _rec()["status"] == post_store.GENERATED
    assert rs.read_status(PROFILE)["ready"] == 1


def test_stop_with_no_decision_is_unknown_held_with_evidence(env):
    """The failure from the first click test. They may have clicked Comment, so a
    Stop must not read as a skip, and the draft must not be offered again."""
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_stop()]
    _seed()
    assert rr.ReviewRun(PROFILE, control=control).run() == rs.DEGRADED
    entry = _log()[-1]
    assert entry["outcome"] == rr.UNKNOWN and entry["reason"] == "stopped_with_no_decision"
    assert entry["skip_reason"] is None
    assert entry["evidence"] and os.path.exists(entry["evidence"])
    rec = _rec()
    assert rec["status"] == post_store.GENERATED and rec["needs_check"]
    assert rs.read_status(PROFILE)["ready"] == 0


def test_comment_seen_with_no_thread_selector_counts_as_posted(env):
    """Clicked Comment, box empty, but no comment selector matches this page."""
    def posts_with_no_thread(page):
        page.only_on_page = " You now " + page.box.text
        page.box.text = ""
        page.thread.text = ""
    env.human = [posts_with_no_thread]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_AS_IS and entry["final_text"] == DRAFT
    assert _rec()["status"] == post_store.COMMENTED


def test_comment_posted_just_before_stop_is_posted_not_unknown(env):
    control = rr.ReviewControl()
    env.human = [lambda page: (FakePage.clicks_comment(page), control.request_stop())]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["outcome"] == rr.POSTED_AS_IS
    assert _rec()["status"] == post_store.COMMENTED


def test_comment_posted_just_before_skip_is_posted_not_trashed(env):
    control = rr.ReviewControl()
    env.human = [lambda page: (FakePage.clicks_comment(page),
                               control.request_skip("draft_not_me"))]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["outcome"] == rr.POSTED_AS_IS
    assert _rec()["status"] == post_store.COMMENTED


def test_box_text_alone_on_the_page_is_not_posted(env):
    """Whole-page text includes the box itself, so it only counts once the box is empty."""
    control = rr.ReviewControl()
    env.human = [lambda page: None, lambda page: None, lambda page: control.request_stop()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["outcome"] == rr.UNKNOWN


def test_i_posted_it_finds_the_text_even_if_linkedin_left_it_in_the_box(env):
    """Posted, but the box never emptied, so only their word can end the wait."""
    control = rr.ReviewControl()
    def posted_box_still_full(page):
        page.thread.text += " You now " + page.box.text
        control.say_posted()
    env.human = [posted_box_still_full]
    _seed()
    # DEGRADED: the comment is fine, but the run needed telling.
    assert rr.ReviewRun(PROFILE, control=control).run() == rs.DEGRADED
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_ON_THEIR_WORD and entry["final_text"] == DRAFT
    assert entry["evidence"] and os.path.exists(entry["evidence"])
    assert _rec()["status"] == post_store.COMMENTED


def test_i_posted_it_moves_on_and_records_their_word_when_nothing_is_visible(env):
    control = rr.ReviewControl()
    env.human = [lambda page: control.say_posted(), FakePage.clicks_comment]
    _seed(URL, score=90)
    _seed(URL2, comment="A different draft comment, long enough to find in the thread.",
          score=10)
    assert rr.ReviewRun(PROFILE, control=control).run() == rs.DEGRADED

    first, second = _log()
    assert first["outcome"] == rr.POSTED_ON_THEIR_WORD
    assert first["final_text"] is None
    assert first["last_box_text"] == DRAFT
    assert first["evidence"] and os.path.exists(first["evidence"])
    assert _rec(URL)["status"] == post_store.COMMENTED    # never offered again
    assert second["outcome"] == rr.POSTED_AS_IS           # and the run moved on
    assert env.submit.clicks == 0


def test_i_posted_it_never_touches_the_page(env):
    """It records their word. The only clicks in a run are the opener's."""
    control = rr.ReviewControl()
    env.human = [lambda page: control.say_posted()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert env.submit.clicks == 0
    assert not any(key in _keys_typed(env) for key in SUBMIT_KEYS)
    assert not any(".click(" in script for script in env.scripts)


def test_box_emptying_saves_the_page_and_a_trace_at_that_moment(env):
    env.human = [FakePage.clicks_comment]
    _seed()
    rr.ReviewRun(PROFILE).run()
    entry = _log()[-1]
    assert "box_emptied" in entry["evidence"] and os.path.exists(entry["evidence"])
    traces = os.listdir(pm.get_data_dir(PROFILE, "review_trace"))
    assert len(traces) == 1
    with open(os.path.join(pm.get_data_dir(PROFILE, "review_trace"), traces[0]),
              encoding="utf-8") as f:
        seen = [json.loads(line) for line in f]
    assert seen[0]["box_chars"] == len(DRAFT)
    assert seen[-1]["box_chars"] == 0


def test_empty_box_with_no_comment_visible_tells_them_what_to_do(env):
    control = rr.ReviewControl()
    def clears_box(page):
        page.box.text = ""
    env.human = [clears_box, lambda page: None, lambda page: control.say_posted()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert "I posted it" in control.message


def test_page_text_catches_a_repeat_when_thread_selectors_miss(env):
    env.thread.text = ""
    env.only_on_page = " You 1d " + DRAFT
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    assert _log()[-1]["outcome"] == rr.ALREADY_COMMENTED
    assert env.box.keys == []


def test_a_page_past_its_load_limit_but_on_screen_carries_on(env):
    """The first live timeout: the post, box and thread were all there."""
    env.nav_outcome = cpm.LinkedInCommentPoster.NAV_TIMEOUT
    env.human = [FakePage.clicks_comment]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    assert _log()[-1]["outcome"] == rr.POSTED_AS_IS


def test_a_page_past_its_load_limit_with_no_post_is_a_failure_with_evidence(env):
    env.nav_outcome = cpm.LinkedInCommentPoster.NAV_TIMEOUT
    env.post_on_screen = False
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.DEGRADED
    entry = _log()[-1]
    assert entry["outcome"] == rr.FAILED and os.path.exists(entry["evidence"])
    assert _rec()["status"] == post_store.GENERATED


def test_a_post_that_is_gone_does_not_count_as_slow(env):
    env.nav_outcome = cpm.LinkedInCommentPoster.NAV_UNAVAILABLE
    _seed()
    rr.ReviewRun(PROFILE).run()
    assert _log()[-1]["outcome"] == rr.UNAVAILABLE


def test_comments_are_read_on_the_page_some_accounts_get(env):
    """Taken from a live capture: only article.comments-comment-entity matches."""
    env.entity_comments = [FakeElement("Someone 2h ago An earlier comment")]
    poster = rr.LinkedInCommentPoster(profile_name=PROFILE)
    poster.setup_driver()
    count, texts = poster.comment_thread_snapshot()
    assert count is None      # lazily loaded, so a count proves nothing
    assert texts == ["Someone 2h ago An earlier comment"]


def test_the_click_is_seen_on_that_page(env):
    comments = [FakeElement("Someone 2h ago An earlier comment")]
    env.entity_comments = comments

    def clicks(page):
        comments.insert(0, FakeElement("You now " + page.box.text))
        page.box.text = ""

    env.human = [clicks]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_AS_IS and entry["thread_match"] == "full"


def test_duplicate_check_works_on_that_page(env):
    env.entity_comments = [FakeElement("You 1d " + DRAFT)]
    _seed()
    rr.ReviewRun(PROFILE).run()
    assert _log()[-1]["outcome"] == rr.ALREADY_COMMENTED
    assert env.box.keys == []


def _posts_but_box_stays_full(comments):
    def act(page):
        comments.insert(0, FakeElement("You now " + page.box.text + " Like Reply"))
    return act


def test_posting_is_seen_when_the_box_still_reads_full(env):
    """The fourth live run: the comment landed and the box still read 154 characters."""
    comments = [FakeElement("Someone 2h ago An earlier comment")]
    env.entity_comments = comments
    env.human = [_posts_but_box_stays_full(comments)]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_AS_IS and entry["thread_match"] == "full"
    assert entry["said_posted"] is False and entry["box_emptied"] is False
    assert entry["final_text"] == DRAFT and _rec()["status"] == post_store.COMMENTED
    assert env.box.text == ""                     # cleared, so leaving raises no prompt
    assert env.submit.clicks == 0


def test_an_edit_is_seen_when_the_box_still_reads_full(env):
    comments = [FakeElement("Someone 2h ago An earlier comment")]
    env.entity_comments = comments
    edited = DRAFT + "\nGROK"
    env.human = [FakePage.edits_to(edited), _posts_but_box_stays_full(comments)]
    _seed()
    rr.ReviewRun(PROFILE).run()
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_EDITED and entry["final_text"] == edited


def test_box_text_with_no_posted_comment_keeps_waiting(env):
    """Reading and editing must never count: the text is only in the box."""
    control = rr.ReviewControl()
    env.entity_comments = [FakeElement("Someone 2h ago An earlier comment")]
    env.human = [lambda page: None, FakePage.edits_to(DRAFT + " more words"), lambda page: None,
                 lambda page: control.request_stop()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["outcome"] == rr.UNKNOWN


def test_a_stranger_posting_while_they_read_is_not_their_click(env):
    control = rr.ReviewControl()
    comments = [FakeElement("Someone 2h ago An earlier comment")]
    env.entity_comments = comments
    env.human = [lambda page: comments.insert(0, FakeElement("A stranger now Nice post")),
                 lambda page: None, lambda page: control.request_stop()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[-1]["outcome"] == rr.UNKNOWN


def test_their_first_comment_on_a_post_with_no_comments(env):
    comments = []
    env.entity_comments = comments
    env.thread.text = ""
    env.human = [_posts_but_box_stays_full(comments)]
    _seed()
    rr.ReviewRun(PROFILE).run()
    assert _log()[-1]["outcome"] == rr.POSTED_AS_IS


def test_the_log_says_when_their_word_ended_the_wait(env):
    control = rr.ReviewControl()
    def posted_then_said(page):
        page.thread.text += " You now " + page.box.text
        control.say_posted()
    env.human = [posted_then_said]
    _seed()
    assert rr.ReviewRun(PROFILE, control=control).run() == rs.DEGRADED
    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_ON_THEIR_WORD
    assert entry["said_posted"] is True and entry["box_emptied"] is False


def test_a_box_that_empties_is_still_recorded_as_emptied(env):
    env.human = [FakePage.clicks_comment]
    _seed()
    rr.ReviewRun(PROFILE).run()
    entry = _log()[-1]
    assert entry["said_posted"] is False and entry["box_emptied"] is True


# Long, with line breaks and every kind of quote: good input must survive untouched.
MY_VERSION = ("Good point on \"scope creep\". It's the part that's hard to price.\n\n"
              "What did the first client say when the audit found a gap? " + "More words. " * 60)
DIRECTION = 'This is too negative. Instead find a point of agreement,\nthen ask a "question".'


def _clicks_after_using_my_version(control, text):
    return [lambda page: (control.give_feedback(rr.FEEDBACK_MY_VERSION, text, "draft_not_me"),
                          control.request_use_text(text)),
            lambda page: None,
            FakePage.clicks_comment]


def test_use_this_puts_their_version_in_the_box_and_they_click(env):
    control = rr.ReviewControl()
    env.human = _clicks_after_using_my_version(control, MY_VERSION)
    _seed()
    assert rr.ReviewRun(PROFILE, control=control).run() == rs.HEALTHY

    entry = _log()[-1]
    assert entry["outcome"] == rr.POSTED_EDITED
    assert entry["draft_original"] == DRAFT
    assert entry["final_text"] == MY_VERSION
    assert entry["feedback_type"] == rr.FEEDBACK_MY_VERSION
    assert entry["feedback_text"] == MY_VERSION
    assert entry["feedback_reason"] == "draft_not_me"
    assert entry["feedback_used_in_box"] is True
    assert entry["thread_match"] in ("full", "prefix")
    assert env.submit.clicks == 0
    assert "\n" not in _keys_typed(env)            # never typed key by key
    rec = _rec()
    assert rec["status"] == post_store.COMMENTED
    assert rec["comment"] == MY_VERSION and rec["draft_original"] == DRAFT


def test_use_this_alone_posts_nothing(env):
    """It fills the box and waits. The run ends only because they stop it."""
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_use_text(MY_VERSION), lambda page: None,
                 lambda page: control.request_stop()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert env.submit.clicks == 0
    assert _log()[-1]["outcome"] == rr.UNKNOWN
    assert _rec()["status"] == post_store.GENERATED


def test_direction_with_a_skip_is_logged_word_for_word(env):
    control = rr.ReviewControl()
    env.human = [lambda page: (control.give_feedback(rr.FEEDBACK_DIRECTION, DIRECTION, "draft_not_me"),
                               control.request_skip("draft_not_me"))]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    entry = _log()[-1]
    assert entry["outcome"] == rr.SKIPPED and entry["skip_reason"] == "draft_not_me"
    assert entry["feedback_type"] == rr.FEEDBACK_DIRECTION
    assert entry["feedback_text"] == DIRECTION
    assert entry["feedback_used_in_box"] is False
    assert entry["draft_original"] == DRAFT
    assert env.box.text == ""                      # direction never goes in the box


def test_my_version_logged_with_a_skip_stays_log_only(env):
    control = rr.ReviewControl()
    env.human = [lambda page: (control.give_feedback(rr.FEEDBACK_MY_VERSION, MY_VERSION),
                               control.request_skip("draft_not_me"))]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    entry = _log()[-1]
    assert entry["feedback_text"] == MY_VERSION and entry["feedback_used_in_box"] is False
    assert _rec()["comment"] == DRAFT


def test_feedback_does_not_carry_over_to_the_next_post(env):
    control = rr.ReviewControl()
    env.human = [lambda page: (control.give_feedback(rr.FEEDBACK_DIRECTION, DIRECTION),
                               control.request_skip("not_now")),
                 FakePage.clicks_comment]
    _seed(URL, score=90)
    _seed(URL2, comment="A different draft comment, long enough to find in the thread.",
          score=10)
    rr.ReviewRun(PROFILE, control=control).run()
    assert _log()[0]["feedback_text"] == DIRECTION
    assert _log()[1]["feedback_text"] is None


def test_feedback_never_changes_the_profile_config(env):
    before = open(pm.get_config_path(PROFILE), "rb").read()
    control = rr.ReviewControl()
    env.human = _clicks_after_using_my_version(control, MY_VERSION)
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert open(pm.get_config_path(PROFILE), "rb").read() == before


def test_text_that_does_not_land_says_so_and_keeps_evidence(env):
    control = rr.ReviewControl()
    env.insert_fails = True
    env.human = [lambda page: (control.give_feedback(rr.FEEDBACK_MY_VERSION, MY_VERSION),
                               control.request_use_text(MY_VERSION)),
                 lambda page: None,
                 lambda page: control.request_stop()]
    _seed()
    rr.ReviewRun(PROFILE, control=control).run()
    assert "did not go in" in control.message
    assert _log()[-1]["feedback_used_in_box"] is False
    failures = pm.get_data_dir(PROFILE, "failures")
    assert any("use_text_did_not_land" in name for name in os.listdir(failures))
    assert env.submit.clicks == 0


def test_blank_text_cannot_be_used_and_is_not_kept():
    control = rr.ReviewControl()
    with pytest.raises(ValueError):
        control.request_use_text("   \n ")
    control.give_feedback(rr.FEEDBACK_DIRECTION, "  \n")
    assert control.take_feedback() is None
    with pytest.raises(ValueError):
        control.give_feedback("made_up", "words")


@pytest.mark.parametrize("answers, expected", [
    (["2"], {"code": "sales_pitch", "note": None}),
    ([str(len(rr.SKIP_REASONS)), "My own words"], {"code": "other", "note": "My own words"}),
    ([str(len(rr.SKIP_REASONS)), "   "], None),
    (["99"], None),
    (["nope"], None),
])
def test_console_skip_also_requires_a_reason(monkeypatch, answers, expected):
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    control = rr.ReviewControl()
    rr._console_skip(control)
    assert control.take_skip() == expected


# ─── The skip window ──────────────────────────────────────────────────────────

# In a child process on purpose. Tk objects belong to the thread that made them,
# and garbage collected later on another thread they abort the whole interpreter,
# which took the rest of the test suite down with no summary line.
WINDOW_CHECK = r"""
import sys, tkinter
from linkedin_automation import review_run as rr, review_window

control = rr.ReviewControl()
try:
    win = review_window.ReviewWindow(control)
except tkinter.TclError as exc:
    print("NO DISPLAY", exc)
    sys.exit(0)
win.root.withdraw()
labels = dict((c, l) for c, l, _ in rr.SKIP_REASONS)
first_code, first_label, _ = rr.SKIP_REASONS[0]

assert list(win.dropdown["values"]) == [l for _, l, _ in rr.SKIP_REASONS]

win.open_skip_panel()
assert control.take_skip() is None, "pressing Skip alone must not skip"

win.confirm_skip()
assert control.take_skip() is None and win.problem.get(), "no reason must be refused"

win.reason.set(first_label)
win.cancel_skip()
assert control.take_skip() is None, "cancel must not skip"
assert win.panel_open is False and win.reason.get() == ""

win.open_skip_panel()
win.reason.set(first_label)
win.confirm_skip()
assert control.take_skip() == {"code": first_code, "note": None}
assert win.panel_open is False

win.open_skip_panel()
win.reason.set(labels[rr.OTHER_REASON])
win.confirm_skip()
assert control.take_skip() is None, "Other with no text must be refused"
win.set_words("My own words")
win.confirm_skip()
assert control.take_skip() == {"code": rr.OTHER_REASON, "note": "My own words"}
feedback = control.take_feedback()
assert feedback["kind"] == rr.FEEDBACK_DIRECTION and feedback["text"] == "My own words"

# Long text with line breaks and quotes comes out exactly as typed.
exact = 'Too "negative".\n\nFind agreement, then ask: it\'s better. ' + "More. " * 200
win.open_skip_panel()
win.reason.set(first_label)
win.set_words(exact)
win.confirm_skip()
assert control.take_skip()["code"] == first_code
assert control.take_feedback()["text"] == exact

# Use this: needs their own version, needs no reason, and does not skip.
win.open_skip_panel()
win.set_words("Only a direction")
win.use_this()
assert control.take_use_text() is None and win.problem.get(), "a direction cannot go in the box"
win.kind.set(rr.FEEDBACK_MY_VERSION)
win.set_words("")
win.use_this()
assert control.take_use_text() is None and win.problem.get(), "blank cannot go in the box"
win.set_words(exact)
win.use_this()
assert control.take_use_text() == exact
assert control.take_skip() is None, "Use this must not skip"
assert control.take_feedback()["kind"] == rr.FEEDBACK_MY_VERSION
assert win.panel_open is False and win.words() == ""

control.done.set()
win.refresh()
print("WINDOW OK")
"""


def test_skip_window_requires_a_reason():
    root = os.path.dirname(os.path.dirname(os.path.abspath(cpm.__file__)))
    result = subprocess.run([sys.executable, "-c", WINDOW_CHECK], cwd=root,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    if "NO DISPLAY" in result.stdout:
        pytest.skip(result.stdout)
    assert "WINDOW OK" in result.stdout


def test_unknown_is_held_back_with_evidence_named(env):
    env.human = [lambda page: (setattr(page.box, "text", ""),
                               setattr(page.thread, "text", THREAD + " a change"))]
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.DEGRADED

    entry = _log()[-1]
    assert entry["outcome"] == rr.UNKNOWN
    assert entry["evidence"] and os.path.exists(entry["evidence"])
    rec = _rec()
    assert rec["status"] == post_store.GENERATED and rec["needs_check"]
    assert rs.read_status(PROFILE)["ready"] == 0   # not offered again


def test_release_puts_a_held_draft_back(env):
    _seed(needs_check="could not tell")
    key = _rec()["key"]
    rr.resolve_by_hand(PROFILE, release=key)
    assert rs.read_status(PROFILE)["ready"] == 1


def test_mark_commented_by_hand_removes_it_from_ready(env):
    _seed()
    rr.resolve_by_hand(PROFILE, mark_commented=URL)
    assert _rec()["status"] == post_store.COMMENTED
    assert rs.read_status(PROFILE)["ready"] == 0
    assert env.quit_called is False


def test_comment_already_on_the_thread_is_not_typed_again(env):
    env.thread.text = THREAD + " You now " + DRAFT
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    assert _log()[-1]["outcome"] == rr.ALREADY_COMMENTED
    assert env.box.keys == []
    assert _rec()["status"] == post_store.COMMENTED


def test_stop_ends_the_run_and_closes_the_browser(env):
    control = rr.ReviewControl()
    env.human = [lambda page: control.request_stop()]
    _seed(URL, score=90)
    _seed(URL2, comment="A different draft comment, long enough to find in the thread.",
          score=10)
    rr.ReviewRun(PROFILE, control=control).run()
    assert [e["outcome"] for e in _log()] == [rr.UNKNOWN]
    assert env.quit_called


def test_count_limits_the_run_to_one_post(env):
    env.human = [FakePage.clicks_comment]
    _seed(URL, score=90)
    _seed(URL2, comment="A different draft comment, long enough to find in the thread.",
          score=10)
    rr.ReviewRun(PROFILE, count=1).run()
    assert len(_log()) == 1
    assert _rec(URL2)["status"] == post_store.GENERATED


def test_failed_composer_keeps_evidence_and_the_run_is_degraded(env, monkeypatch):
    monkeypatch.setattr(rr, "open_composer", lambda p: (None, "comment_button_not_found"))
    _seed()
    assert rr.ReviewRun(PROFILE).run() == rs.DEGRADED
    entry = _log()[-1]
    assert entry["outcome"] == rr.FAILED
    assert entry["evidence"] and os.path.exists(entry["evidence"])
    assert env.quit_called


def test_nothing_ready_opens_no_browser_and_is_healthy(env):
    assert rr.ReviewRun(PROFILE).run() == rs.HEALTHY
    assert env.quit_called is False
    assert rs.read_status(PROFILE)["ready"] == 0


def test_browser_lock_held_means_no_browser_and_failed(env):
    other = browser_lock.ProfileBrowserLock(PROFILE, owner="someone else")
    assert other.acquire()
    try:
        _seed()
        assert rr.ReviewRun(PROFILE).run() == rs.FAILED
        assert env.quit_called is False
    finally:
        other.release()


# ─── The store ────────────────────────────────────────────────────────────────

def test_review_ready_rules(env):
    now = datetime.now()
    _seed(URL)
    _seed(URL2, comment="Too old to count.",
          generated_at=(now - timedelta(days=4)).isoformat())
    store = post_store.load_synced_store(PROFILE)
    assert [r["url"] for r in store.review_ready(max_age_days=3)] == [URL]
    assert len(store.review_ready()) == 2

    store.mark_needs_check(URL, "unsure")
    assert store.review_ready(max_age_days=3) == []
    store.clear_needs_check(URL)
    assert len(store.review_ready(max_age_days=3)) == 1


def test_unreadable_draft_date_is_not_assumed_fresh(env):
    _seed(generated_at="not a date")
    store = post_store.load_synced_store(PROFILE)
    assert store.review_ready(max_age_days=3) == []


def test_mark_reviewed_keeps_the_original_even_on_old_records(env):
    _seed()
    store = post_store.load_synced_store(PROFILE)
    store._resolve(URL).pop("draft_original")       # as a record from before this field
    store.mark_reviewed(URL, comment="Edited on the screen.")
    rec = store._resolve(URL)
    assert rec["draft_original"] == DRAFT and rec["comment"] == "Edited on the screen."


def test_a_new_draft_clears_a_hold(env):
    _seed()
    store = post_store.load_synced_store(PROFILE)
    store.mark_needs_check(URL, "unsure")
    store.mark_generated(URL, "A fresh draft.")
    assert "needs_check" not in store._resolve(URL)


# ─── The lock ─────────────────────────────────────────────────────────────────

def test_lock_is_exclusive_and_releases(env):
    first = browser_lock.ProfileBrowserLock(PROFILE, owner="first")
    second = browser_lock.ProfileBrowserLock(PROFILE, owner="second")
    assert first.acquire() is True
    assert second.acquire() is False
    assert browser_lock.is_held(PROFILE) is True
    assert browser_lock.holder(PROFILE)["owner"] == "first"
    first.release()
    assert browser_lock.is_held(PROFILE) is False
    assert second.acquire() is True
    second.release()


def test_lock_is_dropped_when_its_process_dies(env):
    """A crash cannot leave a stale lock that blocks every later run.

    The child ends itself with os._exit, as a crash would. Killing it from here
    would only kill the venv's python.exe launcher on Windows, not the Python
    process behind it that holds the lock.
    """
    script = textwrap.dedent(f"""
        import os, sys
        from linkedin_automation import browser_lock, profile_manager as pm
        pm.DATA_ROOT = {pm.DATA_ROOT!r}
        lock = browser_lock.ProfileBrowserLock({PROFILE!r}, owner="child")
        print("held" if lock.acquire() else "refused", flush=True)
        sys.stdin.readline()
        os._exit(1)
    """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(cpm.__file__)))
    child = subprocess.Popen([sys.executable, "-c", script], cwd=root, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == "held"
        assert browser_lock.is_held(PROFILE) is True
        child.stdin.write("go\n")
        child.stdin.flush()
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        child.stdin.close()
        child.stdout.close()
    deadline = time.time() + 5        # the OS releases it shortly after the exit
    while browser_lock.is_held(PROFILE) and time.time() < deadline:
        time.sleep(0.1)
    assert browser_lock.is_held(PROFILE) is False


def test_dashboard_will_not_start_a_browser_task_under_the_lock(env):
    lock = browser_lock.ProfileBrowserLock(PROFILE, owner="review_run")
    assert dashboard.can_start_browser_task(PROFILE) is True
    assert lock.acquire()
    try:
        assert dashboard.can_start_browser_task(PROFILE) is False
    finally:
        lock.release()
