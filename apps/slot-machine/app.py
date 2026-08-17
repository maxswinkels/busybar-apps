#!/usr/bin/env python3
"""BUSY Bar Slot Machine - animated classic fruit machine for the 72x16 display.

Features
--------
- Immediate spin on startup.
- Automatic spins every N seconds.
- Three independently decelerating reels with overshoot/bounce.
- Classic pixel symbols: CHERRY, LEMON, BELL, BAR, STAR, 7.
- Random weighted outcomes with wins, big wins and a rare jackpot.
- Win flashes, symbol pulses, marquee borders and score count-up.
- Optional synthesized "ding ding ding" audio generated locally as WAV.
- Manual START-button spin support via the BUSY Bar status WebSocket (no busylib).
- Test modes for deterministic win/jackpot sequences.

Examples
--------
python3 app.py
python3 app.py --host 127.0.0.1:8080
python3 app.py --spin-interval 15 --sound off
python3 app.py --test-win
python3 app.py --test-jackpot
"""

from __future__ import annotations

import argparse
import asyncio
import io
import queue
import json
import math
import random
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
import zlib

APP = "slot-machine"
W, H = 72, 16
DEFAULT_HOST = "10.0.4.20"
PRIORITY = 40
FPS = 15
FRAME_T = 1.0 / FPS
RING = 5

# ---------- palette ----------
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GOLD = (255, 180, 0)
AMBER = (255, 120, 0)
RED = (255, 30, 30)
DARK_RED = (110, 0, 0)
GREEN = (30, 220, 80)
LIME = (170, 255, 40)
YELLOW = (255, 235, 40)
BLUE = (40, 130, 255)
CYAN = (30, 235, 255)
MAGENTA = (255, 50, 210)
PURPLE = (150, 40, 255)
SILVER = (210, 220, 230)
DARK = (16, 10, 4)

REEL_X = [2, 26, 50]
REEL_W = 20
REEL_Y = 1
REEL_H = 14
SYMBOL_W = 14
SYMBOL_H = 12
SPIN_CENTER_Y = 10   # +2 px only for reel/spin rendering
AWARD_CENTER_Y = 8   # original vertical position for win/jackpot symbol screens

# Symbol weight controls the natural resting probability when not forcing a win.
SYMBOL_WEIGHTS = {
    "CHERRY": 28,
    "LEMON": 24,
    "BELL": 16,
    "BAR": 13,
    "STAR": 11,
    "SEVEN": 8,
}
SYMBOLS = list(SYMBOL_WEIGHTS)

PAYOUTS = {
    ("CHERRY", "CHERRY"): (30, "WIN"),
    ("CHERRY", "CHERRY", "CHERRY"): (120, "BIG WIN"),
    ("LEMON", "LEMON", "LEMON"): (90, "WIN"),
    ("BELL", "BELL", "BELL"): (180, "BIG WIN"),
    ("BAR", "BAR", "BAR"): (300, "BIG WIN"),
    ("STAR", "STAR", "STAR"): (500, "SUPER WIN"),
    ("SEVEN", "SEVEN", "SEVEN"): (777, "JACKPOT"),
}

# ---------- tiny pixel font ----------
FONT = {
    "0": ["111","101","101","101","111"],
    "1": ["010","110","010","010","111"],
    "2": ["111","001","111","100","111"],
    "3": ["111","001","111","001","111"],
    "4": ["101","101","111","001","001"],
    "5": ["111","100","111","001","111"],
    "6": ["111","100","111","101","111"],
    "7": ["111","001","010","010","010"],
    "8": ["111","101","111","101","111"],
    "9": ["111","101","111","001","111"],
    "A": ["010","101","111","101","101"],
    "B": ["110","101","110","101","110"],
    "C": ["111","100","100","100","111"],
    "D": ["110","101","101","101","110"],
    "E": ["111","100","110","100","111"],
    "G": ["111","100","101","101","111"],
    "I": ["111","010","010","010","111"],
    "J": ["001","001","001","101","111"],
    "K": ["101","101","110","101","101"],
    "L": ["100","100","100","100","111"],
    "N": ["101","111","111","111","101"],
    "O": ["111","101","101","101","111"],
    "P": ["110","101","110","100","100"],
    "R": ["110","101","110","101","101"],
    "S": ["111","100","111","001","111"],
    "T": ["111","010","010","010","010"],
    "U": ["101","101","101","101","111"],
    "W": ["101","101","111","111","101"],
    "Y": ["101","101","010","010","010"],
    " ": ["0","0","0","0","0"],
    "+": ["0","010","111","010","0"],
}


def _blank(color=BLACK):
    return [color] * (W * H)


def _px(buf, x, y, c):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = c


def _rect(buf, x, y, w, h, c):
    for yy in range(max(0, y), min(H, y + h)):
        base = yy * W
        for xx in range(max(0, x), min(W, x + w)):
            buf[base + xx] = c


def _line_h(buf, x, y, w, c):
    _rect(buf, x, y, w, 1, c)


def _line_v(buf, x, y, h, c):
    _rect(buf, x, y, 1, h, c)


def draw_text(buf, text, x, y, color=WHITE, scale=1, center=False):
    text = text.upper()
    width = sum((len(FONT.get(ch, FONT[" "])[0]) + 1) * scale for ch in text)
    if text:
        width -= scale
    if center:
        x -= width // 2
    pen = x
    for ch in text:
        glyph = FONT.get(ch, FONT[" "])
        gw = len(glyph[0])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    _rect(buf, pen + gx * scale, y + gy * scale, scale, scale, color)
        pen += (gw + 1) * scale


# ---------- symbols ----------
def symbol_cherry(buf, x, y):
    _rect(buf, x+2, y+6, 4, 4, RED)
    _rect(buf, x+8, y+7, 4, 4, RED)
    _px(buf, x+2, y+9, DARK_RED); _px(buf, x+8, y+10, DARK_RED)
    _line_v(buf, x+6, y+2, 5, GREEN)
    _line_v(buf, x+9, y+2, 6, GREEN)
    _line_h(buf, x+6, y+2, 4, GREEN)
    _rect(buf, x+4, y+1, 3, 2, LIME)


def symbol_lemon(buf, x, y):
    _rect(buf, x+3, y+3, 8, 6, YELLOW)
    _rect(buf, x+2, y+5, 10, 3, YELLOW)
    _px(buf, x+2, y+4, GOLD); _px(buf, x+11, y+8, GOLD)
    _rect(buf, x+7, y+1, 3, 2, GREEN)


def symbol_bell(buf, x, y):
    y -= 1  # optical alignment: bell sits 1 px higher
    _rect(buf, x+4, y+2, 6, 1, GOLD)
    _rect(buf, x+3, y+3, 8, 1, YELLOW)
    _rect(buf, x+2, y+4, 10, 5, GOLD)
    _rect(buf, x+1, y+8, 12, 2, YELLOW)
    _rect(buf, x+5, y+10, 4, 1, AMBER)
    _px(buf, x+6, y+11, WHITE); _px(buf, x+7, y+11, WHITE)


def symbol_bar(buf, x, y):
    y -= 1  # optical alignment: BAR sits 1 px higher
    _rect(buf, x+1, y+3, 12, 7, SILVER)
    _rect(buf, x+2, y+4, 10, 5, BLACK)
    draw_text(buf, "BAR", x+2, y+4, WHITE, 1)


def symbol_star(buf, x, y):
    pts = [(6,1),(5,3),(2,3),(4,5),(3,8),(6,6),(9,8),(8,5),(10,3),(7,3)]
    for px, py in pts:
        _rect(buf, x+px, y+py, 2, 2, MAGENTA if py < 5 else PURPLE)
    _rect(buf, x+5, y+4, 4, 3, WHITE)


def symbol_seven(buf, x, y):
    _rect(buf, x+1, y+2, 12, 2, RED)
    _rect(buf, x+9, y+4, 3, 2, RED)
    _rect(buf, x+7, y+6, 3, 2, RED)
    _rect(buf, x+5, y+8, 3, 3, RED)
    _line_h(buf, x+2, y+1, 8, WHITE)


SYMBOL_DRAW = {
    "CHERRY": symbol_cherry,
    "LEMON": symbol_lemon,
    "BELL": symbol_bell,
    "BAR": symbol_bar,
    "STAR": symbol_star,
    "SEVEN": symbol_seven,
}


def draw_symbol(buf, name, cx, cy, pulse=0):
    x = int(cx - SYMBOL_W/2)
    y = int(cy - SYMBOL_H/2)
    if pulse:
        # bright backing shadow gives a convincing 1px pulse on this tiny matrix
        _rect(buf, x-1, y-1, SYMBOL_W+2, SYMBOL_H+2, WHITE if pulse > 1 else GOLD)
        _rect(buf, x, y, SYMBOL_W, SYMBOL_H, BLACK)
    SYMBOL_DRAW[name](buf, x, y)


# ---------- frame encoding / HTTP ----------
def _png(pixels):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


class Busy:
    def __init__(self, host):
        self.base = "http://" + host.replace("http://", "").replace("https://", "").rstrip("/")
        self.frame_no = 0

    def _post(self, path, data, content_type):
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": content_type})
        with urllib.request.urlopen(req, timeout=6) as r:
            return r.getcode()

    def show(self, pixels):
        fn = f"frame{self.frame_no % RING}.png"
        self.frame_no += 1
        try:
            self._post(f"/api/assets/upload?application_name={APP}&file={fn}", _png(pixels), "application/octet-stream")
            body = {"application_name": APP, "priority": PRIORITY,
                    "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}]}
            return self._post("/api/display/draw", json.dumps(body).encode(), "application/json")
        except urllib.error.HTTPError as e:
            return e.code

    def clear(self):
        qs = urllib.parse.urlencode({"application_name": APP})
        req = urllib.request.Request(self.base + "/api/display/draw?" + qs, method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass

    def upload_audio(self, name, wav_bytes):
        try:
            self._post(f"/api/assets/upload?application_name={APP}&file={name}", wav_bytes, "application/octet-stream")
            return True
        except Exception as e:
            print(f"audio upload failed: {e}")
            return False

    def play_audio(self, name):
        body = json.dumps({"application_name": APP, "path": name}).encode()
        try:
            self._post("/api/audio/play", body, "application/json")
        except Exception as e:
            print(f"audio play failed: {e}")


# ---------- synthesized ding ----------
def make_ding_wav():
    sr = 22050
    notes = [(1046.5, 0.00), (1318.5, 0.18), (1568.0, 0.36)]
    duration = 0.72
    n = int(sr * duration)
    samples = []
    for i in range(n):
        t = i / sr
        v = 0.0
        for freq, start in notes:
            dt = t - start
            if 0 <= dt < 0.32:
                env = math.exp(-7.5 * dt)
                # fundamental + a little metallic overtone
                v += env * (math.sin(2*math.pi*freq*dt) + 0.33*math.sin(2*math.pi*freq*2.01*dt))
        v = max(-1.0, min(1.0, v * 0.42))
        samples.append(int(v * 32767))
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return out.getvalue()


# ---------- game logic ----------
def weighted_symbol():
    return random.choices(SYMBOLS, weights=[SYMBOL_WEIGHTS[s] for s in SYMBOLS], k=1)[0]


def choose_result(force=None):
    if force == "win":
        return random.choice([
            ["CHERRY", "CHERRY", weighted_symbol()],
            ["LEMON", "LEMON", "LEMON"],
            ["BELL", "BELL", "BELL"],
            ["BAR", "BAR", "BAR"],
        ])
    if force == "jackpot":
        return ["SEVEN", "SEVEN", "SEVEN"]

    # 2.4% explicit win pool, otherwise independently weighted symbols.
    if random.random() < 0.024:
        r = random.random()
        if r < 0.45:
            third = weighted_symbol()
            return ["CHERRY", "CHERRY", third]
        if r < 0.67:
            return ["CHERRY"] * 3
        if r < 0.80:
            return ["LEMON"] * 3
        if r < 0.90:
            return ["BELL"] * 3
        if r < 0.965:
            return ["BAR"] * 3
        if r < 0.995:
            return ["STAR"] * 3
        return ["SEVEN"] * 3
    return [weighted_symbol(), weighted_symbol(), weighted_symbol()]


def payout(result):
    tup = tuple(result)
    if tup in PAYOUTS:
        return PAYOUTS[tup]
    if result[0] == "CHERRY" and result[1] == "CHERRY":
        return PAYOUTS[("CHERRY", "CHERRY")]
    return (0, None)


class Reel:
    def __init__(self, x, start_symbol):
        self.x = x
        self.symbols = SYMBOLS[:]
        random.shuffle(self.symbols)
        self.pos = float(self.symbols.index(start_symbol))
        self.vel = 0.0
        self.target = start_symbol
        self.stop_at = 0.0
        self.stopped = True
        self.bounce_t = 0.0

    def start(self, target, stop_at):
        self.target = target
        self.stop_at = stop_at
        self.vel = random.uniform(20.0, 25.0)
        self.stopped = False
        self.bounce_t = 0.0

    def update(self, elapsed, dt):
        if self.stopped:
            if self.bounce_t > 0:
                self.bounce_t = max(0, self.bounce_t - dt)
            return
        # hold high speed, then exponential deceleration approaching assigned stop.
        remaining = self.stop_at - elapsed
        if remaining > 0.45:
            self.vel = min(27.0, self.vel + 22.0 * dt)
        else:
            self.vel = max(2.2, self.vel * (0.78 ** (dt * FPS)))
        self.pos = (self.pos + self.vel * dt) % len(self.symbols)
        if remaining <= 0:
            target_i = self.symbols.index(self.target)
            self.pos = float(target_i) + 0.12  # intentional overshoot
            self.stopped = True
            self.bounce_t = 0.18

    def center_pos(self):
        if self.stopped and self.bounce_t > 0:
            # 0.12 -> slight downward overshoot then springs to zero.
            k = self.bounce_t / 0.18
            return (self.pos - 0.12) + 0.12 * k
        return self.pos

    def draw(self, buf, dim=False):
        cx = self.x + REEL_W // 2
        pos = self.center_pos()
        base = math.floor(pos)
        frac = pos - base
        spacing = 14
        for off in (-1, 0, 1):
            idx = (base + off) % len(self.symbols)
            cy = SPIN_CENTER_Y + (off - frac) * spacing
            if -8 <= cy <= 24:
                draw_symbol(buf, self.symbols[idx], cx, cy)
        # reel mask/window frame
        frame = (110, 70, 15) if dim else GOLD
        _line_v(buf, self.x, 1, 14, frame)
        _line_v(buf, self.x + REEL_W - 1, 1, 14, frame)
        _line_h(buf, self.x, 1, REEL_W, frame)
        _line_h(buf, self.x, 14, REEL_W, frame)


class SlotMachine:
    def __init__(self, busy, sound=True):
        self.busy = busy
        self.sound = sound
        self.audio_name = "slot-ding.wav"
        self.audio_ready = False
        self.credit = 0
        self.current = ["CHERRY", "BAR", "SEVEN"]
        self.reels = [Reel(REEL_X[i], self.current[i]) for i in range(3)]
        if sound:
            self.audio_ready = busy.upload_audio(self.audio_name, make_ding_wav())

    def base_frame(self, marquee_phase=0, dim=False):
        buf = _blank(BLACK)
        # dark cabinet backdrop and two-line neon trim
        _rect(buf, 0, 0, W, H, DARK)
        for x in range(0, W, 4):
            c = GOLD if ((x//4 + marquee_phase) % 2 == 0) else RED
            _px(buf, x, 0, c); _px(buf, x+1, 15, c)
        for reel in self.reels:
            reel.draw(buf, dim=dim)
        return buf

    def idle_frame(self, phase):
        buf = self.base_frame(phase)
        # tiny center separators / indicator lamps
        _px(buf, 23, SPIN_CENTER_Y, AMBER); _px(buf, 47, SPIN_CENTER_Y, AMBER)
        return buf

    def show_for(self, seconds, builder):
        t0 = time.monotonic()
        f = 0
        while time.monotonic() - t0 < seconds:
            self.busy.show(builder(f, time.monotonic()-t0))
            f += 1
            time.sleep(FRAME_T)

    def spin(self, force=None):
        result = choose_result(force)
        now = time.monotonic()
        stops = [1.15, 1.52, 1.95]
        # Near-miss drama: if two 7s and not jackpot, make reel 3 land just after 7.
        if force is None and result[:2] == ["SEVEN", "SEVEN"] and result[2] != "SEVEN":
            stops[2] += 0.25
        for i, reel in enumerate(self.reels):
            reel.start(result[i], stops[i])

        t0 = time.monotonic()
        last = t0
        frame = 0
        while True:
            t = time.monotonic()
            elapsed = t - t0
            dt = min(0.08, t - last)
            last = t
            for reel in self.reels:
                reel.update(elapsed, dt)
            buf = self.base_frame(frame // 2)
            self.busy.show(buf)
            frame += 1
            if all(r.stopped and r.bounce_t <= 0 for r in self.reels) and elapsed > 2.05:
                break
            time.sleep(FRAME_T)

        self.current = result
        amount, label = payout(result)
        if amount:
            self.credit += amount
            self.win_sequence(result, amount, label)
        else:
            # short settle dwell after a loss
            self.show_for(0.9, lambda f, t: self.idle_frame(f//3))
        return result, amount

    def win_sequence(self, result, amount, label):
        if self.sound and self.audio_ready:
            self.busy.play_audio(self.audio_name)

        # 1) impact flashes
        self.show_for(0.45, lambda f, t: _blank(WHITE if f % 2 == 0 else GOLD))

        # 2) symbol pulse + marquee, 1.2s
        def pulse(f, t):
            buf = _blank(DARK)
            phase = f // 2
            for x in range(W):
                c = [RED, GOLD, WHITE, MAGENTA][(x//3 + phase) % 4]
                _px(buf, x, 0, c); _px(buf, x, 15, c)
            p = 2 if (f // 2) % 2 == 0 else 0
            for i, sym in enumerate(result):
                draw_symbol(buf, sym, REEL_X[i] + REEL_W//2, AWARD_CENTER_Y, pulse=p)
            return buf
        self.show_for(1.15 if label != "JACKPOT" else 1.65, pulse)

        # 3) win banner, wipes left-right
        banner_text = "JACKPOT" if label == "JACKPOT" else ("BIG WIN" if "BIG" in label else "WIN")
        def banner(f, t):
            buf = _blank(BLACK)
            fill = min(W, int(t / 0.35 * W))
            _rect(buf, 0, 0, fill, H, RED if label == "JACKPOT" else GOLD)
            draw_text(buf, banner_text, W//2, 5, WHITE if fill > 30 else BLACK, 1, center=True)
            return buf
        self.show_for(0.75, banner)

        # 4) count-up payout; jackpot gets longer show.
        duration = 2.0 if label == "JACKPOT" else 1.35
        def countup(f, t):
            frac = min(1.0, t / duration)
            shown = max(1, int(amount * (1 - (1-frac)**3)))
            buf = _blank(BLACK)
            if label == "JACKPOT":
                # falling confetti pixels
                rng = random.Random(f // 2)
                for _ in range(18):
                    _px(buf, rng.randrange(W), rng.randrange(H), rng.choice([RED,GOLD,CYAN,MAGENTA,WHITE]))
            draw_text(buf, "+" + str(shown), W//2, 5, GREEN if label != "JACKPOT" else YELLOW, 1, center=True)
            return buf
        self.show_for(duration, countup)

        # 5) final credit splash, tiny but readable at 72x16
        def credit(f, t):
            buf = _blank(BLACK)
            draw_text(buf, "WIN", 2, 1, GOLD)
            draw_text(buf, str(amount), 2, 8, WHITE)
            draw_text(buf, str(self.credit), 71, 8, GREEN, center=False)
            # right-align manually using font width
            # overwrite by redrawing at calculated x
            return buf
        # Better custom right-aligned version
        def credit2(f, t):
            buf = _blank(BLACK)
            draw_text(buf, "WIN", 2, 1, GOLD)
            draw_text(buf, str(amount), 2, 8, WHITE)
            txt = str(self.credit)
            tw = sum((len(FONT.get(ch, FONT[' '])[0]) + 1) for ch in txt) - 1
            draw_text(buf, txt, 70 - tw, 8, GREEN)
            return buf
        self.show_for(1.0, credit2)



class StartButtonListener:
    """Direct BUSY Bar START-button listener over /api/status/ws.

    This mirrors the media-player implementation: the status WebSocket carries
    protobuf State messages. We decode only the fields needed for button input:
    State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1.
    START enum=2; PRESS=0 and RELEASE=1. Only PRESS queues a spin.
    """
    def __init__(self, host, token=None):
        self.host = host
        self.token = token
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.available = True
        self.error = None

    def start(self):
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            self.available = False
            self.error = f"websockets not installed: {exc}"
            print(f"START button unavailable ({self.error}); automatic spins still work")
            return
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def spin_requested(self):
        requested = False
        try:
            while True:
                self.events.get_nowait()
                requested = True
        except queue.Empty:
            return requested

    def _thread_main(self):
        try:
            asyncio.run(self._listen_forever())
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            print(f"START button listener stopped ({exc}); automatic spins still work")

    async def _listen_forever(self):
        import websockets

        url = _ws_url(self.host, self.token)
        backoff = 0.5
        connected_once = False
        while not self.stop_event.is_set():
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
                    else:
                        print("controls: START=spin (direct WebSocket)")
                    connected_once = True
                    self.available = True
                    self.error = None
                    backoff = 0.5

                    async for message in ws:
                        if self.stop_event.is_set():
                            return
                        if isinstance(message, str):
                            continue
                        try:
                            for event in _decode_state_inputs(bytes(message)):
                                be = event.get("button_event")
                                if be and be.get("button") == 2 and be.get("action") == 0:
                                    self.events.put(True)
                        except Exception as exc:
                            print(f"controls: ignored malformed status frame: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                self.available = False
                self.error = str(exc)
                print(f"controls: input disconnected: {exc}; reconnecting in {backoff:g}s")
                deadline = time.monotonic() + backoff
                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                backoff = min(3.0, backoff * 2.0)


def _read_varint(buf, pos):
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
            raise ValueError(f"unsupported protobuf wire type {wire}")


def _decode_input_event(buf):
    for field_no, wire, value in _iter_proto_fields(buf):
        if field_no != 1 or wire != 2:  # InputEvent.button_event
            continue
        button = 0
        action = 0
        for f, w, v in _iter_proto_fields(value):
            if w == 0 and f == 1:
                button = int(v)
            elif w == 0 and f == 2:
                action = int(v)
        return {"button_event": {"button": button, "action": action}}
    return None


def _decode_state_inputs(frame):
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

def parse_args():
    p = argparse.ArgumentParser(description="Animated classic slot machine for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--token", default=None, help="Wi-Fi access-key PIN, if required")
    p.add_argument("--spin-interval", type=float, default=15.0,
                   help="seconds between automatic spins (default: 15)")
    p.add_argument("--sound", choices=["on", "off"], default="on")
    p.add_argument("--auto-spin", choices=["on", "off"], default="on")
    p.add_argument("--test-win", action="store_true", help="force one winning spin and exit")
    p.add_argument("--test-jackpot", action="store_true", help="force 777 jackpot and exit")
    p.add_argument("--seed", type=int, default=None, help="random seed for repeatable testing")
    return p.parse_args()


def main():
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    args.spin_interval = max(3.0, args.spin_interval)

    busy = Busy(args.host)
    slot = SlotMachine(busy, sound=args.sound == "on")
    controls = StartButtonListener(args.host, args.token)
    controls.start()
    print(f"slot-machine -> {busy.base}  (Ctrl-C to stop)")
    try:
        # Always spin immediately on startup.
        if args.test_jackpot:
            slot.spin("jackpot")
            return
        if args.test_win:
            slot.spin("win")
            return

        slot.spin()
        last_spin = time.monotonic()
        frame = 0
        while True:
            now = time.monotonic()
            manual_spin = controls.spin_requested()
            automatic_spin = args.auto_spin == "on" and now - last_spin >= args.spin_interval
            if manual_spin or automatic_spin:
                slot.spin()
                last_spin = time.monotonic()
            else:
                busy.show(slot.idle_frame(frame // 4))
                frame += 1
                time.sleep(0.12)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {busy.base} - {e.reason}")
    finally:
        controls.stop()
        busy.clear()


if __name__ == "__main__":
    main()
