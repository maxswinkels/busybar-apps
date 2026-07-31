#!/usr/bin/env python3
"""Demoscene pixel effects: fire, matrix rain, plasma.

    python app.py [fire|rain|plasma]     # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar

Each frame is rendered to a single 72x16 image and pushed to the bar as one
image element. Drawing hundreds of individual rectangles costs ~3.6 ms each on
the device, so a full frame of rects only manages 3-6 fps; a full-frame image
is a flat ~50 ms regardless of how busy the picture is, so every effect runs
smoothly at ~18 fps.
"""
import json
import math
import random
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

APP = "pixel-fire"
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


def _post(path, data, content_type):
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5):
        pass


def _clear():
    req = urllib.request.Request(
        BASE + "/api/display/draw?application_name=" + APP, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.URLError:
        pass


def _png(pixels):
    """72x16 flat list of (r, g, b) -> minimal RGBA PNG bytes (stdlib only)."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)  # filter type 0 (none) per scanline
        for x in range(W):
            r, g, b = pixels[y * W + x]
            raw += bytes((r, g, b, 255))

    def _chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


# The bar renders one uploaded image far faster than many rect elements, but it
# briefly locks an asset while a draw reads it. Uploading over that same name
# too soon returns HTTP 508, so we rotate through a few filenames.
_RING = 4
_frame = 0


def show(pixels):
    """Push one full-screen frame: upload the PNG, draw it as one image."""
    global _frame
    fn = "frame%d.png" % (_frame % _RING)
    _frame += 1
    _post("/api/assets/upload?application_name=%s&file=%s" % (APP, fn),
          _png(pixels), "application/octet-stream")
    body = {"application_name": APP,
            "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}]}
    try:
        _post("/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = a higher-priority app owns the display
            raise


def _flatten(buf, palette):
    """2D index buffer + rgb palette -> flat list of (r, g, b), row-major."""
    px = []
    for y in range(H):
        row = buf[y]
        for x in range(W):
            px.append(palette[row[x]])
    return px

# ---------------------------------------------------------------------------
# Palettes (r, g, b). Index 0 is the background (off / black) for fire and rain;
# plasma fills the whole frame so it indexes its palette directly.
# ---------------------------------------------------------------------------

FIRE_PALETTE = [
    (0x00, 0x00, 0x00),  # 0 background (off)
    (0x3C, 0x00, 0x00),  # 1 near-black red
    (0x82, 0x10, 0x00),  # 2 dark red
    (0xC8, 0x32, 0x00),  # 3 red
    (0xFF, 0x64, 0x00),  # 4 orange-red
    (0xFF, 0xA0, 0x28),  # 5 orange
    (0xFF, 0xE0, 0x60),  # 6 yellow
    (0xFF, 0xF8, 0xC8),  # 7 near-white
]

PLASMA_PALETTE = [
    (0x0A, 0x00, 0x50),  # 0 deep blue
    (0x3A, 0x00, 0x80),  # 1 indigo
    (0x70, 0x00, 0xA0),  # 2 purple
    (0xB0, 0x00, 0x6A),  # 3 magenta
    (0xD0, 0x40, 0x00),  # 4 orange-red
    (0xE0, 0x80, 0x00),  # 5 amber
    (0xFF, 0xD0, 0x00),  # 6 yellow
]

RAIN_HEAD = (0x66, 0xCC, 0xFF)
RAIN_TRAIL = [(0x33, 0x88, 0xEE), (0x22, 0x55, 0xBB), (0x11, 0x2E, 0x66)]

# ---------------------------------------------------------------------------
# Effect selection
# ---------------------------------------------------------------------------

def _effect_arg():
    valid = {"fire", "rain", "plasma"}
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--host":
            skip_next = True
            continue
        if a in valid:
            return a
    return "fire"

EFFECT = _effect_arg()

# ---------------------------------------------------------------------------
# Fire effect
# ---------------------------------------------------------------------------

_heat = [[0] * W for _ in range(H + 1)]  # row H = heat source
_heat_prev = [[0] * W for _ in range(H)]  # temporal blend buffer


def _tick_fire():
    # Reseed the source row fully each frame: pure random flicker
    for x in range(W):
        _heat[H][x] = random.randint(120, 235)

    # Propagate upward (row H-1 down to 0)
    new_heat = [[0] * W for _ in range(H)]
    for y in range(H - 1, -1, -1):
        for x in range(W):
            xl = max(0, x - 1)
            xr = min(W - 1, x + 1)
            avg = (_heat[y + 1][xl] + _heat[y + 1][x] + _heat[y + 1][xr]
                   + (_heat[y + 1][x] if y + 2 > H else _heat[min(H, y + 2)][x])) // 4
            decay = random.randint(4, 24)
            new_heat[y][x] = max(0, avg - decay)

    # Temporal blend: 66% old, 34% new → slows the flicker without waves
    for y in range(H):
        for x in range(W):
            _heat[y][x] = (2 * _heat_prev[y][x] + new_heat[y][x]) // 3
        # Update prev for next frame
        _heat_prev[y] = list(_heat[y])

    # Map heat → palette index (7 steps + 0=black). No horizontal smoothing is
    # needed now that each frame is one image (rect count no longer matters).
    n = len(FIRE_PALETTE) - 1  # 7
    buf = [[0] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            h = _heat[y][x]
            buf[y][x] = 0 if h < 24 else max(1, min(n, 1 + (h * n) // 256))

    return _flatten(buf, FIRE_PALETTE)

# ---------------------------------------------------------------------------
# Rain (matrix) effect
# ---------------------------------------------------------------------------

_drops = []


def _init_rain():
    global _drops
    _drops = []
    for _ in range(20):
        _drops.append({
            "col": random.randint(0, W - 1),
            "y": random.randint(-4, H - 1),
            "speed": random.uniform(0.6, 1.4),
        })


def _tick_rain():
    # per-cell index: 0=off, 1..3 = trail (dim→bright), 4 = head
    palette = [(0x00, 0x00, 0x00)] + RAIN_TRAIL + [RAIN_HEAD]
    TRAIL_LEN = len(RAIN_TRAIL)
    HEAD_IDX = len(palette) - 1

    buf = [[0] * W for _ in range(H)]
    for drop in _drops:
        drop["y"] += drop["speed"]
        hy = int(drop["y"])
        cx = drop["col"]
        if 0 <= hy < H:
            buf[hy][cx] = HEAD_IDX
        for t, color_idx in enumerate(range(1, HEAD_IDX)):
            ty = hy - 1 - t
            if 0 <= ty < H:
                buf[ty][cx] = color_idx
        # respawn once fully off the bottom
        if hy - TRAIL_LEN > H:
            drop["col"] = random.randint(0, W - 1)
            drop["y"] = random.uniform(-6, -1)
            drop["speed"] = random.uniform(0.6, 1.4)

    return _flatten(buf, palette)

# ---------------------------------------------------------------------------
# Plasma effect
# ---------------------------------------------------------------------------

_t = 0.0


def _tick_plasma():
    global _t
    _t += 0.12
    n = len(PLASMA_PALETTE)

    buf = [[0] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            v = (math.sin(x / 6.0 + _t)
                 + math.sin(y / 4.0 - 1.3 * _t)
                 + math.sin((x + y) / 8.0 + 0.7 * _t))
            # v ∈ [-3, 3] → normalize → [0, n-1]
            idx = int((v + 3.0) / 6.0 * (n - 1) + 0.5)
            buf[y][x] = max(0, min(n - 1, idx))

    return _flatten(buf, PLASMA_PALETTE)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_TICKS = {"fire": _tick_fire, "rain": _tick_rain, "plasma": _tick_plasma}
FRAME_T = 1.0 / 20.0  # cap; the image push itself paces us to ~18 fps


def main():
    if EFFECT == "rain":
        _init_rain()
    tick = _TICKS[EFFECT]
    print(f"pixel_fire [{EFFECT}] → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            t0 = time.monotonic()
            show(tick())
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


if __name__ == "__main__":
    main()
