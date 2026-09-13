#!/usr/bin/env python3
"""DVD Bounce: a color-changing screensaver for the BUSY Bar's 72x16 LEDs.

    python3 app.py --host 127.0.0.1:8321
    python3 app.py --host 127.0.0.1:8080 --speed 8 --corner-flash

Standard library only. The manager discovers controls through --help.
"""

import argparse
import json
import math
import random
import signal
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "dvd-bounce"
W, H = 72, 16
BLACK = (0, 0, 0)
SPRITES = {
    "dvd": (
        "####..#...#.####.",
        "#...#.#...#.#...#",
        "#...#.#...#.#...#",
        "#...#..#.#..#...#",
        "####....#...####.",
        ".................",
        "..#####...#####..",
        "....#########....",
    ),
    "ball": (
        ".###.",
        "#####",
        "#####",
        "#####",
        ".###.",
    ),
}
PALETTES = {
    "classic": ((0, 170, 255), (255, 60, 170), (255, 220, 0),
                (60, 255, 90), (180, 80, 255), (255, 100, 30)),
    "neon": ((0, 255, 255), (255, 0, 180), (160, 255, 0), (160, 60, 255)),
    "pastel": ((130, 200, 255), (255, 150, 190), (255, 220, 140),
               (160, 240, 180), (200, 170, 255)),
}


class Bounce:
    """Reflect velocity at each wall, preserving speed and leftover travel time."""

    def __init__(self, speed=10, angle=45, logo="dvd", palette="classic",
                 brightness=80, seed=None, corner_flash=False):
        self.sprite = SPRITES[logo]
        self.max_x = W - len(self.sprite[0])
        self.max_y = H - len(self.sprite)
        self.rng = random.Random(seed)
        # Integer starts + the default 45-degree path allow real corner hits.
        self.x = float(self.rng.randrange(self.max_x + 1))
        self.y = float(self.rng.randrange(self.max_y + 1))
        radians = math.radians(angle)
        self.vx = speed * math.cos(radians) * self.rng.choice((-1, 1))
        self.vy = speed * math.sin(radians) * self.rng.choice((-1, 1))
        self.palette = PALETTES[palette]
        self.color_index = self.rng.randrange(len(self.palette))
        self.brightness = brightness / 100.0
        self.corner_flash = corner_flash
        self.flash_remaining = 0.0
        self.bounces = 0
        self.corners = 0

    def update(self, dt):
        if not math.isfinite(dt) or dt < 0:
            raise ValueError("elapsed time must be finite and nonnegative")
        remaining = dt
        while remaining > 0:
            tx = ((self.max_x if self.vx > 0 else 0) - self.x) / self.vx
            ty = ((self.max_y if self.vy > 0 else 0) - self.y) / self.vy
            collision = max(0.0, min(tx, ty))
            step = min(remaining, collision)
            self.x += self.vx * step
            self.y += self.vy * step
            self.flash_remaining = max(0.0, self.flash_remaining - step)
            if collision > remaining:
                break
            remaining -= step
            hit_x = math.isclose(tx, collision, rel_tol=0, abs_tol=1e-9)
            hit_y = math.isclose(ty, collision, rel_tol=0, abs_tol=1e-9)
            if hit_x:
                self.x = float(self.max_x if self.vx > 0 else 0)
                self.vx = -self.vx
            if hit_y:
                self.y = float(self.max_y if self.vy > 0 else 0)
                self.vy = -self.vy
            self.bounces += 1
            # Every impact gets a different color; a corner is one event.
            self.color_index = (self.color_index + self.rng.randrange(
                1, len(self.palette))) % len(self.palette)
            if hit_x and hit_y:
                self.corners += 1
                if self.corner_flash:
                    self.flash_remaining = 0.5

    def pixels(self):
        color = ((255, 255, 255) if self.flash_remaining > 0
                 else self.palette[self.color_index])
        color = tuple(round(c * self.brightness) for c in color)
        pixels = [BLACK] * (W * H)
        x = max(0, min(self.max_x, round(self.x)))
        y = max(0, min(self.max_y, round(self.y)))
        for sy, row in enumerate(self.sprite):
            for sx, pixel in enumerate(row):
                if pixel == "#":
                    pixels[(y + sy) * W + x + sx] = color
        return pixels


def png(pixels):
    """Encode one opaque RGBA frame without a Pillow dependency."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for r, g, b in pixels[y * W:(y + 1) * W]:
            raw.extend((r, g, b, 255))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b""))


class Display:
    def __init__(self, host):
        self.base = (host if "://" in host else "http://" + host).rstrip("/")
        self.frame = 0

    def request(self, path, data=None, content_type="application/json", method="POST"):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=2) as response:
            return response.status

    def show(self, pixels):
        # Rotate assets: firmware briefly locks an image while rendering it.
        filename = "frame%d.png" % (self.frame % 4)
        self.frame += 1
        query = urllib.parse.urlencode({"application_name": APP, "file": filename})
        self.request("/api/assets/upload?" + query, png(pixels), "application/octet-stream")
        body = {"application_name": APP, "priority": 10,
                "elements": [{"id": "frame", "type": "image", "path": filename,
                              "x": 0, "y": 0}]}
        try:
            return self.request("/api/display/draw", json.dumps(body).encode())
        except urllib.error.HTTPError as exc:
            if exc.code == 409:  # A higher-priority app owns the screen.
                return 409
            raise

    def clear(self):
        try:
            self.request("/api/display/draw?application_name=" + APP, method="DELETE")
        except (urllib.error.URLError, OSError):
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Bouncing DVD screensaver for BUSY Bar")
    parser.add_argument("--host", default="10.0.4.20", help="bar or manager host:port")
    parser.add_argument("--speed", type=float, default=10, metavar="2.0-40.0",
                        help="travel speed in pixels/second (default: 10)")
    parser.add_argument("--angle", type=float, default=45, metavar="5.0-85.0",
                        help="diagonal angle in degrees (default: 45)")
    parser.add_argument("--fps", type=int, default=17, metavar="1-20",
                        help="frames per second (default: 17)")
    parser.add_argument("--logo", choices=["dvd", "ball"], default="dvd",
                        help="bouncing sprite (default: dvd)")
    parser.add_argument("--palette", choices=["classic", "neon", "pastel"], default="classic",
                        help="colors used at each bounce (default: classic)")
    parser.add_argument("--brightness", type=int, default=80, metavar="1-100",
                        help="logo brightness percentage (default: 80)")
    parser.add_argument("--corner-flash", action="store_true",
                        help="flash the logo white for a perfect corner hit")
    parser.add_argument("--seed", type=int, default=None,
                        help="optional seed to repeat a path and its colors")
    parser.add_argument("--test", action="store_true", help="draw one frame and exit")
    args = parser.parse_args(argv)
    for name, low, high in (("speed", 2, 40), ("angle", 5, 85),
                           ("fps", 1, 20), ("brightness", 1, 100)):
        value = getattr(args, name)
        if not math.isfinite(value) or not low <= value <= high:
            parser.error("--%s must be between %s and %s" % (name, low, high))
    return args


def main(argv=None):
    args = parse_args(argv)
    bounce = Bounce(args.speed, args.angle, args.logo, args.palette,
                    args.brightness, args.seed, args.corner_flash)
    display = Display(args.host)
    if args.test:
        # Leave the single test frame visible, like the other animation apps.
        print("test: drew one DVD Bounce frame (status %s)" % display.show(bounce.pixels()))
        return

    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    print("DVD Bounce -> %s | %s, %g px/s, %g degrees" %
          (display.base, args.logo, args.speed, args.angle), flush=True)
    previous = time.monotonic()
    last_warning = -math.inf
    try:
        while not stopped.is_set():
            started = time.monotonic()
            corners = bounce.corners
            bounce.update(started - previous)
            previous = started
            if bounce.corners > corners:
                print("Corner hit! Total: %d" % bounce.corners, flush=True)
            try:
                display.show(bounce.pixels())
            except (urllib.error.URLError, OSError) as exc:
                if started - last_warning >= 10:
                    print("Display unavailable; retrying: %s" % exc, file=sys.stderr, flush=True)
                    last_warning = started
                stopped.wait(1)
            stopped.wait(max(0, 1.0 / args.fps - (time.monotonic() - started)))
    finally:
        display.clear()
        print("DVD Bounce stopped.", flush=True)


if __name__ == "__main__":
    main()
