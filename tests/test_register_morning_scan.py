"""The scheduled scan keeps its clock time when the clocks change."""

import os
import re
import shutil
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tools", "register_morning_scan.ps1")

windows_only = pytest.mark.skipif(
    os.name != "nt" or not shutil.which("powershell"),
    reason="needs Windows PowerShell and its ScheduledTasks module")


@windows_only
@pytest.mark.parametrize("extra", [[], ["-Weekdays"]])
def test_start_time_is_plain_local_time(extra):
    """An offset or a Z would pin the task to UTC, so it moves with daylight saving.

    -ShowStartTime registers nothing.
    """
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", SCRIPT, "-ProfileName", "x", "-At", "07:30",
         "-ShowStartTime", *extra],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T07:30:00", result.stdout.strip()), result.stdout
