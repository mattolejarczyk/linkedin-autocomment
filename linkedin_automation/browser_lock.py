"""A per-profile lock on the Chrome session, shared across processes.

The dashboard's ``can_start_browser_task`` only knows about its own jobs. A
scheduled scan or a review run started outside it would open the same
``--user-data-dir`` and collide, so those hold this lock for as long as their
browser is open.

An OS file lock rather than a marker file: the OS drops it when the process dies,
so a crash cannot leave a stale lock that blocks every later run.
"""

import json
import logging
import os
from datetime import datetime

from . import profile_manager as pm

try:
    import msvcrt
except ImportError:      # not Windows
    msvcrt = None
    import fcntl

logger = logging.getLogger(__name__)

LOCK_FILE = "browser.lock"


def _lock_path(profile_name):
    return os.path.join(pm.get_data_dir(profile_name), LOCK_FILE)


def _take(handle):
    """Lock an open file without waiting. Raises OSError if it is held."""
    handle.seek(0)
    if msvcrt:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _drop(handle):
    handle.seek(0)
    if msvcrt:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProfileBrowserLock:
    """Non-blocking claim on one profile's Chrome session."""

    def __init__(self, profile_name, owner="unknown"):
        self.profile_name = profile_name
        self.owner = owner
        self._file = None

    def acquire(self) -> bool:
        """True if the lock is now ours, False if another holder has it."""
        handle = open(_lock_path(self.profile_name), "ab+")
        try:
            _take(handle)
        except OSError:
            handle.close()
            return False
        self._file = handle
        try:
            with open(_lock_path(self.profile_name) + ".owner", "w",
                      encoding="utf-8") as f:
                json.dump({"owner": self.owner, "pid": os.getpid(),
                           "since": datetime.now().isoformat()}, f)
        except OSError:
            logger.debug("could not write the lock owner", exc_info=True)
        return True

    def release(self):
        if self._file is None:
            return
        try:
            _drop(self._file)
        except OSError:
            logger.debug("could not release the browser lock", exc_info=True)
        self._file.close()
        self._file = None


def is_held(profile_name) -> bool:
    """Is another holder using this profile's browser right now?

    An error here answers False: the dashboard's own job check still applies, and
    a broken lock file must not stop every browser task for good.
    """
    try:
        with open(_lock_path(profile_name), "ab+") as handle:
            try:
                _take(handle)
            except OSError:
                return True
            _drop(handle)
            return False
    except Exception:
        logger.debug("could not check the browser lock", exc_info=True)
        return False


def holder(profile_name) -> dict:
    """Who last took the lock, for a log line. Empty if unknown."""
    try:
        with open(_lock_path(profile_name) + ".owner", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
