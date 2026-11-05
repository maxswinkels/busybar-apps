"""
The status line side: `busybar-cclimits statusline`.

Claude Code runs its status line command with a JSON object on stdin at
every update. This stores the rate limits from it in the feed file, then
either runs the status line command you had before (`--then`), with the same
input, its output passed through untouched - or, with nothing to run, prints
a short line of its own.

Nothing here may break the status line: a feed that cannot be written is
skipped, and only the standard library is imported.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from . import limits

NAMES = {"five_hour": "5h", "seven_day": "week", "spend_limit": "spend"}


def own_line(status: Any, now: float) -> str:
    """`5h 42% (1h17) · week 18% (3d4h)`, or the model's name before any limits arrive."""
    feed = limits.Feed.parse(limits.merge({}, status, now))
    parts = []
    for name, window in feed.at(now).items():
        part = f"{NAMES[name]} {window.used:.0f}%"
        if (left := window.left(now)) is not None:
            part += f" ({limits.duration(left)})"
        parts.append(part)
    if parts:
        return " · ".join(parts)
    model = status.get("model") if isinstance(status, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    return name if isinstance(name, str) else "Claude Code"


def run(
    then: str | None,
    feed: Path,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    data = (stdin or sys.stdin.buffer).read()
    now = time.time()
    status: Any = None
    try:
        status = json.loads(data or b"null")
        limits.record(feed, status, now)
    except (ValueError, OSError):
        pass  # the status line still shows
    if then:
        # The command the status line had: a shell command, as Claude Code runs it.
        return subprocess.run(then, shell=True, input=data, check=False).returncode
    print(own_line(status, now), file=stdout or sys.stdout)
    return 0
