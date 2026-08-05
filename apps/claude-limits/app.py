#!/usr/bin/env python3
"""Claude Limits: live Claude Code usage bars with an animated Clawd mascot.

    python app.py                          # BUSY Bar over USB (10.0.4.20)
    python app.py --host 127.0.0.1:8080    # emulator or a Wi-Fi bar
    python app.py --host 127.0.0.1:8080 --mock   # no credentials needed

Front (72x16): the 8 px Clawd mascot on the left, a gray panel on the right
holding the 5-hour session bar (top) and the weekly bar (bottom). Both turn
dark red above 85% and show their time-to-reset inside the bar above 75%.
Back (160x80, OLED): the full dashboard - percentages, bars, reset times, and
how many Claude Code sessions are live right now.

Reads the local Claude Code OAuth token **read-only** (macOS Keychain, or
~/.claude/.credentials.json elsewhere) and never writes it back; an expired
token renders "AUTH EXPIRED - RUN claude" instead. Session activity comes from
the mtimes of ~/.claude/projects/*/*.jsonl - transcripts are never opened.

Standard library only.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = "claude-limits"
DRAW_PATH = "/api/display/draw"
DEVICE_TIMEOUT = 10

ANIM_INTERVAL = 0.25  # s; ~4 fps mascot re-push
ACTIVITY_INTERVAL = 2.0  # s; how often the local session scan re-runs


class Cfg:
    """The two render-time knobs every layout function reads off ``cfg``."""

    element_timeout_seconds = 480  # dead-man window: a dead app clears itself
    thresholds = {"high": 85}


CFG = Cfg()


# --------------------------------------------------------------------------
# Claude Code credentials (read-only)
# --------------------------------------------------------------------------

KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = "~/.claude/.credentials.json"


def access_token():
    """Return the current Claude Code OAuth access token, or ``None``.

    Strictly read-only: the token is never refreshed and never written back.
    Refreshing would rotate the refresh token, and persisting the rotation
    means an LED widget mutating the Keychain entry your ``claude`` login
    depends on - so an expired token simply renders the degraded frame.
    """
    blob = None
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                timeout=10,
            )
            if proc.returncode == 0:
                blob = proc.stdout.decode("utf-8")
        except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
            blob = None
    if blob is None:
        try:
            with open(Path(CREDENTIALS_FILE).expanduser(), encoding="utf-8") as fh:
                blob = fh.read()
        except OSError:
            return None
    try:
        oauth = json.loads(blob)["claudeAiOauth"]
        if float(oauth["expiresAt"]) / 1000 <= time.time():
            return None  # expired -> "AUTH EXPIRED - RUN claude"
        return oauth["accessToken"]
    except (ValueError, KeyError, TypeError):
        return None


# --------------------------------------------------------------------------
# Usage endpoint
# --------------------------------------------------------------------------

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_TIMEOUT = 15
USER_AGENT = "claude-code/2.1.220"


@dataclass
class UsageSnapshot:
    five_pct: float
    five_resets: datetime
    week_pct: float
    week_resets: datetime
    fetched_at: float


class AuthError(Exception):
    """The token was rejected - render the auth-degraded frame."""


class UsageError(Exception):
    """A transient failure - keep showing the last good snapshot."""


def _parse_resets(raw: str) -> datetime:
    """Parse a resets_at timestamp into a tz-aware UTC datetime (3.9-safe)."""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_usage(token: str) -> UsageSnapshot:
    """Fetch the 5-hour and 7-day utilization for the signed-in account.

    NOTE: ``/api/oauth/usage`` is an **undocumented** endpoint that Claude Code
    itself calls, hence the pinned User-Agent. It is the single most likely
    thing to break in a future release; everything else here is public API.
    """
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=USAGE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthError(f"usage HTTP {exc.code}")
        if exc.code == 429:
            raise UsageError("rate limited")
        raise UsageError(f"usage HTTP {exc.code}")
    except Exception as exc:
        raise UsageError(f"usage request failed: {exc}")

    try:
        five = data["five_hour"]
        week = data["seven_day"]
        return UsageSnapshot(
            five_pct=float(five["utilization"]),
            five_resets=_parse_resets(five["resets_at"]),
            week_pct=float(week["utilization"]),
            week_resets=_parse_resets(week["resets_at"]),
            fetched_at=time.time(),
        )
    except (KeyError, TypeError, ValueError):
        raise UsageError("unexpected payload")


# --------------------------------------------------------------------------
# Local session activity
# --------------------------------------------------------------------------

PROJECTS_DIR = "~/.claude/projects"
SESSION_WINDOW = 300  # s; a transcript touched this recently is a live session
WORKING_WINDOW = 10  # s; a transcript touched this recently means a live turn


@dataclass
class Activity:
    """How many Claude Code sessions are live, and whether one is mid-turn."""

    sessions: int = 0
    working: bool = False


def read_activity(now: float) -> Activity:
    """Count live sessions from transcript mtimes under ``~/.claude/projects``.

    Pure ``os.scandir`` + ``st_mtime``: transcripts are never opened (they run
    to tens of megabytes). Any read error degrades to zero sessions rather
    than taking the display down.
    """
    root = Path(PROJECTS_DIR).expanduser()
    sessions = 0
    working = False
    try:
        projects = list(os.scandir(root))
    except OSError:
        return Activity()
    for proj in projects:
        try:
            if not proj.is_dir():
                continue
            with os.scandir(proj.path) as entries:
                for entry in entries:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    age = now - entry.stat().st_mtime
                    if age <= SESSION_WINDOW:
                        sessions += 1
                    if age <= WORKING_WINDOW:
                        working = True
        except OSError:
            continue  # one unreadable project must not hide the others
    return Activity(sessions, working)


# --------------------------------------------------------------------------
# Layout
#
# Element ids, coordinates, fonts, and text formats are the load-bearing
# layout contract: element ``type`` and ``display`` for a given id must never
# change (the firmware rejects a redraw with 400 otherwise). Every returned
# element carries ``timeout`` so a dead app auto-reverts the screen.
#
# Three frame kinds are derived from the inputs (no state parameter):
#   * ``snap is None``            -> auth-degraded frame (AUTH EXPIRED);
#   * snapshot older than 15 min  -> stale frame (STALE 21m);
#   * otherwise                   -> normal frame.
# --------------------------------------------------------------------------

WHITE = "#FFFFFFFF"
TRANSPARENT = ["#00000000"]
STALE_SECONDS = 900  # last good snapshot older than 15 min -> stale frame

# Two-color usage palette (Claude orange-brown OK / dark red above 85%).
C_OK = "#CD6E58FF"          # Claude orange-brown ("ok"), same as the crab coral
C_HIGH = "#B3261EFF"        # dark red ("high", usage > 85%)
C_PANEL = "#3A3A3AFF"       # gray background behind the front bars
C_PANEL_EDGE = "#4A4A4AFF"  # gray panel outline
C_BAR_EDGE = "#1F1F1FFF"    # bar track outline
C_ON_OK = "#1A1A1AFF"       # text on an orange bar fill
C_ON_HIGH = "#FFFFFFFF"     # text on a dark-red bar fill
LIMIT_HIGH = 85             # usage pct above which everything turns dark red

# Clawd mascot palette/constants.
C_CLAWD = "#CD6E58FF"       # coral body
C_EYE = "#000000FF"         # 1x1 black eye
C_TRANS = "#00000000"       # transparent (blink / eye holes)
C_PINK = "#F590C0FF"        # decorative heart/confetti
C_RAIN = "#8FB8E8FF"        # pale-blue rain streak (rain)
C_MOON = "#F4E7A6FF"        # pale-yellow crescent moon (moonlight)
ANIM_TIMEOUT = 3            # s; animated clawd rect auto-revert window

# Eye look offsets in 14x8 grid cols/rows, applied per ``ss``.
_EYE_OFFSET = {
    "fwd": (0, 0),
    "left": (-1, 0),
    "right": (1, 0),
    "down": (0, 1),
    "blink": (0, 0),
}


def limit_color(pct: float, thresholds: dict) -> str:
    """Two-color usage indicator: Claude orange-brown at/below the high
    threshold, dark red above it."""
    hi = thresholds.get("high", LIMIT_HIGH)
    return C_HIGH if pct > hi else C_OK


def fmt_rel_short(seconds: float) -> str:
    """Front compact form: ``3h59m`` or ``59m``. Negatives clamp to 0."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{max(m, 0)}m"


def fmt_rel_long(seconds: float) -> str:
    """Back parenthesized session form: ``(3h 59m)`` or ``(59m)``."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"({h}h {m:02d}m)"
    return f"({max(m, 0)}m)"


def fmt_rel_week(seconds: float) -> str:
    """Week-range form: ``6d`` at/over 48 h, ``1d 5h`` from 24 h, else
    ``23h59m`` / ``9h05m`` / ``59m``. Negatives clamp to 0."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    d = h // 24
    h = h % 24
    m = (seconds % 3600) // 60
    if h + d * 24 >= 48:
        return f"{d}d"
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{max(m, 0)}m"


def fmt_rel_week_long(seconds: float) -> str:
    """Back parenthesized week form: ``(2d 5h)`` or ``(23h 59m)``."""
    seconds = max(0, int(seconds))
    h = seconds // 3600
    d = h // 24
    h = h % 24
    m = (seconds % 3600) // 60
    if d > 0:
        return f"({d}d {h}h)"
    if h > 0:
        return f"({h}h {m:02d}m)"
    return f"({max(m, 0)}m)"


def fmt_abs(dt: datetime, weekly: bool) -> str:
    """Render a reset time in local time. Session -> ``%H:%M``; weekly ->
    ``%H:%M`` when under a day away, else weekday ``%a %H:%M``."""
    local = dt.astimezone()
    if not weekly:
        return local.strftime("%H:%M")
    if local - datetime.now().astimezone() < timedelta(hours=24):
        return local.strftime("%H:%M")
    return local.strftime("%a %H:%M")


def _text(eid, text, x, y, align, font, color, display, timeout):
    return {
        "id": eid,
        "type": "text",
        "text": text,
        "font": font,
        "color": color,
        "align": align,
        "x": int(x),
        "y": int(y),
        "timeout": timeout,
        "display": display,
    }


def _rect(eid, x, y, width, height, fill, fill_colors, border_width,
          border_color, display, timeout):
    return {
        "id": eid,
        "type": "rectangle",
        "x": int(x),
        "y": int(y),
        "width": int(width),
        "height": int(height),
        "radius": 0,
        "fill": fill,
        "fill_colors": list(fill_colors),
        "border_width": int(border_width),
        "border_color": border_color,
        "timeout": timeout,
        "display": display,
    }


def _bar_fill(eid, x, y, pct, display, timeout, color=WHITE, width=154,
              height=6):
    """Progress fill rect; transparent (invisible 1 px) when pct < 1 so the
    element id stays alive without drawing a visible pixel."""
    fill_width = max(1, round(width * pct / 100))
    fill_colors = [color] if pct >= 1 else TRANSPARENT
    return _rect(eid, x, y, fill_width, height, "solid", fill_colors, 0, WHITE,
                 display, timeout)


def _bar_outline(eid, x, y, display, timeout):
    return _rect(eid, x, y, 156, 8, "none", [], 1, WHITE, display, timeout)


def _pixel(eid, x, y, color, display):
    """A 1x1 animated extra rect on the ANIM_TIMEOUT window."""
    return _rect(eid, x, y, 1, 1, "solid", [color], 0, "#00000000", display,
                 ANIM_TIMEOUT)


def _char(eid, x, y, ch, display, color=WHITE):
    """A 1-char animated extra text on the ANIM_TIMEOUT window."""
    return _text(eid, ch, x, y, "top_left", "small", color, display,
                 ANIM_TIMEOUT)


def clawd_elements(ox, oy, ss, eyes, body_color, eye_hidden_fill, idprefix,
                   display, timeout) -> list:
    """Build the 9 rectangle elements of one Clawd starting at (ox, oy).

    Scales a 14x8 cell grid by ``ss``. Eye fill swaps to ``eye_hidden_fill``
    on ``'blink'`` so ids stay stable/sent.
    """
    ex, ey = _EYE_OFFSET.get(eyes, (0, 0))
    eye_fc = eye_hidden_fill if eyes == "blink" else C_EYE
    body_fc = [body_color]
    rects = [
        _rect(f"{idprefix}body_top", ox + 3 * ss, oy, 8 * ss, 2 * ss, "solid",
              body_fc, 0, "#00000000", display, timeout),
        _rect(f"{idprefix}body_mid", ox + 1 * ss, oy + 2 * ss, 12 * ss, 2 * ss,
              "solid", body_fc, 0, "#00000000", display, timeout),
        _rect(f"{idprefix}body_bot", ox + 3 * ss, oy + 4 * ss, 8 * ss, 2 * ss,
              "solid", body_fc, 0, "#00000000", display, timeout),
    ]
    for i, cx in enumerate((3, 5, 8, 10)):
        rects.append(_rect(f"{idprefix}leg_{i}", ox + cx * ss, oy + 6 * ss, ss,
                           2 * ss, "solid", body_fc, 0, "#00000000", display, timeout))
    for i, cx in enumerate((4, 9)):
        rects.append(_rect(f"{idprefix}eye_{i}",
                           ox + (cx + ex) * ss, oy + (1 + ey) * ss, ss, ss,
                           "solid", [eye_fc], 0, "#00000000", display, timeout))
    return rects


# ---- motion helpers (no deps) ----
def lerp(a, b, u):
    return a + (b - a) * u


def ease_out(u):
    return 1 - (1 - u) * (1 - u)


def ease_inout(u):
    return 3 * u * u - 2 * u * u * u


def ping(u):
    """0 -> 1 -> 0 triangle over u in [0, 1]."""
    return 2 * u if u < 0.5 else 2 * (1 - u)


def trip(u):
    """+1 / -1 pulse (rounded sine)."""
    return round(math.sin(u * 2 * math.pi))


def blink(t, period=0.9, frac=0.1):
    """Return 'blink' inside the last ``frac`` of each ``period``, else None."""
    return "blink" if (t % period) >= period * (1 - frac) else None


def gated(u, a, b):
    return a <= u <= b


@dataclass
class Frame:
    """One animation frame shared by the front (8 px lane) and back crabs."""
    t: float          # normalized progress 0..1 within the current animation
    dur: float        # this animation's duration in seconds (schedule)
    anim: str         # stable animation name
    mood: str         # 'ok' | 'high' | 'stale' | 'auth' (derived from snapshot)
    fx: int           # front crab origin, fx in [0,13]
    fy: int           # front crab origin, fy in [0,8]
    feyes: str
    bx: int           # back crab origin, bx in [6,140]
    by: int           # back crab origin, by in [30,50]
    beyes: str
    fextras: list = field(default_factory=list)  # front extra element dicts
    bextras: list = field(default_factory=list)  # back extra element dicts
    # NB: ``working`` must stay last - every animation below builds a Frame
    # positionally through ``bextras``.
    working: bool = False  # a Claude Code turn is live right now


# ---- the 21 animations (each f(t) -> Frame) ----
def a_breath(t):
    return Frame(t, 8, "breath", "ok", 2, 4 + trip(t), "fwd",
                 76, 40 + trip(t), "fwd")


def a_walk(t):
    p = ping(t)
    return Frame(t, 10, "walk", "ok",
                 round(lerp(2, 11, p)), 4 + trip(4 * t), "fwd",
                 round(lerp(10, 130, p)), 40 + trip(4 * t), "fwd")


def a_jump(t):
    lift = abs(math.sin(6 * math.pi * t))
    e = "blink" if lift > 0.9 else "fwd"
    return Frame(t, 6, "jump", "ok", 2, 4 - round(lift * 3), e,
                 76, 40 - round(lift * 6), e)


def a_wave_r(t):
    up = math.sin(4 * math.pi * t) > 0
    e = "right" if up else "fwd"
    fx, fy, bx, by = 6, 4, 70, 40
    fe = [_pixel("fx_4a", fx + 14, fy + 1, C_CLAWD, "front"),
          _pixel("fx_4b", fx + 15, fy + (0 if up else 1), C_CLAWD, "front")]
    be = [_pixel("bx_4a", bx + 14, by + 1, C_CLAWD, "back"),
          _pixel("bx_4b", bx + 15, by + (0 if up else 1), C_CLAWD, "back")]
    return Frame(t, 6, "wave_r", "ok", fx, fy, e, bx, by, e, fe, be)


def a_wave_l(t):
    up = math.sin(4 * math.pi * t) > 0
    e = "left" if up else "fwd"
    fx, fy, bx, by = 8, 4, 70, 40
    fe = [_pixel("fx_5a", fx - 1, fy + 1, C_CLAWD, "front"),
          _pixel("fx_5b", fx - 2, fy + (0 if up else 1), C_CLAWD, "front")]
    be = [_pixel("bx_5a", bx - 1, by + 1, C_CLAWD, "back"),
          _pixel("bx_5b", bx - 2, by + (0 if up else 1), C_CLAWD, "back")]
    return Frame(t, 6, "wave_l", "ok", fx, fy, e, bx, by, e, fe, be)


def a_look(t):
    if t < 0.22:
        e = "right"
    elif t < 0.44:
        e = "fwd"
    elif t < 0.66:
        e = "left"
    elif t < 0.88:
        e = "fwd"
    else:
        e = "right"
    fe, be = [], []
    if gated(t, 0.5, 0.65):
        fe = [_char("fx_6a", 2 + 13, 4 - 2, "?", "front")]
        be = [_char("bx_6a", 76 + 15, 40 - 2, "?", "back")]
    return Frame(t, 8, "look", "ok", 2, 4, e, 76, 40, e, fe, be)


def a_sparkle(t):
    fx, fy, bx, by = 2, 4, 76, 40
    fe, be = [], []
    if gated(t, 0.3, 0.5) or gated(t, 0.75, 0.9):
        fe = [_pixel("fx_7a", fx + 4, fy + 1, WHITE, "front"),
              _pixel("fx_7b", fx + 9, fy + 1, WHITE, "front")]
        be = [_pixel("bx_7a", bx + 4, by + 1, WHITE, "back"),
              _pixel("bx_7b", bx + 9, by + 1, WHITE, "back")]
    if gated(t, 0.55, 0.7):
        fe += [_pixel("fx_7c", fx + 14, fy - 1, C_PINK, "front")]
        be += [_pixel("bx_7c", bx + 14, by - 1, C_PINK, "back")]
    return Frame(t, 7, "sparkle", "ok", fx, fy, "fwd", bx, by, "fwd", fe, be)


def a_sleep(t):
    fx, fy, bx, by = 2, 6, 76, 42
    e = "blink" if t > 0.2 else "fwd"
    fe = [_char("fx_8a", fx + 13, fy - 2, "z", "front")] if gated(t, 0.3, 0.6) else []
    be = [_char("bx_8a", bx + 15, by - 2, "z", "back")] if gated(t, 0.3, 0.6) else []
    if gated(t, 0.55, 1.0):
        fe += [_char("fx_8b", fx + 15, fy - 3, "Z", "front")]
        be += [_char("bx_8b", bx + 16, by - 3, "Z", "back")]
    return Frame(t, 8, "sleep", "ok", fx, fy, e, bx, by, e, fe, be)


def a_panic(t):
    fx, fy, bx, by = 2 + trip(8 * t), 4, 76 + trip(8 * t) * 2, 40
    fe = [_pixel("fx_9a", fx + 13, fy + 1, WHITE, "front")] if t > 0.15 else []
    be = [_pixel("bx_9a", bx + 13, by + 1, WHITE, "back")] if t > 0.15 else []
    return Frame(t, 7, "panic", "ok", fx, fy, "down", bx, by, "down", fe, be)


def a_worry(t):
    fx, fy, bx, by = 2, 4, 76, 40
    fe = [_pixel("fx_10a", fx + 13, fy + 1, WHITE, "front")] if t > 0.2 else []
    be = [_pixel("bx_10a", bx + 13, by + 1, WHITE, "back")] if t > 0.2 else []
    return Frame(t, 7, "worry", "ok", fx, fy, "down", bx, by, "down", fe, be)


def a_dance(t):
    s = math.sin(4 * math.pi * t)
    fx = 6 + round(s) * 2
    fy = 4 + trip(4 * t)
    bx = 76 + round(s) * 4
    by = 40 + trip(4 * t)
    if t < 0.25 or 0.5 <= t < 0.75:
        e = "left"
    else:
        e = "right"
    fe, be = [], []
    if s > 0:
        fe = [_pixel("fx_11a", fx + 13, fy - 1, C_PINK, "front"),
              _pixel("fx_11b", fx + 14, fy, C_PINK, "front")]
        be = [_pixel("bx_11a", bx + 13, by - 1, C_PINK, "back"),
              _pixel("bx_11b", bx + 14, by, C_PINK, "back")]
    return Frame(t, 9, "dance", "ok", fx, fy, e, bx, by, e, fe, be)


def a_chase(t):
    if t < 0.5:
        fx = round(lerp(2, 13, min(1, 2 * t)))
        bx = round(lerp(60, 126, min(1, 2 * t)))
        e = "right"
    else:
        fx = round(lerp(13, 2, min(1, 2 * (t - 0.5))))
        bx = round(lerp(126, 60, min(1, 2 * (t - 0.5))))
        e = "fwd"
    return Frame(t, 8, "chase", "ok", fx, 4, e, bx, 40, e)


def a_rain(t):
    """London Clawd: stands under a storm, pale-blue streaks falling around."""
    fx, fy, bx, by = 2, 4, 76, 40
    e = "down" if t % 0.5 < 0.08 else "fwd"
    fe, be = [], []
    for i in range(3):
        rr = int(((t + i / 3.0) % 1.0) * 5)
        fe.append(_pixel(f"fx_12_{i}", fx + 2 + i * 4, fy - 2 + rr, C_RAIN, "front"))
        be.append(_pixel(f"bx_12_{i}", bx + 2 + i * 4, by - 2 + rr, C_RAIN, "back"))
    return Frame(t, 8, "rain", "ok", fx, fy, e, bx, by, e, fe, be)


def a_surf(t):
    """Clawd Surfing: rides a wave side to side with a white spray pixel."""
    s = math.sin(2 * math.pi * t)
    fx = round(lerp(3, 8, ping(t)))
    fy = 4 + trip(2 * t)
    bx = round(lerp(70, 110, ping(t)))
    by = 40 + trip(2 * t)
    fe = [_pixel("fx_13", fx + 14, fy, WHITE, "front")] if s > 0 else []
    be = [_pixel("bx_13", bx + 14, by, WHITE, "back")] if s > 0 else []
    return Frame(t, 6, "surf", "ok", fx, fy, "fwd", bx, by, "fwd", fe, be)


def a_love(t):
    """Clawd Love: a pink heart rises off his head, then bursts into pixels."""
    fx, fy, bx, by = 2, 4, 76, 40
    fe, be = [], []
    if t < 0.55:
        h = int(t / 0.55 * 3)
        fe = [_pixel("fx_14a", fx + 5, fy - 1 - h, C_PINK, "front"),
              _pixel("fx_14b", fx + 7, fy - 1 - h, C_PINK, "front"),
              _pixel("fx_14c", fx + 6, fy - h, C_PINK, "front")]
        be = [_pixel("bx_14a", bx + 5, by - 1 - h, C_PINK, "back"),
              _pixel("bx_14b", bx + 7, by - 1 - h, C_PINK, "back"),
              _pixel("bx_14c", bx + 6, by - h, C_PINK, "back")]
    else:
        p = (t - 0.55) / 0.45
        for i in range(4):
            ang = i * math.pi / 2
            fe.append(_pixel(f"fx_14d{i}", fx + 6 + round(2 * math.cos(ang) * p),
                             fy - 2 + round(2 * math.sin(ang) * p), C_PINK, "front"))
            be.append(_pixel(f"bx_14d{i}", bx + 6 + round(2 * math.cos(ang) * p),
                             by - 2 + round(2 * math.sin(ang) * p), C_PINK, "back"))
    return Frame(t, 8, "love", "ok", fx, fy, "fwd", bx, by, "fwd", fe, be)


def a_mad(t):
    """Mad Clawd: trembling with crimson heat-vision beams streaking sideways."""
    shake = trip(10 * t)
    fx, fy, bx, by = 2 + shake, 4, 76 + 2 * shake, 40
    e = "blink" if t % 0.6 < 0.15 else "down"
    fe, be = [], []
    if t > 0.15:
        for i in range(3):
            fe.append(_pixel(f"fx_15_{i}", fx + 12 + i, fy + 1, C_HIGH, "front"))
            be.append(_pixel(f"bx_15_{i}", bx + 12 + i, by + 1, C_HIGH, "back"))
    return Frame(t, 6, "mad", "ok", fx, fy, e, bx, by, e, fe, be)


def a_moonlight(t):
    """Moonlit Clawd: dozed off low with a pale moon and a drifting z."""
    fx, fy, bx, by = 2, 6, 76, 42
    e = "blink" if t > 0.3 else "fwd"
    fe = [_pixel("fx_16", fx + 13, fy - 3, C_MOON, "front")] if gated(t, 0.4, 1.0) else []
    be = [_pixel("bx_16", bx + 14, by - 3, C_MOON, "back")] if gated(t, 0.4, 1.0) else []
    if gated(t, 0.5, 0.8):
        fe += [_char("fx_16b", fx + 15, fy - 4, "z", "front")]
        be += [_char("bx_16b", bx + 16, by - 4, "z", "back")]
    return Frame(t, 8, "moonlight", "ok", fx, fy, e, bx, by, e, fe, be)


def a_dealwithit(t):
    """Clawd Life: a cool, slow nod (GTA-style shade drop)."""
    tilt = round(math.sin(2 * math.pi * t) * 0.6)
    fx, fy, bx, by = 2, 4 + tilt, 76, 40 + tilt
    e = "blink" if t % 1.5 < 0.12 else "fwd"
    return Frame(t, 7, "dealwithit", "ok", fx, fy, e, bx, by, e)


def a_fire(t):
    """This-is-fine Clawd: calm while little flames lick up around him."""
    fx, fy, bx, by = 2, 4, 76, 40
    fe, be = [], []
    for i in range(4):
        p = int(((t + i / 4.0) % 1.0) * 2)
        c = C_OK if i % 2 else C_HIGH
        fe.append(_pixel(f"fx_18_{i}", fx + 1 + i * 3, fy + 3 + p, c, "front"))
        be.append(_pixel(f"bx_18_{i}", bx + 1 + i * 3, by + 3 + p, c, "back"))
    return Frame(t, 7, "fire", "ok", fx, fy, "fwd", bx, by, "fwd", fe, be)


def a_mariachi(t):
    """Mariachlawd: side-step dance while shaking pink maracas."""
    s = math.sin(4 * math.pi * t)
    fx = 4 + round(s) * 2
    fy = 4 + trip(4 * t)
    bx = 76 + round(s) * 4
    by = 40 + trip(4 * t)
    fe, be = [], []
    if s > 0:
        fe = [_pixel("fx_19", fx + 13, fy - 1, C_PINK, "front"),
              _pixel("fx_19b", fx + 14, fy, C_PINK, "front")]
        be = [_pixel("bx_19", bx + 13, by - 1, C_PINK, "back"),
              _pixel("bx_19b", bx + 14, by, C_PINK, "back")]
    return Frame(t, 7, "mariachi", "ok", fx, fy, "fwd", bx, by, "fwd", fe, be)


def a_barbie(t):
    """Clawd on the Barbie: a pink sausage flips overhead on a small arc."""
    fx, fy, bx, by = 2, 4, 76, 40
    fe, be = [], []
    if t < 0.7:
        a = t / 0.7 * math.pi
        dy = round(math.sin(a) * 2)
        dx = round(math.cos(a) * 2)
        fe = [_pixel("fx_20a", fx + 5 + dx, fy - 2 - dy, C_PINK, "front"),
              _pixel("fx_20b", fx + 7 + dx, fy - 2 - dy, C_PINK, "front")]
        be = [_pixel("bx_20a", bx + 5 + dx, by - 2 - dy, C_PINK, "back"),
              _pixel("bx_20b", bx + 7 + dx, by - 2 - dy, C_PINK, "back")]
    return Frame(t, 8, "barbie", "ok", fx, fy, "right", bx, by, "right", fe, be)


ANIMS = [a_breath, a_walk, a_jump, a_wave_r, a_wave_l, a_look,
         a_sparkle, a_sleep, a_panic, a_worry, a_dance, a_chase,
         a_rain, a_surf, a_love, a_mad, a_moonlight, a_dealwithit,
         a_fire, a_mariachi, a_barbie]
for _anim in ANIMS:
    _anim.dur = _anim(0.0).dur  # internal loop duration per animation

HOLD = 120.0  # s; each animation stays on screen before the next takes over


def animation_at(now: float, hold: float) -> Frame:
    """Pick the animation active at epoch ``now`` (stateless rotation).

    Each animation is held for ``hold`` seconds, looping its internal motion,
    before the rotation advances to the next one.
    """
    idx = int(now // hold) % len(ANIMS)
    a = ANIMS[idx]
    local = now % hold
    t = (local % a.dur) / a.dur
    return a(t)


def mood_for(snap, now: float, cfg) -> str:
    """Derive the Clawd mood from the snapshot: auth > stale > high > ok."""
    if snap is None:
        return "auth"
    if now - snap.fetched_at > STALE_SECONDS:
        return "stale"
    if max(snap.five_pct, snap.week_pct) > cfg.thresholds.get("high", LIMIT_HIGH):
        return "high"
    return "ok"


def animation_for(snap, now: float, cfg, hold: float = HOLD) -> Frame:
    f = animation_at(now, hold)
    f.mood = mood_for(snap, now, cfg)
    return f


def frame_elements(frame: Frame, cfg) -> list:
    """The animated mask: 9 ``fc_*`` rects (front, ss=1 at (fx,fy)), 9 ``bc_*``
    rects (back, ss=1 at (bx,by)), plus fextras/bextras, all on ANIM_TIMEOUT.

    Eyes resolve per-anim base -> blink override -> worried 'down' for non-ok
    moods. high/stale/auth moods also sprout a sweat pixel (panic/worry carry
    their own). ``frame.working`` only doubles the blink rate - it is
    deliberately kept out of the mood ladder so a live turn can never mask the
    >85% warning, which is exactly when you most need to see it.
    """
    feyes, beyes = frame.feyes, frame.beyes
    busy = blink(frame.t, period=0.5, frac=0.25) if frame.working else blink(frame.t)
    if busy:
        feyes = beyes = "blink"
    elif frame.mood in ("auth", "stale", "high"):
        feyes = beyes = "down"

    els = clawd_elements(frame.fx, frame.fy, 1, feyes, C_CLAWD, C_TRANS,
                         "fc_", "front", ANIM_TIMEOUT)
    els += clawd_elements(frame.bx, frame.by, 1, beyes, C_CLAWD, C_TRANS,
                          "bc_", "back", ANIM_TIMEOUT)
    els += list(frame.fextras)
    els += list(frame.bextras)
    if frame.mood in ("auth", "stale", "high") and frame.anim not in ("panic", "worry"):
        els.append(_pixel("fx_swm", frame.fx + 13, frame.fy + 1, WHITE, "front"))
        els.append(_pixel("bx_swm", frame.bx + 13, frame.by + 1, WHITE, "back"))
    return els


def front_bars(snap, now: float, cfg, fresh: bool) -> list:
    """Front right-side panel: gray background with the day (top) and week
    (bottom) progress bars. ``fresh`` = a normal (non-auth/non-stale) frame.

    Returns static elements on the 480 s timeout; the day time text (existing
    ``fsess`` id) shows inside the day bar only when ``fresh and usage > 75``.
    """
    timeout = cfg.element_timeout_seconds
    front = "front"
    pct = snap.five_pct if snap is not None else 0.0
    week = snap.week_pct if snap is not None else 0.0
    th = cfg.thresholds
    hi = th.get("high", LIMIT_HIGH)

    els = [
        _rect("fpanel", 28, 0, 44, 16, "solid", [C_PANEL], 1, C_PANEL_EDGE,
              front, timeout),
        _rect("fday_track", 31, 1, 38, 6, "none", [], 1, C_BAR_EDGE, front,
              timeout),
        _bar_fill("fday_fill", 32, 2, pct, front, timeout,
                  limit_color(pct, th), width=36, height=4),
        _rect("fweek_track", 31, 9, 38, 6, "none", [], 1, C_BAR_EDGE, front,
              timeout),
        _bar_fill("fweek_fill", 32, 10, week, front, timeout,
                  limit_color(week, th), width=36, height=4),
    ]
    if fresh and pct > 75:
        five_win = (snap.five_resets - datetime.fromtimestamp(now, timezone.utc)).total_seconds()
        fsess_color = C_ON_HIGH if pct > hi else C_ON_OK
        fsess_text = fmt_rel_short(five_win)
    else:
        fsess_color = WHITE
        fsess_text = " "
    els.append(_text("fsess", fsess_text, 33, 1, "top_left", "tiny",
                     fsess_color, front, timeout))
    if fresh and week > 75:
        week_win = (snap.week_resets - datetime.fromtimestamp(now, timezone.utc)).total_seconds()
        fweek_color = C_ON_HIGH if week > hi else C_ON_OK
        fweek_text = fmt_rel_week(week_win)
    else:
        fweek_color = WHITE
        fweek_text = " "
    els.append(_text("fweek", fweek_text, 33, 9, "top_left", "tiny",
                     fweek_color, front, timeout))
    # Reserved slot, deliberately blank: x=70 top_right sits inside the
    # x 28..71 panel and any text here collides with ``fday_track``. On the
    # 72x16 front, session activity is expressed by the mascot alone.
    els.append(_text("fagents", " ", 70, 0, "top_right", "small", WHITE, front,
                     timeout))
    return els


def _sessions_line(act: Activity) -> str:
    """Back status line: ``2 SESSIONS`` / ``1 SESSION - WORKING``."""
    line = f"{act.sessions} SESSION" + ("" if act.sessions == 1 else "S")
    return line + " - WORKING" if act.working else line


def _back_panel(act, timeout, *, sess_p, sess_p_color, sess_r, sess_fill,
                sess_fill_color, week_p, week_p_color, week_r, week_fill,
                week_fill_color) -> list:
    """The 12 static back dashboard elements (ids/types are the firmware
    contract; ``bagents`` and ``bsess_r``/``bweek_r`` stay WHITE)."""
    back = "back"
    return [
        _text("btitle", "CLAUDE LIMITS", 80, 1, "top_mid", "large", WHITE,
              back, timeout),
        _text("bsess_l", "SESSION", 2, 13, "top_left", "normal", WHITE, back,
              timeout),
        _text("bsess_p", sess_p, 158, 13, "top_right", "normal", sess_p_color,
              back, timeout),
        _bar_outline("bsess_bar", 2, 22, back, timeout),
        _bar_fill("bsess_fill", 3, 23, sess_fill, back, timeout,
                  sess_fill_color),
        _text("bsess_r", sess_r, 2, 32, "top_left", "normal", WHITE, back,
              timeout),
        _text("bweek_l", "WEEKLY", 2, 43, "top_left", "normal", WHITE, back,
              timeout),
        _text("bweek_p", week_p, 158, 43, "top_right", "normal", week_p_color,
              back, timeout),
        _bar_outline("bweek_bar", 2, 52, back, timeout),
        _bar_fill("bweek_fill", 3, 53, week_fill, back, timeout,
                  week_fill_color),
        _text("bweek_r", week_r, 2, 62, "top_left", "normal", WHITE, back,
              timeout),
        _text("bagents", _sessions_line(act), 2, 72, "top_left", "normal",
              WHITE, back, timeout),
    ]


def build_elements(snap, act, cfg, now: float, frame: Frame = None) -> list:
    timeout = cfg.element_timeout_seconds
    if frame is None:
        frame = animation_for(snap, now, cfg)
    clawd = frame_elements(frame, cfg)

    # ---- auth-degraded frame (no snapshot at all) ----
    if snap is None:
        return (front_bars(snap, now, cfg, fresh=False) + _back_panel(
            act, timeout,
            sess_p=" ", sess_p_color=WHITE, sess_r="AUTH EXPIRED - RUN claude",
            sess_fill=0, sess_fill_color=WHITE,
            week_p=" ", week_p_color=WHITE, week_r=" ",
            week_fill=0, week_fill_color=WHITE,
        ) + clawd)

    now_dt = datetime.fromtimestamp(now, timezone.utc)

    # ---- stale frame (last good snapshot older than 15 min) ----
    if now - snap.fetched_at > STALE_SECONDS:
        age_m = int((now - snap.fetched_at) // 60)
        stale_text = f"STALE {age_m}m"
        five, week = snap.five_pct, snap.week_pct
        th = cfg.thresholds
        return (front_bars(snap, now, cfg, fresh=False) + _back_panel(
            act, timeout,
            sess_p=f"{five:.0f}%", sess_p_color=limit_color(five, th),
            sess_r=stale_text, sess_fill=five,
            sess_fill_color=limit_color(five, th),
            week_p=f"{week:.0f}%", week_p_color=limit_color(week, th),
            week_r=stale_text, week_fill=week,
            week_fill_color=limit_color(week, th),
        ) + clawd)

    # ---- normal frame ----
    five, week = snap.five_pct, snap.week_pct
    th = cfg.thresholds
    bsess_r = f"resets {fmt_abs(snap.five_resets, False)}"
    if five > 75:
        five_win = (snap.five_resets - now_dt).total_seconds()
        bsess_r += f" {fmt_rel_long(five_win)}"
    bweek_r = f"resets {fmt_abs(snap.week_resets, True)}"
    if week > 75:
        week_win = (snap.week_resets - now_dt).total_seconds()
        bweek_r += f" {fmt_rel_week_long(week_win)}"
    return (front_bars(snap, now, cfg, fresh=True) + _back_panel(
        act, timeout,
        sess_p=f"{five:.0f}%", sess_p_color=limit_color(five, th),
        sess_r=bsess_r, sess_fill=five, sess_fill_color=limit_color(five, th),
        week_p=f"{week:.0f}%", week_p_color=limit_color(week, th),
        week_r=bweek_r, week_fill=week, week_fill_color=limit_color(week, th),
    ) + clawd)


# --------------------------------------------------------------------------
# BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs)
# --------------------------------------------------------------------------


class DeviceError(Exception):
    """Raised only when the device is unreachable (network failure)."""


class BusyBar:
    """Thin wrapper around the device canvas API.

    ``draw`` returns the HTTP status so the caller can branch on
    200/400/401/409; only network-level failures raise :class:`DeviceError`.
    """

    def __init__(self, base: str, token: str, priority: int):
        self.base = base
        self.token = token
        self.priority = priority

    def _headers(self) -> dict:
        headers = {}
        if self.token:
            headers["X-API-Token"] = self.token
        return headers

    def draw(self, elements: list) -> tuple:
        """POST a frame; returns ``(status, body)``."""
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        body = json.dumps({
            "application_name": APP,
            "priority": self.priority,
            "elements": elements,
        }).encode("utf-8")
        req = urllib.request.Request(self.base + DRAW_PATH, data=body,
                                     headers=headers, method="POST")
        return self._send(req)

    def clear(self) -> tuple:
        """DELETE all of this app's elements; returns ``(status, body)``."""
        url = f"{self.base}{DRAW_PATH}?application_name={APP}"
        req = urllib.request.Request(url, headers=self._headers(),
                                     method="DELETE")
        return self._send(req)

    def _send(self, req) -> tuple:
        try:
            with urllib.request.urlopen(req, timeout=DEVICE_TIMEOUT) as resp:
                return int(resp.status), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise DeviceError(str(getattr(exc, "reason", exc))) from exc
        except (OSError, TimeoutError) as exc:
            raise DeviceError(str(exc)) from exc


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def base_url(host: str) -> str:
    """``10.0.4.20`` / ``127.0.0.1:8080`` / ``http://bar`` -> a base URL."""
    host = host.strip().replace("http://", "", 1).rstrip("/")
    return "http://" + host


def mock_snapshot(now: float, five: float, week: float) -> UsageSnapshot:
    """Fixture snapshot: no OAuth, no network, no Claude install required."""
    base = datetime.now(timezone.utc)
    return UsageSnapshot(
        five_pct=five,
        five_resets=base + timedelta(hours=2, minutes=13),
        week_pct=week,
        week_resets=base + timedelta(hours=29),
        fetched_at=now,
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20",
                   help="BUSY Bar ip[:port] (default 10.0.4.20; emulator: 127.0.0.1:8080)")
    p.add_argument("--token", default=os.environ.get("BUSY_HTTP_PASSWORD"),
                   help="device HTTP API password (prefer BUSY_HTTP_PASSWORD); localhost needs none")
    p.add_argument("--priority", type=int, default=20,
                   help="draw priority 1-100 (default 20, yields to notifications)")
    p.add_argument("--poll", type=float, default=180.0,
                   help="seconds between usage refreshes (default 180)")
    p.add_argument("--hold", type=float, default=HOLD,
                   help="seconds each mascot animation stays on screen (default 120)")
    p.add_argument("--once", action="store_true",
                   help="draw a single frame and exit")
    p.add_argument("--mock", action="store_true",
                   help="use fixture usage data (no credentials needed)")
    p.add_argument("--mock-usage", default="60,70", metavar="FIVE,WEEK",
                   help="fixture percentages for --mock (default 60,70)")
    p.add_argument("--mock-auth-fail", action="store_true",
                   help="render the AUTH EXPIRED frame")
    args = p.parse_args(argv)

    if not 1 <= args.priority <= 100:
        p.error("--priority must be between 1 and 100")
    if args.hold <= 0:
        p.error("--hold must be positive")
    try:
        five, week = (float(v) for v in args.mock_usage.split(","))
    except ValueError:
        p.error("--mock-usage must be two numbers, e.g. 60,70")
    args.mock_five, args.mock_week = five, week
    return args


def refresh(args, last_good):
    """Return ``(snapshot|None, last_good)`` for this poll.

    ``None`` means "render the auth-degraded frame". A transient usage failure
    falls back to the last good snapshot, which ages into the stale frame -
    a usage outage must never masquerade as an auth problem.
    """
    if args.mock_auth_fail:
        return None, last_good
    if args.mock:
        snap = mock_snapshot(time.time(), args.mock_five, args.mock_week)
        return snap, snap

    token = access_token()
    if token is None:
        return None, last_good
    try:
        snap = fetch_usage(token)
    except AuthError:
        return None, last_good
    except UsageError as exc:
        print(f"usage unavailable ({exc}), showing last known data",
              file=sys.stderr, flush=True)
        if last_good is None:
            # No good data ever: synthesise an already-stale snapshot so the
            # display says STALE rather than claiming 0% usage.
            base = datetime.now(timezone.utc)
            return UsageSnapshot(0.0, base + timedelta(hours=1), 0.0,
                                 base + timedelta(days=1),
                                 time.time() - STALE_SECONDS - 60), None
        return last_good, last_good
    return snap, snap


def main(argv=None) -> int:
    args = parse_args(argv)
    base = base_url(args.host)
    bar = BusyBar(base, args.token, args.priority)

    snap = None
    last_good = None
    act = Activity()
    last_data = 0.0
    last_activity = 0.0
    blocked = False  # currently outbid by a higher-priority app

    print(f"{APP} -> {base}  (Ctrl-C to stop)", flush=True)
    try:
        while True:
            now = time.time()
            if last_data == 0.0 or now - last_data >= args.poll:
                snap, last_good = refresh(args, last_good)
                last_data = time.time()
            if now - last_activity >= ACTIVITY_INTERVAL:
                act = read_activity(now)
                last_activity = now

            frame = animation_for(snap, now, CFG, args.hold)
            frame.working = act.working
            # The full frame goes out every tick: the emulator replaces the
            # whole element list per draw (firmware upserts by id), so a
            # mascot-only push would blank the dashboard between polls.
            elements = build_elements(snap, act, CFG, now, frame)

            status, body = bar.draw(elements)
            if status == 200:
                blocked = False
            elif status == 409:
                # A higher-priority app owns the display; keep trying quietly.
                # Under --once there is no "keep trying", so report failure:
                # nothing was drawn, and a smoke test should say so.
                if args.once:
                    sys.exit("error: display busy, another app has priority "
                             f"(raise --priority above {args.priority})")
                if not blocked:
                    print("display busy: another app has priority, waiting...",
                          flush=True)
                    blocked = True
            elif status in (401, 403):
                sys.exit("error: device rejected the token; set "
                         "BUSY_HTTP_PASSWORD or pass --token")
            elif status == 400:
                sys.exit(f"error: device rejected the frame: {body.strip()}")
            else:
                sys.exit(f"error: unexpected HTTP {status}: {body.strip()}")

            if args.once:
                return 0
            time.sleep(ANIM_INTERVAL)
    except KeyboardInterrupt:
        try:
            bar.clear()
        except DeviceError:
            pass
        print("\nstopped.")
        return 0
    except DeviceError as exc:
        sys.exit(f"error: {base} unreachable: {exc}")


if __name__ == "__main__":
    sys.exit(main())
