#!/usr/bin/env python3
"""Matrix Digital Rain for BUSY Bar.

Classic green digital-rain animation for the 72x16 BUSY Bar display.
Each frame is rasterized locally and pushed as a single PNG image element,
which keeps animation smooth and avoids the display element-count limit.

    python3 matrix_digital_rain.py
    python3 matrix_digital_rain.py --host 127.0.0.1:8080
    python3 matrix_digital_rain.py --fps 12 --speed 1.2 --density 0.75
    python3 matrix_digital_rain.py --theme cyan
    python3 matrix_digital_rain.py --test

No external Python dependencies are required.
"""

import argparse
import json
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "matrix-digital-rain"
W, H = 72, 16

# Ultra-compact 2x3 pixel glyphs. At 72x16 this gives the rain enough
# columns and vertical character cells to look dense rather than oversized.
FONT = {
    "0": ["11", "11", "11"], "1": ["01", "01", "01"],
    "2": ["11", "01", "10"], "3": ["11", "01", "11"],
    "4": ["10", "11", "01"], "5": ["11", "10", "01"],
    "6": ["10", "11", "11"], "7": ["11", "01", "01"],
    "8": ["11", "11", "11"], "9": ["11", "11", "01"],
    "A": ["11", "11", "10"], "C": ["11", "10", "11"],
    "E": ["11", "11", "10"], "F": ["11", "11", "10"],
    "H": ["10", "11", "10"], "J": ["01", "01", "11"],
    "N": ["11", "11", "11"], "P": ["11", "11", "10"],
    "S": ["11", "10", "01"], "T": ["11", "01", "01"],
    "U": ["10", "10", "11"], "X": ["10", "01", "10"],
    "Y": ["10", "11", "01"], "Z": ["11", "01", "10"],
    "+": ["00", "11", "01"], "-": ["00", "11", "00"],
    "#": ["11", "11", "11"], "*": ["10", "01", "10"],
}

SMALL_FONT = FONT
BIG_FONT = {
    "0": ["111","101","101","101","111"], "1": ["010","110","010","010","111"],
    "2": ["111","001","111","100","111"], "3": ["111","001","111","001","111"],
    "4": ["101","101","111","001","001"], "5": ["111","100","111","001","111"],
    "6": ["111","100","111","101","111"], "7": ["111","001","010","010","010"],
    "8": ["111","101","111","101","111"], "9": ["111","101","111","001","111"],
    "A": ["010","101","111","101","101"], "C": ["111","100","100","100","111"],
    "E": ["111","100","110","100","111"], "F": ["111","100","110","100","100"],
    "H": ["101","101","111","101","101"], "J": ["001","001","001","101","111"],
    "N": ["101","111","111","111","101"], "P": ["110","101","110","100","100"],
    "S": ["111","100","111","001","111"], "T": ["111","010","010","010","010"],
    "U": ["101","101","101","101","111"], "X": ["101","101","010","101","101"],
    "Y": ["101","101","010","010","010"], "Z": ["111","001","010","100","111"],
    "+": ["000","010","111","010","000"], "-": ["000","000","111","000","000"],
    "#": ["101","111","101","111","101"], "*": ["000","101","010","101","000"],
}
FONT = SMALL_FONT
CHARS = list(SMALL_FONT)

THEMES = {
    "green": {
        "head": (220, 255, 220),
        "bright": (60, 255, 90),
        "mid": (0, 170, 45),
        "dim": (0, 70, 18),
    },
    "cyan": {
        "head": (220, 255, 255),
        "bright": (60, 255, 255),
        "mid": (0, 170, 190),
        "dim": (0, 60, 70),
    },
    "amber": {
        "head": (255, 245, 210),
        "bright": (255, 180, 35),
        "mid": (180, 90, 0),
        "dim": (70, 30, 0),
    },
}


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _png(pixels):
    """Encode a flat 72x16 RGB buffer as a minimal RGBA PNG using stdlib only."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        blob = tag + data
        return (struct.pack(">I", len(data)) + blob +
                struct.pack(">I", zlib.crc32(blob) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


_RING = 4
_frame_no = 0


def _post(host, path, data, content_type):
    req = urllib.request.Request(
        _base(host) + path,
        data=data,
        method="POST",
        headers={"Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.getcode()


def show(host, pixels):
    """Upload and draw one full-screen frame."""
    global _frame_no
    filename = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1

    try:
        _post(
            host,
            "/api/assets/upload?application_name=%s&file=%s" % (APP, filename),
            _png(pixels),
            "application/octet-stream",
        )
        body = {
            "application_name": APP,
            "elements": [
                {"id": "frame", "type": "image", "path": filename, "x": 0, "y": 0}
            ],
        }
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return 409
        raise


def clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.URLError:
        pass


def blank():
    return [(0, 0, 0)] * (W * H)


def px(buf, x, y, color):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = color


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw_glyph(buf, x, y, char, color, font):
    glyph = font.get(char)
    if not glyph:
        return
    for gy, row in enumerate(glyph):
        for gx, bit in enumerate(row):
            if bit == "1":
                px(buf, x + gx, y + gy, color)


class Drop:
    """One falling rain column rendered in a 4x6 logical character cell."""

    def __init__(self, col, density, speed, char_size):
        self.col = col
        self.density = density
        self.base_speed = speed
        self.font = SMALL_FONT if char_size == "small" else BIG_FONT
        self.cell_w = 3 if char_size == "small" else 4
        self.cell_h = 4 if char_size == "small" else 6
        self.reset(initial=True)

    def reset(self, initial=False):
        self.length = random.randint(2, 6)
        self.row = random.uniform(-8.0, 2.0 if initial else -1.0)
        self.speed = random.uniform(0.55, 1.35) * self.base_speed
        self.active = random.random() < self.density
        self.chars = [random.choice(CHARS) for _ in range(self.length + 2)]
        self.mutate_clock = random.randint(1, 4)

    def update(self, dt):
        if not self.active:
            if random.random() < min(1.0, dt * 0.18 * self.density):
                self.active = True
                self.row = random.uniform(-4.0, -1.0)
            return

        self.row += self.speed * dt * 7.5
        self.mutate_clock -= 1
        if self.mutate_clock <= 0:
            self.mutate_clock = random.randint(1, 4)
            if self.chars:
                self.chars[random.randrange(len(self.chars))] = random.choice(CHARS)

        if self.row - self.length > 4.0:
            self.reset()

    def draw(self, buf, theme):
        if not self.active:
            return

        x = self.col * self.cell_w
        head_row = int(self.row)

        for i in range(self.length):
            logical_row = head_row - i
            y = logical_row * self.cell_h - 1
            if y >= H or y + 3 < 0:
                continue

            if i == 0:
                color = theme["head"]
            elif i == 1:
                color = theme["bright"]
            else:
                t = (i - 2) / max(1, self.length - 2)
                color = mix(theme["mid"], theme["dim"], min(1.0, t))

            char = self.chars[i % len(self.chars)]
            draw_glyph(buf, x, y, char, color, self.font)


class MatrixRain:
    def __init__(self, density=0.75, speed=1.0, theme="green", char_size="small"):
        self.theme = THEMES[theme]
        cell_w = 3 if char_size == "small" else 4
        self.cols = W // cell_w
        self.drops = [Drop(c, density, speed, char_size) for c in range(self.cols)]

    def update(self, dt):
        for drop in self.drops:
            drop.update(dt)

    def frame(self):
        buf = blank()
        for drop in self.drops:
            drop.draw(buf, self.theme)
        return buf


def parse_args():
    p = argparse.ArgumentParser(description="Matrix Digital Rain for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20", help="BUSY Bar host (default: 10.0.4.20)")
    p.add_argument("--fps", type=float, default=12.0, help="frames per second (default: 12)")
    p.add_argument("--speed", type=float, default=1.0, help="fall speed multiplier (default: 1.0)")
    p.add_argument("--density", type=float, default=0.78, help="active column density 0..1 (default: 0.78)")
    p.add_argument("--theme", choices=sorted(THEMES), default="green", help="color theme")
    p.add_argument("--char-size", choices=["small", "big"], default="small",
                   help="character size: small (2x3) or big (3x5), default: small")
    p.add_argument("--seed", type=int, default=None, help="random seed for reproducible animation")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    return p.parse_args()


def main():
    args = parse_args()
    args.fps = max(1.0, min(30.0, args.fps))
    args.speed = max(0.1, min(4.0, args.speed))
    args.density = max(0.05, min(1.0, args.density))

    if args.seed is not None:
        random.seed(args.seed)

    rain = MatrixRain(args.density, args.speed, args.theme, args.char_size)

    if args.test:
        for _ in range(8):
            rain.update(0.10)
        status = show(args.host, rain.frame())
        print(f"test: drew one frame ({args.theme}) status {status}")
        return

    print(f"matrix-digital-rain -> {_base(args.host)}  (Ctrl-C to stop)")
    interval = 1.0 / args.fps
    last = time.monotonic()

    try:
        while True:
            start = time.monotonic()
            now = start
            dt = min(0.25, now - last)
            last = now

            rain.update(dt)
            show(args.host, rain.frame())

            elapsed = time.monotonic() - start
            if elapsed < interval:
                time.sleep(interval - elapsed)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as exc:
        sys.exit(f"error: HTTP {exc.code} - {exc.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as exc:
        sys.exit(f"error: cannot reach {_base(args.host)} - {exc.reason}")
    finally:
        clear(args.host)


if __name__ == "__main__":
    main()
