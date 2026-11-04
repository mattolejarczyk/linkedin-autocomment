"""Review drafted comments from a list, with no browser at all.

For a screen that shows every ready post with its draft: the human reads the post in
their own browser, pastes the comment and submits it themselves, then tells this module
what happened. It opens nothing, clicks nothing and posts nothing.

    --list                         one JSON object on stdout
    --posted KEY [--final-text-stdin]
    --skip KEY --reason CODE [--note TEXT]

Exit 0 done. Exit 1 refused, with a one-line reason on stderr. Anything else failed.

A write takes the same lock a morning scan or review run holds, and is refused while
one is active: a review run keeps the store in memory for its whole length and saves
it later, which would silently undo a change made underneath it. Reading takes no lock.

"posted_by_hand" is the human's word and was not read back from LinkedIn, and the log
says so. final_text is null unless they supply it: if they change the comment only in
LinkedIn's box, nothing here can see that, so the log never claims the draft went out
unchanged. Their words are stored exactly as given.
"""

import argparse
import json
import os
import sys
import traceback

from . import browser_lock
from . import post_store
from . import profile_manager as pm
from . import review_run as rr
from . import review_status as rs

EXIT_DONE = 0
EXIT_REFUSED = 1
EXIT_FAILED = 3

OUTCOME_POSTED = "posted_by_hand"
ROUTE = "review_list"

SOURCE_THEIR_TEXT = "supplied_by_human"
SOURCE_NOT_SUPPLIED = "not_supplied"

#: Whether the final text differs from the draft, ignoring only whitespace. The screen
#: sends final text every time, so its presence alone says nothing about an edit.
AS_IS = "as_is"
EDITED = "edited"

SETTLED = (post_store.COMMENTED, post_store.TRASH, post_store.UNAVAILABLE)


class Refused(Exception):
    """A request this module will not carry out. The message is one line for stderr."""


def _check_profile(profile_name):
    # By path, because get_data_dir creates the folder, which would make a mistyped
    # profile look real and report "nothing ready".
    if not os.path.isdir(os.path.join(pm.DATA_ROOT, profile_name)):
        raise Refused(f"unknown profile: {profile_name}")


def _read_store(profile_name, reconcile=False):
    """The profile's store. An unreadable file must not read as "nothing ready".

    PostStore loads a store it cannot read as empty, so an empty result with a non-empty
    file on disk is a refusal here, not an answer.
    """
    path = post_store.PostStore._default_path(profile_name)
    store = (post_store.load_synced_store(profile_name) if reconcile
             else post_store.PostStore(profile_name))
    if not store.posts and os.path.exists(path) and os.path.getsize(path) > 200:
        raise Refused("the post store could not be read; nothing was changed")
    return store


def list_ready(profile_name) -> dict:
    """Every ready draft and the skip reasons. Reads only, and takes no lock.

    The posted ledger is applied in memory, not saved. It is the authoritative record of
    what was commented on, so a post whose store record was overwritten by another
    writer is still never offered again, and a repeat comment cannot be undone.
    """
    store = _read_store(profile_name)
    ledger = post_store._read_json(pm.get_progress_file(profile_name)) or {}
    store.sync_with_progress(ledger.get("posted_comments") or [])
    drafts = [{field: rec.get(field) for field in
               ("key", "url", "text", "comment", "generated_at", "relevance_score", "reviewed_at")}
              | {"author": rec.get("author")}
              for rec in rs.ready_drafts(profile_name, store=store)]
    reasons = [{"code": code, "label": label,
                "needs_note": rr.skip_reason_problem(code) is not None}
               for code, label, _ in rr.SKIP_REASONS]
    return {"drafts": drafts, "skip_reasons": reasons}


def _open_draft(store, key):
    """The record for ``key`` if it can still be settled, or a refusal saying why."""
    rec = store.posts.get(key)
    if rec is None:
        raise Refused(f"unknown key: {key}")
    if rec.get("status") in SETTLED:
        raise Refused(f"already settled ({rec.get('status')}): {key}")
    if rec.get("status") != post_store.GENERATED:
        raise Refused(f"not a drafted comment ({rec.get('status')}): {key}")
    return rec


class _Write:
    """Hold the browser lock for one change, with the store freshly read inside it."""

    def __init__(self, profile_name):
        self.profile_name = profile_name
        self.lock = browser_lock.ProfileBrowserLock(profile_name, owner="review_list")

    def __enter__(self):
        if not self.lock.acquire():
            holder = browser_lock.holder(self.profile_name).get("owner", "another process")
            raise Refused(f"busy: {holder} is using this profile; try again when it finishes")
        try:
            return _read_store(self.profile_name, reconcile=True)
        except BaseException:
            self.lock.release()
            raise

    def __exit__(self, *exc):
        self.lock.release()


def record_posted(profile_name, key, final_text=None) -> str:
    """The human says they posted this one. Their word; nothing was read back."""
    if final_text is not None and not final_text.strip():
        raise Refused("the final text is empty; leave out --final-text-stdin if there is none")
    with _Write(profile_name) as store:
        rec = _open_draft(store, key)
        if not rec.get("url"):
            raise Refused(f"this post has no link: {key}")
        logged = dict(rec)
        draft = logged.get("draft_original") or logged.get("comment")
        final_vs_draft = None
        if final_text is not None:
            final_vs_draft = AS_IS if rr.normalize(final_text) == rr.normalize(draft) else EDITED
        store.clear_needs_check(key)
        if final_text is not None:
            store.set_final_comment(key, final_text)

        run = rr.ReviewRun(profile_name)
        poster = rr.LinkedInCommentPoster(profile_name=profile_name)
        posted = poster.progress.setdefault("posted_comments", [])
        if rec["url"] not in posted:
            posted.append(rec["url"])
        poster.save_progress()
        store.sync_with_progress(posted, save=True)

        run.log_outcome(logged, OUTCOME_POSTED, loaded_text=logged.get("comment"),
                        final_text=final_text, said_posted=True, route=ROUTE,
                        final_text_source=SOURCE_THEIR_TEXT if final_text is not None
                        else SOURCE_NOT_SUPPLIED, final_vs_draft=final_vs_draft)
        status = rs.write_status(profile_name)
    return f"recorded as posted: {key} (ready {status['ready']})"


def record_skip(profile_name, key, reason, note=None) -> str:
    """The same effects as a Skip in a review run: a log line, then Trash unless "not now"."""
    problem = rr.skip_reason_problem(reason, note)
    if problem:
        raise Refused(f"bad skip reason {reason!r}: {problem}")
    with _Write(profile_name) as store:
        rec = _open_draft(store, key)
        logged = dict(rec)
        rr.ReviewRun(profile_name).log_outcome(
            logged, rr.SKIPPED, loaded_text=logged.get("comment"), reason="skipped",
            skip_reason=reason, skip_note=note if (note or "").strip() else None, route=ROUTE)
        kept = rr.skip_keeps_in_queue(reason)
        if not kept:
            store.reject(key, save=True)
        status = rs.write_status(profile_name)
    what = "kept in the queue" if kept else "moved to Trash"
    return f"skipped ({reason}), {what}: {key} (ready {status['ready']})"


def _read_final_text_stdin() -> str:
    """Their final wording, exactly as sent. Bad encoding is refused, never repaired."""
    try:
        return sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"the final text is not valid UTF-8: {exc.reason}") from exc


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true", help="Print every ready draft as JSON")
    action.add_argument("--posted", metavar="KEY", help="Record that the human posted this one")
    action.add_argument("--skip", metavar="KEY", help="Record that the human skipped this one")
    parser.add_argument("--final-text-stdin", action="store_true",
                        help="With --posted: read the final wording from stdin as UTF-8")
    parser.add_argument("--reason", help="With --skip: a skip reason code")
    parser.add_argument("--note", help="With --skip: the human's words, needed for Other")
    args = parser.parse_args(argv)

    try:
        _check_profile(args.profile)
        if args.list:
            # ensure_ascii keeps stdout pure ASCII, so no console encoding can break it.
            sys.stdout.write(json.dumps(list_ready(args.profile)) + "\n")
        elif args.posted:
            final = _read_final_text_stdin() if args.final_text_stdin else None
            print(record_posted(args.profile, args.posted, final))
        else:
            if not args.reason:
                raise Refused("--skip needs --reason")
            print(record_skip(args.profile, args.skip, args.reason, args.note))
        return EXIT_DONE
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except Exception:
        traceback.print_exc()
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
