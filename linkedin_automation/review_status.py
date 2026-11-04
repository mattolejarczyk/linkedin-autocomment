"""The small status file behind "how many comments are ready to review?".

Written by the morning scan and refreshed by the review run. ``ready`` is always
counted from the store, so a failed scan still reports real numbers, and
``verdict`` says separately whether the last scan could look at all. "Could not
look" must never read as "0 ready".
"""

import json
import logging
import os
from datetime import datetime

from . import post_store
from . import profile_manager as pm

logger = logging.getLogger(__name__)

STATUS_FILE = "review_status.json"

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
FAILED = "FAILED"

EXIT_CODES = {HEALTHY: 0, FAILED: 1, DEGRADED: 2}


def status_path(profile_name):
    return os.path.join(pm.get_data_dir(profile_name), STATUS_FILE)


def read_status(profile_name) -> dict:
    """The last status written, or {} if there is none or it cannot be read."""
    try:
        with open(status_path(profile_name), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def ready_drafts(profile_name, store=None, approved_only=False):
    """The drafts a human could review now, using the profile's age limit."""
    store = store or post_store.load_synced_store(profile_name)
    settings = pm.get_review_settings(profile_name)
    return store.review_ready(max_age_days=settings["max_draft_age_days"],
                              approved_only=approved_only)


def write_status(profile_name, store=None, **fields) -> dict:
    """Merge ``fields`` over the last status, recount ready, write atomically.

    A status with no scan behind it says so: DEGRADED, reason ``no_scan_yet``.
    """
    status = read_status(profile_name)
    status.update(fields)
    status["profile"] = profile_name
    status.setdefault("verdict", DEGRADED)
    status.setdefault("reasons", ["no_scan_yet"])
    for key in ("scan_started_at", "scan_finished_at", "scan_skipped",
                "posts_seen", "qualified", "new_drafts", "evidence"):
        status.setdefault(key, None)

    ready = ready_drafts(profile_name, store=store)
    status["ready"] = len(ready)
    drafted = [r.get("generated_at") for r in ready if r.get("generated_at")]
    status["oldest_ready_generated_at"] = min(drafted) if drafted else None
    status["status_written_at"] = datetime.now().isoformat()

    path = status_path(profile_name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, path)
    return status
