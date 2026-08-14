#!/usr/bin/env python3
"""Pixel Runner: an endless jump-and-duck runner for the BUSY Bar.

A dino -- or a cat -- runs across the 72x16 front panel. START jumps, OK or
BACK ducks. Cacti grow out of the ground and birds come in at head height, the
world speeds up, day turns to night, and a crash costs the run but not the app:
a few seconds later it starts over. Left alone for half a minute it plays
itself, so the bar always has something moving on it.

    python3 app.py                        # BUSY Bar over USB (10.0.4.20)
    python3 app.py --host 192.168.11.239  # the LAN bar -- see the note on Wi-Fi
    python3 app.py --preview              # ANSI render here; SPACE jumps, S ducks
    python3 app.py --demo                 # autopilot with a fast ramp
    python3 app.py --runner cat
    python3 app.py --once                 # draw one frame and exit

The buttons need `websockets` (see requirements.txt). Without it the app still
runs, it just plays itself and says so once.

A bar whose HTTP API is password-protected takes the password from the
BUSY_HTTP_PASSWORD environment variable (or --token); localhost needs none.

Play it over USB. A push is two HTTP requests -- 46 ms over USB against 96 ms
over Wi-Fi with a p90 of 275 ms -- and a runner whose frames land 275 ms apart
is a slideshow you cannot time a jump in. Over Wi-Fi this is a thing to watch,
not a thing to play.
"""

import argparse
import asyncio
import json
import math
import os
import queue
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

APP = "pixel-runner"
W, H = 72, 16

PRIORITY = 30              # above the ambient apps (10-20), because somebody is
                           # standing at the bar pressing buttons, and below a
                           # work session (90), which still owns the whole device
ELEMENT_TIMEOUT = 60       # s the device keeps the frame without a repush; the
                           # loop renews it every 20 s, so a killed app lets the
                           # panel clear itself instead of freezing mid-jump

AUTH_ERROR = ("device rejected the token; set BUSY_HTTP_PASSWORD "
              "or pass --token")


# ---------------------------------------------------------------------------
# BUSY Bar HTTP helpers
# ---------------------------------------------------------------------------


class DeviceError(RuntimeError):
    """The bar could not be reached at all, as opposed to answering with a
    status. It is the one failure the app retries forever: a bar on the other
    end of a flaky USB link goes away and comes back all the time."""


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


def _headers(content_type=None, token=None):
    headers = {"Content-Type": content_type} if content_type else {}
    if token:
        headers["X-API-Token"] = token
    return headers


def _post(host, path, data, content_type, token=None):
    req = urllib.request.Request(
        _base(host) + path,
        data=data,
        method="POST",
        headers=_headers(content_type, token),
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.getcode()


def show(host, pixels, token=None):
    """Upload and draw one full-frame image. Returns the HTTP status."""
    global _frame_no
    filename = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1

    try:
        _post(
            host,
            "/api/assets/upload?application_name=%s&file=%s" % (APP, filename),
            _png(pixels),
            "application/octet-stream",
            token,
        )
        body = {
            "application_name": APP,
            "priority": PRIORITY,
            "elements": [
                {"id": "frame", "type": "image", "path": filename, "x": 0, "y": 0,
                 "timeout": ELEMENT_TIMEOUT}
            ],
        }
        return _post(host, "/api/display/draw", json.dumps(body).encode(),
                     "application/json", token)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise DeviceError(str(exc)) from exc


def _clear(host, token=None):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs,
                                 method="DELETE", headers=_headers(token=token))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.getcode()
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


# ---------------------------------------------------------------------------
# The network, off the render thread
# ---------------------------------------------------------------------------
# One slot, newest wins: a runner frame is a complete snapshot, so a bar that
# falls behind should get the current jump, never a queue of old ones. The
# policy is the repo's -- see the busybar-render-loop skill -- with one addition
# a game needs: while the display is blocked (409) the main loop freezes the
# game instead of simulating a run nobody can see and lose.

REPUSH_EVERY = 20.0        # s: re-push an unchanged frame anyway, so an element
                           # the firmware expired comes back
DEVICE_BACKOFF_MIN = 1.0   # s before the first retry after the bar goes away
DEVICE_BACKOFF_MAX = 15.0  # ...doubling up to here: long enough to stop
                           # hammering, short enough that a bar coming back is
                           # noticed within a coffee break
BUSY_RETRY = 2.0           # s between attempts while another app holds the panel
PUSH_TICK = 0.25           # s the push thread waits before re-checking its own
                           # timers; a fresh frame wakes it immediately
JOIN_TIMEOUT = 5.0         # s to wait for an in-flight push on the way out


class Pusher(threading.Thread):
    """Hands frames to the bar without ever blocking the game.

    A thread cannot exit the process, so an auth or protocol failure lands in
    ``fatal`` and the render loop, which checks it once a frame, does the
    exiting. Rebinding one dict value is atomic, hence no lock.
    """

    def __init__(self, host, token=None):
        super().__init__(daemon=True, name="runner-push")
        self.host = host
        self.token = token
        self.last_sent = None        # the last frame the device accepted
        self.last_push = 0.0
        self.retry_at = 0.0          # holds pushes off a gone or busy bar
        self.backoff = DEVICE_BACKOFF_MIN
        self.offline = None          # the outage, announced once, not per retry
        self.blocked = False         # 409: a work session or another app owns it
        self.fatal = None            # auth/protocol failure message, or None
        self._slot = {"frame": None}
        self._wake = threading.Event()
        self._stop = False

    def offer(self, frame):
        self._slot["frame"] = frame
        self._wake.set()

    def stop(self):
        self._stop = True
        self._wake.set()
        self.join(timeout=JOIN_TIMEOUT)

    def run(self):
        while not self._stop and self.fatal is None:
            self._wake.wait(PUSH_TICK)
            self._wake.clear()
            self.step(self._slot["frame"], time.monotonic())

    def step(self, frame, now):
        """One push decision, with `now` passed in so the whole policy is
        testable without a thread, a sleep or a bar on the desk."""
        if frame is None or self.fatal is not None or now < self.retry_at:
            return
        if frame == self.last_sent and now - self.last_push < REPUSH_EVERY:
            return
        try:
            status = show(self.host, frame, self.token)
        except DeviceError as exc:
            self.backoff = min(DEVICE_BACKOFF_MAX, self.backoff * 2)
            self.retry_at = now + self.backoff
            if self.offline is None:
                # Announced once: a bar that is off for an hour would otherwise
                # write a line every retry, and the log is how someone finds out
                # it came back.
                self.offline = str(exc)
                print("pixel-runner: device unreachable (%s); retrying" % exc)
            return

        if self.offline is not None:
            self.offline = None
            print("pixel-runner: device back.")
        self.backoff = DEVICE_BACKOFF_MIN
        self.last_push = now

        if status in (200, 201, 204):
            self.last_sent = frame
            self.blocked = False
        elif status in (401, 403):
            self.fatal = AUTH_ERROR
        elif status == 409:
            self.retry_at = now + BUSY_RETRY     # a work session owns the whole
            if not self.blocked:                 # device for up to an hour;
                print("pixel-runner: display busy, game paused...")
                self.blocked = True              # standing down beats retrying
        elif status == 508:
            pass    # asset name came back while the device still held it; the
                    # next push rotates to another name in the ring
        else:
            self.fatal = "unexpected HTTP %d from the device" % status


def _sigterm(signum, frame):
    # launchd, systemd and a bare `kill` all send SIGTERM. Raising here puts
    # them on the Ctrl-C path, so there is one exit that clears the display.
    raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# Pixel primitives
# ---------------------------------------------------------------------------


def _px(buf, x, y, rgb):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = rgb


def _rect(buf, x, y, w, h, rgb):
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    for yy in range(y0, y1):
        base = yy * W
        for xx in range(x0, x1):
            buf[base + xx] = rgb


def _blend(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


# A 3x5 bitmap font, the one the other apps in this repo hand-rolled. Wide
# enough for GAME OVER and a four-digit score on a 72 px panel, which is all the
# text this app has; a device `text` element would need a second draw per frame.
FONT = {
    "0": ["111", "101", "101", "101", "111"],
    "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"],
    "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"],
    "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"],
    "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"],
    "9": ["111", "101", "111", "001", "111"],
    "A": ["010", "101", "111", "101", "101"],
    "B": ["110", "101", "110", "101", "110"],
    "C": ["111", "100", "100", "100", "111"],
    "D": ["110", "101", "101", "101", "110"],
    "E": ["111", "100", "110", "100", "111"],
    "F": ["111", "100", "110", "100", "100"],
    "G": ["111", "100", "101", "101", "111"],
    "H": ["101", "101", "111", "101", "101"],
    "I": ["111", "010", "010", "010", "111"],
    "J": ["001", "001", "001", "101", "111"],
    "K": ["101", "101", "110", "101", "101"],
    "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101"],
    "N": ["101", "111", "111", "111", "101"],
    "O": ["111", "101", "101", "101", "111"],
    "P": ["110", "101", "110", "100", "100"],
    "Q": ["111", "101", "101", "111", "011"],
    "R": ["110", "101", "110", "101", "101"],
    "S": ["111", "100", "111", "001", "111"],
    "T": ["111", "010", "010", "010", "010"],
    "U": ["101", "101", "101", "101", "111"],
    "V": ["101", "101", "101", "101", "010"],
    "W": ["101", "101", "111", "111", "101"],
    "X": ["101", "101", "010", "101", "101"],
    "Y": ["101", "101", "010", "010", "010"],
    "Z": ["111", "001", "010", "100", "111"],
    " ": ["000", "000", "000", "000", "000"],
    "!": ["010", "010", "010", "000", "010"],
}

GLYPH_W = 4                # 3 px of glyph plus 1 px of tracking


def text_width(text):
    return max(0, len(text) * GLYPH_W - 1)


def draw_text(buf, text, x, y, rgb, center=False):
    """Draw uppercase text at (x, y). `center` treats x as the middle."""
    text = text.upper()
    if center:
        x -= text_width(text) // 2
    pen = x
    for ch in text:
        glyph = FONT.get(ch, FONT[" "])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    _px(buf, pen + gx, y + gy, rgb)
        pen += GLYPH_W


def _blit(buf, rows, x, y, rgb, eye=(12, 12, 16)):
    """Stamp a sprite. '#' is the body colour, 'o' the eye, '.' transparent."""
    for dy, row in enumerate(rows):
        for dx, ch in enumerate(row):
            if ch == "#":
                _px(buf, x + dx, y + dy, rgb)
            elif ch == "o":
                _px(buf, x + dx, y + dy, eye)


# ---------------------------------------------------------------------------
# Geometry and physics
# ---------------------------------------------------------------------------
# Every number below is boxed in by the panel being 16 px tall. The runner is
# 7 px, so it stands on rows 6..12 with 6 px of sky above its head -- which is
# the whole jump budget. Apex is 5.9 px: any higher and the head leaves the
# panel, any lower and a 4 px cactus stops being clearable.

GROUND_Y = 13              # first ground row
FEET_Y = GROUND_Y - 1      # 12: the row the runner's feet rest on

RUNNER_X = 8               # fixed; the world moves, the runner does not
RUNNER_H = 7
DUCK_H = 4

GRAVITY = 110.0            # px/s^2
JUMP_V = 36.0              # px/s -> apex 5.89 px at 0.33 s, landed at 0.65 s
JUMP_APEX = JUMP_V * JUMP_V / (2 * GRAVITY)
JUMP_AIRTIME = 2 * JUMP_V / GRAVITY
DUCK_FALL = 2.6            # gravity multiplier while ducking in mid-air, so a
                           # jump can be cut short to meet a low bird

# The jump is a fixed arc: no variable height from how long START is held. A
# button press travels the status WebSocket, and the display round trip alone is
# 96 ms over Wi-Fi with a p90 of 275 ms -- a third of the airtime. Timing a hold
# through that would be a lottery, so the arc is decided at takeoff.
#
# Gravity is what it is because of the arithmetic the panel forces. Apex is
# capped at 6 px by the runner's head, so the only free variable is how long the
# arc lasts, and the arc has to outlast the obstacle: an obstacle is in the way
# for (its width + the hitbox) / speed seconds. The first draft used 150 px/s^2,
# which gave a 0.56 s hop -- shorter than the 0.62 s a 3 px cactus blocks at the
# opening speed, so the game was unwinnable from the first obstacle and the
# autopilot proved it by dying at 35 points every time.

SPEED_START = 28.0         # px/s. Also a floor, not just an opening pace: a
                           # slower world leaves obstacles in the way longer
                           # than one jump lasts
SPEED_MAX = 44.0           # 2.9 px per frame at 15 fps; faster reads as teleporting
SPEED_RAMP = 0.45          # px/s per second -> top speed after ~35 s

SCORE_PER_PX = 1.0 / 2.6   # ~9 points/s at the start, ~15 at top speed
MILESTONE = 100            # points between score flashes
BIRD_AFTER = 350           # points before birds join in
NIGHT_EVERY = 400          # points between day and night
NIGHT_FADE = 1.4           # s the palette takes to cross over

GAP_MIN = 0.85             # s of clear ground after an obstacle passes, on top
GAP_MAX = 1.95             # of the airtime a jump costs
CLEAR_MARGIN = 0.18        # s of slack demanded of every spawned obstacle: 2.7
                           # frames at 15 fps, so a jump a frame early or late
                           # still clears. Below ~0.1 only a perfect frame works,
                           # and the autopilot measured 15 crashes in 5 minutes
                           # at 6 fps against none at 15 with this value

CRASH_FREEZE = 0.65        # s of frozen, flashing wreck before the score screen
OVER_SECONDS = 5.0         # s of score screen before it starts over on its own


# ---------------------------------------------------------------------------
# Sprites
# ---------------------------------------------------------------------------
# Two-frame run cycles, driven by distance rather than by frame count: a frame
# dropped by a slow link then costs resolution, not evenness, which is the same
# reason the physics runs off elapsed time.

DINO_RUN = [
    [
        "......###",
        "......#o#",
        "#....####",
        "##..#####",
        ".########",
        "..######.",
        "..#...##.",
    ],
    [
        "......###",
        "......#o#",
        "#....####",
        "##..#####",
        ".########",
        "..######.",
        "..##..#..",
    ],
]
DINO_JUMP = [
    "......###",
    "......#o#",
    "#....####",
    "##..#####",
    ".########",
    "..######.",
    "..#..#...",
]
DINO_DUCK = [
    [
        "......####",
        "#.....#o##",
        "##########",
        "..##..##..",
    ],
    [
        "......####",
        "#.....#o##",
        "##########",
        "..#....#..",
    ],
]

CAT_RUN = [
    [
        "#.....#.#",
        "#.....###",
        "##....#o#",
        ".########",
        ".########",
        ".##..##..",
        ".#....#..",
    ],
    [
        "#.....#.#",
        "#.....###",
        "##....#o#",
        ".########",
        ".########",
        ".##..##..",
        "..#..#...",
    ],
]
CAT_JUMP = [
    "......#.#",
    "#.....###",
    "##....#o#",
    ".########",
    ".########",
    "..#..##..",
    ".#.....#.",
]
CAT_DUCK = [
    [
        "#......#.#",
        "##.....###",
        ".#######o#",
        "..##...##.",
    ],
    [
        "#......#.#",
        "##.....###",
        ".#######o#",
        "..#.....#.",
    ],
]

# hit / duck_hit are (dx, dy, w, h) inside the sprite. Both are smaller than
# what is drawn: a tail or a snout brushing a cactus reads as a near miss, and
# a hitbox that matches the drawing exactly makes the game feel unfair.
RUNNERS = {
    "dino": {
        "run": DINO_RUN, "jump": DINO_JUMP, "duck": DINO_DUCK,
        "hit": (2, 1, 5, 6), "duck_hit": (1, 0, 7, 4),
        "day": (86, 214, 118), "night": (150, 232, 186),
    },
    "cat": {
        "run": CAT_RUN, "jump": CAT_JUMP, "duck": CAT_DUCK,
        "hit": (2, 1, 5, 6), "duck_hit": (1, 0, 7, 4),
        "day": (245, 158, 66), "night": (250, 196, 130),
    },
}

# Cacti, widest last. Which of them the spawner may use is decided by the
# physics at the current speed, not by the score -- see clearable().
CACTI = [
    (["#.#", "###", ".#."], 3, 3),
    ([".#.", "#.#", "###", ".#."], 3, 4),
    (["#.#.#", "#####", ".#.#."], 5, 3),
    (["#.#.#.", "######", ".#..#."], 6, 3),
]
CACTUS_INSET = 1           # px shaved off a cactus's hitbox. Its arms are one
                           # pixel thick and the runner brushing one reads as a
                           # near miss, not a crash

BIRD = [
    ["...##.", "######", "..#..."],
    ["...#..", "######", "..##.."],
]
BIRD_W, BIRD_H = 6, 3
BIRD_Y = 6                 # rows 6..8: over a ducking runner (9..12), into a
                           # standing one (7..12) and into a jumping one
BIRD_SPEED = 1.15          # birds fly at you, so they close faster than the ground


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

DAY = {
    "sky": (12, 20, 44),
    "ground": (168, 132, 76),
    "soil": (74, 56, 34),
    "grit": (206, 176, 118),
    "cactus": (44, 176, 84),
    "bird": (255, 146, 74),    # deliberately not the score's grey: a bird flies
                               # at head height, straight through where the
                               # digits sit, and the two must not read as one
    "cloud": (54, 74, 116),
    "score": (198, 206, 220),
    "dust": (150, 130, 100),
}
NIGHT = {
    "sky": (2, 3, 12),
    "ground": (78, 80, 104),
    "soil": (28, 28, 44),
    "grit": (120, 122, 152),
    "cactus": (26, 104, 62),
    "bird": (236, 128, 60),
    "cloud": (26, 30, 54),
    "score": (128, 140, 176),
    "dust": (70, 72, 92),
}
MILESTONE_COLOR = (255, 214, 92)
CRASH_COLOR = (255, 92, 76)


def palette(night):
    """Blend the two palettes. `night` is 0 at noon and 1 at midnight."""
    return {k: _blend(DAY[k], NIGHT[k], night) for k in DAY}


# ---------------------------------------------------------------------------
# The game
# ---------------------------------------------------------------------------


def jump_window(height):
    """Seconds the runner's feet spend strictly above `height` px, or 0.

    Solves h(t) = JUMP_V*t - GRAVITY*t^2/2 >= height. It is what decides which
    obstacles may be spawned, so the spawner can never produce one the physics
    cannot clear.
    """
    disc = JUMP_V * JUMP_V - 2 * GRAVITY * height
    if disc <= 0:
        return 0.0
    return 2 * math.sqrt(disc) / GRAVITY


def clearable(hit_w, height, speed):
    """Can a jump started at the right moment clear this obstacle at this speed?

    The runner is over the obstacle for as long as the two hitboxes overlap in
    x, which is (obstacle width + runner width) / speed. CLEAR_MARGIN is the
    slack that keeps a perfectly-timed jump from being the only one that works.
    """
    runner_w = RUNNERS["dino"]["hit"][2]
    passing = (hit_w + runner_w) / max(1e-6, speed)
    return jump_window(height) >= passing + CLEAR_MARGIN


class Runner:
    """The character. Height above the ground in pixels, positive up.

    Ducking in mid-air pulls it down faster, which is the only way to answer a
    bird you have already jumped at.
    """

    def __init__(self, kind="dino"):
        self.kind = kind
        self.spec = RUNNERS[kind]
        self.y = 0.0
        self.vy = 0.0
        self.ducking = False
        self.airborne = False
        self.landed_at = None      # t of the last landing, for the dust puff

    def jump(self):
        if not self.airborne:
            self.vy = JUMP_V
            self.airborne = True
            return True
        return False

    def update(self, dt, t, duck_held):
        self.ducking = duck_held and not self.airborne
        if self.airborne:
            g = GRAVITY * (DUCK_FALL if duck_held else 1.0)
            self.vy -= g * dt
            self.y += self.vy * dt
            if self.y <= 0.0:
                self.y = 0.0
                self.vy = 0.0
                self.airborne = False
                self.landed_at = t
                self.ducking = duck_held

    @property
    def height(self):
        """Integer pixels off the ground -- the value the drawing uses, so the
        hitbox is exactly what the player can see."""
        return int(round(self.y))

    def box(self):
        """(x, y, w, h) hitbox in panel coordinates."""
        if self.ducking:
            dx, dy, w, h = self.spec["duck_hit"]
            top = FEET_Y - DUCK_H + 1
        else:
            dx, dy, w, h = self.spec["hit"]
            top = FEET_Y - RUNNER_H + 1 - self.height
        return (RUNNER_X + dx, top + dy, w, h)

    def sprite(self, distance):
        if self.airborne:
            return self.spec["jump"], FEET_Y - RUNNER_H + 1 - self.height
        phase = int(distance / 4.0) % 2      # a step every 4 px of ground
        if self.ducking:
            return self.spec["duck"][phase], FEET_Y - DUCK_H + 1
        return self.spec["run"][phase], FEET_Y - RUNNER_H + 1


class Obstacle:
    def __init__(self, kind, rows, x, w, h, y):
        self.kind = kind           # "cactus" or "bird"
        self.rows = rows           # a sprite, or a list of two for the bird
        self.x = float(x)
        self.w = w
        self.h = h
        self.y = y                 # top row
        self.hit_w = max(1, w - CACTUS_INSET) if kind == "cactus" else w

    def update(self, dt, speed):
        self.x -= speed * dt * (BIRD_SPEED if self.kind == "bird" else 1.0)

    @property
    def speed_factor(self):
        return BIRD_SPEED if self.kind == "bird" else 1.0

    def box(self):
        """Hitbox in panel coordinates -- narrower than the drawing for a
        cactus, exactly the drawing for a bird, which is already thin."""
        left = int(round(self.x)) + (self.w - self.hit_w)
        return (left, self.y, self.hit_w, self.h)


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class Game:
    """One run, plus the crash and the score screen that follow it.

    Everything that carries time takes `now`, so a three-minute run is a loop
    over a list of floats in a test rather than three minutes of waiting.
    """

    RUN, CRASH, OVER = "run", "crash", "over"

    def __init__(self, runner="dino", seed=None, demo=False, now=0.0,
                 show_score=True):
        self.spec_name = runner
        self.rng = random.Random(seed)
        self.demo = demo
        # The score belongs to a run somebody is playing. While the autopilot
        # has the controls the digits are the one part of the frame that says
        # otherwise -- a four-digit number nobody earned, climbing on its own.
        # The render loop flips this as attract mode comes and goes.
        self.show_score = show_score
        self.high = 0
        self.reset(now)

    def reset(self, now):
        self.state = self.RUN
        self.t = 0.0
        self._last_now = now
        self.runner = Runner(self.spec_name)
        self.obstacles = []
        self.distance = 0.0
        self.speed = SPEED_START
        self.score = 0
        self.night = 0.0
        self.night_target = 0.0
        self.crash_at = 0.0
        self.milestone_until = -1.0
        self.next_spawn = self.t + 1.6      # a moment of empty ground, so the
                                            # scene reads before the first cactus

    # -- time -------------------------------------------------------------
    def hold(self, now):
        """Let wall-clock time pass without simulating any of it. Used while the
        display is blocked: a run the player cannot see is a run they cannot
        play, and it would only be lost."""
        self._last_now = now

    def update(self, now, jump=False, duck=False):
        dt = min(0.25, max(0.0, now - self._last_now))
        self._last_now = now
        self.t += dt

        if self.state == self.CRASH:
            if self.t - self.crash_at >= CRASH_FREEZE:
                self.state = self.OVER
            return
        if self.state == self.OVER:
            if jump or self.t - self.crash_at >= CRASH_FREEZE + OVER_SECONDS:
                self.reset(now)
            return

        ramp = SPEED_RAMP * (2.5 if self.demo else 1.0)
        self.speed = min(SPEED_MAX, SPEED_START + ramp * self.t)
        self.distance += self.speed * dt
        score = int(self.distance * SCORE_PER_PX)
        if score // MILESTONE > self.score // MILESTONE:
            self.milestone_until = self.t + 0.9
        self.score = score

        if jump:
            self.runner.jump()
        self.runner.update(dt, self.t, duck)

        self._day_night(dt)
        self._spawn()
        for obstacle in self.obstacles:
            obstacle.update(dt, self.speed)
        self.obstacles = [o for o in self.obstacles if o.x + o.w > -2]

        box = self.runner.box()
        for obstacle in self.obstacles:
            if _overlap(box, obstacle.box()):
                self.state = self.CRASH
                self.crash_at = self.t
                self.high = max(self.high, self.score)
                break

    # -- world ------------------------------------------------------------
    def _day_night(self, dt):
        # The target is a step function of the score; `night` walks toward it at
        # a fixed rate, so the palette crosses over in NIGHT_FADE seconds
        # however the score got there.
        cycle = max(1, int(NIGHT_EVERY * (0.25 if self.demo else 1.0)))
        self.night_target = float((self.score // cycle) % 2)
        step = dt / NIGHT_FADE
        delta = self.night_target - self.night
        if abs(delta) <= step:
            self.night = self.night_target
        else:
            self.night += step * (1 if delta > 0 else -1)

    def _spawn(self):
        if self.t < self.next_spawn:
            return
        birds_allowed = self.score >= (BIRD_AFTER * (0.2 if self.demo else 1.0))
        if birds_allowed and self.rng.random() < 0.28:
            obstacle = Obstacle("bird", BIRD, W + 2, BIRD_W, BIRD_H, BIRD_Y)
        else:
            # Only offer shapes this speed can still clear. The check is the
            # physics itself, so the spawner cannot invent an impossible run
            # however fast the world gets.
            # Judged at today's speed, not at the slightly higher one the world
            # will have reached by the time the cactus arrives: the ramp adds
            # under 1 px/s over that second, and being optimistic about it only
            # buys a shape a second before it is reliably jumpable.
            choices = [c for c in CACTI
                       if clearable(max(1, c[1] - CACTUS_INSET), c[2], self.speed)]
            rows, w, h = self.rng.choice(choices or [CACTI[0]])
            obstacle = Obstacle("cactus", rows, W + 2, w, h, GROUND_Y - h)
        self.obstacles.append(obstacle)
        gap = self.rng.uniform(GAP_MIN, GAP_MAX) + JUMP_AIRTIME
        if obstacle.kind == "bird":
            gap += 0.35        # a bird needs a beat afterwards to stand up again
        self.next_spawn = self.t + gap

    # -- drawing ----------------------------------------------------------
    def render(self):
        """A pure function of the game state: same state, same 1152 pixels.

        That is what makes the unchanged-frame skip in the pusher correct, and
        what lets the tests check the picture without a device.
        """
        pal = palette(self.night)
        buf = [pal["sky"]] * (W * H)

        self._draw_sky(buf, pal)
        self._draw_ground(buf, pal)

        for obstacle in self.obstacles:
            rows = obstacle.rows
            if obstacle.kind == "bird":
                rows = rows[int(self.t * 7.0) % 2]     # wingbeat, ~3.5 flaps/s
                color = pal["bird"]
            else:
                color = pal["cactus"]
            _blit(buf, rows, int(round(obstacle.x)), obstacle.y, color)

        self._draw_runner(buf, pal)

        if self.state == self.OVER:
            self._draw_over(buf, pal)
        elif self.show_score:
            self._draw_score(buf, pal)
        return buf

    def _draw_sky(self, buf, pal):
        if self.night > 0.55:
            # Stars are pinned to the world, so they drift with everything else,
            # just slower -- parallax is most of what sells depth at this size.
            for i in range(9):
                sx = int((i * 23 + 7 - self.distance * 0.12)) % W
                sy = 1 + (i * 5) % 5
                if (i + int(self.t * 1.5)) % 7:
                    _px(buf, sx, sy, pal["score"])
            # The moon does not scroll -- it is the one thing far enough away to
            # stand still, and parking it at x=48 keeps it off both the runner
            # at x=8 and the score at the right edge.
            for dy, row in enumerate(["..##.", ".###.", ".###.", "..##."]):
                for dx, ch in enumerate(row):
                    if ch == "#":
                        _px(buf, 48 + dx, 1 + dy, (236, 232, 198))
        else:
            for i in range(3):
                cx = int((i * 29 + 11 - self.distance * 0.25)) % (W + 16) - 8
                cy = 1 + (i * 3) % 3
                _rect(buf, cx + 1, cy, 4, 1, pal["cloud"])
                _rect(buf, cx, cy + 1, 6, 1, pal["cloud"])

    def _draw_ground(self, buf, pal):
        _rect(buf, 0, GROUND_Y, W, 1, pal["ground"])
        _rect(buf, 0, GROUND_Y + 1, W, H - GROUND_Y - 1, pal["soil"])
        # Grit scrolls with the world: a fixed pattern under a moving runner is
        # the one thing that makes the whole scene look static.
        shift = int(self.distance)
        for x in range(W):
            n = (x + shift) * 2654435761 & 0xFFFFFFFF
            if n % 11 == 0:
                _px(buf, x, GROUND_Y + 1 + (n >> 8) % 2, pal["grit"])
            elif n % 23 == 0:
                _px(buf, x, GROUND_Y, pal["grit"])

    def _draw_runner(self, buf, pal):
        spec = RUNNERS[self.spec_name]
        color = _blend(spec["day"], spec["night"], self.night)
        if self.state == self.CRASH and int((self.t - self.crash_at) * 12) % 2:
            color = CRASH_COLOR
        rows, top = self.runner.sprite(self.distance)
        _blit(buf, rows, RUNNER_X, top, color)
        landed = self.runner.landed_at
        if landed is not None and self.t - landed < 0.18 and self.state == self.RUN:
            _px(buf, RUNNER_X - 1, FEET_Y, pal["dust"])
            _px(buf, RUNNER_X - 2, FEET_Y - 1, pal["dust"])

    def _draw_score(self, buf, pal):
        text = "%04d" % min(9999, self.score)
        color = pal["score"]
        if self.t < self.milestone_until and int(self.t * 8) % 2:
            color = MILESTONE_COLOR
        draw_text(buf, text, W - 1 - text_width(text), 1, color)

    def _draw_over(self, buf, pal):
        # The frozen scene stays behind a dimmed sky so the crash is still
        # readable, with the two lines the player actually wants.
        for i in range(W * H):
            buf[i] = _blend(buf[i], (0, 0, 0), 0.72)
        draw_text(buf, "GAME OVER", W // 2, 2, (255, 236, 200), center=True)
        if self.show_score:
            line = "%d  HI %d" % (self.score, self.high)
            draw_text(buf, line, W // 2, 9, pal["score"], center=True)
        left = max(0.0, CRASH_FREEZE + OVER_SECONDS - (self.t - self.crash_at))
        _rect(buf, 0, H - 1, int(W * left / OVER_SECONDS), 1, (90, 96, 120))


# ---------------------------------------------------------------------------
# Autopilot
# ---------------------------------------------------------------------------
# Used by --demo, by --preview without a terminal, and by attract mode after a
# stretch with nobody at the bar. It reads the same state a player sees and
# presses the same two buttons.

AUTO_JUMP_LEAD = 0.22      # s before contact. The feet clear 4 px between 0.14
                           # and 0.51 s after takeoff, and a frame at 15 fps is
                           # 0.07 s, so jumping anywhere in 0.15..0.22 lands the
                           # whole pass inside that window
AUTO_DUCK_LEAD = 0.45      # s: ducking early costs nothing, ducking late is a hit
AUTO_DUCK_TAIL = 0.15      # s to keep holding after the bird's tail passes
AUTO_RESTART_AFTER = 2.0   # s the autopilot leaves its own score screen up --
                           # long enough for a passer-by to read what it managed


def autopilot(game):
    """(jump, duck) for the current frame."""
    if game.state != Game.RUN:
        restart = (game.state == Game.OVER
                   and game.t - game.crash_at >= CRASH_FREEZE + AUTO_RESTART_AFTER)
        return (restart, False)
    jump = duck = False
    hit_x, _, hit_w, _ = game.runner.box()
    for obstacle in game.obstacles:
        speed = game.speed * obstacle.speed_factor
        ox, _, ow, _ = obstacle.box()
        lead = (ox - (hit_x + hit_w)) / max(1e-6, speed)
        tail = (ox + ow - hit_x) / max(1e-6, speed)
        if tail < -AUTO_DUCK_TAIL:
            continue
        if obstacle.kind == "bird":
            if lead <= AUTO_DUCK_LEAD:
                duck = True
        elif 0 <= lead <= AUTO_JUMP_LEAD:
            jump = True
    return (jump, duck)


# ---------------------------------------------------------------------------
# Buttons, over the status WebSocket
# ---------------------------------------------------------------------------
# State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1, with
# Button OK=0 BACK=1 START=2 and ButtonAction PRESS=0 RELEASE=1. proto3 drops
# zero values, so an OK press arrives as an empty ButtonEvent -- both fields
# default to 0 here or the most common press would decode as nothing at all.

BTN_OK, BTN_BACK, BTN_START = 0, 1, 2
DUCK_MAX_HOLD = 4.0        # s a duck survives without its RELEASE. The release
                           # is a packet like any other, and one lost to a Wi-Fi
                           # drop would otherwise leave the runner crouched
                           # forever, which looks exactly like a hung app.


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


def _iter_proto_fields(buf):
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
            raise ValueError("unsupported protobuf wire type %d" % wire)


def decode_buttons(frame):
    """Status frame -> [(button, action)], empty for the frames carrying
    brightness, Wi-Fi or timers, which is most of them."""
    events = []
    for field_no, wire, update in _iter_proto_fields(frame):
        if field_no != 2 or wire != 2:            # State.updates
            continue
        for uf, uw, uv in _iter_proto_fields(update):
            if uf != 11 or uw != 2:               # StateUpdate.input
                continue
            for ef, ew, ev in _iter_proto_fields(uv):
                if ef != 1 or ew != 2:            # InputEvent.button_event
                    continue
                button = action = 0
                for bf, bw, bv in _iter_proto_fields(ev):
                    if bw == 0 and bf == 1:
                        button = int(bv)
                    elif bw == 0 and bf == 2:
                        action = int(bv)
                events.append((button, action))
    return events


def _ws_url(host, token=None):
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


class Buttons:
    """START jumps, OK and BACK duck while held.

    The reader is a daemon thread with a bounded queue; input that dies degrades
    to "no input" rather than taking the game down. The WS URL carries the token
    in its query string, so it is a secret and never reaches a log line.
    """

    def __init__(self, host, token=None):
        self.host = host
        self.token = token
        self.events = queue.Queue(maxsize=256)
        self.available = False
        self._held = {}            # button -> monotonic time of its PRESS
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        try:
            import websockets  # noqa: F401
        except ImportError:
            print("pixel-runner: no `websockets`, so the buttons are dead; "
                  "the game will play itself (pip install -r requirements.txt)")
            return False
        self._thread = threading.Thread(target=self._main, daemon=True,
                                        name="runner-input")
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def poll(self, now):
        """(jump, duck, human) since the last call -- never blocks.

        `jump` is an edge and `duck` a level, which is why presses are counted
        and holds are tracked: a press and its release inside one frame still
        has to produce a jump.
        """
        jump = human = False
        while True:
            try:
                button, action = self.events.get_nowait()
            except queue.Empty:
                break
            human = True
            if action == 0:
                self._held[button] = now
                if button == BTN_START:
                    jump = True
            else:
                self._held.pop(button, None)
        for button, pressed_at in list(self._held.items()):
            if now - pressed_at > DUCK_MAX_HOLD:
                del self._held[button]
        duck = any(b in self._held for b in (BTN_OK, BTN_BACK))
        return jump, duck, human

    def forget_holds(self):
        self._held.clear()

    def _main(self):
        try:
            asyncio.run(self._listen())
        except Exception as exc:                  # a thread cannot exit the app
            print("pixel-runner: input stopped (%s); playing itself" % exc)

    async def _listen(self):
        import websockets

        url = _ws_url(self.host, self.token)
        backoff = 0.5
        seen = False
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, max_size=4 * 1024 * 1024, ping_interval=20,
                    ping_timeout=20, close_timeout=3, open_timeout=8,
                ) as ws:
                    # The stream stays silent until this is sent, on every new
                    # session, reconnects included.
                    await ws.send(json.dumps({"enable": True}))
                    self.available = True
                    self.forget_holds()     # a release may have been lost with
                    backoff = 0.5           # the connection that carried it
                    print("pixel-runner: buttons %s (START jumps, OK/BACK ducks)"
                          % ("reconnected" if seen else "ready"))
                    seen = True
                    async for message in ws:
                        if self._stop.is_set():
                            return
                        if isinstance(message, str):
                            continue          # text frames are acks, not state
                        try:
                            for event in decode_buttons(bytes(message)):
                                if not self.events.full():
                                    self.events.put_nowait(event)
                        except ValueError:
                            pass              # a malformed frame is not a reason
            except asyncio.CancelledError:    # to stop reading the next one
                raise
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.available = False
                print("pixel-runner: input disconnected (%s); retrying in %.1fs"
                      % (exc, backoff))
                deadline = time.monotonic() + backoff
                while not self._stop.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                backoff = min(3.0, backoff * 2.0)


# ---------------------------------------------------------------------------
# Terminal preview, and playing it there
# ---------------------------------------------------------------------------
# The upper half block with a truecolor foreground and background draws two
# pixel rows per text row, which comes out roughly square. 16 rows become 8.

PREVIEW_ROWS = H // 2
KEY_DUCK_HOLD = 0.45       # s a keystroke keeps the runner down. A terminal
                           # sends key repeats, not press/release, so a hold has
                           # to be inferred from how recently a key arrived.


def _ansi(pixels):
    out = []
    for y in range(0, H, 2):
        row = []
        for x in range(W):
            tr, tg, tb = pixels[y * W + x]
            br, bg, bb = pixels[(y + 1) * W + x]
            row.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm▀"
                       % (tr, tg, tb, br, bg, bb))
        out.append("".join(row) + "\x1b[0m")
    return "\n".join(out)


def _preview_frame(pixels, label, first):
    """Redraw in place. Scrolling a fresh copy every frame turns the terminal
    into a flipbook and makes the timing impossible to judge."""
    body = _ansi(pixels) + "\n" + label + "\x1b[K"
    if not first:
        body = "\x1b[%dA" % (PREVIEW_ROWS + 1) + body
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


class Keyboard:
    """Raw-mode stdin for --preview: SPACE or W jumps, S or DOWN ducks, Q quits.

    Falls back to "no keyboard" whenever stdin is not a terminal -- a piped or
    redirected run then plays itself instead of crashing on termios.
    """

    def __init__(self):
        self.ok = False
        self._saved = None
        self._duck_until = 0.0

    def start(self):
        try:
            import termios
            import tty
        except ImportError:
            return False
        if not sys.stdin.isatty():
            return False
        try:
            self._saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            return False
        self.ok = True
        return True

    def stop(self):
        if self._saved is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)
            self._saved = None
        self.ok = False

    def poll(self, now):
        """(jump, duck, quit, human)."""
        if not self.ok:
            return False, False, False, False
        import select

        jump = quit_ = human = False
        while select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            if not ch:
                break
            human = True
            if ch in (" ", "w", "W", "\r", "\n"):
                jump = True
            elif ch in ("s", "S"):
                self._duck_until = now + KEY_DUCK_HOLD
            elif ch in ("q", "Q", "\x03"):
                quit_ = True
            elif ch == "\x1b":
                rest = sys.stdin.read(2) if select.select([sys.stdin], [], [], 0)[0] else ""
                if rest.endswith("A"):
                    jump = True
                elif rest.endswith("B"):
                    self._duck_until = now + KEY_DUCK_HOLD
        return jump, now < self._duck_until, quit_, human


# ---------------------------------------------------------------------------
# CLI and main loop
# ---------------------------------------------------------------------------

DEFAULT_HOST = "10.0.4.20"
DEFAULT_FPS = 15.0         # what USB's p90 push (60 ms) allows with room to
                           # spare; Wi-Fi's p90 allows 4, which is why the
                           # docstring says to play this over USB
ATTRACT_AFTER = 25.0       # s without a button before the autopilot takes over


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Endless jump-and-duck runner for the BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help="BUSY Bar host (default: %s)" % DEFAULT_HOST)
    p.add_argument("--token", default=os.environ.get("BUSY_HTTP_PASSWORD"),
                   help="device HTTP API password (prefer BUSY_HTTP_PASSWORD); "
                        "localhost needs none")
    p.add_argument("--runner", choices=sorted(RUNNERS), default="dino",
                   help="who does the running (default: dino)")
    p.add_argument("--fps", type=float, default=DEFAULT_FPS,
                   help="frames per second, 4-20 (default: %g)" % DEFAULT_FPS)
    p.add_argument("--autopilot", choices=["auto", "on", "off"], default="auto",
                   help="'auto' plays by itself until a button is pressed and "
                        "again after %g s of silence (default), 'on' never hands "
                        "over, 'off' leaves the running to a player"
                        % ATTRACT_AFTER)
    p.add_argument("--attract-after", type=float, default=ATTRACT_AFTER,
                   help="seconds of no input before autopilot (default: %g)"
                        % ATTRACT_AFTER)
    p.add_argument("--demo", action="store_true",
                   help="autopilot with a fast ramp: top speed, birds and night "
                        "inside half a minute")
    p.add_argument("--preview", action="store_true",
                   help="render in this terminal instead of on a device")
    p.add_argument("--once", "--test", dest="once", action="store_true",
                   help="draw one frame mid-jump and exit")
    p.add_argument("--seed", type=int, default=None,
                   help="fixed random seed for a repeatable run")
    args = p.parse_args(argv)
    args.fps = max(4.0, min(20.0, args.fps))
    args.attract_after = max(0.0, args.attract_after)
    if args.demo:
        args.autopilot = "on"
    return args


def _one_frame(args):
    """The --once picture: a few seconds of autopilot so the frame has a runner
    in the air over a cactus, which is what the app looks like."""
    # The still is a portrait of the app being played, so it keeps its score --
    # the autopilot here is only a way to find a frame worth drawing.
    game = Game(runner=args.runner, seed=args.seed if args.seed is not None else 7,
                demo=args.demo, now=0.0)
    dt = 1.0 / 15.0
    now = 0.0
    for _ in range(int(25 / dt)):
        now += dt
        jump, duck = autopilot(game)
        game.update(now, jump, duck)
        if game.runner.airborne and game.runner.vy < 0 and game.obstacles:
            break
    return game.render()


def main(argv=None):
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, _sigterm)

    if args.once:
        # One synchronous push and out: there is no second frame to retry into,
        # so every failure is an exit, and the frame it drew is the point --
        # hence no clear on the way out either.
        pixels = _one_frame(args)
        if args.preview:
            _preview_frame(pixels, "one frame", first=True)
            return 0
        try:
            status = show(args.host, pixels, args.token)
        except DeviceError as exc:
            sys.exit("error: %s" % exc)
        except KeyboardInterrupt:
            sys.exit("interrupted")
        if status in (401, 403):
            sys.exit("error: %s" % AUTH_ERROR)
        if status not in (200, 201, 204, 508):
            sys.exit("error: unexpected HTTP %d from the device" % status)
        print("once: drew one runner frame (status %d)" % status)
        return 0

    game = Game(runner=args.runner, seed=args.seed, demo=args.demo,
                now=time.monotonic())
    pusher = None
    buttons = None
    keyboard = None

    if args.preview:
        keyboard = Keyboard()
        if keyboard.start():
            print("pixel-runner: preview, no device  (SPACE jumps, S ducks, Q quits)")
        else:
            print("pixel-runner: preview, no device and no keyboard  (Ctrl-C to stop)")
        sys.stdout.write("\x1b[?25l")       # a cursor parked in the artwork
    else:                                   # blinks over a pixel every frame
        print("pixel-runner -> %s  (Ctrl-C to stop)" % _base(args.host))
        pusher = Pusher(args.host, args.token)
        pusher.start()
        if args.autopilot != "on":
            buttons = Buttons(args.host, args.token)
            if not buttons.start():
                buttons = None

    frame_interval = 1.0 / args.fps
    last_human = time.monotonic()
    # It starts out playing itself. A bar nobody is standing at would otherwise
    # spend its first half-minute running into the first cactus over and over,
    # and the first press hands a fresh run to whoever showed up.
    attract = args.autopilot != "off"
    first = True

    try:
        while True:
            frame_start = time.monotonic()
            jump = duck = human = False
            if buttons is not None:
                jump, duck, human = buttons.poll(frame_start)
            if keyboard is not None:
                jump, duck, quit_, human = keyboard.poll(frame_start)
                if quit_:
                    break

            if human:
                last_human = frame_start
                if attract and args.autopilot != "on":
                    # Somebody walked up to the bar. Hand them a fresh run
                    # rather than the wreck of whatever the autopilot was doing.
                    attract = False
                    game.reset(frame_start)
                    jump = duck = False
            elif args.autopilot == "auto" and not attract:
                attract = frame_start - last_human > args.attract_after

            if attract:
                jump, duck = autopilot(game)
            # Attract mode comes and goes mid-run, so this is decided per frame.
            game.show_score = not attract

            if pusher is not None and pusher.blocked:
                game.hold(frame_start)       # a work session owns the panel
            else:
                game.update(frame_start, jump, duck)
            pixels = game.render()

            if pusher is None:
                _preview_frame(pixels, "score %4d  hi %4d  %5.1f px/s%s"
                               % (game.score, game.high, game.speed,
                                  "  [autopilot]" if attract else ""), first)
                first = False
            else:
                # Handing the frame over and going straight back to the loop is
                # what keeps the runner running: a bar that is slow, busy or
                # gone costs stale pixels, never a stalled game.
                pusher.offer(pixels)
                if pusher.fatal:
                    sys.exit("error: %s" % pusher.fatal)

            # Sleep the remainder, not a whole interval: time.sleep(interval)
            # adds the render to every frame, so 15 fps would run at 13.
            elapsed = time.monotonic() - frame_start
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if buttons is not None:
            buttons.stop()
        if keyboard is not None:
            keyboard.stop()
        if args.preview:
            sys.stdout.write("\x1b[?25h")   # or Ctrl-C leaves an invisible one
            sys.stdout.flush()
        else:
            try:
                if pusher:
                    pusher.stop()   # let a frame already in the air land first,
                _clear(args.host, args.token)   # or it would overdraw the clear
            except KeyboardInterrupt:
                pass                # an impatient second Ctrl-C lands here and
                                    # would turn a clean exit into a traceback
    return 0


if __name__ == "__main__":
    sys.exit(main())
