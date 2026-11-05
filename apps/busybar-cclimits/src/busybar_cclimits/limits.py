"""
Claude Code's rate limits as its status line reports them, and the file that
carries them from the status line to the service.

Claude Code hands its status line command a JSON object on every update.
For claude.ai subscribers, and behind a Claude apps gateway with a spend
limit, it holds `rate_limits`: a rolling `five_hour` window, a `seven_day`
one and a `spend_limit`, each with `used_percentage` (0-100) and
`resets_at` (Unix seconds). A window can be missing, and Claude Code drops
one once its `resets_at` has passed - so a window that is no longer
reported keeps its last values here, and reads as reset once its time is up.

Only the standard library: this runs inside the status line, every few
seconds, and must not slow it down.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FEED_ENV = "BUSYBAR_CCLIMITS_FEED"
WINDOWS = ("five_hour", "seven_day", "spend_limit")
# Kept from each window; the rest of the status line input never reaches the file.
FIELDS = ("used_percentage", "resets_at", "used_usd", "limit_usd", "period")
# A status line that runs every few seconds rewrites the file this often at
# most when nothing changed, so `seen_at` stays a useful "Claude Code is open".
SEEN_EVERY = 30
VERSION = 1


def default_feed() -> Path:
    """$BUSYBAR_CCLIMITS_FEED, else ~/.cache/busybar-cclimits/limits.json."""
    if found := os.environ.get(FEED_ENV):
        return Path(found).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(cache).expanduser() / "busybar-cclimits" / "limits.json"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True)
class Window:
    """One limit: how much of it is used, and when it starts over."""

    used: float  # percent; a spend limit can go past 100
    resets_at: int | None  # Unix seconds; None once it has reset
    used_usd: float | None = None
    limit_usd: float | None = None
    period: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> Window | None:
        if not isinstance(raw, dict) or (used := _number(raw.get("used_percentage"))) is None:
            return None
        resets = _number(raw.get("resets_at"))
        period = raw.get("period")
        return cls(
            used=max(used, 0.0),
            resets_at=int(resets) if resets is not None else None,
            used_usd=_number(raw.get("used_usd")),
            limit_usd=_number(raw.get("limit_usd")),
            period=period if isinstance(period, str) else None,
        )

    @property
    def running(self) -> bool:
        return self.resets_at is not None

    def at(self, now: float) -> Window:
        """The window as of `now`: once its reset time has passed, none of it is used."""
        if self.resets_at is not None and now >= self.resets_at:
            spent = 0.0 if self.used_usd is not None else None
            return Window(0.0, None, spent, self.limit_usd, self.period)
        return self

    def left(self, now: float) -> float | None:
        """Seconds until it resets; None when no window is running."""
        return None if self.resets_at is None else max(0.0, self.resets_at - now)


@dataclass(frozen=True)
class Feed:
    """What the status line or the usage API last wrote."""

    windows: dict[str, Window]
    updated_at: float | None  # when the limits last came in
    seen_at: float | None  # when the status line last ran (never, with the API alone)
    changed_at: float | None = None  # when a percentage last moved

    @classmethod
    def parse(cls, data: Any) -> Feed:
        data = data if isinstance(data, dict) else {}
        raw = data.get("rate_limits")
        raw = raw if isinstance(raw, dict) else {}
        # Any name: the status line sends WINDOWS, the usage API per-model ones too.
        windows = {name: w for name, value in raw.items() if (w := Window.parse(value)) is not None}
        return cls(
            windows,
            _number(data.get("updated_at")),
            _number(data.get("seen_at")),
            _number(data.get("changed_at")),
        )

    def active_at(self) -> float | None:
        """The last sign of Claude Code at work: the status line ran, or usage moved."""
        times = [t for t in (self.seen_at, self.changed_at) if t is not None]
        return max(times) if times else None

    def at(self, now: float) -> dict[str, Window]:
        return {name: window.at(now) for name, window in self.windows.items()}


def _clean(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: raw[key] for key in FIELDS if key in raw}


def merge(stored: dict[str, Any], status: Any, now: float, *, seen: bool = True) -> dict[str, Any]:
    """
    The file's next contents, from what it holds and one input: the status
    line's (`seen`: the status line ran) or the usage API's, in the same form.

    A window that comes in replaces the stored one, field by field while the
    period is the same: a spend limit's dollar amounts arrive on a slower
    schedule than its percentage, and must not blink out in between.
    """
    limits = stored.get("rate_limits")
    limits = (
        {k: v for k, v in limits.items() if isinstance(v, dict)} if isinstance(limits, dict) else {}
    )
    before = {name: window.get("used_percentage") for name, window in limits.items()}
    updated_at = stored.get("updated_at")
    incoming = status.get("rate_limits") if isinstance(status, dict) else None
    if isinstance(incoming, dict):
        for name, raw in incoming.items():
            if not isinstance(name, str) or Window.parse(raw) is None:
                continue
            new = _clean(raw)
            old = limits.get(name, {})
            same_period = old.get("resets_at") == new.get("resets_at")
            limits[name] = {**old, **new} if same_period else new
            updated_at = now
    after = {name: window.get("used_percentage") for name, window in limits.items()}
    changed_at = stored.get("changed_at")
    if before and after != before:
        changed_at = now
    return {
        "version": VERSION,
        "seen_at": now if seen else stored.get("seen_at"),
        "updated_at": updated_at,
        "changed_at": changed_at,
        "rate_limits": limits,
    }


def read_raw(path: Path) -> dict[str, Any]:
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    return {}


def read(path: Path) -> Feed | None:
    """The feed, or None when nothing has written one yet."""
    data = read_raw(path)
    return Feed.parse(data) if data else None


def write(path: Path, data: dict[str, Any]) -> None:
    """Replace the file in one step, so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".limits-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record(path: Path, status: Any, now: float, *, seen: bool = True) -> bool:
    """Store one input (see `merge`); False when nothing needed writing."""
    stored = read_raw(path)
    new = merge(stored, status, now, seen=seen)
    # The times move on with every input; only the limits themselves are worth
    # a write each time, the times every SEEN_EVERY seconds.
    same = stored.get("version") == VERSION and new["rate_limits"] == stored.get("rate_limits")
    last = _number(stored.get("seen_at" if seen else "updated_at"))
    if same and last is not None and 0 <= now - last < SEEN_EVERY:
        return False
    write(path, new)
    return True


def record_limits(path: Path, found: dict[str, dict[str, Any]], now: float) -> bool:
    """Store limits from the usage API, already in the status line's form."""
    return record(path, {"rate_limits": found}, now, seen=False)


def duration(seconds: float) -> str:
    """A short time span for a 4-5 character slot: 42m, 3h05, 21h, 3d4h."""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 0)}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 10:
        return f"{hours}h{minutes:02d}"
    if hours < 24:
        return f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"
