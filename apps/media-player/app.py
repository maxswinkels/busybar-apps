#!/usr/bin/env python3
"""Cross-platform Now Playing prototype for BUSY Bar.

macOS:  osascript/JXA + MediaRemote.framework (no Python dependencies)
Windows: PowerShell + Windows.Media.Control WinRT (experimental, no Python deps)

Examples:
    python3 app.py
    python3 app.py --host 127.0.0.1:8080
    python3 app.py --print-only
    python3 app.py --demo --host 127.0.0.1:8080

The display adapts to partial metadata. If elapsed time appears unreliable
(e.g. browsers/QuickTime reporting a fixed 0 while playing), the progress bar
is hidden rather than showing misleading progress.
"""

from __future__ import annotations

import argparse
import asyncio
import queue
import threading
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from typing import Optional

APP = "now-playing"
DEFAULT_HOST = "10.0.4.20"
POLL_INTERVAL = 1.0
PRIORITY = 30

WHITE = "#FFFFFFFF"
DIM = "#8A8A8AFF"
GREEN = "#38E56FFF"
AMBER = "#FFB020FF"
BAR_BG = "#252525FF"
BLACK = "#000000FF"
DEFAULT_TITLE_COLOR = WHITE
DEFAULT_ARTIST_COLOR = DIM
DEFAULT_TIME_COLOR = "#0080FFFF"


# ---------------------------------------------------------------------------
# Normalized model
# ---------------------------------------------------------------------------

@dataclass
class NowPlaying:
    active: bool = False
    state: str = "idle"       # playing / paused / idle / unknown
    app: Optional[str] = None
    bundle_id: Optional[str] = None
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[float] = None
    elapsed: Optional[float] = None
    playback_rate: Optional[float] = None
    # True when the backend supplies a position that is recalculated by the OS
    # on every poll (e.g. MediaRemote calculatedPlaybackPosition on macOS).
    elapsed_live: bool = False
    elapsed_reliable: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# macOS backend
# ---------------------------------------------------------------------------

MAC_JXA = r'''
ObjC.import('Foundation');

function jsValue(v) {
    if (v === undefined || v === null) return null;
    try {
        const x = ObjC.unwrap(v);
        if (x === undefined || x === null) return null;
        return x;
    } catch (e) {
        try { return String(v); } catch (_) { return null; }
    }
}

function numberValue(v) {
    const x = jsValue(v);
    if (x === null) return null;
    const n = Number(x);
    return Number.isFinite(n) ? n : null;
}

function run() {
    const bundle = $.NSBundle.bundleWithPath('/System/Library/PrivateFrameworks/MediaRemote.framework');
    if (!bundle || !bundle.load) {
        return JSON.stringify({ok:false, error:'Could not load MediaRemote.framework'});
    }

    const Request = $.NSClassFromString('MRNowPlayingRequest');
    if (!Request) {
        return JSON.stringify({ok:false, error:'MRNowPlayingRequest class not found'});
    }

    let playerPath = null;
    let item = null;
    let info = null;
    try { playerPath = Request.localNowPlayingPlayerPath; } catch (_) {}
    try { item = Request.localNowPlayingItem; } catch (_) {}
    try { if (item) info = item.nowPlayingInfo; } catch (_) {}

    let app = null;
    let bundleId = null;
    try {
        if (playerPath && playerPath.client) {
            app = jsValue(playerPath.client.displayName);
            bundleId = jsValue(playerPath.client.bundleIdentifier);
        }
    } catch (_) {}

    if (!info) {
        return JSON.stringify({ok:true, active:false, app:app, bundle_id:bundleId,
            title:null, artist:null, album:null, duration:null, elapsed:null,
            playback_rate:null, elapsed_live:false, state:'idle'});
    }

    function get(key) {
        try { return info.valueForKey(key); } catch (_) { return null; }
    }

    const rate = numberValue(get('kMRMediaRemoteNowPlayingInfoPlaybackRate'));
    const title = jsValue(get('kMRMediaRemoteNowPlayingInfoTitle'));
    const artist = jsValue(get('kMRMediaRemoteNowPlayingInfoArtist'));
    const album = jsValue(get('kMRMediaRemoteNowPlayingInfoAlbum'));
    const duration = numberValue(get('kMRMediaRemoteNowPlayingInfoDuration'));

    // Prefer MediaRemote's calculated playback position. Unlike the raw
    // NowPlayingInfo elapsed field, this keeps advancing while playback is
    // already in progress and is available immediately when this app starts.
    let calculatedElapsed = null;
    try {
        if (item && item.metadata) {
            calculatedElapsed = numberValue(item.metadata.calculatedPlaybackPosition);
        }
    } catch (_) {}
    const rawElapsed = numberValue(get('kMRMediaRemoteNowPlayingInfoElapsedTime'));
    const elapsed = calculatedElapsed !== null ? calculatedElapsed : rawElapsed;

    let state = 'unknown';
    if (rate !== null) state = rate > 0 ? 'playing' : 'paused';

    return JSON.stringify({ok:true, active:!!(title || artist || album || app),
        app:app, bundle_id:bundleId, title:title, artist:artist, album:album,
        duration:duration, elapsed:elapsed, playback_rate:rate,
        elapsed_live:(calculatedElapsed !== null), state:state});
}
'''


def read_macos() -> NowPlaying:
    try:
        p = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", MAC_JXA],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        return NowPlaying(error=f"macOS backend: {exc}")

    if p.returncode != 0:
        return NowPlaying(error=(p.stderr or p.stdout).strip() or "osascript failed")
    try:
        d = json.loads(p.stdout)
    except Exception as exc:
        return NowPlaying(error=f"invalid macOS backend JSON: {exc}")

    if not d.get("ok", True):
        return NowPlaying(error=d.get("error") or "macOS backend failed")
    return _from_dict(d)



# ---------------------------------------------------------------------------
# OS-global media controls
# ---------------------------------------------------------------------------

MAC_CONTROL_JXA = r'''
ObjC.import('Foundation');

function run(argv) {
    const command = Number(argv[0]);
    const bundle = $.NSBundle.bundleWithPath('/System/Library/PrivateFrameworks/MediaRemote.framework/');
    if (!bundle || !bundle.load) return 'Could not load MediaRemote.framework';

    const Controller = $.NSClassFromString('MRNowPlayingController');
    if (!Controller) return 'MRNowPlayingController class not found';

    const controller = Controller.localRouteController;
    if (!controller) return 'localRouteController unavailable';

    const options = $.NSDictionary.alloc.init;
    controller.sendCommandOptionsCompletion(command, options, null);

    // MediaRemote dispatches commands asynchronously. Keep the JXA process
    // alive briefly so the command reaches the active media client before
    // osascript exits. Without this, the first command may work while later
    // commands are silently dropped.
    delay(0.35);
    return 'ok';
}
'''

WINDOWS_CONTROL_PS = r'''
param([string]$Command)
$ErrorActionPreference = 'Stop'
function Wait-WinRT($op) {
    while ($op.Status -eq 0) { Start-Sleep -Milliseconds 10 }
    if ($op.Status -eq 1) { return $op.GetResults() }
    throw "WinRT async operation failed with status $($op.Status)"
}
[void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$mgr = Wait-WinRT ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync())
$session = $mgr.GetCurrentSession()
if ($null -eq $session) { throw 'No active media session' }
switch ($Command) {
    'toggle'   { [void](Wait-WinRT ($session.TryTogglePlayPauseAsync())) }
    'next'     { [void](Wait-WinRT ($session.TrySkipNextAsync())) }
    'previous' { [void](Wait-WinRT ($session.TrySkipPreviousAsync())) }
    default    { throw "Unknown command: $Command" }
}
'''


def media_command(command: str):
    if sys.platform == "darwin":
        ids = {"toggle": 2, "next": 4, "previous": 5}
        cmd_id = ids.get(command)
        if cmd_id is None:
            return False, f"unsupported command: {command}"
        try:
            p = subprocess.run(
                ["osascript", "-l", "JavaScript", "-e", MAC_CONTROL_JXA, str(cmd_id)],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:
            return False, str(exc)
        detail = (p.stderr or p.stdout).strip()
        return p.returncode == 0 and (not detail or detail == "ok"), detail or "ok"

    if sys.platform == "win32":
        try:
            p = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", WINDOWS_CONTROL_PS,
                 "-Command", command],
                capture_output=True, text=True, timeout=8,
            )
        except Exception as exc:
            return False, str(exc)
        detail = (p.stderr or p.stdout).strip()
        return p.returncode == 0, detail or "ok"

    return False, f"media controls unsupported on {sys.platform}"


# ---------------------------------------------------------------------------
# Minimal BUSY Bar input WebSocket client
# ---------------------------------------------------------------------------
# Firmware /api/status/ws frames are protobuf.  We only decode the fields needed
# here, avoiding busylib (and therefore zeroconf/pydantic/etc.). Wire layout:
# State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1 or
# InputEvent.encoder_event=3.  Button START enum=2; action PRESS=0/RELEASE=1.


def _read_varint(buf: bytes, pos: int):
    value = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7f) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


def _iter_proto_fields(buf: bytes):
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field_no, wire, value
        elif wire == 1:
            if pos + 8 > len(buf):
                return
            yield field_no, wire, buf[pos:pos + 8]
            pos += 8
        elif wire == 2:
            n, pos = _read_varint(buf, pos)
            end = pos + n
            if end > len(buf):
                return
            yield field_no, wire, buf[pos:end]
            pos = end
        elif wire == 5:
            if pos + 4 > len(buf):
                return
            yield field_no, wire, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _zigzag32(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def _decode_input_event(buf: bytes):
    for field_no, wire, value in _iter_proto_fields(buf):
        if wire != 2:
            continue
        if field_no == 1:  # button_event
            button = 0
            action = 0
            for f, w, v in _iter_proto_fields(value):
                if w == 0 and f == 1:
                    button = int(v)
                elif w == 0 and f == 2:
                    action = int(v)
            return {"button_event": {"button": button, "action": action}}
        if field_no == 3:  # encoder_event
            raw_delta = 0
            for f, w, v in _iter_proto_fields(value):
                if w == 0 and f == 1:
                    raw_delta = int(v)
            return {"encoder_event": {"delta": _zigzag32(raw_delta)}}
    return None


def _decode_state_inputs(frame: bytes):
    events = []
    for field_no, wire, update in _iter_proto_fields(frame):
        if field_no != 2 or wire != 2:  # State.updates
            continue
        for uf, uw, uv in _iter_proto_fields(update):
            if uf == 11 and uw == 2:  # StateUpdate.input
                event = _decode_input_event(uv)
                if event:
                    events.append(event)
    return events


def _ws_url(host: str, token: Optional[str] = None) -> str:
    raw = host.rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/api/status/ws"
    query = parsed.query
    if token:
        token_q = urllib.parse.urlencode({"x-api-token": token})
        query = token_q if not query else query + "&" + token_q
    return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", query, ""))


class InputListener:
    def __init__(self, address: str, token: Optional[str] = None):
        self._address = address
        self._token = token
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self.available = True
        self.error = None

    def start(self):
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            self.available = False
            self.error = f"websockets not installed: {exc}"
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def poll(self):
        events = []
        try:
            while True:
                events.append(self._queue.get_nowait())
        except queue.Empty:
            return events

    def _run(self):
        try:
            asyncio.run(self._listen())
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            print(f"controls: BUSY Bar input unavailable: {exc}")

    async def _listen(self):
        import websockets
        url = _ws_url(self._address, self._token)
        async with websockets.connect(
            url, max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=20
        ) as ws:
            await ws.send(json.dumps({"enable": True}))
            async for message in ws:
                if self._stop.is_set():
                    break
                if isinstance(message, str):
                    continue
                try:
                    for event in _decode_state_inputs(bytes(message)):
                        self._queue.put(event)
                except Exception as exc:
                    print(f"controls: ignored malformed status frame: {exc}")


def process_control_events(listener: InputListener, last_wheel_at: float, cooldown: float, invert_dial: bool = False):
    for event in listener.poll():
        now = time.monotonic()
        if "button_event" in event:
            be = event["button_event"]
            # START=2, PRESS=0.  Ignore RELEASE so one physical click = one toggle.
            if be.get("button") == 2 and be.get("action") == 0:
                ok, detail = media_command("toggle")
                print("control: PLAY/PAUSE" + ("" if ok else f" failed: {detail}"))
        elif "encoder_event" in event:
            delta = int(event["encoder_event"].get("delta", 0) or 0)
            if delta and now - last_wheel_at >= cooldown:
                # Default mapping follows the physical direction seen from the
                # front of the BUSY Bar. --invert-dial swaps Next/Previous.
                forward = delta < 0
                if invert_dial:
                    forward = not forward
                command = "next" if forward else "previous"
                ok, detail = media_command(command)
                label = "NEXT" if forward else "PREVIOUS"
                print(f"control: {label}" + ("" if ok else f" failed: {detail}"))
                last_wheel_at = now
    return last_wheel_at

# ---------------------------------------------------------------------------
# Windows backend (experimental)
# ---------------------------------------------------------------------------

WINDOWS_PS = r'''
$ErrorActionPreference = 'Stop'

function Wait-WinRT($op) {
    while ($op.Status -eq 0) { Start-Sleep -Milliseconds 10 }
    if ($op.Status -eq 1) { return $op.GetResults() }
    throw "WinRT async operation failed with status $($op.Status)"
}

[void][Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType=WindowsRuntime]
$mgr = Wait-WinRT ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync())
$session = $mgr.GetCurrentSession()
if ($null -eq $session) {
    @{ ok=$true; active=$false; state='idle' } | ConvertTo-Json -Compress
    exit 0
}

$props = Wait-WinRT ($session.TryGetMediaPropertiesAsync())
$playback = $session.GetPlaybackInfo()
$timeline = $session.GetTimelineProperties()

$state = switch ([int]$playback.PlaybackStatus) {
    4 { 'playing' }
    5 { 'paused' }
    3 { 'paused' }
    default { 'unknown' }
}

$duration = $null
$elapsed = $null
if ($null -ne $timeline) {
    try { $duration = $timeline.EndTime.TotalSeconds } catch {}
    try { $elapsed = $timeline.Position.TotalSeconds } catch {}
}

@{
    ok = $true
    active = $true
    state = $state
    app = $session.SourceAppUserModelId
    bundle_id = $session.SourceAppUserModelId
    title = if ($props.Title) { [string]$props.Title } else { $null }
    artist = if ($props.Artist) { [string]$props.Artist } else { $null }
    album = if ($props.AlbumTitle) { [string]$props.AlbumTitle } else { $null }
    duration = $duration
    elapsed = $elapsed
    elapsed_live = ($null -ne $elapsed)
    playback_rate = $null
} | ConvertTo-Json -Compress
'''


def read_windows() -> NowPlaying:
    shell = "powershell.exe"
    try:
        p = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", WINDOWS_PS],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as exc:
        return NowPlaying(error=f"Windows backend: {exc}")

    if p.returncode != 0:
        return NowPlaying(error=(p.stderr or p.stdout).strip() or "PowerShell backend failed")
    try:
        d = json.loads(p.stdout.strip())
    except Exception as exc:
        return NowPlaying(error=f"invalid Windows backend JSON: {exc}; output={p.stdout!r}")
    return _from_dict(d)


def _from_dict(d: dict) -> NowPlaying:
    def num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return NowPlaying(
        active=bool(d.get("active")),
        state=str(d.get("state") or "unknown"),
        app=_clean(d.get("app")),
        bundle_id=_clean(d.get("bundle_id")),
        title=_clean(d.get("title")),
        artist=_clean(d.get("artist")),
        album=_clean(d.get("album")),
        duration=num(d.get("duration")),
        elapsed=num(d.get("elapsed")),
        playback_rate=num(d.get("playback_rate")),
    )


def _clean(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------------------
# Reliability tracking
# ---------------------------------------------------------------------------

class ElapsedTracker:
    """Normalize elapsed time and keep it moving between OS updates.

    Some players publish elapsed only on seek/pause/state changes rather than on
    every query.  When we have a trustworthy non-zero position, keep a local
    monotonic anchor and extrapolate while playing.  A fresh zero is considered
    tentative until the source itself advances, which avoids inventing progress
    for QuickTime/browser sessions that expose a permanently-zero position.
    """

    ZERO_GRACE_S = 3.0

    def __init__(self):
        self.identity = None
        self.raw_elapsed = None
        self.anchor_elapsed = None
        self.anchor_wall = None
        self.first_valid_wall = None
        self.source_advanced = False
        self.reliable_bundles = set()

    def _reset(self, ident, now):
        self.identity = ident
        self.raw_elapsed = None
        self.anchor_elapsed = None
        self.anchor_wall = None
        self.first_valid_wall = now
        self.source_advanced = False

    def update(self, np: NowPlaying) -> NowPlaying:
        ident = (np.bundle_id, np.title, np.duration)
        now = time.monotonic()
        if ident != self.identity:
            self._reset(ident, now)

        e = np.elapsed
        d = np.duration
        valid = e is not None and d is not None and d > 0 and 0 <= e <= d + 5

        if not valid:
            # Some players temporarily stop publishing elapsed at pause/end of
            # track while duration/metadata remain available. Preserve the last
            # trustworthy timeline instead of collapsing "2:30/2:31" to "2:31"
            # and hiding the progress bar.
            if (
                d is not None and d > 0
                and self.source_advanced
                and self.anchor_elapsed is not None
                and np.active
            ):
                held = self.anchor_elapsed
                if np.state == "playing" and self.anchor_wall is not None and not np.elapsed_live:
                    rate = np.playback_rate if np.playback_rate and np.playback_rate > 0 else 1.0
                    held += (now - self.anchor_wall) * rate
                np.elapsed = max(0.0, min(d, held))
                np.elapsed_reliable = True
                return np
            np.elapsed_reliable = False
            return np

        # A non-zero position is immediately useful. If a bundle has already
        # demonstrated a working timeline on an earlier track, trust a fresh
        # 0:00 on the next track and start the local monotonic clock immediately.
        # This fixes players such as VLC, which can publish new metadata at 0:00
        # and then stop updating elapsed until pause/resume. Browsers/QuickTime
        # that have never demonstrated a real timeline still use the zero grace.
        bundle_known_reliable = bool(np.bundle_id and np.bundle_id in self.reliable_bundles)
        if e > 0.5 or (bundle_known_reliable and np.state == "playing"):
            self.source_advanced = True

        if self.raw_elapsed is not None and e > self.raw_elapsed + 0.25:
            self.source_advanced = True

        raw_changed = (
            self.raw_elapsed is None
            or abs(e - self.raw_elapsed) > 0.25
        )

        if self.anchor_elapsed is None or raw_changed:
            self.anchor_elapsed = e
            self.anchor_wall = now

        self.raw_elapsed = e

        # For a source stuck at zero, wait briefly for real evidence of movement.
        # This keeps QuickTime/Chromium-style zero placeholders from becoming a
        # fabricated timeline.  Non-zero sources (e.g. VLC) are reliable from
        # the very first poll, so the bar appears immediately.
        if not self.source_advanced:
            if e <= 0.5 and now - self.first_valid_wall >= self.ZERO_GRACE_S:
                np.elapsed_reliable = False
                return np
            np.elapsed_reliable = False
            return np

        # A live OS-calculated position is authoritative. Do not extrapolate it:
        # when playback is paused the value stops, even if a player leaves a
        # stale playback-rate flag set to 1. This fixes the counter continuing
        # to run after a BUSY Bar Play/Pause command.
        if np.elapsed_live:
            predicted = e
            self.anchor_elapsed = e
            self.anchor_wall = now
        else:
            predicted = self.anchor_elapsed
            if np.state == "playing" and self.anchor_wall is not None:
                rate = np.playback_rate if np.playback_rate and np.playback_rate > 0 else 1.0
                predicted += (now - self.anchor_wall) * rate

        np.elapsed = max(0.0, min(d, predicted))
        np.elapsed_reliable = True
        if np.bundle_id:
            self.reliable_bundles.add(np.bundle_id)
        return np


# ---------------------------------------------------------------------------
# BUSY Bar API
# ---------------------------------------------------------------------------

def _base(host: str) -> str:
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _request(host, method, path, body=None, content_type="application/json"):
    req = urllib.request.Request(
        _base(host) + path,
        data=body,
        method=method,
        headers={"Content-Type": content_type} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def draw(host, elements):
    body = json.dumps({
        "application_name": APP,
        "priority": PRIORITY,
        "elements": elements,
    }).encode("utf-8")
    return _request(host, "POST", "/api/display/draw", body)


def clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    return _request(host, "DELETE", "/api/display/draw?" + qs)[0]


def text(el_id, txt, x, y, *, font="small", color=WHITE, align=None,
         width=None, scroll_rate=None):
    el = {
        "id": el_id, "type": "text", "text": str(txt), "x": x, "y": y,
        "font": font, "color": color,
    }
    if align is not None:
        el["align"] = align
    if width is not None:
        el["width"] = width
    if scroll_rate is not None:
        el["scroll_rate"] = scroll_rate
        el["scroll_start_delay"] = 900
        el["scroll_repeat_delay"] = 1800
    return el


def rect(el_id, x, y, w, h, color):
    return {
        "id": el_id, "type": "rectangle", "x": x, "y": y,
        "width": w, "height": h, "border_width": 0,
        "fill": "solid", "fill_colors": [color],
    }


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def fmt_time(v: Optional[float]) -> str:
    if v is None:
        return "--:--"
    total = max(0, int(v))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _status_glyph(state):
    return "PLAY" if state == "playing" else "PAUSE" if state == "paused" else ""


def _source_label(np: NowPlaying) -> str:
    if np.app:
        # Windows SourceAppUserModelId can be very long; keep a readable tail.
        if len(np.app) > 28 and ("!" in np.app or "." in np.app):
            return np.app.split("!")[-1].split(".")[-1]
        return np.app
    return "Now Playing"


def _time_counter(np: NowPlaying, separator: str = "/") -> Optional[str]:
    if not np.duration:
        return None
    if np.elapsed_reliable and np.elapsed is not None:
        return f"{fmt_time(np.elapsed)}{separator}{fmt_time(np.duration)}"
    return fmt_time(np.duration)


def _layout_parts(np: NowPlaying, title_color=DEFAULT_TITLE_COLOR, artist_color=DEFAULT_ARTIST_COLOR, time_separator="/"):
    """Return the stable text/layout state for the current Now Playing item.

    This deliberately excludes the elapsed *value*: line1/line2 must not be
    redrawn every second, otherwise the BUSY Bar restarts their hardware text
    scrolling animation.  The counter width is included because a transition
    such as 9:59 -> 10:00 can legitimately change the space available to line2.
    """
    if np.error:
        return {
            "mode": "error", "line1": "NOW PLAYING ERROR", "line2": np.error,
            "line1_color": AMBER, "line2_color": DIM, "left_width": 72,
            "counter_width": 0,
        }

    if not np.active:
        return {
            "mode": "idle", "line1": "NOW PLAYING", "line2": "Waiting for media",
            "line1_color": DIM, "line2_color": DIM, "left_width": 72,
            "counter_width": 0,
        }

    title = np.title
    artist = np.artist
    source = _source_label(np)
    glyph = _status_glyph(np.state)
    counter = _time_counter(np, time_separator)

    line1 = title or f"{glyph} {source}"
    if title and artist:
        line2 = artist
    elif title:
        line2 = f"{glyph} {source}"
    else:
        line2 = "Playing" if np.state == "playing" else "Paused"

    # The BUSY Bar small font advances by about 3 px per character.
    # Reserve the counter width plus an explicit 6 px visual gap so the
    # artist/source text never overlaps the right-aligned time counter.
    counter_width = (len(counter) * 3) if counter else 0
    left_width = max(12, 72 - counter_width - (6 if counter else 0))
    return {
        "mode": "active", "line1": line1, "line2": line2,
        "line1_color": title_color,
        "line2_color": artist_color,
        "left_width": left_width, "counter_width": counter_width,
    }


def build_static_elements(np: NowPlaying, title_color=DEFAULT_TITLE_COLOR, artist_color=DEFAULT_ARTIST_COLOR, time_separator="/"):
    """Elements that should only be resent when text/layout actually changes."""
    lp = _layout_parts(np, title_color, artist_color, time_separator)
    if lp["mode"] == "error":
        return [
            text("line1", lp["line1"], 0, 0, color=lp["line1_color"]),
            text("line2", lp["line2"], 0, 7, color=lp["line2_color"],
                 width=72, scroll_rate=540),
        ]
    if lp["mode"] == "idle":
        return [
            text("line1", lp["line1"], 36, 0, align="top_mid", color=lp["line1_color"]),
            text("line2", lp["line2"], 36, 7, align="top_mid", color=lp["line2_color"]),
        ]
    return [
        text("line1", lp["line1"], 0, 0, width=72, scroll_rate=480,
             color=lp["line1_color"]),
        text("line2", lp["line2"], 0, 7, width=lp["left_width"], scroll_rate=480,
             color=lp["line2_color"]),
    ]


def build_dynamic_elements(np: NowPlaying, time_color=DEFAULT_TIME_COLOR, time_separator="/"):
    """Counter + timeline, safe to update every second without touching scroll text."""
    if np.error or not np.active:
        return [
            text("time", "", 72, 7, align="top_right", color=BLACK),
            rect("prog_bg", 0, 15, 72, 1, BLACK),
            rect("prog", 0, 15, 1, 1, BLACK),
        ]

    counter = _time_counter(np, time_separator)
    elements = [
        text("time", counter or "", 72, 7, align="top_right",
             color=time_color if counter else BLACK),
    ]

    if np.elapsed_reliable and np.duration and np.elapsed is not None:
        frac = max(0.0, min(1.0, np.elapsed / np.duration))
        w = max(1, min(72, int(round(frac * 72))))
        elements += [
            rect("prog_bg", 0, 15, 72, 1, BAR_BG),
            rect("prog", 0, 15, w, 1, GREEN if np.state == "playing" else AMBER),
        ]
    else:
        elements += [
            rect("prog_bg", 0, 15, 72, 1, BLACK),
            rect("prog", 0, 15, 1, 1, BLACK),
        ]
    return elements


def static_render_key(np: NowPlaying, time_separator="/"):
    """Anything here changing is allowed to restart text scrolling."""
    lp = _layout_parts(np, time_separator=time_separator)
    return (
        lp["mode"], lp["line1"], lp["line2"], lp["line1_color"],
        lp["line2_color"], lp["left_width"], lp["counter_width"],
    )


def dynamic_render_key(np: NowPlaying):
    return (
        np.active, np.state,
        _time_counter(np),
        round(np.duration or 0),
        round(np.elapsed or 0) if np.elapsed_reliable else None,
        np.elapsed_reliable, bool(np.error),
    )


# ---------------------------------------------------------------------------
# Demo / console
# ---------------------------------------------------------------------------

def demo_frame(t: float) -> NowPlaying:
    duration = 313.0
    elapsed = t % duration
    return NowPlaying(
        active=True, state="playing", app="VLC", bundle_id="org.videolan.vlc",
        title="AnatomiaMaster — cross-platform Now Playing prototype",
        artist="BUSY Bar Demo", duration=duration, elapsed=elapsed,
        playback_rate=1.0, elapsed_live=True, elapsed_reliable=True,
    )


def print_info(np: NowPlaying):
    if np.error:
        print("Error:", np.error)
        return
    print(f"State:    {np.state}")
    print(f"App:      {np.app or '--'}")
    print(f"Title:    {np.title or '--'}")
    print(f"Artist:   {np.artist or '--'}")
    print(f"Album:    {np.album or '--'}")
    print(f"Duration: {fmt_time(np.duration)}")
    print(f"Elapsed:  {fmt_time(np.elapsed)}")
    print(f"Reliable: {'yes' if np.elapsed_reliable else 'no'}")


def choose_backend():
    if sys.platform == "darwin":
        return read_macos, "macOS/MediaRemote"
    if sys.platform == "win32":
        return read_windows, "Windows/SMTC"
    return lambda: NowPlaying(error=f"unsupported platform: {sys.platform}"), "unsupported"


def main() -> int:
    p = argparse.ArgumentParser(description="Cross-platform Now Playing for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST, help="BUSY Bar host (default: 10.0.4.20)")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL, help="poll interval seconds (default: 1)")
    p.add_argument("--print-only", action="store_true", help="print normalized metadata; do not contact BUSY Bar")
    p.add_argument("--json", action="store_true", help="with --print-only, emit normalized JSON")
    p.add_argument("--demo", action="store_true", help="use synthetic metadata instead of OS Now Playing")
    p.add_argument("--once", action="store_true", help="poll/draw once and exit")
    p.add_argument("--token", default=None, help="BUSY Bar Wi-Fi access token/PIN, if required")
    p.add_argument("--no-controls", action="store_true", help="disable START/wheel media controls")
    p.add_argument("--title-color", default=DEFAULT_TITLE_COLOR, help="title color as #RRGGBB or #RRGGBBAA (default: white)")
    p.add_argument("--artist-color", default=DEFAULT_ARTIST_COLOR, help="artist/source color as #RRGGBB or #RRGGBBAA (default: gray)")
    p.add_argument("--time-color", default=DEFAULT_TIME_COLOR, help="time counter color as #RRGGBB or #RRGGBBAA (default: blue)")
    p.add_argument("--time-separator", default="/", help="separator between elapsed and total time (default: /)")
    p.add_argument("--wheel-cooldown", type=float, default=0.40, help="seconds between wheel track changes (default: 0.40)")
    p.add_argument("--invert-dial", action="store_true", help="invert rotary encoder direction for Previous/Next")
    args = p.parse_args()

    def normalize_color(value: str, option: str) -> str:
        value = value.strip()
        if not value.startswith("#"):
            value = "#" + value
        if len(value) == 7:
            value += "FF"
        if len(value) != 9 or any(c not in "0123456789abcdefABCDEF" for c in value[1:]):
            p.error(f"{option} must be #RRGGBB or #RRGGBBAA")
        return value.upper()

    args.title_color = normalize_color(args.title_color, "--title-color")
    args.artist_color = normalize_color(args.artist_color, "--artist-color")
    args.time_color = normalize_color(args.time_color, "--time-color")
    if not args.time_separator or len(args.time_separator) > 3 or any(c in "\r\n\t" for c in args.time_separator):
        p.error("--time-separator must be 1 to 3 printable characters")

    backend, backend_name = choose_backend()
    tracker = ElapsedTracker()
    print(f"{APP}: backend={backend_name}" + (" [DEMO]" if args.demo else ""))
    if not args.print_only:
        print(f"BUSY Bar -> {_base(args.host)}  (Ctrl-C to stop)")

    last_static_key = None
    last_dynamic_key = None
    demo_start = time.monotonic()
    controls = None
    last_wheel_at = 0.0
    if not args.print_only and not args.no_controls and not args.once:
        controls = InputListener(args.host, args.token)
        controls.start()
        if controls.available:
            print("controls: START=play/pause  wheel=previous/next (direct WebSocket)" + (" [inverted]" if args.invert_dial else ""))
        else:
            print(f"controls: unavailable ({controls.error or 'unknown error'})")

    try:
        while True:
            if controls is not None:
                last_wheel_at = process_control_events(
                    controls, last_wheel_at, max(0.0, args.wheel_cooldown), args.invert_dial
                )
            if args.demo:
                np = demo_frame(time.monotonic() - demo_start)
            else:
                np = tracker.update(backend())

            if args.print_only:
                if args.json:
                    print(json.dumps(asdict(np), ensure_ascii=False, indent=2))
                else:
                    print_info(np)
                if args.once or not args.demo:
                    return 0 if not np.error else 1
            else:
                # Keep scrolling text and the once-per-second timeline in separate
                # draw requests. Re-sending a scrolling text element resets the BUSY
                # Bar's hardware scroll animation, even when its text is unchanged.
                skey = static_render_key(np, args.time_separator)
                dkey = dynamic_render_key(np)

                if skey != last_static_key:
                    status, response_body = draw(args.host, build_static_elements(np, args.title_color, args.artist_color, args.time_separator))
                    if status == 409:
                        print("display busy (409); will retry")
                    elif status not in (200, 201, 204):
                        detail = response_body.decode("utf-8", "ignore").strip()
                        print(f"static draw returned HTTP {status}: {detail or '<empty response>'}")
                    else:
                        last_static_key = skey

                if dkey != last_dynamic_key:
                    status, response_body = draw(args.host, build_dynamic_elements(np, args.time_color, args.time_separator))
                    if status == 409:
                        print("display busy (409); will retry")
                    elif status not in (200, 201, 204):
                        detail = response_body.decode("utf-8", "ignore").strip()
                        print(f"dynamic draw returned HTTP {status}: {detail or '<empty response>'}")
                    else:
                        last_dynamic_key = dkey

                if args.once:
                    return 0

            time.sleep(max(0.25, args.interval))

    except KeyboardInterrupt:
        print("\nstopped.")
        return 0
    finally:
        if controls is not None:
            controls.stop()
        if not args.print_only:
            try:
                clear(args.host)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
