#!/usr/bin/env python3
"""Lunar Phases for BUSY Bar.

A small pixel-art Moon travels from right to left through a full lunar cycle,
then returns from left to right through the remaining phases. At the end it
parks in a presentation position and shows today's lunar phase in the selected
language.

The animation starts automatically every N seconds and can also be triggered
with the BUSY Bar START/top button.

Examples:
    python3 app.py
    python3 app.py --host 127.0.0.1:8080
    python3 app.py --language it
    python3 app.py --auto-seconds 90 --fps 14
    python3 app.py --debug-phase full --font large

Dependency for physical controls only:
    pip install websockets

No busylib and no websocket-client are used.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import queue
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "lunar-phases"
W, H = 72, 16
DEFAULT_HOST = "10.0.4.20"
DEFAULT_FPS = 12
DEFAULT_AUTO_SECONDS = 60.0

MOON_R = 6
MOON_DIAMETER = 13
TRAVEL_LEFT_X = -2
TRAVEL_RIGHT_X = W - MOON_DIAMETER + 2
PARK_X = 2
MOON_Y = 1

OUTBOUND_SECONDS = 2.6
RETURN_SECONDS = 2.6
PARK_SECONDS = 0.45
LABEL_FADE_SECONDS = 0.55

BLACK = (0, 0, 0)
SPACE = (1, 2, 7)
STAR_DIM = (45, 55, 85)
STAR_BRIGHT = (130, 150, 210)
MOON_DARK = (7, 10, 18)
MOON_EDGE = (62, 68, 82)
MOON_MID = (145, 149, 154)
MOON_LIGHT = (224, 224, 214)
MOON_HIGHLIGHT = (250, 247, 225)
TEXT_MAIN = (205, 215, 244)
TEXT_ACCENT = (123, 151, 255)

SYNODIC_MONTH_DAYS = 29.530588853
# Well-known new moon epoch, close enough for a display widget.
NEW_MOON_EPOCH = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)

PHASE_KEYS = (
    "new",
    "waxing_crescent",
    "first_quarter",
    "waxing_gibbous",
    "full",
    "waning_gibbous",
    "last_quarter",
    "waning_crescent",
)

TITLE_TRANSLATIONS = {
    "en": "LUNAR PHASES",
    "fr": "PHASES LUNAIRES",
    "de": "MONDPHASEN",
    "es": "FASES LUNARES",
    "nl": "MAANFASEN",
    "it": "FASI LUNARI",
}

EASTER_EGG_TRANSLATIONS = {
    "en": ["THAT'S NO", "MOON..."],
    "fr": ["CE N'EST PAS", "UNE LUNE..."],
    "de": ["DAS IST KEIN", "MOND..."],
    "es": ["ESO NO ES", "UNA LUNA..."],
    "nl": ["DAT IS GEEN", "MAAN..."],
    "it": ["QUELLA NON E'", "UNA LUNA..."],
}

TRANSLATIONS = {
    "en": {
        "new": "NEW MOON",
        "waxing_crescent": "WAXING CRESCENT",
        "first_quarter": "FIRST QUARTER",
        "waxing_gibbous": "WAXING GIBBOUS",
        "full": "FULL MOON",
        "waning_gibbous": "WANING GIBBOUS",
        "last_quarter": "LAST QUARTER",
        "waning_crescent": "WANING CRESCENT",
    },
    "fr": {
        "new": "NOUVELLE LUNE",
        "waxing_crescent": "PREMIER CROISSANT",
        "first_quarter": "PREMIER QUARTIER",
        "waxing_gibbous": "GIBBEUSE CROISSANTE",
        "full": "PLEINE LUNE",
        "waning_gibbous": "GIBBEUSE DECROISSANTE",
        "last_quarter": "DERNIER QUARTIER",
        "waning_crescent": "DERNIER CROISSANT",
    },
    "de": {
        "new": "NEUMOND",
        "waxing_crescent": "ZUNEHMENDE SICHEL",
        "first_quarter": "ERSTES VIERTEL",
        "waxing_gibbous": "ZUNEHMENDER MOND",
        "full": "VOLLMOND",
        "waning_gibbous": "ABNEHMENDER MOND",
        "last_quarter": "LETZTES VIERTEL",
        "waning_crescent": "ABNEHMENDE SICHEL",
    },
    "es": {
        "new": "LUNA NUEVA",
        "waxing_crescent": "CRECIENTE",
        "first_quarter": "CUARTO CRECIENTE",
        "waxing_gibbous": "GIBOSA CRECIENTE",
        "full": "LUNA LLENA",
        "waning_gibbous": "GIBOSA MENGUANTE",
        "last_quarter": "CUARTO MENGUANTE",
        "waning_crescent": "MENGUANTE",
    },
    "nl": {
        "new": "NIEUWE MAAN",
        "waxing_crescent": "WASSENDE SIKKEL",
        "first_quarter": "EERSTE KWARTIER",
        "waxing_gibbous": "WASSENDE MAAN",
        "full": "VOLLE MAAN",
        "waning_gibbous": "AFNEMENDE MAAN",
        "last_quarter": "LAATSTE KWARTIER",
        "waning_crescent": "AFNEMENDE SIKKEL",
    },
    "it": {
        "new": "LUNA NUOVA",
        "waxing_crescent": "LUNA CRESCENTE",
        "first_quarter": "PRIMO QUARTO",
        "waxing_gibbous": "GIBBOSA CRESCENTE",
        "full": "LUNA PIENA",
        "waning_gibbous": "GIBBOSA CALANTE",
        "last_quarter": "ULTIMO QUARTO",
        "waning_crescent": "LUNA CALANTE",
    },
}

# Readable pixel fonts for the final phase label. Unsupported accented
# letters are transliterated by normalize_text().
LARGE_FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}


SMALL_FONT = {
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "011", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "-": ("000", "000", "111", "000", "000"),
    " ": ("000", "000", "000", "000", "000"),
}

FONTS = {
    "small": {"glyphs": SMALL_FONT, "advance": 4, "height": 5},
    "large": {"glyphs": LARGE_FONT, "advance": 6, "height": 7},
}

STARS = (
    (8, 2, 0), (21, 12, 1), (31, 4, 2), (45, 1, 1),
    (58, 11, 0), (68, 5, 2), (13, 8, 2), (52, 7, 0),
)


def parse_args():
    p = argparse.ArgumentParser(description="Animated lunar phases for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST, help="BUSY Bar host ip[:port]")
    p.add_argument(
        "--language", "--lang", choices=sorted(TRANSLATIONS), default="en",
        help="display language: en, fr, de, es, nl, it (default: en)",
    )
    p.add_argument(
        "--auto-seconds", type=float, default=DEFAULT_AUTO_SECONDS,
        help=f"automatically replay every N seconds; 0 disables (default: {DEFAULT_AUTO_SECONDS:g})",
    )
    p.add_argument(
        "--fps", type=int, default=DEFAULT_FPS,
        help=f"animation frame rate (default: {DEFAULT_FPS})",
    )
    p.add_argument(
        "--font", choices=sorted(FONTS), default="small",
        help="pixel font: small (3x5, default) or large (5x7)",
    )
    p.add_argument(
        "--debug-phase", choices=("current",) + PHASE_KEYS, default="current",
        help="force a lunar phase for testing instead of using the current phase",
    )
    p.add_argument(
        "--easter-egg", action="store_true",
        help="replace the final lunar phase with a Death Star easter egg",
    )
    p.add_argument(
        "--demo", action="store_true",
        help="same as --auto-seconds 8; useful with the emulator",
    )
    return p.parse_args()


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _ws_url(host):
    raw = host.rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunparse((scheme, parsed.netloc, "/api/status/ws", "", "", ""))


def _png(pixels):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            r, g, b = pixels[y * W + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 5))
        + chunk(b"IEND", b"")
    )


_RING = 5
_frame_no = 0


def _post(host, path, data, content_type):
    req = urllib.request.Request(
        _base(host) + path,
        data=data,
        method="POST",
        headers={"Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode()


def show(host, pixels):
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    try:
        q = urllib.parse.urlencode({"application_name": APP, "file": fn})
        _post(host, "/api/assets/upload?" + q, _png(pixels), "application/octet-stream")
        body = {
            "application_name": APP,
            "priority": 30,
            "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}],
        }
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return 409
        raise


def clear(host):
    q = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + q, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


def delete_assets(host):
    q = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/assets/upload?" + q, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


# ---------------------------------------------------------------------------
# Minimal protobuf decoder for /api/status/ws
# ---------------------------------------------------------------------------

def _read_varint(buf, pos):
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


def _fields(buf):
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_no, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field_no, wire, value
        elif wire == 1:
            pos += 8
        elif wire == 2:
            n, pos = _read_varint(buf, pos)
            end = pos + n
            if end > len(buf):
                return
            yield field_no, wire, buf[pos:end]
            pos = end
        elif wire == 5:
            pos += 4
        else:
            return


def _is_start_press(raw):
    """Decode a physical START/top-button press.

    BUSY firmware revisions seen in the existing apps used two protobuf enum/
    oneof layouts. This decoder accepts both known layouts while rejecting the
    encoder form, so the app remains usable across those revisions.
    """
    try:
        for sf, sw, update in _fields(raw):
            if sf != 2 or sw != 2:  # State.updates
                continue
            for uf, uw, input_event in _fields(update):
                if uf != 11 or uw != 2:  # StateUpdate.input
                    continue
                for inf, inw, payload in _fields(input_event):
                    if inw != 2 or inf not in (1, 3):
                        continue
                    vals = {f: v for f, w, v in _fields(payload) if w == 0}
                    button = int(vals.get(1, 0))
                    action = int(vals.get(2, 0))

                    # Current schema: button_event field 1, START=2, PRESS=0.
                    if inf == 1 and button == 2 and action == 0:
                        return True

                    # Older observed schema: button_event field 3, START=0,
                    # PRESS=0. Encoder field 3 has a non-zero delta in field 1,
                    # so only an empty/zero payload is accepted here.
                    if inf == 3 and button == 0 and action == 0:
                        return True
    except Exception:
        return False
    return False


class ButtonListener:
    def __init__(self, host):
        self.host = host
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.available = True
        self.error = None

    def start(self):
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            self.available = False
            self.error = f"websockets not installed: {exc}"
            return
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def poll_press(self):
        pressed = False
        try:
            while True:
                self.events.get_nowait()
                pressed = True
        except queue.Empty:
            return pressed

    def _run(self):
        try:
            asyncio.run(self._listen_forever())
        except Exception as exc:
            if not self.stop_event.is_set():
                self.available = False
                self.error = str(exc)
                print(f"[ws] controls unavailable: {exc}")

    async def _listen_forever(self):
        import websockets

        backoff = 1.0
        connected_once = False
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    _ws_url(self.host),
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(json.dumps({"enable": True}))
                    print("[ws] reconnected" if connected_once else "[ws] connected")
                    connected_once = True
                    backoff = 1.0
                    async for message in ws:
                        if self.stop_event.is_set():
                            return
                        if isinstance(message, str):
                            continue
                        if _is_start_press(bytes(message)):
                            self.events.put(time.monotonic())
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                print(f"[ws] disconnected: {exc}; reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(8.0, backoff * 1.7)


# ---------------------------------------------------------------------------
# Lunar math
# ---------------------------------------------------------------------------

def lunar_phase_fraction(now=None):
    """Return phase age normalized to [0,1): 0=new, 0.5=full."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    days = (now.astimezone(dt.timezone.utc) - NEW_MOON_EPOCH).total_seconds() / 86400.0
    return (days / SYNODIC_MONTH_DAYS) % 1.0


def phase_key(phase):
    idx = int(math.floor((phase * 8.0) + 0.5)) % 8
    return PHASE_KEYS[idx]


def configured_phase(debug_phase):
    """Return the real phase, or an exact canonical phase when debugging."""
    if debug_phase == "current":
        return lunar_phase_fraction()
    return PHASE_KEYS.index(debug_phase) / 8.0


# ---------------------------------------------------------------------------
# Pixel rendering
# ---------------------------------------------------------------------------

def blank():
    return [SPACE] * (W * H)


def px(buf, x, y, color):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = color


def blend(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t + 0.5) for i in range(3))


def scale(c, amount):
    amount = max(0.0, min(1.0, amount))
    return tuple(int(v * amount + 0.5) for v in c)


def draw_stars(buf, t, dim_for_text=False):
    for x, y, phase in STARS:
        pulse = 0.5 + 0.5 * math.sin(t * 1.8 + phase * 2.1)
        col = blend(STAR_DIM, STAR_BRIGHT, pulse * (0.45 if dim_for_text else 0.75))
        px(buf, x, y, col)
        if pulse > 0.88 and not dim_for_text:
            px(buf, x + 1, y, scale(col, 0.55))


def moon_surface_color(nx, ny):
    # Radial shading plus deterministic tiny "maria" patches.
    radial = max(0.0, 1.0 - math.sqrt(nx * nx + ny * ny))
    base = blend(MOON_MID, MOON_LIGHT, 0.35 + radial * 0.65)
    patch = (
        math.sin(nx * 8.1 + ny * 5.7)
        + math.sin(nx * 13.2 - ny * 9.3) * 0.55
        + math.cos(nx * 5.1 + ny * 12.7) * 0.35
    )
    if patch > 1.0:
        base = blend(base, MOON_MID, min(0.42, (patch - 1.0) * 0.22))
    return base


def is_illuminated(nx, ny, phase):
    """Orthographic sphere lighting for a phase fraction.

    Observer is +Z. Sun direction rotates in the X/Z plane. The sign convention
    produces a bright right-hand crescent while waxing and a bright left-hand
    crescent while waning.
    """
    rr = nx * nx + ny * ny
    if rr > 1.0:
        return False, -1.0
    nz = math.sqrt(max(0.0, 1.0 - rr))
    angle = math.tau * phase
    sx = math.sin(angle)
    sz = -math.cos(angle)
    dot = nx * sx + nz * sz
    return dot > -0.03, dot


def draw_moon(buf, x, phase, shimmer=0.0):
    cx = x + MOON_R
    cy = MOON_Y + MOON_R
    for yy in range(cy - MOON_R, cy + MOON_R + 1):
        for xx in range(cx - MOON_R, cx + MOON_R + 1):
            dx = xx - cx
            dy = yy - cy
            rr = dx * dx + dy * dy
            if rr > MOON_R * MOON_R:
                continue
            nx = dx / MOON_R
            ny = dy / MOON_R
            lit, dot = is_illuminated(nx, ny, phase)
            edge = math.sqrt(rr) / MOON_R
            if edge > 0.90:
                col = MOON_EDGE
            elif lit:
                col = moon_surface_color(nx, ny)
                # A very slight brightness modulation makes the moving moon feel alive.
                col = blend(col, MOON_HIGHLIGHT, max(0.0, dot) * 0.14 + shimmer * 0.05)
            else:
                # Keep the unlit side faintly visible, like earthshine, rather than black.
                earthshine = max(0.0, 0.18 - edge * 0.05)
                col = blend(MOON_DARK, MOON_EDGE, earthshine)
            px(buf, xx, yy, col)


def normalize_text(text, font_name="large"):
    repl = {
        "À": "A", "Á": "A", "Â": "A", "Ä": "A", "Ã": "A",
        "È": "E", "É": "E", "Ê": "E", "Ë": "E",
        "Ì": "I", "Í": "I", "Î": "I", "Ï": "I",
        "Ò": "O", "Ó": "O", "Ô": "O", "Ö": "O",
        "Ù": "U", "Ú": "U", "Û": "U", "Ü": "U",
        "Ç": "C", "Ñ": "N", "ß": "SS",
    }
    s = text.upper()
    out = "".join(repl.get(ch, ch) for ch in s)
    glyphs = FONTS[font_name]["glyphs"]
    return "".join(ch if ch in glyphs else " " for ch in out)


def text_width(text, font_name="large"):
    advance = FONTS[font_name]["advance"]
    return max(0, len(normalize_text(text, font_name)) * advance - 1)


def draw_text(buf, text, x, y, color, clip_x0=0, clip_x1=W, font_name="large"):
    pen = x
    glyphs = FONTS[font_name]["glyphs"]
    advance = FONTS[font_name]["advance"]
    for ch in normalize_text(text, font_name):
        glyph = glyphs.get(ch, glyphs[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                xx = pen + gx
                if bit == "1" and clip_x0 <= xx < clip_x1:
                    px(buf, xx, y + gy, color)
        pen += advance


def split_label(text, max_width, font_name="large"):
    words = normalize_text(text, font_name).split()
    if not words:
        return [""]
    lines = []
    for word in words:
        if not lines:
            lines.append(word)
            continue
        candidate = lines[-1] + " " + word
        if text_width(candidate, font_name) <= max_width:
            lines[-1] = candidate
        elif len(lines) < 2:
            lines.append(word)
        else:
            lines[-1] += " " + word
    return lines[:2]


def draw_phase_label(buf, text, alpha, elapsed, font_name="small"):
    x0, x1 = 18, 72
    width = x1 - x0
    lines = split_label(text, width, font_name)
    col = scale(TEXT_MAIN, alpha)

    # No decorative accent line here: on the 16px-high panel it could look
    # like an unwanted blue/purple display artefact.
    if font_name == "small":
        ys = [5] if len(lines) == 1 else [2, 9]
    else:
        ys = [4] if len(lines) == 1 else [0, 9]
    for line, y in zip(lines, ys):
        tw = text_width(line, font_name)
        if tw <= width:
            x = x0 + (width - tw) // 2
        else:
            overflow = tw - width
            cycle = max(1.0, 2.0 * (overflow + 8))
            p = (elapsed * 9.0) % cycle
            offset = int(p if p <= overflow + 8 else cycle - p)
            x = x0 + 3 - offset
        draw_text(buf, line, x, y, col, x0, x1, font_name)


def ease(t):
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def travel_phase(progress, outbound):
    # One full visual lunar cycle over the round trip: 0->0.5 outbound,
    # 0.5->1.0 on return. That exposes every canonical phase in order.
    return progress * 0.5 if outbound else 0.5 + progress * 0.5


def draw_sweep_title(buf, language, moon_x, direction, font_name="small"):
    """Draw the localized app title as if the moving moon wipes/writes it.

    On the right-to-left leg only the pixels still ahead of the moon remain,
    so the title is progressively erased. On the left-to-right leg the same
    clipping boundary expands, making the moon appear to write the title back.
    If the selected font is too wide for the 72px panel, fall back to small.
    """
    text = TITLE_TRANSLATIONS[language]
    chosen = font_name
    if text_width(text, chosen) > W - 4:
        chosen = "small"

    tw = text_width(text, chosen)
    x = (W - tw) // 2
    y = 5 if chosen == "small" else 4

    # The moon's visual center is the moving wipe edge. A tiny overlap keeps
    # the title hidden under the lunar disk instead of visibly touching it.
    wipe_x = int(round(moon_x + MOON_R - 1))
    if direction == "left":
        clip_x0, clip_x1 = 0, max(0, min(W, wipe_x))
    else:
        clip_x0, clip_x1 = 0, max(0, min(W, wipe_x))

    draw_text(buf, text, x, y, TEXT_MAIN, clip_x0, clip_x1, chosen)


def build_animation_frame(elapsed, current_phase, language="en", font_name="small"):
    total_travel = OUTBOUND_SECONDS + RETURN_SECONDS
    buf = blank()
    draw_stars(buf, elapsed)

    if elapsed < OUTBOUND_SECONDS:
        # First pass: keep the title completely hidden. The reveal starts only
        # after the moon has reached the left edge once.
        p = ease(elapsed / OUTBOUND_SECONDS)
        x = int(round(TRAVEL_RIGHT_X + (TRAVEL_LEFT_X - TRAVEL_RIGHT_X) * p))
        phase = travel_phase(p, True)
        draw_moon(buf, x, phase, shimmer=0.5 + 0.5 * math.sin(elapsed * 8.0))
        return buf, False

    if elapsed < total_travel:
        local = elapsed - OUTBOUND_SECONDS
        p = ease(local / RETURN_SECONDS)
        x = int(round(TRAVEL_LEFT_X + (TRAVEL_RIGHT_X - TRAVEL_LEFT_X) * p))
        phase = travel_phase(p, False) % 1.0
        draw_sweep_title(buf, language, x, "right", font_name)
        draw_moon(buf, x, phase, shimmer=0.5 + 0.5 * math.sin(elapsed * 8.0))
        return buf, False

    # After touching the right edge, make a short elegant parking glide to the
    # left-side information-card position, while morphing to today's real phase.
    local = elapsed - total_travel
    if local < PARK_SECONDS:
        p = ease(local / PARK_SECONDS)
        x = int(round(TRAVEL_RIGHT_X + (PARK_X - TRAVEL_RIGHT_X) * p))
        # On this final right-to-left glide the moon wipes the title away,
        # mirroring the reveal performed on the return leg.
        draw_sweep_title(buf, language, x, "left", font_name)
        # shortest circular interpolation from new-moon endpoint to current phase
        phase = current_phase * p
        draw_moon(buf, x, phase, shimmer=0.2)
        return buf, False

    return final_frame(current_phase, elapsed - total_travel - PARK_SECONDS, language, font_name), True



def draw_death_star(buf, x=PARK_X):
    """Draw a compact 13px Death Star inspired pixel-art sphere."""
    cx = x + MOON_R
    cy = MOON_Y + MOON_R
    for yy in range(cy - MOON_R, cy + MOON_R + 1):
        for xx in range(cx - MOON_R, cx + MOON_R + 1):
            dx, dy = xx - cx, yy - cy
            rr = dx * dx + dy * dy
            if rr > MOON_R * MOON_R:
                continue
            edge = math.sqrt(rr) / MOON_R
            col = (88, 94, 104) if edge < 0.86 else (48, 53, 62)
            if (xx + yy) % 5 == 0:
                col = (112, 118, 126)
            px(buf, xx, yy, col)
    # Equatorial trench.
    for xx in range(cx - 5, cx + 6):
        px(buf, xx, cy + 1, (30, 34, 42))
    # Superlaser dish in the upper-right quadrant.
    dish_cx, dish_cy = cx + 2, cy - 2
    for yy in range(dish_cy - 2, dish_cy + 3):
        for xx in range(dish_cx - 2, dish_cx + 3):
            if (xx - dish_cx) ** 2 + (yy - dish_cy) ** 2 <= 4:
                px(buf, xx, yy, (34, 39, 48))
    px(buf, dish_cx, dish_cy, (150, 158, 166))


def easter_egg_frame(elapsed, language="en", font_name="small"):
    buf = blank()
    draw_stars(buf, elapsed, dim_for_text=True)
    draw_death_star(buf)
    # Localized Star Wars easter-egg quote, split for the 72x16 display.
    col = scale(TEXT_MAIN, min(1.0, elapsed / LABEL_FADE_SECONDS))
    lines = EASTER_EGG_TRANSLATIONS[language]
    chosen = "small" if text_width(lines[0], font_name) > 54 else font_name
    ys = [2, 9] if chosen == "small" else [0, 9]
    for line, y in zip(lines, ys):
        x0 = 18
        tw = text_width(line, chosen)
        x = x0 + max(0, (54 - tw) // 2)
        draw_text(buf, line, x, y, col, x0, W, chosen)
    return buf


def final_frame(current_phase, label_elapsed, language="en", font_name="small", easter_egg=False):
    if easter_egg:
        return easter_egg_frame(label_elapsed, language, font_name)
    buf = blank()
    draw_stars(buf, label_elapsed, dim_for_text=True)
    draw_moon(buf, PARK_X, current_phase, shimmer=0.15)
    label = TRANSLATIONS[language][phase_key(current_phase)]
    alpha = min(1.0, max(0.0, label_elapsed / LABEL_FADE_SECONDS))
    draw_phase_label(buf, label, alpha, label_elapsed, font_name)
    return buf


def main():
    args = parse_args()
    args.fps = max(6, min(20, args.fps))
    if args.demo:
        args.auto_seconds = 8.0
    args.auto_seconds = max(0.0, args.auto_seconds)

    listener = ButtonListener(args.host)
    listener.start()
    if not listener.available:
        print(f"[ws] physical button disabled: {listener.error}")

    current_phase = configured_phase(args.debug_phase)
    current_key = phase_key(current_phase)
    print(
        f"lunar-phases -> {_base(args.host)}  language={args.language}  "
        f"phase={current_key}  font={args.font}  debug={args.debug_phase}  easter_egg={args.easter_egg}  (Ctrl-C to stop)"
    )
    print("Press the top/START button to replay the lunar cycle.")

    active = True  # play once on startup
    started = time.monotonic()
    last_animation_start = started
    last_frame_at = 0.0
    phase_refresh_at = time.monotonic()

    try:
        while True:
            now = time.monotonic()

            if listener.poll_press():
                active = True
                started = now
                last_animation_start = now
                current_phase = configured_phase(args.debug_phase)
                current_key = phase_key(current_phase)
                print(f"trigger: button -> {current_key}")

            if args.auto_seconds > 0 and not active and now - last_animation_start >= args.auto_seconds:
                active = True
                started = now
                last_animation_start = now
                current_phase = configured_phase(args.debug_phase)
                current_key = phase_key(current_phase)
                print(f"trigger: auto -> {current_key}")

            # Refresh the displayed real phase periodically even when idle.
            if now - phase_refresh_at >= 900:
                current_phase = configured_phase(args.debug_phase)
                current_key = phase_key(current_phase)
                phase_refresh_at = now

            frame_interval = 1.0 / args.fps
            if now - last_frame_at >= frame_interval:
                if active:
                    frame, finished = build_animation_frame(now - started, current_phase, args.language, args.font)
                    # build_animation_frame uses English only for its final fallback;
                    # replace the final frame with the selected language.
                    if finished:
                        label_elapsed = now - started - OUTBOUND_SECONDS - RETURN_SECONDS - PARK_SECONDS
                        frame = final_frame(current_phase, label_elapsed, args.language, args.font, args.easter_egg)
                        active = False
                    show(args.host, frame)
                else:
                    # Redrawing this slowly keeps the stars gently twinkling without
                    # hammering the device while the app is idle.
                    show(args.host, final_frame(current_phase, now - started, args.language, args.font, args.easter_egg))
                last_frame_at = now

            time.sleep(0.008 if active else 0.08)

    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.URLError as exc:
        sys.exit(f"error: cannot reach {_base(args.host)} - {exc.reason}")
    finally:
        listener.stop()
        clear(args.host)
        delete_assets(args.host)


if __name__ == "__main__":
    main()
