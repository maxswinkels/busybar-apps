#!/usr/bin/env python3
"""Pinball DMD demo for BUSY Bar.

A procedural 90s/early-2000s pinball dot-matrix-display inspired attract mode
for the 72x16 BUSY Bar. No copyrighted table graphics are used.

Examples:
  python3 pinball_busybar_v5.py
  python3 pinball_busybar_v5.py --host 127.0.0.1:8080
  python3 pinball_busybar_v5.py --fps 14 --palette color
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

APP = "busybar-pinball"
W, H = 72, 16

PALETTES = {
    "amber": [(8, 2, 0), (70, 15, 0), (160, 45, 0), (255, 120, 10)],
    "red":   [(8, 0, 0), (70, 0, 0), (170, 10, 0), (255, 60, 10)],
}

# Multicolour mode keeps the black-background DMD character, but uses a
# restrained arcade palette to separate information and give each scene its
# own identity. Level 1/2/3 still controls brightness within the chosen hue.
COLOR_RAMPS = {
    "orange": [(20, 4, 0), (95, 20, 0), (190, 55, 0), (255, 135, 20)],
    "yellow": [(18, 12, 0), (85, 55, 0), (190, 130, 0), (255, 225, 70)],
    "red":    [(20, 0, 0), (95, 0, 5), (195, 15, 20), (255, 65, 35)],
    "green":  [(0, 14, 4), (0, 75, 20), (20, 170, 55), (90, 255, 120)],
    "cyan":   [(0, 10, 18), (0, 65, 90), (0, 155, 195), (75, 235, 255)],
    "blue":   [(2, 4, 22), (10, 35, 105), (20, 85, 210), (80, 160, 255)],
    "purple": [(12, 0, 20), (60, 10, 90), (135, 25, 190), (225, 85, 255)],
    "pink":   [(20, 0, 10), (95, 10, 55), (200, 25, 120), (255, 95, 185)],
    "white":  [(12, 12, 12), (75, 75, 75), (170, 170, 170), (255, 255, 255)],
}

FONT = {
    "A": ["01110","10001","10001","11111","10001","10001","10001"],
    "B": ["11110","10001","10001","11110","10001","10001","11110"],
    "C": ["01111","10000","10000","10000","10000","10000","01111"],
    "D": ["11110","10001","10001","10001","10001","10001","11110"],
    "E": ["11111","10000","10000","11110","10000","10000","11111"],
    "F": ["11111","10000","10000","11110","10000","10000","10000"],
    "G": ["01111","10000","10000","10111","10001","10001","01111"],
    "H": ["10001","10001","10001","11111","10001","10001","10001"],
    "I": ["11111","00100","00100","00100","00100","00100","11111"],
    "J": ["00111","00010","00010","00010","10010","10010","01100"],
    "K": ["10001","10010","10100","11000","10100","10010","10001"],
    "L": ["10000","10000","10000","10000","10000","10000","11111"],
    "M": ["10001","11011","10101","10101","10001","10001","10001"],
    "N": ["10001","11001","10101","10011","10001","10001","10001"],
    "O": ["01110","10001","10001","10001","10001","10001","01110"],
    "P": ["11110","10001","10001","11110","10000","10000","10000"],
    "Q": ["01110","10001","10001","10001","10101","10010","01101"],
    "R": ["11110","10001","10001","11110","10100","10010","10001"],
    "S": ["01111","10000","10000","01110","00001","00001","11110"],
    "T": ["11111","00100","00100","00100","00100","00100","00100"],
    "U": ["10001","10001","10001","10001","10001","10001","01110"],
    "V": ["10001","10001","10001","10001","10001","01010","00100"],
    "W": ["10001","10001","10001","10101","10101","11011","10001"],
    "X": ["10001","10001","01010","00100","01010","10001","10001"],
    "Y": ["10001","10001","01010","00100","00100","00100","00100"],
    "Z": ["11111","00001","00010","00100","01000","10000","11111"],
    "0": ["01110","10001","10011","10101","11001","10001","01110"],
    "1": ["00100","01100","00100","00100","00100","00100","01110"],
    "2": ["01110","10001","00001","00010","00100","01000","11111"],
    "3": ["11110","00001","00001","01110","00001","00001","11110"],
    "4": ["00010","00110","01010","10010","11111","00010","00010"],
    "5": ["11111","10000","10000","11110","00001","00001","11110"],
    "6": ["01110","10000","10000","11110","10001","10001","01110"],
    "7": ["11111","00001","00010","00100","01000","01000","01000"],
    "8": ["01110","10001","10001","01110","10001","10001","01110"],
    "9": ["01110","10001","10001","01111","00001","00001","01110"],
    "!": ["00100","00100","00100","00100","00100","00000","00100"],
    "-": ["00000","00000","00000","11111","00000","00000","00000"],
    " ": ["000","000","000","000","000","000","000"],
}


def parse_args():
    p = argparse.ArgumentParser(description="90s pinball DMD inspired attract mode for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--title-top", default="BUSYBAR",
                   help="top word on the opening title screen (default: BUSYBAR)")
    p.add_argument("--title-bottom", default="PINBALL",
                   help="bottom word on the opening title screen (default: PINBALL)")
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--palette", choices=["color", *sorted(PALETTES)], default="color",
                   help="display palette (default: color; amber/red emulate classic monochrome DMDs)")
    p.add_argument("--test", action="store_true", help="render a few frames locally as PNGs and exit")
    return p.parse_args()


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _png(pixels):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            r, g, b = pixels[y * W + x]
            raw += bytes((r, g, b, 255))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


_RING = 4
_frame_no = 0

def _post(host, path, data, content_type):
    req = urllib.request.Request(_base(host) + path, data=data, method="POST", headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode()


def show(host, pixels):
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    try:
        _post(host, f"/api/assets/upload?application_name={APP}&file={fn}", _png(pixels), "application/octet-stream")
        body = {"application_name": APP, "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}]}
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
        return e.code


def clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def blank():
    return [0] * (W * H)


def px(buf, x, y, v):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = max(buf[y * W + x], max(0, min(3, int(v))))


def rect(buf, x, y, w, h, v):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            px(buf, xx, yy, v)


def line(buf, x0, y0, x1, y1, v):
    dx, sx = abs(x1 - x0), 1 if x0 < x1 else -1
    dy, sy = -abs(y1 - y0), 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        px(buf, x0, y0, v)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy; x0 += sx
        if e2 <= dx:
            err += dx; y0 += sy


def text_width(s, scale=1):
    return sum((len(FONT.get(ch, FONT[" "])[0]) + 1) * scale for ch in s.upper()) - scale


def draw_text(buf, s, x, y, v=3, scale=1):
    pen = x
    for ch in s.upper():
        glyph = FONT.get(ch, FONT[" "])
        gw = len(glyph[0])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    rect(buf, pen + gx * scale, y + gy * scale, scale, scale, v)
        pen += (gw + 1) * scale


def centered_text(buf, s, y, v=3, scale=1):
    draw_text(buf, s, (W - text_width(s, scale)) // 2, y, v, scale)


def tracked_text_width(s, spacing=2):
    """Pixel width with explicit inter-letter tracking."""
    chars = s.upper()
    if not chars:
        return 0
    return sum(len(FONT.get(ch, FONT[" "])[0]) for ch in chars) + spacing * (len(chars) - 1)


def draw_tracked_text(buf, s, x, y, v=3, spacing=2):
    """Draw 5x7 text with a configurable number of pixels between glyphs."""
    pen = x
    for ch in s.upper():
        glyph = FONT.get(ch, FONT[" "])
        gw = len(glyph[0])
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1":
                    px(buf, pen + gx, y + gy, v)
        pen += gw + spacing


def centered_bold_text(buf, s, y, v=3, spacing=1):
    """Draw a 1px-thickened 5x7 title with optional inter-letter tracking."""
    width = tracked_text_width(s, spacing)
    x = (W - (width + 1)) // 2
    draw_tracked_text(buf, s, x, y, v, spacing)
    draw_tracked_text(buf, s, x + 1, y, v, spacing)


def dither_rgb(level_buf, palette):
    # Keep the image discrete, not anti-aliased.
    return [palette[v] for v in level_buf]


def colorize(level_buf, scene_name, t):
    """Map intensity pixels to a restrained per-scene arcade colour scheme.

    The renderer itself remains a 4-level DMD-style bitmap. Colour is applied
    only here, so the geometry, dithering and hard pixel edges stay intact.
    """
    out = []
    pulse = int(t * 6) & 1
    for i, level in enumerate(level_buf):
        if level <= 0:
            out.append((0, 0, 0))
            continue
        x, y = i % W, i // W

        if scene_name == "logo":
            # Keep each title line monochrome regardless of its text length.
            # The starburst outside the lettering retains the animated accent.
            if 1 <= y <= 7:
                hue = "purple"
            elif 9 <= y <= 15:
                hue = "cyan"
            else:
                hue = "blue" if (x // 5 + pulse) % 2 else "pink"

        elif scene_name == "score":
            # SCORE, score value, BALL and multiplier are each monochrome blocks.
            score_w = text_width("SCORE")
            if y < 8 and 1 <= x < 1 + score_w:
                hue = "cyan"
            elif y < 8:
                hue = "yellow"
            elif y >= 9 and x < 43:
                hue = "green"
            elif y >= 9 and x >= 55:
                hue = "pink"
            else:
                hue = "orange"

        elif scene_name == "race":
            # Road/track is blue-white; the racing car gets hot bodywork and cool glass.
            if y >= 13:
                hue = "white"
            elif y <= 7:
                hue = "red"
            else:
                hue = "cyan" if (x // 3) % 2 else "orange"

        elif scene_name == "multiball":
            # MULTIBALL itself stays monochrome; surrounding balls/bumper sparks
            # provide the colour without splitting the lettering.
            title_w = tracked_text_width("MULTIBALL", 2) + 1
            title_x = (W - title_w) // 2
            title_y = min(5, max(-7, int(t * 12) - 7))
            if title_y <= y <= title_y + 7 and title_x <= x < title_x + title_w:
                hue = "yellow"
            elif (x < 14 or x > 57):
                hue = "purple" if (x + y + pulse) % 2 else "cyan"
            else:
                hue = "red" if (x + y) % 3 == 0 else "orange"

        elif scene_name == "jackpot":
            # Both text lines are strictly monochrome; only surrounding sparks vary.
            title_x = (W - text_width("JACKPOT!")) // 2
            amount_x = (W - text_width("5000000")) // 2
            if 0 <= y <= 6 and title_x <= x < title_x + text_width("JACKPOT!"):
                hue = "red"
            elif 9 <= y <= 15 and amount_x <= x < amount_x + text_width("5000000"):
                hue = "yellow"
            else:
                hue = "purple" if (x + y + pulse) % 3 == 0 else "cyan"

        elif scene_name == "bonus":
            # BONUS and the multiplier are each a single hue.
            bonus_x = (W - text_width("BONUS")) // 2
            if 0 <= y <= 6 and bonus_x <= x < bonus_x + text_width("BONUS"):
                hue = "green"
            elif 7 <= y <= 15 and 24 <= x <= 48:
                hue = "yellow"
            else:
                hue = "cyan" if (x // 4 + pulse) % 2 else "purple"

        elif scene_name == "extra_ball":
            # The scrolling phrase itself is always one colour. Decorative
            # pixels may use a second hue without splitting the lettering.
            if 4 <= y <= 10:
                hue = "pink"
            else:
                hue = "purple" if (x // 5 + pulse) % 2 else "cyan"

        elif scene_name == "insert_coin":
            hue = "yellow"
        elif scene_name == "super_jackpot":
            # Both words stay monochrome; sparks provide the extra colour.
            if 0 <= y <= 6:
                hue = "purple"
            elif 9 <= y <= 15:
                hue = "yellow"
            else:
                hue = "red" if (x + y + pulse) % 2 else "cyan"
        elif scene_name == "ball_locked":
            # BALL LOCKED remains a single-colour message; lock/ball accents vary.
            if 0 <= y <= 6:
                hue = "cyan"
            elif 9 <= y <= 15:
                hue = "yellow"
            else:
                hue = "purple" if (x + pulse) % 2 else "white"
        elif scene_name == "tilt":
            hue = "red"
        elif scene_name == "match":
            # MATCH and the selected number are separate monochrome text blocks.
            # Decorative edge lamps provide the secondary colours.
            if y <= 6:
                hue = "cyan"
            elif 8 <= y <= 15 and 24 <= x <= 48:
                hue = "yellow"
            else:
                hue = "purple" if (x + pulse) % 2 else "pink"
        else:
            hue = "orange"

        out.append(COLOR_RAMPS[hue][level])
    return out


TITLE_TOP = "BUSYBAR"
TITLE_BOTTOM = "PINBALL"

def scene_logo(t, cycle=0):
    b = blank()
    # expanding concentric diamond / starburst
    cx, cy = 36, 7
    r = 3 + int((t * 14) % 20)
    for k, v in ((r, 1), (r-4, 2), (r-8, 3)):
        if k > 0:
            line(b, cx-k, cy, cx, cy-k//2, v); line(b, cx, cy-k//2, cx+k, cy, v)
            line(b, cx+k, cy, cx, cy+k//2, v); line(b, cx, cy+k//2, cx-k, cy, v)
    centered_text(b, TITLE_TOP, 1, 2)
    centered_text(b, TITLE_BOTTOM, 9, 3)
    return b


_score_cycle = None
_score_value = 0
_score_step = -1


def scene_score(t, cycle=0):
    b = blank()

    # Stateful score simulation. Every attract loop starts from a different
    # plausible score, then adds random pinball-like awards several times per
    # second while this card is visible. The score therefore only goes up.
    global _score_cycle, _score_value, _score_step
    step = int(t * 6.0)
    if cycle != _score_cycle:
        _score_cycle = cycle
        _score_value = random.randrange(10000, 90000, 100)
        _score_step = -1
    while _score_step < step:
        _score_step += 1
        if _score_step == 0:
            continue
        award = random.choice((100, 250, 500, 1000, 2500, 5000, 10000))
        # Occasional larger award, as if a target bank / mode just completed.
        if random.random() < 0.12:
            award *= random.choice((2, 3))
        _score_value = min(999999, _score_value + award)

    ball = 1 + ((cycle * 5 + 1) % 3)
    mult = 2 + ((cycle + int(t)) % 4)
    draw_text(b, "SCORE", 1, 0, 1)
    s = f"{_score_value:06d}"
    draw_text(b, s, max(0, 71 - text_width(s)), 0, 3)
    draw_text(b, f"BALL {ball}", 1, 9, 2)
    draw_text(b, f"X{mult}", 55, 9, 3)
    if int(t * 5) % 2:
        rect(b, 43, 10, 2, 2, 2); rect(b, 47, 10, 2, 2, 1)
    return b

def scene_jackpot(t, cycle=0):
    b = blank()
    phase = int(t * 10) % 8
    for x in range(-10, 82, 12):
        line(b, x + phase, 15, x + 10 + phase, 0, 1)
    centered_text(b, "JACKPOT!", 0, 3)
    amount = "5000000"
    centered_text(b, amount, 9, 2)
    # sparks
    for i in range(10):
        a = i * math.pi * 0.2 + t * 4
        rr = 6 + (i % 3)
        px(b, 36 + int(math.cos(a) * rr), 8 + int(math.sin(a) * rr * 0.45), 3)
    return b


def scene_race(t, cycle=0):
    b = blank()
    # Side-view racing car crossing the display from left to right.
    car_x = int((t * 25) % (W + 20)) - 18
    bob = 1 if int(t * 10) % 2 else 0
    y = 6 + bob

    # Dashed road and a moving lane marker sell the racing motion.
    rect(b, 0, 14, W, 1, 1)
    road_phase = int(t * 30) % 12
    for x in range(-road_phase, W, 12):
        rect(b, x, 12, 6, 1, 1)

    # Compact Formula/GT-like silhouette: nose -> cockpit -> rear wing.
    rect(b, car_x + 2, y + 3, 13, 3, 3)        # body
    rect(b, car_x + 12, y + 2, 5, 2, 3)        # nose
    rect(b, car_x + 5, y + 1, 6, 2, 2)         # cockpit
    rect(b, car_x + 1, y, 2, 4, 2)             # rear wing upright
    rect(b, car_x, y, 5, 1, 3)                 # rear wing
    rect(b, car_x + 7, y, 3, 1, 1)             # windscreen/glass
    # Wheels, with bright hubs on alternating frames.
    for wx in (car_x + 4, car_x + 13):
        rect(b, wx, y + 5, 3, 2, 2)
        px(b, wx + 1, y + 5, 3 if int(t * 12) % 2 else 1)
    # A few exhaust/speed pixels behind the car.
    for i in range(4):
        px(b, car_x - 2 - i * 3, y + 4 + (i & 1), max(1, 3 - i // 2))
    return b

def scene_multiball(t, cycle=0):
    """Bold MULTIBALL drops from above, surrounded by pinball-style pixel art."""
    b = blank()

    # Enter from above and settle around the vertical centre.
    title_y = min(5, -7 + int(t * 12))
    centered_bold_text(b, "MULTIBALL", title_y, 3, spacing=2)

    # Two animated pinballs bounce near the lower corners. Their tiny highlights
    # make them read as metallic balls even at this resolution.
    bounce = abs((int(t * 12) % 10) - 5)
    for bx, phase in ((8, 0), (63, 3)):
        by = 12 - max(0, 3 - abs(bounce - phase))
        rect(b, bx - 1, by - 1, 3, 3, 2)
        px(b, bx - 1, by - 1, 3)
        px(b, bx, by, 1)

    # Mini bumper/starbursts at the sides. Alternate intensity for a mechanical
    # pop effect rather than a smooth animation.
    flash = 3 if int(t * 8) % 2 else 1
    for cx, cy in ((18, 12), (53, 12)):
        px(b, cx, cy, 3)
        px(b, cx - 2, cy, flash); px(b, cx + 2, cy, flash)
        px(b, cx, cy - 2, flash); px(b, cx, cy + 2, flash)
        px(b, cx - 1, cy - 1, 2); px(b, cx + 1, cy - 1, 2)

    # A few travelling spark pixels keep the card alive after the title settles.
    spark_phase = int(t * 20)
    for i in range(5):
        sx = (spark_phase + i * 15) % W
        sy = 1 + ((i * 3 + spark_phase // 3) % 14)
        px(b, sx, sy, 1 + (i % 3))
    return b


def scene_bonus(t, cycle=0):
    b = blank()
    centered_text(b, "BONUS", 0, 2)
    multiplier = 1 + int(t * 2.0) % 10
    centered_text(b, f"{multiplier}X", 7, 3, scale=1)
    # moving scanlines and side chevrons
    y = int((t * 8) % H)
    for x in range(0, W, 2):
        px(b, x, y, 1)
    for yy in range(2, 14, 3):
        px(b, 2, yy, 2); px(b, 3, yy+1, 2); px(b, 69, yy, 2); px(b, 68, yy+1, 2)
    return b


def scene_extra_ball(t, cycle=0):
    b = blank()
    phrase = "EXTRA BALL"
    width = text_width(phrase)

    # Scroll once, fully from right to left. The scene duration is long enough
    # for the final letter to leave the display before the next attract card.
    x = W - int(t * 31)
    draw_text(b, phrase, x, 4, 3)

    # Bonus-style moving border / sparkle treatment.
    phase = int(t * 14) % 8
    for xx in range(-phase, W, 8):
        px(b, xx, 1, 1)
        px(b, W - 1 - xx, 14, 1)
    for i in range(5):
        sx = (int(t * 23) + i * 17) % W
        sy = 2 + ((i * 5 + int(t * 9)) % 11)
        px(b, sx, sy, 2 if i & 1 else 1)
    return b


def scene_insert_coin(t, cycle=0):
    """Large INSERT/COIN rolls upward, then the complete message flashes twice."""
    b = blank()

    # Phase 1: two oversized words form one vertical rolling message. At scale
    # 2, INSERT is 70px wide and still fits the 72px BUSY Bar almost edge-to-edge.
    # COIN follows immediately behind it from below.
    roll_time = 2.35
    if t < roll_time:
        speed = 15.5  # pixels/sec upward
        insert_y = 16 - int(t * speed)
        coin_y = insert_y + 18
        centered_text(b, "INSERT", insert_y, 3, scale=2)
        centered_text(b, "COIN", coin_y, 3, scale=2)

        # Sparse vertical motion streaks make the upward movement read clearly
        # without competing with the lettering.
        phase = int(t * 18) % 9
        for x in (3, 68):
            for yy in range(phase - 9, H, 9):
                rect(b, x, yy, 1, 3, 1)
        return b

    # Phase 2: settle the complete phrase on two lines, flash it exactly twice
    # (ON-OFF-ON-OFF), then hold it steadily ON for one full second.
    ft = t - roll_time
    flash_step = int(ft / 0.24)
    visible = flash_step in (0, 2) or ft >= 0.96
    if visible:
        centered_text(b, "INSERT", 0, 3)
        centered_text(b, "COIN", 9, 3)
    return b


def scene_super_jackpot(t, cycle=0):
    b = blank()
    # A short pixel zoom: SUPER settles first, then JACKPOT slams in underneath.
    super_y = max(0, 5 - int(t * 12))
    jackpot_y = min(9, 16 - int(max(0.0, t - 0.28) * 18))
    centered_text(b, "SUPER", super_y, 2)
    centered_text(b, "JACKPOT", jackpot_y, 3)
    # Expanding starburst around the message.
    rr = 2 + int((t * 15) % 12)
    for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,1),(1,-1),(-1,-1)):
        px(b, 36 + dx * rr, 8 + dy * max(1, rr // 2), 1 + (rr % 3))
    return b


def scene_ball_locked(t, cycle=0):
    b = blank()
    centered_text(b, "BALL", 0, 2)
    centered_text(b, "LOCKED", 9, 3)
    # A pinball rolls into a small lock/cage at the right.
    ball_x = min(57, -4 + int(t * 28))
    ball_y = 7 + int(math.sin(t * 9) * 2)
    rect(b, ball_x, ball_y, 3, 3, 2); px(b, ball_x, ball_y, 3)
    # Lock body and shackle.
    rect(b, 61, 5, 8, 8, 1)
    rect(b, 63, 3, 4, 3, 2)
    rect(b, 64, 4, 2, 2, 0)
    if t > 2.0 and int(t * 8) % 2:
        rect(b, 60, 4, 10, 10, 3)
        rect(b, 61, 5, 8, 8, 0)
    return b


def scene_tilt(t, cycle=0):
    b = blank()
    # Deliberately stark: large red TILT! shakes, then briefly blacks out.
    if 1.55 < t < 1.85:
        return b
    shake = (-1, 1, 0, 1, -1, 0)[int(t * 18) % 6]
    centered_bold_text(b, "TILT!", 4 + shake, 3, spacing=2)
    # Warning bars at the edges vibrate in the opposite direction.
    for yy in range(1, 15, 3):
        px(b, 2 + shake, yy, 2); px(b, 69 - shake, yy, 2)
    return b


def scene_match(t, cycle=0):
    """Classic end-of-ball MATCH: spin the tens, settle, then flash the result."""
    b = blank()
    # Stable pseudo-random result for the whole attract cycle: 00, 10 ... 90.
    rng = random.Random(0x4D41544348 + cycle * 7919)
    result = rng.randrange(10) * 10

    centered_text(b, "MATCH", 0, 3)

    # For the first part, race through the possible match numbers like a reel.
    # The reel progressively slows before snapping to the selected value.
    settle_at = 2.45
    if t < settle_at:
        # Fast at first, slower near the stop. Quantising the phase gives the
        # characteristic stepped electromechanical / DMD number roll.
        progress = t / settle_at
        rate = 13.0 - 8.0 * progress
        step = int(t * rate + t * t * 1.7)
        value = ((step + cycle * 3) % 10) * 10
        centered_bold_text(b, f"{value:02d}", 8, 3, spacing=2)
    else:
        # Once stopped, flash the winning number twice, then leave it lit.
        ft = t - settle_at
        visible = True
        if ft < 1.0:
            visible = int(ft / 0.25) % 2 == 0
        if visible:
            centered_bold_text(b, f"{result:02d}", 8, 3, spacing=2)

    # Small side lamps chase while the reel spins and freeze when it settles.
    lamp_phase = int(t * 10) if t < settle_at else result // 10
    for i, yy in enumerate((2, 6, 10, 14)):
        v = 3 if (i + lamp_phase) % 4 == 0 else 1
        rect(b, 2, yy, 2, 1, v)
        rect(b, 68, yy, 2, 1, v)
    return b


# The attract sequence deliberately ends with INSERT COIN again. The same scene
# function is reused, so the closing card repeats the full rise/flash/hold motion.
SCENES = [scene_logo, scene_score, scene_race, scene_multiball, scene_jackpot, scene_bonus,
          scene_extra_ball, scene_insert_coin, scene_super_jackpot, scene_ball_locked,
          scene_tilt, scene_match, scene_insert_coin]
DURATIONS = [2.6, 3.8, 3.0, 3.7, 2.4, 2.8, 4.55, 4.31, 2.9, 3.2, 2.2, 4.1, 4.31]

SCENE_NAMES = ["logo", "score", "race", "multiball", "jackpot", "bonus", "extra_ball",
               "insert_coin", "super_jackpot", "ball_locked", "tilt", "match", "insert_coin"]


def render_at(t):
    total = sum(DURATIONS)
    cycle = int(t // total)
    tt = t % total
    acc = 0.0
    for scene, scene_name, dur in zip(SCENES, SCENE_NAMES, DURATIONS):
        if tt < acc + dur:
            local = tt - acc
            return scene(local, cycle), scene_name, local
        acc += dur
    return SCENES[0](0, cycle), SCENE_NAMES[0], 0.0


def _rgb_frame(levels, scene_name, local_t, palette_name):
    if palette_name == "color":
        return colorize(levels, scene_name, local_t)
    return dither_rgb(levels, PALETTES[palette_name])


def save_test_frames(palette_name):
    for i, ts in enumerate((0.4, 3.2, 7.0, 10.4, 13.0, 16.0)):
        levels, scene_name, local_t = render_at(ts)
        data = _png(_rgb_frame(levels, scene_name, local_t, palette_name))
        path = f"/mnt/data/pinball_dmd_preview_{i+1}.png"
        with open(path, "wb") as f:
            f.write(data)
        print(path)


def main():
    global TITLE_TOP, TITLE_BOTTOM
    args = parse_args()
    TITLE_TOP = args.title_top.upper()
    TITLE_BOTTOM = args.title_bottom.upper()
    if args.test:
        save_test_frames(args.palette)
        return
    interval = 1.0 / max(1, args.fps)
    print(f"busybar-pinball -> {_base(args.host)}  palette={args.palette}  (Ctrl-C to stop)")
    t0 = time.monotonic()
    try:
        while True:
            ft = time.monotonic()
            levels, scene_name, local_t = render_at(ft - t0)
            show(args.host, _rgb_frame(levels, scene_name, local_t, args.palette))
            dt = time.monotonic() - ft
            if dt < interval:
                time.sleep(interval - dt)
    except KeyboardInterrupt:
        pass
    finally:
        clear(args.host)
        print("stopped.")


if __name__ == "__main__":
    main()
