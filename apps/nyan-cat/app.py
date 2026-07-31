#!/usr/bin/env python3
"""Nyan cat: pop-tart cat with a rainbow trail and twinkling stars.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar

Each frame is rendered to a single 72x16 image and pushed as one image element.
The cat is ~65 rects; drawing them individually costs ~3.6 ms each (~4 fps on
real hardware), while one full-frame image is a flat ~50 ms, so it runs smooth.
"""
import json
import random
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

APP = "nyan-cat"
W, H = 72, 16

# ---------------------------------------------------------------------------
# BUSY Bar HTTP API — self-contained, stdlib only.
# Over USB the bar is always at 10.0.4.20; --host targets a Wi-Fi bar or the
# emulator. Full API docs are served by the device: http://10.0.4.20/docs
# ---------------------------------------------------------------------------

def _host(default="10.0.4.20"):
    if "--host" in sys.argv:
        i = sys.argv.index("--host")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

BASE = "http://" + _host().replace("http://", "").rstrip("/")


def _png(pixels):
    """72x16 flat list of (r, g, b) -> minimal RGBA PNG bytes (stdlib only)."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def _chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


# The device briefly locks an asset while a draw reads it; re-uploading the same
# name too soon returns HTTP 508, so rotate through a few filenames.
_RING = 4
_frame_no = 0


def _post(path, data, content_type):
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5):
        pass


def show(pixels):
    """Upload the frame PNG and draw it as one image."""
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    _post("/api/assets/upload?application_name=%s&file=%s" % (APP, fn),
          _png(pixels), "application/octet-stream")
    body = {"application_name": APP,
            "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}]}
    try:
        _post("/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = a higher-priority app owns the display
            raise


def _clear():
    req = urllib.request.Request(
        BASE + "/api/display/draw?application_name=" + APP, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.URLError:
        pass

# ---------------------------------------------------------------------------
# Palette and layout (colours as (r, g, b))
# ---------------------------------------------------------------------------

CRUST = (0xFF, 0xCC, 0x99)
FROSTING = (0xFF, 0x99, 0xFF)
SPRINKLE = (0xDD, 0x33, 0x88)
GRAY = (0x99, 0x99, 0x99)
BLACK = (0x00, 0x00, 0x00)
CHEEK = (0xFF, 0x99, 0x99)
STAR = (0xFF, 0xFF, 0xFF)
RAINBOW = [(0xFF, 0x00, 0x00), (0xFF, 0x99, 0x00), (0xFF, 0xFF, 0x00),
           (0x33, 0xFF, 0x00), (0x00, 0x99, 0xFF), (0x66, 0x33, 0xFF)]

CX, BY = 44, 3          # pop-tart body top-left
HX, HY = CX + 9, 5      # head top-left (overlaps the body's right side)
TRAIL_END = CX - 5      # rainbow stops before the tail so the gray tail reads


def _blank():
    return [(0, 0, 0)] * (W * H)


def _rect(buf, x, y, w, h, rgb):
    """Fill a solid 1-color rect into the pixel buffer, clipped to the display."""
    x2, y2 = min(W, x + w), min(H, y + h)
    x, y = max(0, x), max(0, y)
    for yy in range(y, y2):
        base = yy * W
        for xx in range(x, x2):
            buf[base + xx] = rgb


# ---------------------------------------------------------------------------
# Stars (drawn first: they twinkle behind the rainbow and the cat)
# ---------------------------------------------------------------------------

_stars = [{"x": 8, "y": 3, "p": 0}, {"x": 26, "y": 13, "p": 2},
          {"x": 46, "y": 1, "p": 1}, {"x": 66, "y": 11, "p": 3}]


def _tick_stars(buf):
    for s in _stars:
        s["x"] -= 3
        s["p"] = (s["p"] + 1) % 4
        if s["x"] < -2:
            s["x"] = W + random.randint(0, 10)
            s["y"] = random.randint(1, H - 2)
        x, y, p = s["x"], s["y"], s["p"]
        if p == 0:
            _rect(buf, x, y, 1, 1, STAR)
        elif p == 1:
            _rect(buf, x - 1, y, 3, 1, STAR)
            _rect(buf, x, y - 1, 1, 3, STAR)
        elif p == 2:
            _rect(buf, x - 2, y, 5, 1, STAR)
            _rect(buf, x, y - 2, 1, 5, STAR)
        else:
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                _rect(buf, x + dx, y + dy, 1, 1, STAR)


# ---------------------------------------------------------------------------
# Rainbow trail: 6 bands of 2px, wiggling in 8px blocks that alternate offset
# ---------------------------------------------------------------------------

def _rainbow(buf, phase):
    for band, color in enumerate(RAINBOW):
        y = 2 + band * 2
        x = 0
        while x < TRAIL_END:
            w = min(8, TRAIL_END - x)
            off = (x // 8 + phase) % 2
            _rect(buf, x, y + off, w, 2, color)
            x += w


# ---------------------------------------------------------------------------
# The cat: layered rects, later writes draw on top of earlier ones
# ---------------------------------------------------------------------------

def _cat(buf, phase):
    bob = phase                     # body bobs 1px down every other beat
    by, hy = BY + bob, HY + bob

    # tail (flips up/down against the bob)
    _rect(buf, CX - 2, by + 5, 2, 2, GRAY)
    if phase == 0:
        _rect(buf, CX - 4, by + 3, 2, 2, GRAY)
    else:
        _rect(buf, CX - 4, by + 7, 2, 2, GRAY)

    # legs stay planted; a 1px x-shuffle suggests the gallop
    for lx in (CX + 1, CX + 5, CX + 10, CX + 14):
        _rect(buf, lx + bob, 13, 2, 2, GRAY)

    # pop-tart body (inset top/bottom rows fake the rounded corners)
    _rect(buf, CX + 1, by, 12, 1, CRUST)
    _rect(buf, CX, by + 1, 14, 8, CRUST)
    _rect(buf, CX + 1, by + 9, 12, 1, CRUST)
    _rect(buf, CX + 1, by + 1, 12, 8, FROSTING)
    for sx, sy in ((2, 2), (6, 3), (3, 5), (7, 6), (5, 7)):
        _rect(buf, CX + sx, by + sy, 1, 1, SPRINKLE)

    # head + ears
    _rect(buf, HX + 1, hy, 8, 1, GRAY)
    _rect(buf, HX, hy + 1, 10, 6, GRAY)
    _rect(buf, HX + 1, hy + 7, 8, 1, GRAY)
    _rect(buf, HX + 1, hy - 2, 1, 1, GRAY)
    _rect(buf, HX + 1, hy - 1, 2, 1, GRAY)
    _rect(buf, HX + 8, hy - 2, 1, 1, GRAY)
    _rect(buf, HX + 7, hy - 1, 2, 1, GRAY)

    # face: eyes, cheeks, smile
    _rect(buf, HX + 2, hy + 2, 1, 1, BLACK)
    _rect(buf, HX + 7, hy + 2, 1, 1, BLACK)
    _rect(buf, HX + 1, hy + 4, 1, 1, CHEEK)
    _rect(buf, HX + 8, hy + 4, 1, 1, CHEEK)
    _rect(buf, HX + 2, hy + 4, 1, 1, BLACK)
    _rect(buf, HX + 6, hy + 4, 1, 1, BLACK)
    _rect(buf, HX + 3, hy + 5, 3, 1, BLACK)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_t = [0]
FRAME_T = 0.08  # ~12.5 fps; the image push itself takes ~50 ms


def tick():
    _t[0] += 1
    phase = (_t[0] // 3) % 2
    buf = _blank()
    _tick_stars(buf)
    _rainbow(buf, phase)
    _cat(buf, phase)
    show(buf)


if __name__ == "__main__":
    print(f"nyan_cat → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            t0 = time.monotonic()
            tick()
            dt = time.monotonic() - t0
            if dt < FRAME_T:
                time.sleep(FRAME_T - dt)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} — {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {BASE} — {e.reason}")
    finally:
        _clear()
