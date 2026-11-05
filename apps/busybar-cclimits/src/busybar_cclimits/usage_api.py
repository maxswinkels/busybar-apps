"""
Claude Code's usage limits from Anthropic: the endpoint behind Claude Code's
own `/usage`, called with the sign-in Claude Code already has.

The sign-in is read where Claude Code keeps it - the macOS Keychain item
"Claude Code-credentials", else ~/.claude/.credentials.json - and only read:
it is never refreshed and never written back, since a refresh would replace
the refresh token under Claude Code and sign it out. When the access token
has expired this waits; Claude Code renews it the next time it runs. The
token goes nowhere but the Authorization header of that one request, and
never into a log or an error message.

GET https://api.anthropic.com/api/oauth/usage is not documented. It answers
(its shape as seen in October 2026; the numbers are made up)

    {"five_hour": {"utilization": 23.0, "resets_at": "2026-10-21T13:19:59.5+00:00"},
     "seven_day": {"utilization": 41.0, "resets_at": "..."},
     "seven_day_opus": null,
     "extra_usage": {"utilization": 12.5, "monthly_limit": 20000, ...},
     "limits": [{"kind": "weekly_scoped", "percent": 6, "resets_at": "...",
                 "scope": {"model": {"display_name": "Fable"}}}, ...],
     ...}

with `utilization` and `percent` in percent; a window may be null or missing.
Every top-level window of that shape is kept under its own name -
`extra_usage` is the month's usage credits. A model's own limit can also come
only in `limits`, scoped to the model: it becomes `seven_day_<model>` (or
`five_hour_<model>`), so the Fable limit reaches a `seven_day_fable` row.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, limits

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"
KEYCHAIN_SERVICE = "Claude Code-credentials"
TIMEOUT = 15.0
BACKOFF_MAX = 900.0  # seconds


class TokenError(Exception):
    """No usable Claude Code sign-in; the message says what to do."""


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, repr=False)
class Token:
    access: str
    expires_at: float | None  # Unix seconds

    def expired(self, now: float) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def __repr__(self) -> str:  # never the token itself
        return f"Token(expires_at={self.expires_at})"


def credentials_file() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / ".credentials.json"


def parse_credentials(blob: str) -> Token:
    try:
        oauth = json.loads(blob)["claudeAiOauth"]
        access = oauth["accessToken"]
        expires = oauth.get("expiresAt")
    except (ValueError, KeyError, TypeError):
        raise TokenError("Claude Code's stored sign-in is not in a form this understands") from None
    if not isinstance(access, str) or not access:
        raise TokenError("Claude Code's stored sign-in has no access token")
    expires_at = expires / 1000 if isinstance(expires, int | float) else None
    return Token(access, expires_at)


Run = Callable[..., subprocess.CompletedProcess[str]]


def read_token(
    service: str = KEYCHAIN_SERVICE, platform: str = sys.platform, run: Run = subprocess.run
) -> Token:
    """Claude Code's sign-in, as it stored it; raises TokenError."""
    if platform == "darwin":
        try:
            done = run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            done = None
        if done is not None and done.returncode == 0 and done.stdout.strip():
            return parse_credentials(done.stdout)
    path = credentials_file()
    try:
        return parse_credentials(path.read_text())
    except FileNotFoundError:
        raise TokenError(
            "no Claude Code sign-in found: run `claude` and log in with your claude.ai account"
        ) from None
    except OSError as error:
        raise TokenError(f"cannot read {path}: {error.strerror}") from None


def _epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _percent(value: Any) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, int | float) else float(value)


# The `limits` entries scoped to one model, by the window prefix they get.
SCOPED = {"weekly_scoped": "seven_day", "session_scoped": "five_hour"}


def _scoped(entry: Any) -> tuple[str, dict[str, Any]] | None:
    """A `limits` entry for one model's own limit, as (seven_day_<model>, window)."""
    if not isinstance(entry, dict) or (prefix := SCOPED.get(entry.get("kind"))) is None:
        return None
    scope = entry.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    used = _percent(entry.get("percent"))
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") if isinstance(name, str) else ""
    if not slug or used is None:
        return None
    return f"{prefix}_{slug}", {
        "used_percentage": used,
        "resets_at": _epoch(entry.get("resets_at")),
    }


def to_rate_limits(data: Any) -> dict[str, dict[str, Any]]:
    """The answer in the status line's terms: {window: {used_percentage, resets_at}}."""
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(data, dict):
        return found
    for name, window in data.items():
        if not isinstance(name, str) or not isinstance(window, dict):
            continue
        if (used := _percent(window.get("utilization"))) is not None:
            found[name] = {"used_percentage": used, "resets_at": _epoch(window.get("resets_at"))}
    entries = data.get("limits")
    for entry in entries if isinstance(entries, list) else ():
        # A top-level window of the same name says it more precisely.
        if (scoped := _scoped(entry)) is not None and scoped[0] not in found:
            found[scoped[0]] = scoped[1]
    return found


async def fetch(token: Token, client: Any = None) -> dict[str, Any]:
    """One GET of the usage endpoint; raises ApiError."""
    import httpx2

    headers = {
        "Authorization": f"Bearer {token.access}",
        "anthropic-beta": BETA,
        "Accept": "application/json",
        "User-Agent": f"busybar-cclimits/{__version__}",
    }
    own = client is None
    client = client or httpx2.AsyncClient(timeout=TIMEOUT)
    try:
        response = await client.get(USAGE_URL, headers=headers)
    except httpx2.HTTPError as error:
        raise ApiError(f"cannot reach api.anthropic.com ({type(error).__name__})") from None
    finally:
        if own:
            await client.aclose()
    if response.status_code != 200:
        raise ApiError(
            f"api.anthropic.com answered HTTP {response.status_code}", response.status_code
        )
    try:
        data = response.json()
    except ValueError:
        raise ApiError("api.anthropic.com answered something that is not JSON") from None
    if not isinstance(data, dict):
        raise ApiError("api.anthropic.com answered something unexpected")
    return data


class Poller:
    """
    Asks the endpoint every `every` seconds and stores the answer in the feed
    file - the file, and the form, the status line hook writes. A refused or
    failed request keeps the last numbers; rate limiting (429) and other
    failures back off, up to a quarter of an hour.
    """

    def __init__(
        self,
        path: Path,
        every: float,
        token: Callable[[], Token] = read_token,
        get: Callable[[Token], Awaitable[dict[str, Any]]] = fetch,
    ) -> None:
        self.path = path
        self.every = every
        self.token = token
        self.get = get
        self._next = float("-inf")
        self._failures = 0
        self.problem: str | None = None  # the last one; logged when it changes

    def _note(self, problem: str | None) -> None:
        if problem is not None and problem != self.problem:
            logger.warning("%s", problem)
        elif problem is None and self.problem is not None:
            logger.info("usage: answering again")
        self.problem = problem

    async def __call__(self, now: float) -> None:
        if now < self._next:
            return
        self._next = now + self.every
        try:
            token = self.token()
        except TokenError as error:
            self._note(str(error))
            return
        if token.expired(now):
            self._note(
                "the Claude Code sign-in has expired; Claude Code renews it the next time it runs"
            )
            return
        try:
            data = await self.get(token)
        except ApiError as error:
            if error.status in (401, 403):
                # A renewed token fixes it, and the token is read again each time.
                self._note(f"{error}: the sign-in was refused; it renews when Claude Code runs")
                return
            self._failures += 1
            wait = min(BACKOFF_MAX, self.every * 2 ** (self._failures - 1))
            self._next = now + wait
            self._note(f"usage: {error}; asking again in {wait:.0f}s")
            return
        self._failures = 0
        found = to_rate_limits(data)
        self._note(None if found else "usage: the answer holds no limits this understands")
        if found:
            limits.record_limits(self.path, found, now)
