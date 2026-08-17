#!/usr/bin/env python3
"""media-player-karaoke: synchronized lyrics for BUSY Bar (macOS).

Reads macOS Now Playing via MediaRemote/JXA, looks up synced lyrics on LRCLIB,
and renders a 72x16 karaoke line with a bouncing ball over the current word.

Examples:
  python3 media_player_karaoke_v8.py
  python3 media_player_karaoke_v8.py --host 127.0.0.1:8080
  python3 media_player_karaoke_v8.py --demo
  python3 media_player_karaoke_v8.py --debug

The bouncing-ball word timing is estimated inside each LRC line because normal
LRC files provide line timestamps, not word timestamps. The line boundaries
remain synchronized to the player; only the intra-line word timing is inferred.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "media-player-karaoke"
W, H = 72, 16
DEFAULT_HOST = "10.0.4.20"
DEFAULT_FPS = 15
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
USER_AGENT = "busybar-media-player-karaoke/0.8 (https://github.com/busybar)"

BLACK = (0, 0, 0)
WHITE = (240, 240, 240)
DIM = (76, 76, 76)
YELLOW = (255, 220, 35)
CYAN = (40, 210, 255)
BLUE = (35, 120, 255)
RED = (255, 70, 70)
GREEN = (60, 220, 100)

# Compact 3x5-ish font. Rows are bit masks, MSB at the left.
FONT = {
    " ": (2, [0,0,0,0,0]), "!": (1,[1,1,1,0,1]), "'": (1,[1,1,0,0,0]),
    "(": (2,[1,2,2,2,1]), ")": (2,[2,1,1,1,2]), ",": (1,[0,0,0,1,1]),
    "-": (3,[0,0,7,0,0]), ".": (1,[0,0,0,0,1]), "/": (3,[1,1,2,4,4]),
    ":": (1,[0,1,0,1,0]), "?": (3,[6,1,2,0,2]),
    "0": (3,[7,5,5,5,7]), "1": (3,[2,6,2,2,7]), "2": (3,[6,1,7,4,7]),
    "3": (3,[6,1,3,1,6]), "4": (3,[5,5,7,1,1]), "5": (3,[7,4,6,1,6]),
    "6": (3,[3,4,7,5,7]), "7": (3,[7,1,2,2,2]), "8": (3,[7,5,7,5,7]),
    "9": (3,[7,5,7,1,6]),
    "A": (3,[2,5,7,5,5]), "B": (3,[6,5,6,5,6]), "C": (3,[3,4,4,4,3]),
    "D": (3,[6,5,5,5,6]), "E": (3,[7,4,6,4,7]), "F": (3,[7,4,6,4,4]),
    "G": (3,[3,4,5,5,3]), "H": (3,[5,5,7,5,5]), "I": (3,[7,2,2,2,7]),
    "J": (3,[1,1,1,5,2]), "K": (3,[5,5,6,5,5]), "L": (3,[4,4,4,4,7]),
    "M": (5,[17,27,21,17,17]), "N": (4,[9,13,11,9,9]), "O": (3,[2,5,5,5,2]),
    "P": (3,[6,5,6,4,4]), "Q": (4,[6,9,9,11,7]), "R": (3,[6,5,6,5,5]),
    "S": (3,[3,4,2,1,6]), "T": (3,[7,2,2,2,2]), "U": (3,[5,5,5,5,7]),
    "V": (3,[5,5,5,5,2]), "W": (5,[17,17,21,21,10]), "X": (3,[5,5,2,5,5]),
    "Y": (3,[5,5,2,2,2]), "Z": (3,[7,1,2,4,7]),
}

MAC_JXA = r'''
ObjC.import('Foundation');
function js(v) {
    try { if (v === null || v === undefined) return null; return v.js; }
    catch (_) { return null; }
}
function numberValue(v) {
    let x = js(v);
    if (x === null || x === undefined) return null;
    let n = Number(x);
    return Number.isFinite(n) ? n : null;
}
function run() {
    const MediaRemote = $.NSBundle.bundleWithPath('/System/Library/PrivateFrameworks/MediaRemote.framework/');
    MediaRemote.load;
    const MRNowPlayingRequest = $.NSClassFromString('MRNowPlayingRequest');
    if (!MRNowPlayingRequest) return JSON.stringify({});
    const item = MRNowPlayingRequest.localNowPlayingItem;
    if (!item) return JSON.stringify({});
    const info = item.nowPlayingInfo;
    if (!info) return JSON.stringify({});
    function get(k) { try { return info.valueForKey(k); } catch (_) { return null; } }

    const title = js(get('kMRMediaRemoteNowPlayingInfoTitle'));
    const artist = js(get('kMRMediaRemoteNowPlayingInfoArtist'));
    const album = js(get('kMRMediaRemoteNowPlayingInfoAlbum'));
    const duration = numberValue(get('kMRMediaRemoteNowPlayingInfoDuration'));
    const rate = numberValue(get('kMRMediaRemoteNowPlayingInfoPlaybackRate'));
    let calculatedElapsed = null;
    try {
        if (item.metadata) calculatedElapsed = numberValue(item.metadata.calculatedPlaybackPosition);
    } catch (_) {}
    const rawElapsed = numberValue(get('kMRMediaRemoteNowPlayingInfoElapsedTime'));
    const useCalculatedElapsed = rate !== null && rate > 0 && calculatedElapsed !== null;
    const elapsed = useCalculatedElapsed ? calculatedElapsed : rawElapsed;
    let app = null;
    try { app = js(MRNowPlayingRequest.localNowPlayingPlayerPath.client.displayName); } catch (_) {}
    return JSON.stringify({title, artist, album, duration, elapsed, rate, app});
}
'''

# OS-global media controls, mirrored from the official BUSY Bar media-player.
MAC_CONTROL_JXA = r'''ObjC.import('Foundation');

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
    delay(0.35);
    return 'ok';
}
'''


def media_command(command: str):
    if sys.platform != "darwin":
        return False, f"media controls unsupported on {sys.platform}"
    ids = {"toggle": 2, "next": 4, "previous": 5}
    cmd_id = ids.get(command)
    if cmd_id is None:
        return False, f"unsupported command: {command}"
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", MAC_CONTROL_JXA, str(cmd_id)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        return False, str(exc)
    detail = (proc.stderr or proc.stdout).strip()
    return proc.returncode == 0 and (not detail or detail == "ok"), detail or "ok"


# Minimal BUSY Bar input WebSocket client. Firmware status frames are protobuf.
def _read_varint(buf: bytes, pos: int):
    value = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
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
        if field_no == 1:
            button = 0
            action = 0
            for f, w, v in _iter_proto_fields(value):
                if w == 0 and f == 1:
                    button = int(v)
                elif w == 0 and f == 2:
                    action = int(v)
            return {"button_event": {"button": button, "action": action}}
        if field_no == 3:
            raw_delta = 0
            for f, w, v in _iter_proto_fields(value):
                if w == 0 and f == 1:
                    raw_delta = int(v)
            return {"encoder_event": {"delta": _zigzag32(raw_delta)}}
    return None


def _decode_state_inputs(frame: bytes):
    events = []
    for field_no, wire, update in _iter_proto_fields(frame):
        if field_no != 2 or wire != 2:
            continue
        for uf, uw, uv in _iter_proto_fields(update):
            if uf == 11 and uw == 2:
                event = _decode_input_event(uv)
                if event:
                    events.append(event)
    return events


def _ws_url(host: str, token=None) -> str:
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
    def __init__(self, address: str, token=None):
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
            asyncio.run(self._listen_forever())
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            print(f"controls: listener stopped: {exc}")

    async def _listen_forever(self):
        import websockets
        url = _ws_url(self._address, self._token)
        backoff = 0.5
        max_backoff = 3.0
        connected_once = False
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    open_timeout=8,
                ) as ws:
                    await ws.send(json.dumps({"enable": True}))
                    if connected_once:
                        print("controls: BUSY Bar input reconnected")
                    connected_once = True
                    self.available = True
                    self.error = None
                    backoff = 0.5
                    async for message in ws:
                        if self._stop.is_set():
                            return
                        if isinstance(message, str):
                            continue
                        try:
                            for event in _decode_state_inputs(bytes(message)):
                                self._queue.put(event)
                        except Exception as exc:
                            print(f"controls: ignored malformed status frame: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.available = False
                self.error = str(exc)
                print(f"controls: BUSY Bar input disconnected: {exc}; reconnecting in {backoff:g}s")
                deadline = time.monotonic() + backoff
                while not self._stop.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(min(0.25, deadline - time.monotonic()))
                backoff = min(max_backoff, backoff * 2.0)


def process_control_events(listener, last_wheel_at: float, cooldown: float, invert_dial: bool = False):
    for event in listener.poll():
        now = time.monotonic()
        if "button_event" in event:
            be = event["button_event"]
            if be.get("button") == 2 and be.get("action") == 0:
                ok, detail = media_command("toggle")
                print("control: PLAY/PAUSE" + ("" if ok else f" failed: {detail}"))
        elif "encoder_event" in event:
            delta = int(event["encoder_event"].get("delta", 0) or 0)
            if delta and now - last_wheel_at >= cooldown:
                forward = delta < 0
                if invert_dial:
                    forward = not forward
                command = "next" if forward else "previous"
                ok, detail = media_command(command)
                label = "NEXT" if forward else "PREVIOUS"
                print(f"control: {label}" + ("" if ok else f" failed: {detail}"))
                last_wheel_at = now
    return last_wheel_at



def parse_args():
    p = argparse.ArgumentParser(description="Synchronized karaoke lyrics for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--fps", type=int, default=DEFAULT_FPS, help="render FPS (default: 15)")
    p.add_argument("--poll", type=float, default=0.35, help="Now Playing poll interval")
    p.add_argument("--sync-offset", type=float, default=0.0,
                   help="lyrics timing offset in seconds; positive = lyrics/ball earlier (default: 0)")
    p.add_argument("--ball-lead", type=float, default=0.10,
                   help="extra ball lead in seconds relative to the lyric highlight (default: 0.10)")
    p.add_argument("--no-latency-comp", action="store_true",
                   help="disable automatic compensation for BUSY Bar upload/draw latency")
    p.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "busybar-media-player-karaoke"))
    p.add_argument("--debug", action="store_true")
    p.add_argument("--demo", action="store_true", help="synthetic lyrics/player; no macOS Now Playing needed")
    p.add_argument("--no-ball", action="store_true", help="hide bouncing ball")
    p.add_argument("--two-line", action="store_true",
                   help="show current lyric plus the next line; disables the bouncing ball")
    p.add_argument("--no-underline", action="store_true",
                   help="hide the progress underline below the current lyric word")
    p.add_argument("--token", default=None, help="BUSY Bar Wi-Fi API token/PIN, if required")
    p.add_argument("--no-controls", action="store_true", help="disable BUSY Bar START/wheel media controls")
    p.add_argument("--wheel-cooldown", type=float, default=0.40,
                   help="seconds between wheel track changes (default: 0.40)")
    p.add_argument("--invert-dial", action="store_true",
                   help="invert rotary encoder direction for Previous/Next")
    return p.parse_args()


def _base(host):
    s = host.rstrip("/")
    return s if s.startswith(("http://", "https://")) else "http://" + s


def _png(pixels):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for r, g, b in pixels[y * W:(y + 1) * W]:
            raw += bytes((r, g, b, 255))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


class BusyDisplay:
    def __init__(self, host):
        self.base = _base(host)
        self.frame = 0

    def _request(self, method, path, data=None, content_type=None):
        headers = {"Content-Type": content_type} if content_type else {}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode()

    def show(self, pixels):
        name = f"karaoke{self.frame % 5}.png"
        self.frame += 1
        q = urllib.parse.urlencode({"application_name": APP, "file": name})
        try:
            self._request("POST", "/api/assets/upload?" + q, _png(pixels), "application/octet-stream")
            body = json.dumps({"application_name": APP, "elements": [
                {"id": "frame", "type": "image", "path": name, "x": 0, "y": 0}
            ]}).encode()
            return self._request("POST", "/api/display/draw", body, "application/json")
        except urllib.error.HTTPError as e:
            if e.code in (409, 508):
                return e.code
            raise

    def clear(self):
        q = urllib.parse.urlencode({"application_name": APP})
        try:
            self._request("DELETE", "/api/display/draw?" + q)
        except Exception:
            pass


class MacNowPlaying:
    """Read MediaRemote and maintain a smooth local playback clock.

    On some macOS/player combinations MediaRemote's elapsed position only
    changes when a transport event occurs (play/pause/seek).  Using that value
    directly makes karaoke animation appear frozen.  We therefore treat it as
    an anchor: while playbackRate > 0, elapsed advances from time.monotonic().
    A genuinely changed MediaRemote position re-anchors the clock, preserving
    seek and pause/resume accuracy.
    """

    SOURCE_CHANGE_EPS = 0.05
    SEEK_EPS = 0.80
    # Small source-vs-local clock errors are normal. Applying them wholesale
    # every poll creates visible jitter, so use a gentle phase-locked correction.
    DRIFT_GAIN = 0.22
    MAX_DRIFT_CORRECTION = 0.075

    def __init__(self, debug=False):
        self.debug = debug
        self.last = {}
        self._track = None
        self._anchor_elapsed = 0.0
        self._anchor_mono = time.monotonic()
        self._last_source_elapsed = None
        self._last_rate = 0.0

    @staticmethod
    def _identity(data):
        return (data.get("app"), data.get("artist"), data.get("title"), data.get("album"))

    def _clocked(self, data):
        now = time.monotonic()
        ident = self._identity(data)
        source = data.get("elapsed")
        try:
            source = float(source) if source is not None else None
        except (TypeError, ValueError):
            source = None
        try:
            rate = float(data.get("rate") or 0.0)
        except (TypeError, ValueError):
            rate = 0.0

        changed_track = ident != self._track
        source_changed = (source is not None and
                          (self._last_source_elapsed is None or
                           abs(source - self._last_source_elapsed) > self.SOURCE_CHANGE_EPS))

        # What our local clock says before considering the newest MediaRemote sample.
        predicted = self._anchor_elapsed
        if self._last_rate > 0:
            predicted += (now - self._anchor_mono) * self._last_rate

        if changed_track:
            self._track = ident
            self._anchor_elapsed = max(0.0, source or 0.0)
            self._anchor_mono = now
        elif rate <= 0:
            # Freeze immediately on pause. Prefer a newly supplied source value,
            # otherwise freeze at the locally predicted position.
            frozen = source if source_changed else predicted
            self._anchor_elapsed = max(0.0, frozen or 0.0)
            self._anchor_mono = now
        elif self._last_rate <= 0 < rate:
            # Resume: MediaRemote usually sends a fresh elapsed anchor.
            self._anchor_elapsed = max(0.0, source if source is not None else predicted)
            self._anchor_mono = now
        elif source_changed:
            # Treat MediaRemote as a reference clock, not a frame clock. Normal
            # refreshes differ from our monotonic extrapolation by a few tens or
            # hundreds of ms; snapping to each one makes the animation jitter.
            # Small errors are therefore corrected gradually (a tiny PLL), while
            # a real seek still re-anchors immediately.
            error = source - predicted
            if abs(error) >= self.SEEK_EPS:
                if self.debug:
                    print(f"playback seek/re-anchor: predicted={predicted:.2f}s source={source:.2f}s error={error:+.2f}s")
                corrected = source
            else:
                correction = max(-self.MAX_DRIFT_CORRECTION,
                                 min(self.MAX_DRIFT_CORRECTION, error * self.DRIFT_GAIN))
                corrected = predicted + correction
                if self.debug and abs(error) >= 0.12:
                    print(f"playback drift: error={error:+.3f}s correction={correction:+.3f}s")
            self._anchor_elapsed = max(0.0, corrected)
            self._anchor_mono = now

        self._last_source_elapsed = source
        self._last_rate = rate

        elapsed = self._anchor_elapsed
        if rate > 0:
            elapsed += (now - self._anchor_mono) * rate
        duration = data.get("duration")
        try:
            if duration is not None:
                elapsed = min(elapsed, float(duration))
        except (TypeError, ValueError):
            pass

        out = dict(data)
        out["elapsed"] = max(0.0, elapsed)
        return out

    def read(self):
        if sys.platform != "darwin":
            return {}
        try:
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", MAC_JXA],
                               capture_output=True, text=True, timeout=4)
            if r.returncode != 0:
                if self.debug: print("now-playing:", r.stderr.strip())
                return self.last
            data = json.loads(r.stdout.strip() or "{}")
            if data.get("title"):
                data = self._clocked(data)
                self.last = data
            return data
        except Exception as e:
            if self.debug: print("now-playing error:", e)
            return self.last

    def extrapolate(self, data):
        """Advance the cached elapsed value between the slower JXA polls."""
        if not data:
            return data
        now = time.monotonic()
        out = dict(data)
        if self._last_rate > 0:
            elapsed = self._anchor_elapsed + (now - self._anchor_mono) * self._last_rate
            try:
                if out.get("duration") is not None:
                    elapsed = min(elapsed, float(out["duration"]))
            except (TypeError, ValueError):
                pass
            out["elapsed"] = max(0.0, elapsed)
        else:
            out["elapsed"] = self._anchor_elapsed
        return out


_TIMESTAMP = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s?(.*)$")


def parse_lrc(text):
    rows = []
    for raw in (text or "").splitlines():
        m = _TIMESTAMP.match(raw.strip("\r"))
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        lyric = m.group(3).strip()
        rows.append((t, lyric))
    rows.sort(key=lambda x: x[0])
    return rows


def normalize(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.upper().split())


def track_key(info):
    material = "\x1f".join(str(info.get(k) or "") for k in ("artist", "title", "album", "duration"))
    return hashlib.sha1(material.encode("utf-8", "ignore")).hexdigest()


class LyricsFetcher:
    def __init__(self, cache_dir, debug=False):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.debug = debug
        self.requests = queue.Queue()
        self.results = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, info):
        self.requests.put(dict(info))

    def poll(self):
        latest = None
        try:
            while True:
                latest = self.results.get_nowait()
        except queue.Empty:
            return latest

    def close(self):
        self.stop.set()

    def _http_json(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def _lookup(self, info):
        key = track_key(info)
        path = self.cache / (key + ".json")
        if path.exists():
            try:
                obj = json.loads(path.read_text("utf-8"))
                return key, obj
            except Exception:
                pass

        title, artist = info.get("title") or "", info.get("artist") or ""
        album = info.get("album") or ""
        duration = info.get("duration")
        candidates = []
        if title and artist and album and duration:
            params = urllib.parse.urlencode({"track_name": title, "artist_name": artist,
                                             "album_name": album, "duration": round(float(duration), 2)})
            try:
                candidates = [self._http_json(LRCLIB_GET + "?" + params)]
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        if not candidates and title:
            params = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
            try:
                candidates = self._http_json(LRCLIB_SEARCH + "?" + params) or []
            except Exception:
                candidates = []

        # Prefer synced lyrics and closest duration.
        synced = [c for c in candidates if isinstance(c, dict) and c.get("syncedLyrics")]
        if synced:
            if duration:
                synced.sort(key=lambda c: abs(float(c.get("duration") or 1e9) - float(duration)))
            obj = {"status": "ok", "source": "lrclib", "record": synced[0]}
        else:
            obj = {"status": "not_found", "source": "lrclib"}
        try:
            path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass
        return key, obj

    def _run(self):
        while not self.stop.is_set():
            try:
                info = self.requests.get(timeout=0.2)
            except queue.Empty:
                continue
            # Collapse queued changes to the newest track.
            try:
                while True:
                    info = self.requests.get_nowait()
            except queue.Empty:
                pass
            try:
                key, obj = self._lookup(info)
                self.results.put((key, obj))
                if self.debug:
                    print("lyrics:", info.get("artist"), "-", info.get("title"), obj.get("status"))
            except Exception as e:
                if self.debug: print("lyrics error:", e)
                self.results.put((track_key(info), {"status": "error", "error": str(e)}))


def blank():
    return [BLACK] * (W * H)


def px(buf, x, y, color):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = color


def rect(buf, x, y, w, h, color):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            px(buf, xx, yy, color)


def glyph(ch):
    ch = normalize(ch)[:1]
    return FONT.get(ch, FONT.get("?"))


def text_width(text):
    total = 0
    for ch in normalize(text):
        w, _ = FONT.get(ch, FONT["?"])
        total += w + 1
    return max(0, total - 1)


def draw_text(buf, text, x, y, color, clip_left=0, clip_right=W):
    pen = x
    for ch in normalize(text):
        w, rows = FONT.get(ch, FONT["?"])
        for yy, mask in enumerate(rows):
            for xx in range(w):
                if mask & (1 << (w - 1 - xx)):
                    sx = pen + xx
                    if clip_left <= sx < clip_right:
                        px(buf, sx, y + yy, color)
        pen += w + 1
    return pen


def word_layout(line):
    words = re.findall(r"\S+", normalize(line))
    positions = []
    x = 0
    for word in words:
        w = text_width(word)
        positions.append((word, x, w))
        x += w + 4  # visible gap between words
    return words, positions, max(0, x - 4)


def estimated_word_times(line, start, end):
    words, positions, width = word_layout(line)
    if not words:
        return [], positions, width
    duration = max(0.25, end - start)
    # Weight by letters but add a fixed minimum so short words still get a beat.
    weights = [max(2.2, len(re.sub(r"[^A-Z0-9]", "", w)) + 0.8) for w in words]
    total = sum(weights)
    cursor = start
    times = []
    for weight in weights:
        span = duration * weight / total
        times.append((cursor, cursor + span))
        cursor += span
    times[-1] = (times[-1][0], end)
    return times, positions, width


def current_line(rows, elapsed, total_duration=None):
    if not rows:
        return None
    idx = -1
    for i, (t, _) in enumerate(rows):
        if t <= elapsed:
            idx = i
        else:
            break
    if idx < 0:
        return {"index": -1, "start": 0.0, "end": rows[0][0], "text": ""}
    start, text = rows[idx]
    if idx + 1 < len(rows):
        end = rows[idx + 1][0]
    elif total_duration and total_duration > start:
        end = float(total_duration)
    else:
        end = start + max(3.0, len(text) * 0.16)
    # Pathological timestamp gaps make the ball crawl. Keep display motion sane,
    # while line selection itself still uses the real LRC timestamps.
    visual_end = min(end, start + max(2.0, min(12.0, len(text) * 0.22)))
    return {"index": idx, "start": start, "end": end, "visual_end": visual_end, "text": text}


def render_karaoke(rows, info, ball=True, underline=True, sync_offset=0.0, ball_lead=0.10, latency_comp=0.0):
    buf = blank()
    # Render slightly into the future to compensate for transport/display
    # latency. --sync-offset is the user calibration; latency_comp is measured
    # automatically from recent upload+draw calls.
    elapsed = max(0.0, float(info.get("elapsed") or 0.0) + sync_offset + latency_comp)
    duration = info.get("duration")
    cur = current_line(rows, elapsed, duration)
    if not cur or cur["index"] < 0 or not cur["text"]:
        return render_standby(info, "found")

    times, positions, full_w = estimated_word_times(cur["text"], cur["start"], cur["visual_end"])
    if not positions:
        return buf

    # Find active word. After visual_end, pin to last word until next real LRC line.
    wi = len(times) - 1
    for i, (a, b) in enumerate(times):
        if elapsed < b:
            wi = i
            break
    a, b = times[wi]
    p = 1.0 if b <= a else max(0.0, min(1.0, (elapsed - a) / (b - a)))

    # The visual ball benefits from a small independent lead: LRC timestamps
    # mark line starts, while our word timestamps are inferred. Advancing only
    # the ball makes it land closer to the perceived vocal attack without
    # prematurely changing the highlighted word.
    ball_elapsed = elapsed + ball_lead
    ball_wi = wi
    for i, (ba, bb) in enumerate(times):
        if ball_elapsed < bb:
            ball_wi = i
            break
    ba, bb = times[ball_wi]
    bp = 1.0 if bb <= ba else max(0.0, min(1.0, (ball_elapsed - ba) / (bb - ba)))

    _, wx, ww = positions[wi]
    _, bwx, bww = positions[ball_wi]
    if ball_wi + 1 < len(positions):
        _, nx, nw = positions[ball_wi + 1]
        ball_world_x = (bwx + bww/2) + ((nx + nw/2) - (bwx + bww/2)) * bp
    else:
        ball_world_x = bwx + bww * (0.25 + 0.5 * bp)

    # Keep active word/ball around the center; don't scroll short lines.
    if full_w <= W - 4:
        camera = -(W - full_w)//2
    else:
        target = ball_world_x - W * 0.47
        camera = max(0.0, min(float(full_w - W + 4), target))
    x0 = int(round(2 - camera))

    # Draw each word separately so sung/current/future states are obvious.
    for i, (word, x, width) in enumerate(positions):
        if i < wi:
            col = CYAN
        elif i == wi:
            col = WHITE
        else:
            col = DIM
        draw_text(buf, word, x0 + x, 10, col)

    # Progress underline under the current word. It can be disabled independently
    # from the bouncing ball with --no-underline.
    if underline:
        underline_w = max(1, int(round(ww * p)))
        rect(buf, x0 + wx, 15, underline_w, 1, YELLOW)

    if ball:
        bx = int(round(x0 + ball_world_x))
        # Classic bouncing ball: highest midway between words, lower on the beat.
        arc = math.sin(math.pi * bp)
        by = int(round(6 - 4 * arc))
        rect(buf, bx - 1, by, 2, 2, YELLOW)
        # one bright pixel gives the 2x2 ball more sparkle on the LED matrix
        px(buf, bx - 1, by, WHITE)
    return buf



def render_karaoke_two_line(rows, info, underline=True, sync_offset=0.0, latency_comp=0.0):
    """Two-line karaoke view: current lyric on top, next lyric underneath.

    The current line keeps the same word-level inferred timing as the classic
    renderer, but progress is indicated only by the underline.  The next LRC
    line is shown early in a dim color so the singer can anticipate it.
    """
    buf = blank()
    elapsed = max(0.0, float(info.get("elapsed") or 0.0) + sync_offset + latency_comp)
    duration = info.get("duration")
    cur = current_line(rows, elapsed, duration)
    if not cur or cur["index"] < 0 or not cur["text"]:
        return render_standby(info, "found")

    times, positions, full_w = estimated_word_times(cur["text"], cur["start"], cur["visual_end"])
    if not positions:
        return buf

    # Active word and progress within it.
    wi = len(times) - 1
    for i, (a, b) in enumerate(times):
        if elapsed < b:
            wi = i
            break
    a, b = times[wi]
    p = 1.0 if b <= a else max(0.0, min(1.0, (elapsed - a) / (b - a)))
    _, wx, ww = positions[wi]

    # Current line: center when it fits; otherwise keep the active word near
    # the middle of the matrix so the underline remains easy to follow.
    active_center = wx + ww / 2.0
    if full_w <= W - 4:
        camera = -(W - full_w) // 2
    else:
        target = active_center - W * 0.47
        camera = max(0.0, min(float(full_w - W + 4), target))
    x0 = int(round(2 - camera))

    for i, (word, x, width) in enumerate(positions):
        if i < wi:
            col = CYAN
        elif i == wi:
            col = WHITE
        else:
            col = DIM
        draw_text(buf, word, x0 + x, 0, col)

    if underline:
        underline_w = max(1, int(round(ww * p)))
        rect(buf, x0 + wx, 6, underline_w, 1, YELLOW)

    # Preview the next phrase on the second line.  It is deliberately stable:
    # always left-aligned, never scrolled.  If it is wider than the 72-pixel
    # matrix, the excess is simply clipped at the right edge.
    next_idx = cur["index"] + 1
    if next_idx < len(rows):
        next_text = rows[next_idx][1]
        _, next_positions, _ = word_layout(next_text)
        next_x0 = 0
        for word, x, width in next_positions:
            draw_text(buf, word, next_x0 + x, 10, DIM)

    return buf


TINY_FONT = {
    " ": (1, [0,0,0]),
    "a": (2, [1,3,3]), "b": (2, [2,3,3]), "c": (2, [3,2,3]),
    "d": (2, [1,3,3]), "e": (2, [3,3,2]), "f": (2, [3,2,2]),
    "g": (2, [3,3,1]), "h": (2, [2,3,3]), "i": (1, [1,1,1]),
    "j": (2, [1,1,3]), "k": (2, [2,3,3]), "l": (1, [1,1,1]),
    "m": (3, [5,7,5]), "n": (2, [2,3,3]), "o": (2, [3,3,3]),
    "p": (2, [3,3,2]), "q": (2, [3,3,1]), "r": (2, [3,2,2]),
    "s": (2, [3,2,1]), "t": (2, [3,2,2]), "u": (2, [2,2,3]),
    "v": (3, [5,5,2]), "w": (3, [5,7,5]), "x": (3, [5,2,5]),
    "y": (2, [3,1,2]), "z": (2, [3,1,3]),
    "-": (2, [0,3,0]), ".": (1, [0,0,1]),
}

def tiny_normalize(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(s.split())

def tiny_text_width(text):
    text = tiny_normalize(text)
    return max(0, sum(TINY_FONT.get(ch, TINY_FONT[" "])[0] + 1 for ch in text) - 1)

def draw_tiny_text(buf, text, x, y, color):
    pen = x
    for ch in tiny_normalize(text):
        w, rows = TINY_FONT.get(ch, TINY_FONT[" "])
        for yy, mask in enumerate(rows):
            for xx in range(w):
                if mask & (1 << (w - 1 - xx)):
                    px(buf, pen + xx, y + yy, color)
        pen += w + 1
    return pen


_MARQUEE_EPOCHS = {}
MARQUEE_SPEED = 12.0       # pixels per second
MARQUEE_EDGE_PAUSE = 0.9   # seconds to hold each end before reversing


def _marquee_x(text, now, left=1, right=71):
    """Return x for a centered-or-ping-pong metadata line.

    Short strings remain perfectly centered. Long strings travel from the left
    edge to the rightmost readable end, pause, and reverse. Each distinct string
    gets its own epoch, so a newly selected track starts at the beginning instead
    of appearing halfway through a marquee cycle.
    """
    width = text_width(text)
    avail = right - left + 1
    if width <= avail:
        return left + (avail - width) // 2

    epoch = _MARQUEE_EPOCHS.setdefault(text, now)
    travel = float(width - avail)
    move_time = travel / MARQUEE_SPEED
    cycle = (2.0 * MARQUEE_EDGE_PAUSE) + (2.0 * move_time)
    t = (now - epoch) % max(0.001, cycle)

    if t < MARQUEE_EDGE_PAUSE:
        offset = 0.0
    elif t < MARQUEE_EDGE_PAUSE + move_time:
        offset = (t - MARQUEE_EDGE_PAUSE) * MARQUEE_SPEED
    elif t < (2.0 * MARQUEE_EDGE_PAUSE) + move_time:
        offset = travel
    else:
        back_t = t - ((2.0 * MARQUEE_EDGE_PAUSE) + move_time)
        offset = travel - back_t * MARQUEE_SPEED

    return int(round(left - max(0.0, min(travel, offset))))


def render_standby(info, lyrics_state, now=None):
    """Two-line metadata card with conditional marquee scrolling.

    Artist and title are centered when they fit. If either exceeds the 72-pixel
    display, only that line scrolls smoothly left/right with a short pause at
    each edge. Lyrics availability is communicated by color: green when synced
    lyrics are available, red when lookup completed without lyrics, blue while
    lookup is still in progress.
    """
    if now is None:
        now = time.monotonic()
    buf = blank()
    if lyrics_state == "found":
        color = GREEN
    elif lyrics_state == "not_found":
        color = RED
    else:
        color = BLUE

    for text, y in [
        (normalize(info.get("artist") or "UNKNOWN ARTIST"), 2),
        (normalize(info.get("title") or "NO MUSIC PLAYING"), 9),
    ]:
        x = _marquee_x(text, now)
        draw_text(buf, text, x, y, color)
    return buf


def render_message(line1, line2=""):
    buf = blank()
    for text, y, col in ((line1, 3, WHITE), (line2, 10, DIM)):
        if not text: continue
        if text_width(text) <= W - 2:
            x = max(1, (W - text_width(text)) // 2)
        else:
            x = 1
        draw_text(buf, text, x, y, col)
    return buf


DEMO_LRC = """[00:00.00] IS THIS THE REAL LIFE
[00:04.00] IS THIS JUST FANTASY
[00:08.00] CAUGHT IN A LANDSLIDE
[00:12.00] NO ESCAPE FROM REALITY
[00:16.00] OPEN YOUR EYES
[00:20.00] LOOK UP TO THE SKIES AND SEE
"""


def demo_info(t):
    cycle = 24.0
    return {"title": "KARAOKE DEMO", "artist": "BUSY BAR", "album": "DEMO",
            "duration": cycle, "elapsed": t % cycle, "rate": 1.0, "app": "demo"}


def main():
    args = parse_args()
    display = BusyDisplay(args.host)
    backend = MacNowPlaying(args.debug)
    fetcher = LyricsFetcher(args.cache_dir, args.debug)
    rows = parse_lrc(DEMO_LRC) if args.demo else []
    lyric_key = "demo" if args.demo else None
    requested_key = None
    last_info = {}
    last_poll = 0.0
    next_frame = 0.0
    t0 = time.monotonic()
    frame_interval = 1.0 / max(1, min(24, args.fps))
    # EMA of end-to-end HTTP upload+draw time. The frame becomes visible only
    # after this delay, so render that far ahead on the next frame.
    draw_latency_ema = 0.0
    self_debug_counter = [0]
    print(f"{APP} -> {_base(args.host)}  (Ctrl-C to stop)")

    controls = None
    last_wheel_at = 0.0
    if not args.demo and not args.no_controls:
        controls = InputListener(args.host, args.token)
        controls.start()
        if controls.available:
            suffix = " [inverted]" if args.invert_dial else ""
            print("controls: START=play/pause  wheel=previous/next (direct WebSocket)" + suffix)
        else:
            print(f"controls: unavailable ({controls.error or 'unknown error'})")

    try:
        while True:
            if controls is not None:
                last_wheel_at = process_control_events(
                    controls, last_wheel_at, max(0.0, args.wheel_cooldown), args.invert_dial
                )
            now = time.monotonic()
            if args.demo:
                info = demo_info(now - t0)
            elif now - last_poll >= max(0.15, args.poll):
                last_poll = now
                info = backend.read()
                if info:
                    last_info = info
            else:
                # JXA polling is intentionally slower than rendering. Keep the
                # karaoke clock moving smoothly between MediaRemote samples.
                info = backend.extrapolate(last_info)

            if not args.demo and info.get("title"):
                key = track_key(info)
                if key != requested_key:
                    requested_key = key
                    lyric_key = None
                    rows = []
                    fetcher.request(info)
                    if args.debug:
                        print("track:", info.get("artist"), "-", info.get("title"),
                              f"duration={info.get('duration')} elapsed={info.get('elapsed')}")

            result = fetcher.poll()
            if result:
                key, obj = result
                if key == requested_key:
                    lyric_key = key
                    if obj.get("status") == "ok":
                        rows = parse_lrc(obj["record"].get("syncedLyrics") or "")
                    else:
                        rows = []

            if now >= next_frame:
                # Keep an absolute cadence instead of scheduling from the end of
                # each HTTP draw. If a frame is late, skip obsolete slots rather
                # than slowing the whole animation down.
                if next_frame == 0.0:
                    next_frame = now
                while next_frame <= now:
                    next_frame += frame_interval

                # Re-sample the extrapolated clock immediately before rasterising;
                # JXA polling may have happened earlier in this loop.
                if not args.demo and last_info:
                    info = backend.extrapolate(last_info)
                latency_comp = 0.0 if args.no_latency_comp else min(0.30, draw_latency_ema)
                if args.demo:
                    if args.two_line:
                        pixels = render_karaoke_two_line(rows, info, not args.no_underline,
                                                         args.sync_offset, latency_comp)
                    else:
                        pixels = render_karaoke(rows, info, not args.no_ball, not args.no_underline,
                                                args.sync_offset, args.ball_lead, latency_comp)
                elif not info.get("title"):
                    pixels = render_message("NO MUSIC PLAYING", "MACOS NOW PLAYING")
                elif requested_key and lyric_key is None:
                    pixels = render_standby(info, "finding", now)
                elif rows:
                    if args.two_line:
                        pixels = render_karaoke_two_line(rows, info, not args.no_underline,
                                                         args.sync_offset, latency_comp)
                    else:
                        pixels = render_karaoke(rows, info, not args.no_ball, not args.no_underline,
                                                args.sync_offset, args.ball_lead, latency_comp)
                else:
                    pixels = render_standby(info, "not_found", now)
                draw_t0 = time.monotonic()
                status = display.show(pixels)
                draw_dt = time.monotonic() - draw_t0
                draw_latency_ema = draw_dt if draw_latency_ema == 0.0 else (draw_latency_ema * 0.82 + draw_dt * 0.18)
                if args.debug and status not in (200, 201, 204, None):
                    print("draw status:", status)
                if args.debug and self_debug_counter[0] % 60 == 0:
                    print(f"render: draw-latency={draw_latency_ema*1000:.0f}ms comp={latency_comp*1000:.0f}ms offset={args.sync_offset:+.2f}s ball-lead={args.ball_lead:+.2f}s")
                self_debug_counter[0] += 1
            sleep_for = min(0.02, max(0.001, next_frame - time.monotonic()))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if controls is not None:
            controls.stop()
        fetcher.close()
        display.clear()


if __name__ == "__main__":
    main()
