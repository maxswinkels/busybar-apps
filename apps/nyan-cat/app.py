#!/usr/bin/env python3
"""Nyan cat: pop-tart cat with a rainbow trail and twinkling stars.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
"""
import json
import random
import sys
import time
import urllib.error
import urllib.request

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

def draw(elements, **extra):
    body = {"application_name": APP, "elements": elements, **extra}
    req = urllib.request.Request(BASE + "/api/display/draw",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5):
        pass

def rectangle(x, y, width, height, **kw):
    return {"type": "rectangle", "x": x, "y": y, "width": width, "height": height, **kw}

# ---------------------------------------------------------------------------
# Palette and layout
# ---------------------------------------------------------------------------

CRUST = "0xFFCC99FF"
FROSTING = "0xFF99FFFF"
SPRINKLE = "0xDD3388FF"
GRAY = "0x999999FF"
BLACK = "0x000000FF"
CHEEK = "0xFF9999FF"
STAR = "0xFFFFFFFF"
RAINBOW = ["0xFF0000FF", "0xFF9900FF", "0xFFFF00FF", "0x33FF00FF",
           "0x0099FFFF", "0x6633FFFF"]

CX, BY = 44, 3          # pop-tart body top-left
HX, HY = CX + 9, 5      # head top-left (overlaps the body's right side)
TRAIL_END = CX - 5      # rainbow stops before the tail so the gray tail reads


def _rect(rects, x, y, w, h, color):
    """Append a solid 1-color rect clipped to the display bounds."""
    x2, y2 = min(W, x + w), min(H, y + h)
    x, y = max(0, x), max(0, y)
    if x >= x2 or y >= y2:
        return
    rects.append(rectangle(x=x, y=y, width=x2 - x, height=y2 - y,
                           border_width=0, fill="solid", fill_colors=[color]))


# ---------------------------------------------------------------------------
# Stars (drawn first: they twinkle behind the rainbow and the cat)
# ---------------------------------------------------------------------------

_stars = [{"x": 8, "y": 3, "p": 0}, {"x": 26, "y": 13, "p": 2},
          {"x": 46, "y": 1, "p": 1}, {"x": 66, "y": 11, "p": 3}]


def _tick_stars(rects):
    for s in _stars:
        s["x"] -= 3
        s["p"] = (s["p"] + 1) % 4
        if s["x"] < -2:
            s["x"] = W + random.randint(0, 10)
            s["y"] = random.randint(1, H - 2)
        x, y, p = s["x"], s["y"], s["p"]
        if p == 0:
            _rect(rects, x, y, 1, 1, STAR)
        elif p == 1:
            _rect(rects, x - 1, y, 3, 1, STAR)
            _rect(rects, x, y - 1, 1, 3, STAR)
        elif p == 2:
            _rect(rects, x - 2, y, 5, 1, STAR)
            _rect(rects, x, y - 2, 1, 5, STAR)
        else:
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                _rect(rects, x + dx, y + dy, 1, 1, STAR)


# ---------------------------------------------------------------------------
# Rainbow trail: 6 bands of 2px, wiggling in 8px blocks that alternate offset
# ---------------------------------------------------------------------------

def _rainbow(rects, phase):
    for band, color in enumerate(RAINBOW):
        y = 2 + band * 2
        x = 0
        while x < TRAIL_END:
            w = min(8, TRAIL_END - x)
            off = (x // 8 + phase) % 2
            _rect(rects, x, y + off, w, 2, color)
            x += w


# ---------------------------------------------------------------------------
# The cat: layered rects, later elements draw on top of earlier ones
# ---------------------------------------------------------------------------

def _cat(rects, phase):
    bob = phase                     # body bobs 1px down every other beat
    by, hy = BY + bob, HY + bob

    # tail (flips up/down against the bob)
    _rect(rects, CX - 2, by + 5, 2, 2, GRAY)
    if phase == 0:
        _rect(rects, CX - 4, by + 3, 2, 2, GRAY)
    else:
        _rect(rects, CX - 4, by + 7, 2, 2, GRAY)

    # legs stay planted; a 1px x-shuffle suggests the gallop
    for lx in (CX + 1, CX + 5, CX + 10, CX + 14):
        _rect(rects, lx + bob, 13, 2, 2, GRAY)

    # pop-tart body (inset top/bottom rows fake the rounded corners)
    _rect(rects, CX + 1, by, 12, 1, CRUST)
    _rect(rects, CX, by + 1, 14, 8, CRUST)
    _rect(rects, CX + 1, by + 9, 12, 1, CRUST)
    _rect(rects, CX + 1, by + 1, 12, 8, FROSTING)
    for sx, sy in ((2, 2), (6, 3), (3, 5), (7, 6), (5, 7)):
        _rect(rects, CX + sx, by + sy, 1, 1, SPRINKLE)

    # head + ears
    _rect(rects, HX + 1, hy, 8, 1, GRAY)
    _rect(rects, HX, hy + 1, 10, 6, GRAY)
    _rect(rects, HX + 1, hy + 7, 8, 1, GRAY)
    _rect(rects, HX + 1, hy - 2, 1, 1, GRAY)
    _rect(rects, HX + 1, hy - 1, 2, 1, GRAY)
    _rect(rects, HX + 8, hy - 2, 1, 1, GRAY)
    _rect(rects, HX + 7, hy - 1, 2, 1, GRAY)

    # face: eyes, cheeks, smile
    _rect(rects, HX + 2, hy + 2, 1, 1, BLACK)
    _rect(rects, HX + 7, hy + 2, 1, 1, BLACK)
    _rect(rects, HX + 1, hy + 4, 1, 1, CHEEK)
    _rect(rects, HX + 8, hy + 4, 1, 1, CHEEK)
    _rect(rects, HX + 2, hy + 4, 1, 1, BLACK)
    _rect(rects, HX + 6, hy + 4, 1, 1, BLACK)
    _rect(rects, HX + 3, hy + 5, 3, 1, BLACK)


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------

_t = [0]


def tick():
    _t[0] += 1
    phase = (_t[0] // 3) % 2
    rects = []
    _tick_stars(rects)
    _rainbow(rects, phase)
    _cat(rects, phase)
    try:
        draw(rects)
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = a higher-priority app owns the display
            raise


if __name__ == "__main__":
    print(f"nyan_cat → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            tick()
            time.sleep(0.08)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} — {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {BASE} — {e.reason}")
