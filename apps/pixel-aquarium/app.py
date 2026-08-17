#!/usr/bin/env python3
"""Pixel Aquarium: a tiny procedural aquarium for the BUSY Bar.

Small pixel fish swim across the 72x16 display while bubbles rise and seaweed
sways near the bottom. A slow day/night cycle changes the water palette and
adds stars/moonlight at night.

    python3 app.py                         # BUSY Bar over USB (10.0.4.20)
    python3 app.py --host 127.0.0.1:8080  # emulator or Wi-Fi bar
    python3 app.py --fish 5 --fps 12
    python3 app.py --cycle-seconds 120    # full day/night cycle duration
    python3 app.py --no-cycle             # keep permanent daytime palette

Stdlib only. Each frame is rasterised to one 72x16 PNG and pushed as a single
image element, which is much smoother than sending many rectangle elements.
"""

import argparse
import json
import math
import random
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "pixel-aquarium"
W, H = 72, 16

# ---------------------------------------------------------------------------
# BUSY Bar HTTP helpers
# ---------------------------------------------------------------------------


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _png(pixels):
    """Flat list of RGB tuples -> minimal 72x16 RGBA PNG bytes."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


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
    """Upload and draw one full-frame image. Returns HTTP status."""
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
        return exc.code
    except urllib.error.URLError as exc:
        raise RuntimeError(f"push failed: {exc}") from exc


def _clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0


# ---------------------------------------------------------------------------
# Pixel primitives
# ---------------------------------------------------------------------------


def _blank(rgb):
    return [rgb] * (W * H)


def _px(buf, x, y, rgb):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = rgb


def _rect(buf, x, y, w, h, rgb):
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(W, x + w)
    y1 = min(H, y + h)
    for yy in range(y0, y1):
        base = yy * W
        for xx in range(x0, x1):
            buf[base + xx] = rgb


def _blend(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ---------------------------------------------------------------------------
# Palette / day-night cycle
# ---------------------------------------------------------------------------

DAY_TOP = (3, 50, 92)
DAY_BOTTOM = (0, 126, 145)
NIGHT_TOP = (0, 5, 28)
NIGHT_BOTTOM = (0, 33, 58)
SAND_DAY = (111, 84, 47)
SAND_NIGHT = (48, 43, 43)
PLANT_DAY = (20, 163, 82)
PLANT_NIGHT = (9, 72, 48)
BUBBLE_DAY = (166, 242, 255)
BUBBLE_NIGHT = (72, 143, 174)
MOON = (226, 235, 206)
STAR = (184, 213, 229)

FISH_PALETTES = [
    ((255, 125, 40), (255, 210, 61)),
    ((255, 71, 94), (255, 164, 184)),
    ((71, 210, 255), (146, 244, 255)),
    ((172, 99, 255), (223, 180, 255)),
    ((75, 231, 118), (168, 255, 147)),
    ((255, 207, 63), (255, 118, 34)),
]


def _daylight(t, cycle_seconds, no_cycle):
    """Return daylight 0..1. Starts in daytime and eases through night."""
    if no_cycle:
        return 1.0
    phase = (t / max(10.0, cycle_seconds)) % 1.0
    # cosine: phase 0 = noon, .5 = midnight, 1 = noon
    return 0.5 + 0.5 * math.cos(phase * math.tau)


def _background(daylight):
    top = _blend(NIGHT_TOP, DAY_TOP, daylight)
    bottom = _blend(NIGHT_BOTTOM, DAY_BOTTOM, daylight)
    buf = _blank(top)
    for y in range(H):
        # Leave lower rows a little darker so fish silhouettes remain readable.
        k = y / (H - 1)
        row = _blend(top, bottom, k)
        _rect(buf, 0, y, W, 1, row)
    return buf


# ---------------------------------------------------------------------------
# Procedural aquarium entities
# ---------------------------------------------------------------------------


class Fish:
    def __init__(self, rng, force_offscreen=False):
        self.rng = rng
        self.dir = rng.choice((-1, 1))
        self.speed = rng.uniform(4.5, 10.0)
        self.size = rng.choice((1, 1, 1, 2))
        self.y = rng.randint(2, 10 if self.size == 1 else 8)
        self.phase = rng.random() * math.tau
        self.palette = rng.randrange(len(FISH_PALETTES))
        self.variant = rng.randrange(3)
        margin = rng.randint(3, 22)
        if force_offscreen:
            self.x = -margin if self.dir > 0 else W + margin
        else:
            self.x = rng.uniform(0, W - 1)

    @property
    def width(self):
        return 6 if self.size == 1 else 9

    def reset(self):
        self.dir = self.rng.choice((-1, 1))
        self.speed = self.rng.uniform(4.5, 10.0)
        self.size = self.rng.choice((1, 1, 1, 2))
        self.y = self.rng.randint(2, 10 if self.size == 1 else 8)
        self.phase = self.rng.random() * math.tau
        self.palette = self.rng.randrange(len(FISH_PALETTES))
        self.variant = self.rng.randrange(3)
        self.x = -self.width - self.rng.randint(1, 18) if self.dir > 0 else W + self.rng.randint(1, 18)

    def update(self, dt, t):
        self.x += self.dir * self.speed * dt
        # Very small vertical drift to keep the school organic without jitter.
        self.phase += dt * 1.3
        if self.dir > 0 and self.x > W + self.width + 2:
            self.reset()
        elif self.dir < 0 and self.x < -self.width - 2:
            self.reset()

    def draw(self, buf, daylight, t):
        x = int(round(self.x))
        y = self.y + int(math.sin(self.phase) * 0.65)
        base, accent = FISH_PALETTES[self.palette]
        base = _blend(tuple(c // 3 for c in base), base, 0.35 + 0.65 * daylight)
        accent = _blend(tuple(c // 3 for c in accent), accent, 0.35 + 0.65 * daylight)
        eye = (6, 10, 14)

        if self.size == 1:
            # 6x4 fish. Body occupies 4x3, with a 2px triangular tail.
            if self.dir > 0:
                _px(buf, x, y + 1, accent)
                _px(buf, x, y + 2, accent)
                _px(buf, x + 1, y, base)
                _rect(buf, x + 1, y + 1, 4, 2, base)
                _px(buf, x + 1, y + 3, base)
                _px(buf, x + 4, y, base)
                _px(buf, x + 4, y + 3, base)
                _px(buf, x + 4, y + 1, eye)
                if self.variant == 1:
                    _px(buf, x + 2, y + 1, accent)
                elif self.variant == 2:
                    _px(buf, x + 3, y + 2, accent)
            else:
                _px(buf, x + 5, y + 1, accent)
                _px(buf, x + 5, y + 2, accent)
                _px(buf, x + 4, y, base)
                _rect(buf, x + 1, y + 1, 4, 2, base)
                _px(buf, x + 4, y + 3, base)
                _px(buf, x + 1, y, base)
                _px(buf, x + 1, y + 3, base)
                _px(buf, x + 1, y + 1, eye)
                if self.variant == 1:
                    _px(buf, x + 3, y + 1, accent)
                elif self.variant == 2:
                    _px(buf, x + 2, y + 2, accent)
        else:
            # 9x6 fish, rarer and slower-looking due to its larger silhouette.
            yy = y
            if self.dir > 0:
                _px(buf, x, yy + 2, accent)
                _px(buf, x, yy + 3, accent)
                _px(buf, x + 1, yy + 1, accent)
                _px(buf, x + 1, yy + 4, accent)
                _rect(buf, x + 2, yy + 1, 5, 4, base)
                _rect(buf, x + 3, yy, 3, 1, base)
                _rect(buf, x + 3, yy + 5, 3, 1, base)
                _px(buf, x + 6, yy + 2, eye)
                _px(buf, x + 4, yy + 2 + self.variant % 2, accent)
            else:
                _px(buf, x + 8, yy + 2, accent)
                _px(buf, x + 8, yy + 3, accent)
                _px(buf, x + 7, yy + 1, accent)
                _px(buf, x + 7, yy + 4, accent)
                _rect(buf, x + 2, yy + 1, 5, 4, base)
                _rect(buf, x + 3, yy, 3, 1, base)
                _rect(buf, x + 3, yy + 5, 3, 1, base)
                _px(buf, x + 2, yy + 2, eye)
                _px(buf, x + 4, yy + 2 + self.variant % 2, accent)


class Bubble:
    def __init__(self, rng, initial=False):
        self.rng = rng
        self.reset(initial)

    def reset(self, initial=False):
        self.x = self.rng.randint(1, W - 2)
        self.y = self.rng.uniform(0, H - 2) if initial else self.rng.uniform(H - 2, H + 8)
        self.speed = self.rng.uniform(3.0, 7.0)
        self.phase = self.rng.random() * math.tau
        self.big = self.rng.random() < 0.18

    def update(self, dt):
        self.y -= self.speed * dt
        self.phase += dt * 2.4
        if self.y < -2:
            self.reset(False)

    def draw(self, buf, daylight):
        x = int(round(self.x + math.sin(self.phase) * 0.8))
        y = int(round(self.y))
        c = _blend(BUBBLE_NIGHT, BUBBLE_DAY, daylight)
        if self.big:
            _px(buf, x, y, c)
            _px(buf, x + 1, y, c)
            _px(buf, x, y + 1, c)
        else:
            _px(buf, x, y, c)


class Plant:
    def __init__(self, rng, x):
        self.x = x
        self.height = rng.randint(3, 7)
        self.phase = rng.random() * math.tau
        self.speed = rng.uniform(0.7, 1.4)
        self.branch = rng.choice((-1, 1))

    def draw(self, buf, t, daylight):
        col = _blend(PLANT_NIGHT, PLANT_DAY, daylight)
        dark = _blend((2, 26, 21), (11, 101, 57), daylight)
        base_y = H - 2
        for i in range(self.height):
            yy = base_y - i
            sway = int(round(math.sin(t * self.speed + self.phase + i * 0.45) * min(1.2, i * 0.18)))
            xx = self.x + sway
            _px(buf, xx, yy, col if i % 2 else dark)
            if i >= 2 and i % 2 == 0:
                _px(buf, xx + self.branch, yy, col)


class Aquarium:
    def __init__(self, fish_count, bubble_count, seed):
        self.rng = random.Random(seed)
        self.fish = [Fish(self.rng, force_offscreen=(i >= fish_count // 2)) for i in range(fish_count)]
        self.bubbles = [Bubble(self.rng, initial=True) for _ in range(bubble_count)]
        plant_x = [2, 8, 14, 22, 31, 40, 50, 59, 67]
        self.plants = [Plant(self.rng, x + self.rng.randint(-1, 1)) for x in plant_x]
        self.stars = [(self.rng.randrange(W), self.rng.randrange(1, 8), self.rng.random() * math.tau) for _ in range(10)]

    def update(self, dt, t):
        for fish in self.fish:
            fish.update(dt, t)
        for bubble in self.bubbles:
            bubble.update(dt)

    def draw(self, t, daylight, night_sky=True):
        buf = _background(daylight)

        # Night details only become visible once the palette is sufficiently dark.
        night = 1.0 - daylight
        if night_sky and night > 0.42:
            star_col = _blend((20, 45, 65), STAR, min(1.0, (night - 0.42) / 0.58))
            for x, y, phase in self.stars:
                if math.sin(t * 2.0 + phase) > 0.15:
                    _px(buf, x, y, star_col)
            # Tiny crescent near the upper-right edge.
            moon_col = _blend((40, 58, 66), MOON, min(1.0, night))
            _px(buf, 65, 2, moon_col)
            _px(buf, 64, 3, moon_col)
            _px(buf, 65, 3, moon_col)
            _px(buf, 64, 4, moon_col)

        # A faint moving caustic line near the surface in daytime.
        if daylight > 0.25:
            caustic = _blend((6, 44, 61), (70, 196, 203), daylight * 0.7)
            for x in range(0, W, 7):
                yy = 1 + ((x // 7 + int(t * 2)) % 2)
                _px(buf, x, yy, caustic)
                if x + 1 < W:
                    _px(buf, x + 1, yy, caustic)

        # Sand/gravel floor.
        sand = _blend(SAND_NIGHT, SAND_DAY, daylight)
        sand_dark = _blend((20, 24, 29), (70, 54, 34), daylight)
        _rect(buf, 0, 14, W, 2, sand)
        for x in range(0, W, 5):
            _px(buf, x, 14 + ((x // 5) % 2), sand_dark)

        for plant in self.plants:
            plant.draw(buf, t, daylight)
        for bubble in self.bubbles:
            bubble.draw(buf, daylight)
        for fish in self.fish:
            fish.draw(buf, daylight, t)

        return buf


# ---------------------------------------------------------------------------
# CLI / main loop
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(description="Procedural pixel aquarium for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20", help="BUSY Bar host (default: 10.0.4.20)")
    p.add_argument("--fps", type=float, default=12.0, help="frames per second (default: 12)")
    p.add_argument("--fish", type=int, default=6, help="number of fish, 1-12 (default: 6)")
    p.add_argument("--bubbles", type=int, default=10, help="number of bubbles, 0-30 (default: 10)")
    p.add_argument(
        "--cycle-seconds",
        type=float,
        default=180.0,
        help="seconds for a complete day/night cycle (default: 180)",
    )
    p.add_argument("--no-cycle", action="store_true", help="disable day/night cycle and keep daytime")
    p.add_argument("--seed", type=int, default=None, help="fixed random seed for a repeatable aquarium")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    args = p.parse_args()
    args.fps = max(1.0, min(20.0, args.fps))
    args.fish = max(1, min(12, args.fish))
    args.bubbles = max(0, min(30, args.bubbles))
    args.cycle_seconds = max(10.0, args.cycle_seconds)
    return args


def main():
    args = parse_args()
    aquarium = Aquarium(args.fish, args.bubbles, args.seed)

    if args.test:
        pixels = aquarium.draw(0.0, 1.0)
        status = show(args.host, pixels)
        print(f"test: drew one aquarium frame (status {status})")
        return

    print(f"pixel-aquarium -> {_base(args.host)}  (Ctrl-C to stop)")
    frame_interval = 1.0 / args.fps
    start = time.monotonic()
    prev = start

    try:
        while True:
            frame_start = time.monotonic()
            t = frame_start - start
            dt = min(0.25, frame_start - prev)
            prev = frame_start

            aquarium.update(dt, t)
            daylight = _daylight(t, args.cycle_seconds, args.no_cycle)
            pixels = aquarium.draw(t, daylight)
            status = show(args.host, pixels)
            if status not in (200, 201, 204, 409):
                print(f"draw returned HTTP {status}")

            elapsed = time.monotonic() - frame_start
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
    except KeyboardInterrupt:
        print("\nstopped.")
    except RuntimeError as exc:
        sys.exit(f"error: {exc}")
    finally:
        _clear(args.host)


if __name__ == "__main__":
    main()
