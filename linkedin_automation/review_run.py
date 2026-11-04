"""Review drafted comments one at a time. The human clicks Comment, never the code.

Each ready post opens in the profile's logged-in Chrome with its draft typed into
LinkedIn's comment box, and the run stops there. The human reads it, edits in the
box if they want, and clicks Comment. The run sees that from the page, logs what
happened and loads the next post.

THIS MODULE MUST NEVER SUBMIT A COMMENT. It has no click on the composer's submit
and no keyboard submit, and tests/test_review_run.py checks that by reading this
file's source as well as by running it against a recording driver. The poster it
borrows from has ``human_click_only`` forced on, so its submit methods raise.

While waiting, it only reads the page: no mouse, no scrolling, no human_behavior
calls, because the human is using that window. The one exception is the human
pressing "Use this" in the control window, which puts their own wording in the box.
Even then the human clicks Comment.

Feedback typed in the control window goes in the log, word for word. It never
changes a persona or voice setting.
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime

from selenium.webdriver.common.by import By

from . import browser_lock
from . import human_behavior as hb
from . import post_store
from . import profile_manager as pm
from . import review_status as rs
from .comment_poster import LinkedInCommentPoster
from .failure_capture import capture_failure

logger = logging.getLogger(__name__)

LOG_FILE = "review_log.jsonl"

POSTED_AS_IS = "posted_as_is"
POSTED_EDITED = "posted_edited"
POSTED_ON_THEIR_WORD = "posted_on_their_word"
SKIPPED = "skipped"
UNKNOWN = "unknown"
FAILED = "failed"
UNAVAILABLE = "unavailable"
ALREADY_COMMENTED = "already_commented"

#: The opener's exact label. The composer's submit also reads "Comment" but has
#: no aria-label, which is what tells the two apart.
OPENER_SELECTOR = "button[aria-label='%s']" % LinkedInCommentPoster.SUBMIT_BUTTON_EXCLUDED_ARIA_LABEL

#: How much of a comment must match in the thread. LinkedIn can collapse a long
#: comment behind "see more", so the whole text is not always on the page.
MATCH_PREFIX_CHARS = 80

#: A run of this many characters of their text counts as finding it, for a comment
#: changed after the last look at the box. Long enough not to be chance wording.
MATCH_PIECE_CHARS = 40

#: How much of the page to keep around a match, so the log shows what really posted.
EXCERPT_EXTRA_CHARS = 200


#: Why a post was skipped: (code, what the dropdown shows, stays in the queue).
#: Only "not now" comes back. The rest leave the queue through the store's normal
#: reject, so they can be restored from the Trash view.
SKIP_REASONS = (
    ("not_relevant", "Post isn't relevant to me", False),
    ("sales_pitch", "Post is a sales pitch I don't want to engage with", False),
    ("draft_not_me", "Draft doesn't sound like me", False),
    ("draft_misses_point", "Draft misses the point of the post", False),
    ("author", "Don't want to engage with this author", False),
    ("not_now", "Not now, show me this again later", True),
    ("other", "Other (type your reason)", False),
)
OTHER_REASON = "other"


#: Shown while a draft waits. The comment is edited in LinkedIn's own box, not here.
EDIT_IN_LINKEDIN_MESSAGE = ("To change the comment, edit it right in LinkedIn's box. "
                            "Then click Comment there. Nothing here posts.")

#: What the human's typed words are.
FEEDBACK_MY_VERSION = "my_version"
FEEDBACK_DIRECTION = "direction"
FEEDBACK_KINDS = (FEEDBACK_DIRECTION, FEEDBACK_MY_VERSION)


def skip_reason_problem(code, note=None):
    """What is wrong with this skip reason, or None if it is acceptable."""
    if code not in {c for c, _, _ in SKIP_REASONS}:
        return "Choose a reason from the list."
    if code == OTHER_REASON and not (note or "").strip():
        return "Type your reason."
    return None


def skip_keeps_in_queue(code) -> bool:
    return any(c == code and keep for c, _, keep in SKIP_REASONS)


def normalize(text) -> str:
    return " ".join((text or "").split())


class BrowserClosed(Exception):
    """The human closed the window, or the browser died."""


class ReviewControl:
    """Skip / Stop requests from the control window, and progress for it to show."""

    def __init__(self):
        self._skip = None
        self._use_text = None
        self._feedback = None
        self._said_posted = threading.Event()
        self._stop = threading.Event()
        self.done = threading.Event()
        self.position = 0
        self.total = 0
        self.author = ""
        self.message = "Starting..."

    def request_skip(self, reason, note=None):
        """Ask to skip this post. A skip always carries a reason."""
        problem = skip_reason_problem(reason, note)
        if problem:
            raise ValueError(problem)
        self._skip = {"code": reason, "note": note if (note or "").strip() else None}

    def give_feedback(self, kind, text, reason=None):
        """Keep what the human typed for this post, exactly as typed."""
        if kind not in FEEDBACK_KINDS:
            raise ValueError(f"Unknown feedback type: {kind}")
        if (text or "").strip():
            self._feedback = {"kind": kind, "text": text, "reason": reason, "used": False}

    def take_feedback(self):
        """The feedback for this post, once. None if there is none."""
        feedback, self._feedback = self._feedback, None
        return feedback

    def request_use_text(self, text):
        """Ask for this text to replace what is in the comment box."""
        if not (text or "").strip():
            raise ValueError("Type your version first.")
        self._use_text = text

    def take_use_text(self):
        text, self._use_text = self._use_text, None
        return text

    def mark_feedback_used(self):
        if self._feedback:
            self._feedback["used"] = True

    def request_stop(self):
        self._stop.set()

    def say_posted(self):
        """The human says they posted this one. Records their word; submits nothing."""
        self._said_posted.set()

    def take_said_posted(self) -> bool:
        said = self._said_posted.is_set()
        self._said_posted.clear()
        return said

    def take_skip(self):
        """The reason given, once per Skip. None if no skip is waiting."""
        skip, self._skip = self._skip, None
        return skip

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()


class ClickWatcher:
    """Decides from what the page shows whether the human posted the comment.

    There is no click event to read without injecting code into LinkedIn's page,
    so this watches for the result. Posted means the text has appeared in a posted
    comment, whatever the box shows: on a live page the box still read as full after
    the comment posted. It also means the box is empty or gone AND the last text seen
    in it is now in the thread or on the page. An empty box alone proves nothing: the
    human may be retyping.

    The thread baseline keeps moving while the box holds text, because "2m ago"
    labels and other people's comments change the thread with no click at all.
    """

    def __init__(self, draft, confirm_seconds=20.0, clock=time.time):
        self.last_text = draft
        self.confirm_seconds = confirm_seconds
        self.clock = clock
        self.baseline = None
        self.emptied_at = None
        self.excerpt = None
        self.items_before = None

    def found_in_page(self, thread, page=None):
        """Is the last text in the thread, or failing that anywhere on the page?

        ``page`` is the whole page's text, which needs no comment selector at all.
        It is only meaningful once the box is empty, or it would find the box itself.
        Returns "full", "prefix", "part", or None. "part" is the comment changed after
        the last look, so ``last_text`` may not be exactly what posted. ``excerpt`` then
        holds what the page shows there, which is the better record.
        """
        wanted = normalize(self.last_text)
        known = self.baseline or ""
        if wanted[:MATCH_PREFIX_CHARS] in known:
            return None
        for text in (thread, page):
            if not text:
                continue
            if wanted in text:
                self._keep_excerpt(text, wanted, wanted)
                return "full"
            if wanted[:MATCH_PREFIX_CHARS] in text:
                self._keep_excerpt(text, wanted[:MATCH_PREFIX_CHARS], wanted)
                return "prefix"
            for start in range(0, max(len(wanted) - MATCH_PIECE_CHARS, 0) + 1, 10):
                piece = wanted[start:start + MATCH_PIECE_CHARS]
                if len(piece) == MATCH_PIECE_CHARS and piece in text and piece not in known:
                    self._keep_excerpt(text, piece, wanted, back=start)
                    return "part"
        return None

    def _keep_excerpt(self, text, found, wanted, back=0):
        at = max(text.index(found) - back, 0)
        self.excerpt = text[at:at + len(wanted) + EXCERPT_EXTRA_CHARS]

    def _found_in_a_comment(self, box_text, items):
        """Has the box's text appeared in a posted comment since the box was filled?

        ``items`` holds only posted comments, never the comment box, so its text
        cannot be the box's own. The first look only records what was already there.
        """
        if self.items_before is None:
            self.items_before = items or ""
            return None
        wanted = normalize(box_text)
        start = wanted[:MATCH_PREFIX_CHARS]
        if not items or start not in items:
            self.items_before = items or self.items_before
            return None
        if start in self.items_before:
            return None
        self._keep_excerpt(items, start, wanted)
        return "full" if wanted in items else "prefix"

    def observe(self, box_text, thread, page=None, items=None):
        """Feed one look at the page. Returns a result dict, or None to keep waiting.

        ``box_text`` is the box's text, "" if empty, None if it is gone.
        ``thread`` is the normalized thread text, None if it cannot be read.
        ``page`` is the normalized whole page text, read only once the box is empty.
        ``items`` is the normalized text of posted comments only, None if unreadable.
        """
        if box_text and box_text.strip():
            self.last_text = box_text
            self.emptied_at = None
            match = self._found_in_a_comment(box_text, items)
            if match:
                return {"posted": True, "final_text": box_text, "match": match,
                        "excerpt": self.excerpt}
            # Not if their text has appeared: LinkedIn may leave it in the box after
            # posting, and "I posted it" still has to be able to find it.
            if thread is not None and (
                    self.baseline is None
                    or normalize(box_text)[:MATCH_PREFIX_CHARS] not in thread):
                self.baseline = thread
            return None

        if self.emptied_at is None:
            self.emptied_at = self.clock()

        match = self.found_in_page(thread, page)
        if match:
            return {"posted": True, "final_text": self.last_text, "match": match,
                    "excerpt": self.excerpt}

        if self.clock() - self.emptied_at < self.confirm_seconds:
            return None
        # "or ''": a post with no comments has no thread until the first one lands.
        if thread and thread != (self.baseline or ""):
            return {"posted": None, "final_text": None,
                    "reason": "box_emptied_and_thread_changed_but_text_not_found"}
        return None      # still nothing: they cleared the box and are thinking


def find_opener(poster):
    """The action-bar button that opens the comment box, or None.

    The composer's submit also reads "Comment", so this never matches on visible
    text the way LinkedInCommentPoster.open_comment_box does. Only the exact
    aria-label qualifies, and anything the poster recognises as a submit is out.
    """
    submits = poster.submit_button_candidates()
    for button in poster.driver.find_elements(By.CSS_SELECTOR, OPENER_SELECTOR):
        try:
            if button.is_displayed() and not any(button == s for s in submits):
                return button
        except Exception:
            continue
    return None


def open_composer(poster):
    """Get an EMPTY comment box on screen. Returns ``(box, problem)``.

    Refuses if any box already holds text. A click with text in a composer is the
    one situation where a wrong click could publish something, and an empty
    comment cannot be submitted, so the click only ever happens with none.
    """
    box = poster.await_comment_input(timeout=1.0)
    if box is None:
        button = find_opener(poster)
        if button is None:
            return None, "comment_button_not_found"
        hb.scroll_to_element(poster.driver, button)
        hb.human_click(poster.driver, button)
        box = poster.await_comment_input()
        if box is None:
            return None, "comment_box_did_not_open"
    if (box.text or "").strip():
        return None, "comment_box_already_has_text"
    return box, None


class PageReader:
    """Read-only looks at the current page. Nothing here clicks, types or scrolls."""

    def __init__(self, poster, url):
        self.poster = poster
        self.url = url

    def check_alive(self):
        try:
            return self.poster.driver.current_url
        except Exception as exc:
            raise BrowserClosed(str(exc)) from exc

    def box_text(self):
        """The box's text, "" if empty, None if no box is on screen."""
        driver = self.poster.driver
        for selector in self.poster.COMMENT_INPUT_SELECTORS:
            try:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    if element.is_displayed():
                        return element.text or ""
            except Exception:
                continue
        return None

    def thread(self):
        _, texts = self.poster.comment_thread_snapshot()
        return normalize(" ".join(texts)) if texts else None

    def comment_items(self):
        """The text of posted comments only. Unlike thread(), this never includes the
        comment box, because it reads per-comment elements and not the list around them."""
        poster = self.poster
        for selector in (poster.POSTED_COMMENT_SELECTOR, poster.POSTED_COMMENT_ENTITY_SELECTOR):
            try:
                elements = poster.driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    return normalize(" ".join(element.text or "" for element in elements))
            except Exception:
                continue
        return None

    def page_text(self):
        """All the page's visible text. Needs no comment selector, so it still works
        when LinkedIn serves markup those selectors do not match."""
        try:
            return normalize(self.poster.driver.execute_script(
                "return document.body.innerText"))
        except Exception:
            return None

    def left_the_post(self):
        return self.poster.navigated_away_from(self.url)


def wait_for_human(reader, control, draft, poll_seconds=0.75, confirm_seconds=20.0,
                   put_in_box=None, on_box_emptied=None, trace=None):
    """Wait until the human posts, skips, stops or says they posted. No time limit.

    It only reads the page, unless the human asks for their own wording to go in
    the box, which ``put_in_box`` does.

    The page is read BEFORE acting on any button, so a comment already posted is
    seen as posted, not skipped. A Stop with no decision yet is never logged as a
    skip: the human may have clicked Comment, so the outcome is unknown.

    ``on_box_emptied`` is called once per post, the first time the box empties, so
    the page is on file at the moment that matters. ``trace`` is called with what
    was seen each time it changes.

    Returns ``{"outcome": ..., "final_text": ..., "reason": ..., "match": ...}``.
    """
    watcher = ClickWatcher(draft, confirm_seconds=confirm_seconds)
    told_cannot_see = False
    captured = False
    last_seen = None

    def undecided(reason):
        return {"outcome": UNKNOWN, "final_text": None, "reason": reason,
                "last_box_text": watcher.last_text}

    while True:
        try:
            reader.check_alive()
        except BrowserClosed:
            return undecided("browser_closed")

        text = control.take_use_text()
        if text and put_in_box and put_in_box(text) is False:
            told_cannot_see = True      # keep the window's more specific message

        box = reader.box_text()
        thread = reader.thread()
        empty = not (box and box.strip())
        page = reader.page_text() if empty else None
        items = reader.comment_items()
        seen = (len(box) if box is not None else None,
                len(thread) if thread is not None else None, empty,
                len(items) if items is not None else None)
        if trace and seen != last_seen:
            trace({"box_chars": seen[0], "box_end": (box or "")[-60:],
                   "thread_chars": seen[1], "comment_chars": seen[3],
                   "page_chars": len(page) if page else None})
            last_seen = seen

        if empty and not captured:
            captured = True
            if on_box_emptied:
                on_box_emptied()

        result = watcher.observe(box, thread, page, items)
        if result is not None and result["posted"]:
            return {"outcome": "posted", "final_text": result["final_text"],
                    "match": result["match"], "excerpt": result["excerpt"], "reason": None,
                    "box_emptied": empty}

        if control.take_said_posted():
            return _read_back_after_their_word(reader, watcher, poll_seconds, confirm_seconds)
        if control.stop_requested:
            return undecided("stopped_with_no_decision")
        skip = control.take_skip()
        if skip:
            return {"outcome": SKIPPED, "final_text": None, "reason": "skipped",
                    "skip_reason": skip["code"], "skip_note": skip["note"],
                    "last_box_text": watcher.last_text}

        if result is not None:
            return undecided(result["reason"])
        if reader.left_the_post():
            return undecided("left_the_post")

        if empty and not told_cannot_see and watcher.emptied_at is not None \
                and watcher.clock() - watcher.emptied_at >= confirm_seconds:
            told_cannot_see = True
            control.message = ("The box is empty but I can't see your comment on the page. "
                               'If you posted it, press "I posted it".')
        time.sleep(poll_seconds)


def _read_back_after_their_word(reader, watcher, poll_seconds, confirm_seconds):
    """They say it posted. Their word decides; the page only adds the final text."""
    deadline = watcher.clock() + confirm_seconds
    while True:
        box = reader.box_text()
        empty = not (box and box.strip())
        match = watcher.found_in_page(reader.thread(), reader.page_text() if empty else None)
        if match:
            return {"outcome": "said_posted", "final_text": watcher.last_text, "match": match,
                    "excerpt": watcher.excerpt, "reason": None,
                    "last_box_text": watcher.last_text, "box_emptied": empty}
        if watcher.clock() >= deadline:
            return {"outcome": "said_posted", "final_text": None, "match": None,
                    "reason": "said_posted_read_back_found_nothing",
                    "last_box_text": watcher.last_text, "box_emptied": empty}
        time.sleep(poll_seconds)


class ReviewRun:
    def __init__(self, profile_name, allow_production=False, control=None, count=None,
                 approved_only=False):
        self.profile_name = profile_name
        self.allow_production = allow_production
        self.control = control or ReviewControl()
        self.count = count
        self.approved_only = approved_only
        self.settings = pm.get_review_settings(profile_name)
        self.poster = None
        self.store = None
        self.tally = {}
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ---- the log

    def _log_path(self):
        return os.path.join(pm.get_data_dir(self.profile_name), LOG_FILE)

    def log_outcome(self, rec, outcome, loaded_text=None, final_text=None, reason=None,
                    match=None, evidence=None, last_box_text=None, skip_reason=None,
                    skip_note=None, feedback=None, excerpt=None, said_posted=False,
                    box_emptied=None, route="review_run", final_text_source=None,
                    final_vs_draft=None):
        """One line per post: the draft, what was approved, and what was posted."""
        self.tally[outcome] = self.tally.get(outcome, 0) + 1
        entry = {
            "at": datetime.now().isoformat(),
            "run_id": self.run_id,
            "route": route,
            "final_text_source": final_text_source,
            "final_vs_draft": final_vs_draft,
            "url": rec.get("url"),
            "key": rec.get("key"),
            "author": rec.get("author"),
            "draft_original": rec.get("draft_original") or rec.get("comment"),
            "approved_text": rec.get("comment") if rec.get("reviewed_at") else None,
            "loaded_text": loaded_text,
            "final_text": final_text,
            "last_box_text": last_box_text,
            "outcome": outcome,
            "reason": reason,
            "skip_reason": skip_reason,
            "skip_note": skip_note,
            "feedback_type": (feedback or {}).get("kind"),
            "feedback_text": (feedback or {}).get("text"),
            "feedback_reason": (feedback or {}).get("reason"),
            "feedback_used_in_box": (feedback or {}).get("used", False),
            "thread_match": match,
            "page_excerpt": excerpt,
            "said_posted": said_posted,
            "box_emptied": box_emptied,
            "evidence": evidence,
        }
        with open(self._log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info("REVIEW %s post=%s reason=%s evidence=%s",
                    outcome, rec.get("url"), reason, evidence)

    # ---- the store

    def _mark_commented(self, url):
        """COMMENTED comes from the posted ledger, as it does for the poster."""
        poster = self.poster
        posted = poster.progress.setdefault("posted_comments", [])
        if url not in posted:
            posted.append(url)
        poster.progress["last_posted"] = datetime.now().isoformat()
        poster.save_progress()
        self.store.sync_with_progress(posted, save=True)

    # ---- one post

    def _evidence(self, label):
        return capture_failure(self.poster.driver, f"review_{label}", self.profile_name)

    def review_one(self, rec):
        """Load one post with its draft in the box and wait for the human."""
        poster = self.poster
        url = rec["url"]
        draft = rec.get("comment") or ""
        # The store record's comment changes once the final text is known, and
        # the log needs the text as it was approved.
        logged = dict(rec)
        if not draft.strip():
            self.log_outcome(logged, FAILED, reason="no_draft_text")
            return

        if not poster.navigate_to_post(url) and not self._usable_though_slow(url):
            nav = getattr(poster, "last_navigation", None) or {}
            if nav.get("outcome") == poster.NAV_UNAVAILABLE:
                poster.record_unavailable(url, nav.get("reason"))
                self.log_outcome(logged, UNAVAILABLE, reason=nav.get("reason"))
            else:
                self.log_outcome(logged, FAILED, reason=nav.get("reason"),
                                 evidence=self._evidence("navigation"))
            return

        already, why = poster.already_commented_here(draft)
        reader = PageReader(poster, url)
        if not already and normalize(draft)[:MATCH_PREFIX_CHARS] in (reader.page_text() or ""):
            # The thread selectors can miss, and a repeat comment cannot be undone.
            already, why = True, "this comment's text is already on the page"
        if already:
            self._mark_commented(url)
            self.log_outcome(logged, ALREADY_COMMENTED, reason=why)
            return

        box, problem = open_composer(poster)
        if box is None:
            self.log_outcome(logged, FAILED, reason=problem, evidence=self._evidence(problem))
            return

        # One line. A newline typed key by key is an Enter key press, and nothing
        # in this module should be pressing Enter in a comment box.
        loaded_text = normalize(draft)
        _, path = poster.enter_comment_text(box, loaded_text)
        if path is None:
            self.log_outcome(logged, FAILED, reason="draft_did_not_register",
                             loaded_text=loaded_text,
                             evidence=self._evidence("draft_did_not_register"))
            return

        self.control.message = EDIT_IN_LINKEDIN_MESSAGE
        self.control.take_feedback()      # nothing carries over from another post
        self.control.take_use_text()
        emptied_evidence = {}

        def on_box_emptied():
            emptied_evidence["path"] = self._evidence("box_emptied")
            logger.info("REVIEW box emptied post=%s evidence=%s", url, emptied_evidence["path"])

        result = wait_for_human(
            reader, self.control, loaded_text,
            poll_seconds=self.settings["poll_seconds"],
            confirm_seconds=self.settings["posted_confirm_seconds"],
            put_in_box=self._put_in_box, on_box_emptied=on_box_emptied,
            trace=lambda seen: self._trace(url, seen))
        result["emptied_evidence"] = emptied_evidence.get("path")
        self._record_result(rec, logged, loaded_text, result)

    def _trace(self, url, seen):
        """What the run saw as it waited, so a miss can be read afterwards."""
        path = os.path.join(pm.get_data_dir(self.profile_name, "review_trace"),
                            f"trace_{self.run_id}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now().isoformat(), "url": url, **seen},
                               ensure_ascii=False) + "\n")

    def _usable_though_slow(self, url) -> bool:
        """A page past its load limit can still be fully usable.

        LinkedIn's load event can arrive well after the post and comment box are on
        screen. With a human waiting to read it, that is no reason to give up: the
        first live timeout was on a page showing the post, the box and the thread.
        """
        poster = self.poster
        nav = getattr(poster, "last_navigation", None) or {}
        if nav.get("outcome") != poster.NAV_TIMEOUT or not poster.post_content_present():
            return False
        logger.info("Page load limit passed but the post is on screen; carrying on: %s", url)
        return True

    def _put_in_box(self, text) -> bool:
        """Replace the box's text with the human's own wording. Never submits.

        Inserted whole rather than typed key by key, so a line break in their text
        is text, not an Enter key press. Says so if it did not land.
        """
        poster = self.poster
        box = poster.await_comment_input(timeout=1.0)
        landed = False
        if box is not None:
            try:
                poster.clear_comment_box(box)
                poster.focus_comment_box(box)
                poster.insert_via_cdp(box, text)
                landed = normalize(box.text) == normalize(text)
            except Exception:
                logger.warning("Could not put the text in the box", exc_info=True)
        if landed:
            self.control.mark_feedback_used()
            self.control.message = "Your version is in the box. Click Comment when you're happy."
        else:
            evidence = self._evidence("use_text_did_not_land")
            logger.warning("Your version did not land in the box; evidence: %s", evidence)
            self.control.message = "Your version did not go in. Check the box before you click."
        return landed

    def _record_result(self, rec, logged, loaded_text, result):
        url = rec["url"]
        outcome = result["outcome"]
        feedback = self.control.take_feedback()
        if outcome == "said_posted":
            # Its own outcome whatever the read-back found: the run did not notice the
            # click, so this must never read like a detection that worked. Their word
            # is enough to never offer it again. final_text is only set if the page
            # confirmed it, with the page saved either way so the miss can be read.
            final = result.get("final_text")
            if final:
                self.store.set_final_comment(rec["key"], final)
            self._mark_commented(url)
            self.log_outcome(logged, POSTED_ON_THEIR_WORD, loaded_text=loaded_text,
                             final_text=final, reason=result.get("reason"),
                             match=result.get("match"), excerpt=result.get("excerpt"),
                             evidence=self._evidence("said_posted_not_detected"),
                             last_box_text=result.get("last_box_text"), feedback=feedback,
                             said_posted=True, box_emptied=result.get("box_emptied"))
            self._clear_box_for_leaving()
        elif outcome == "posted":
            final = result["final_text"]
            edited = normalize(final) != normalize(loaded_text)
            self.store.set_final_comment(rec["key"], final)
            self._mark_commented(url)
            self.log_outcome(logged, POSTED_EDITED if edited else POSTED_AS_IS,
                             loaded_text=loaded_text, final_text=final,
                             match=result.get("match"), feedback=feedback,
                             evidence=result.get("emptied_evidence"),
                             excerpt=result.get("excerpt"),
                             box_emptied=result.get("box_emptied"))
            # LinkedIn can leave the text in the box after posting. Leaving it there
            # risks a "Leave site?" prompt on the next post.
            self._clear_box_for_leaving()
        elif outcome == SKIPPED:
            self.log_outcome(logged, SKIPPED, loaded_text=loaded_text,
                             reason=result.get("reason"),
                             skip_reason=result.get("skip_reason"),
                             skip_note=result.get("skip_note"),
                             last_box_text=result.get("last_box_text"),
                             feedback=feedback)
            self._clear_box_for_leaving()
            if not skip_keeps_in_queue(result.get("skip_reason")):
                self.store.reject(rec["key"], save=True)
        else:
            # Could not tell. It may be on LinkedIn, so it is held back rather
            # than offered again, and a human settles it with --mark-commented
            # or --release.
            self.store.mark_needs_check(rec["key"], result.get("reason"), save=True)
            evidence = None
            if result.get("reason") != "browser_closed":
                evidence = self._evidence("unknown_outcome")
            self.log_outcome(logged, UNKNOWN, loaded_text=loaded_text,
                             reason=result.get("reason"), evidence=evidence,
                             last_box_text=result.get("last_box_text"),
                             feedback=feedback)

    def _clear_box_for_leaving(self):
        """Empty the box so leaving the page raises no "Leave site?" prompt."""
        box = self.poster.await_comment_input(timeout=0.5)
        if box is not None:
            self.poster.clear_comment_box(box)

    def _dismiss_leave_prompt(self):
        """Accept a "Leave site?" prompt if one is up. It would block the next load."""
        try:
            self.poster.driver.switch_to.alert.accept()
        except Exception:
            pass

    def _wait_between_posts(self, last_load):
        gap = float(self.settings["min_seconds_between_posts"])
        while time.time() - last_load < gap and not self.control.stop_requested:
            time.sleep(0.25)

    # ---- the run

    def queue(self):
        recs = rs.ready_drafts(self.profile_name, store=self.store,
                               approved_only=self.approved_only)
        limit = int(self.settings["daily_limit"])
        if self.count is not None:
            limit = min(limit, self.count)
        return recs[:limit]

    def verdict(self):
        if (self.tally.get(UNKNOWN) or self.tally.get(FAILED)
                or self.tally.get(POSTED_ON_THEIR_WORD)):
            return rs.DEGRADED
        return rs.HEALTHY

    def run(self) -> str:
        """Run the review and return its verdict."""
        self.store = post_store.load_synced_store(self.profile_name)
        queue = self.queue()
        self.control.total = len(queue)
        if not queue:
            self.control.message = "Nothing is ready to review."
            logger.info("Nothing is ready to review for %s", self.profile_name)
            rs.write_status(self.profile_name, store=self.store)
            return rs.HEALTHY

        lock = browser_lock.ProfileBrowserLock(self.profile_name, owner="review_run")
        if not lock.acquire():
            self.control.message = "The browser is in use. Try again shortly."
            logger.error("Browser in use by %s; review not started",
                         browser_lock.holder(self.profile_name) or "another process")
            return rs.FAILED
        try:
            return self._run_locked(queue)
        finally:
            lock.release()
            rs.write_status(self.profile_name)

    def _run_locked(self, queue):
        self.poster = LinkedInCommentPoster(profile_name=self.profile_name,
                                            allow_production=self.allow_production)
        self.poster.human_click_only = True
        self.poster.setup_driver()
        try:
            if not self.poster.login():
                self.control.message = "Not logged in to LinkedIn."
                logger.error("Login required for %s", self.profile_name)
                return rs.FAILED

            last_load = 0.0
            for position, rec in enumerate(queue, 1):
                if self.control.stop_requested:
                    break
                self.control.position = position
                self.control.author = rec.get("author") or ""
                self.control.message = "Loading the post..."
                self._wait_between_posts(last_load)
                if self.control.stop_requested:
                    break
                self._dismiss_leave_prompt()
                last_load = time.time()
                try:
                    self.review_one(rec)
                except BrowserClosed:
                    break
                except Exception as exc:
                    if self._browser_gone():
                        self.log_outcome(rec, UNKNOWN, reason="browser_closed")
                        break
                    logger.exception("Review of %s failed", rec.get("url"))
                    self.log_outcome(rec, FAILED, reason=str(exc)[:200],
                                     evidence=self._evidence("exception"))
                if self._browser_gone():
                    break
            return self.verdict()
        finally:
            try:
                self.poster.driver.quit()
                logger.info("Browser closed")
            except Exception:
                logger.debug("browser already gone", exc_info=True)

    def _browser_gone(self):
        try:
            self.poster.driver.current_url
            return False
        except Exception:
            return True


def _console_skip(control):
    """Ask for the reason a skip needs. Anything invalid cancels the skip."""
    for number, (_, label, _) in enumerate(SKIP_REASONS, 1):
        print(f"  {number}. {label}")
    try:
        code = SKIP_REASONS[int(input("Why skip? Number: ").strip()) - 1][0]
    except (ValueError, IndexError):
        print("Not skipped: choose a number from the list.")
        return
    note = input("Your reason: ") if code == OTHER_REASON else None
    problem = skip_reason_problem(code, note)
    if problem:
        print(f"Not skipped: {problem}")
        return
    control.request_skip(code, note)


def _console_control(control):
    """Skip and Stop from the console, for when no window can be shown."""
    def read():
        print("Type s then Enter to skip, q then Enter to stop.")
        for line in sys.stdin:
            answer = line.strip().lower()
            if answer == "s":
                _console_skip(control)
            elif answer == "q":
                control.request_stop()
                return
    threading.Thread(target=read, daemon=True).start()


def resolve_by_hand(profile_name, mark_commented=None, release=None) -> int:
    """Settle a post by hand. No browser."""
    store = post_store.load_synced_store(profile_name)
    if mark_commented:
        poster = LinkedInCommentPoster(profile_name=profile_name)
        posted = poster.progress.setdefault("posted_comments", [])
        if mark_commented not in posted:
            posted.append(mark_commented)
        poster.save_progress()
        changed = store.sync_with_progress(posted, save=True)
        print(f"Marked as commented: {mark_commented} (records changed: {changed})")
    if release:
        ok = store.clear_needs_check(release, save=True)
        print(f"Released for review: {release} ({'ok' if ok else 'nothing held under that key'})")
    status = rs.write_status(profile_name)
    print(f"ready={status['ready']}")
    return pm.EXIT_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--allow-production", action="store_true",
                        help="Allow running against a declared PRODUCTION identity")
    parser.add_argument("--count", type=int, default=None,
                        help="Review at most this many posts (default: the daily limit)")
    parser.add_argument("--approved-only", action="store_true",
                        help="Only posts already approved on the review screen")
    parser.add_argument("--no-window", action="store_true",
                        help="Skip and Stop from the console instead of a window")
    parser.add_argument("--mark-commented", metavar="URL",
                        help="Record that this post was commented on by hand. No browser.")
    parser.add_argument("--release", metavar="KEY",
                        help="Put a held draft back in the queue. No browser.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")

    if args.mark_commented or args.release:
        return resolve_by_hand(args.profile, args.mark_commented, args.release)

    control = ReviewControl()
    review = ReviewRun(args.profile, allow_production=args.allow_production,
                       control=control, count=args.count, approved_only=args.approved_only)
    outcome = {}

    def work():
        try:
            outcome["verdict"] = review.run()
        except pm.LoginRequiredError as exc:
            logger.error("%s", exc)
            outcome["verdict"], outcome["exit"] = rs.FAILED, pm.EXIT_LOGIN_REQUIRED
        except pm.ProductionAccessRefused as exc:
            logger.error("%s", exc)
            outcome["verdict"], outcome["exit"] = rs.FAILED, pm.EXIT_PRODUCTION_REFUSED
        except Exception:
            logger.exception("Review run crashed")
            outcome["verdict"] = rs.FAILED
        finally:
            control.done.set()

    worker = threading.Thread(target=work)
    worker.start()

    shown = False
    if not args.no_window:
        try:
            from .review_window import run_window
            run_window(control)
            shown = True
        except Exception as exc:
            logger.warning("No control window (%s); using the console instead", exc)
    if not shown:
        _console_control(control)
    worker.join()

    verdict = outcome.get("verdict", rs.FAILED)
    print(f"VERDICT: {verdict} {review.tally}")
    return outcome.get("exit", rs.EXIT_CODES[verdict])


if __name__ == "__main__":
    sys.exit(main())
