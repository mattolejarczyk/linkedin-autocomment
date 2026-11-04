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
calls, because the human is using that window.
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
        self._skip = {"code": reason, "note": (note or "").strip() or None}

    def request_stop(self):
        self._stop.set()

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
    so this watches for the result. Posted means the box is empty or gone AND the
    last text seen in it is now in the thread. An empty box alone proves nothing:
    the human may be retyping.

    The thread baseline keeps moving while the box holds text, because "2m ago"
    labels and other people's comments change the thread with no click at all.
    """

    def __init__(self, draft, confirm_seconds=20.0, clock=time.time):
        self.last_text = draft
        self.confirm_seconds = confirm_seconds
        self.clock = clock
        self.baseline = None
        self.emptied_at = None

    def observe(self, box_text, thread):
        """Feed one look at the page. Returns a result dict, or None to keep waiting.

        ``box_text`` is the box's text, "" if empty, None if it is gone.
        ``thread`` is the normalized thread text, None if it cannot be read.
        """
        if box_text and box_text.strip():
            self.last_text = box_text
            self.emptied_at = None
            if thread is not None:
                self.baseline = thread
            return None

        if self.emptied_at is None:
            self.emptied_at = self.clock()

        if thread:
            wanted = normalize(self.last_text)
            already_there = self.baseline and wanted[:MATCH_PREFIX_CHARS] in self.baseline
            if not already_there:
                if wanted in thread:
                    return {"posted": True, "final_text": self.last_text, "match": "full"}
                if wanted[:MATCH_PREFIX_CHARS] in thread:
                    return {"posted": True, "final_text": self.last_text, "match": "prefix"}

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

    def left_the_post(self):
        return self.poster.navigated_away_from(self.url)


def wait_for_human(reader, control, draft, poll_seconds=0.75, confirm_seconds=20.0):
    """Wait, reading only, until the human posts, skips or stops. No time limit.

    Returns ``{"outcome": ..., "final_text": ..., "reason": ..., "match": ...}``.
    """
    watcher = ClickWatcher(draft, confirm_seconds=confirm_seconds)
    while True:
        try:
            reader.check_alive()
        except BrowserClosed:
            return {"outcome": UNKNOWN, "final_text": None, "reason": "browser_closed",
                    "last_box_text": watcher.last_text}
        if control.stop_requested:
            return {"outcome": SKIPPED, "final_text": None, "reason": "stopped",
                    "last_box_text": watcher.last_text}
        skip = control.take_skip()
        if skip:
            return {"outcome": SKIPPED, "final_text": None, "reason": "skipped",
                    "skip_reason": skip["code"], "skip_note": skip["note"],
                    "last_box_text": watcher.last_text}

        result = watcher.observe(reader.box_text(), reader.thread())
        if result is not None:
            if result["posted"]:
                return {"outcome": "posted", "final_text": result["final_text"],
                        "match": result["match"], "reason": None}
            return {"outcome": UNKNOWN, "final_text": None, "reason": result["reason"],
                    "last_box_text": watcher.last_text}

        if reader.left_the_post():
            return {"outcome": UNKNOWN, "final_text": None, "reason": "left_the_post",
                    "last_box_text": watcher.last_text}
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
                    skip_note=None):
        """One line per post: the draft, what was approved, and what was posted."""
        self.tally[outcome] = self.tally.get(outcome, 0) + 1
        entry = {
            "at": datetime.now().isoformat(),
            "run_id": self.run_id,
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
            "thread_match": match,
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

        if not poster.navigate_to_post(url):
            nav = getattr(poster, "last_navigation", None) or {}
            if nav.get("outcome") == poster.NAV_UNAVAILABLE:
                poster.record_unavailable(url, nav.get("reason"))
                self.log_outcome(logged, UNAVAILABLE, reason=nav.get("reason"))
            else:
                self.log_outcome(logged, FAILED, reason=nav.get("reason"),
                                 evidence=self._evidence("navigation"))
            return

        already, why = poster.already_commented_here(draft)
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

        self.control.message = "Read it, edit if you like, then click Comment yourself."
        result = wait_for_human(
            PageReader(poster, url), self.control, loaded_text,
            poll_seconds=self.settings["poll_seconds"],
            confirm_seconds=self.settings["posted_confirm_seconds"])
        self._record_result(rec, logged, loaded_text, result)

    def _record_result(self, rec, logged, loaded_text, result):
        url = rec["url"]
        outcome = result["outcome"]
        if outcome == "posted":
            final = result["final_text"]
            edited = normalize(final) != normalize(loaded_text)
            self.store.set_final_comment(rec["key"], final)
            self._mark_commented(url)
            self.log_outcome(logged, POSTED_EDITED if edited else POSTED_AS_IS,
                             loaded_text=loaded_text, final_text=final,
                             match=result.get("match"))
        elif outcome == SKIPPED:
            self.log_outcome(logged, SKIPPED, loaded_text=loaded_text,
                             reason=result.get("reason"),
                             skip_reason=result.get("skip_reason"),
                             skip_note=result.get("skip_note"),
                             last_box_text=result.get("last_box_text"))
            self._clear_box_for_leaving()
            # A Stop has no reason and stays queued, as does "not now".
            code = result.get("skip_reason")
            if code and not skip_keeps_in_queue(code):
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
                             last_box_text=result.get("last_box_text"))

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
        if self.tally.get(UNKNOWN) or self.tally.get(FAILED):
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
