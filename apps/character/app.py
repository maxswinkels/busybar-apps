#!/usr/bin/env python3
"""Character: a face on the BUSY Bar that reacts to what your machine is doing.

    python3 app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
    python3 app.py --mood grumpy          # hold one expression, ignore the sensors
    python3 app.py --demo                 # cycle every mood, ~2.2 s each
    python3 app.py --preview              # ANSI render in this terminal, no device
    python3 app.py --signals              # print what the sensors report, then exit
    python3 app.py --preview --simulate-input ok,encoder:+3   # reactions, no device

The face is parametric rather than a set of bitmaps: eyes, brows and mouth are
numbers (openness, gaze, tilt, curve) that interpolate, so moods blend into one
another and a blink can land inside any expression. Moods come from the machine
- CPU load, live Claude Code turns, battery, keyboard idle time - and a small
idle layer keeps blinking and glancing around on top of whatever mood is showing.

The face also answers the bar's own controls: ok makes it blink, back makes it
scowl, start lands a punch on the top of its head that it visibly recoils from,
and the wheel aims where the eyes rest - park it on yourself and the face keeps
coming back to you between glances. That layer reads the device's status
stream, which is protobuf over WebSocket and so needs the
``busylib`` package (Character/requirements.txt). Everything else here is
stdlib-only, and --no-input, --preview, --signals and --simulate-input all run
on a machine without it.

Options: --mood NAME  --theme {amber,cyan,coral,mint}  --fps N  --once
         --no-input  --simulate-input SCRIPT  --breathe
         --token PASSWORD  (device HTTP API password, default $BUSY_HTTP_PASSWORD)

A bar whose HTTP API is password-protected takes the password from the
BUSY_HTTP_PASSWORD environment variable (or --token); localhost needs none.

One 72x16 image goes out per frame (individual rect elements cost ~3.6 ms each
on the device, a full-frame image is a flat ~50 ms), and an unchanged frame is
skipped entirely - a resting face pushes nothing at all until it blinks, which
is what keeps the display free for other apps. Frames go out on their own
thread, so a slow or absent bar costs stale pixels rather than a stalled
animation. --breathe trades the stillness away for a face that is never quite
still: it costs ~5 pushes a second at rest.
"""
import argparse
import json
import math
import os
import queue
import random
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, fields, replace

APP = "character"
W, H = 72, 16
BG = (0, 0, 0)

# ---------------------------------------------------------------------------
# Face geometry (72x16)
#
# The panel is 4.5:1, so a centred face would waste two thirds of it: the eyes
# sit near the outer edges and the mouth lives in the gap between them, low
# enough to read as a mouth rather than a third eye. Only the features light up
# (no head outline) - on an LED matrix that gives the most contrast per pixel.
#
#   y 0..2   brows        (raised brows need row 0, so the resting top is y=1)
#   y 3..12  eyes         (ellipse, cy=8, ry=4.5)
#   y 8..15  mouth        (curve around y=11, between the eyes horizontally)
# ---------------------------------------------------------------------------

EYE_CX = (15.0, 57.0)   # left / right eye centre x (mirrored around x=36)
EYE_CY = 8.0
EYE_RX = 7.0
EYE_RY = 4.5
EYE_MIN_RY = 0.9        # a fully closed eye stays a ~2 px lid line, not nothing
PUPIL_R = 2.0           # the dark hole that carries gaze direction; larger and
                        # the eye reads as a ring of glasses rather than an eye
PUPIL_TRAVEL_X = 3.0    # px the pupil moves at |pupil_x| = 1
PUPIL_TRAVEL_Y = 1.8
PUPIL_SHOW = 0.34       # below this openness the lid hides the pupil entirely
LID_LINE = 0.12         # at/below this the eye is drawn as a straight closed lid

BROW_W = 12.0
BROW_Y = 1.0            # resting top y of the brow bar
BROW_T = 2.0            # brow thickness in px
BROW_TILT = 3.0         # px the inner end drops at brow_angle = 1

MOUTH_CX = 36.0         # centred between the eyes
MOUTH_HALF = 9.0        # half width at mouth_w = 1 (18 px, the widest grin)
MOUTH_Y = 10.5          # corner height; the centre bulges from here
MOUTH_AMP = 3.0         # px the centre travels at |mouth_curve| = 1
MOUTH_T = 2.0           # closed-mouth line thickness
MOUTH_MAX_OPEN = 4.0    # extra px of gap at mouth_open = 1
EYE_MAX = 1.25          # widest an eye may open; keeps the ellipse on the panel

SWEAT_X = 66.0          # bead of sweat, outboard of the right eye
SWEAT_Y = 2.0
SWEAT_FALL = 4.0        # px it drifts down over one loop

# ---------------------------------------------------------------------------
# Themes. Every feature shares one colour: two-tone faces read as noise at this
# size. ``tint`` on a mood overrides it (stress goes red) and interpolates.
# ---------------------------------------------------------------------------

THEMES = {
    "amber": {"face": (255, 176, 40), "sweat": (143, 184, 232)},
    "cyan": {"face": (90, 210, 255), "sweat": (255, 255, 255)},
    "coral": {"face": (205, 110, 88), "sweat": (143, 184, 232)},  # Clawd's coral
    "mint": {"face": (90, 230, 160), "sweat": (143, 184, 232)},
}
RED = (255, 64, 48)     # stressed tint
BLUE = (120, 170, 255)  # sad / low-battery tint


# ---------------------------------------------------------------------------
# Face state
#
# One mood = one FaceState. Blending two of them field by field is what makes
# "scowling while looking left" a sum of numbers instead of a 16th bitmap.
# ---------------------------------------------------------------------------

@dataclass
class FaceState:
    eye_l: float = 1.0        # 0 = closed, 1 = wide open
    eye_r: float = 1.0
    pupil_x: float = 0.0      # -1 = hard left, +1 = hard right
    pupil_y: float = 0.0      # -1 = up, +1 = down
    brow_angle: float = 0.0   # +1 = inner ends down (scowl), -1 = up (worry)
    brow_y: float = 0.0       # px offset, negative = raised
    mouth_curve: float = 0.0  # +1 = smile, -1 = frown
    mouth_open: float = 0.0   # 0..1
    mouth_w: float = 0.6      # width as a fraction of MOUTH_HALF; a grin is
                              # wide, a surprised "o" is narrow
    sweat: float = 0.0        # 0..1 bead visibility
    squash: float = 0.0       # vertical compression pinned to the bottom row:
                              # +1 flat, 0 resting, negative stretches upward
    tint: tuple = None        # per-mood colour override, None = theme colour


MOODS = {
    "neutral": FaceState(),
    "happy": FaceState(eye_l=0.82, eye_r=0.82, brow_y=-1.0, mouth_curve=0.9,
                       mouth_w=1.0),
    "excited": FaceState(eye_l=1.1, eye_r=1.1, brow_y=-1.0, mouth_curve=1.0,
                         mouth_open=0.55, mouth_w=0.95, pupil_y=-0.2),
    "focused": FaceState(eye_l=0.5, eye_r=0.5, brow_angle=0.35, brow_y=0.5,
                         mouth_curve=-0.1, mouth_w=0.5),
    "grumpy": FaceState(eye_l=0.7, eye_r=0.7, brow_angle=1.0, brow_y=0.5,
                        mouth_curve=-0.8, mouth_w=0.8),
    "stressed": FaceState(brow_angle=0.85, mouth_curve=-0.65, mouth_open=0.25,
                          mouth_w=0.75, sweat=1.0, pupil_y=0.25, tint=RED),
    "sad": FaceState(eye_l=0.78, eye_r=0.78, brow_angle=-0.85, brow_y=0.5,
                     mouth_curve=-0.6, mouth_w=0.7, pupil_y=0.45, tint=BLUE),
    "surprised": FaceState(eye_l=1.2, eye_r=1.2, brow_y=-1.0, mouth_open=1.0,
                           mouth_w=0.45),
    # Brows drop towards the shut lids: left at their resting height they float
    # as a detached pair of dashes above an otherwise empty face.
    "sleepy": FaceState(eye_l=0.3, eye_r=0.3, brow_y=1.5, pupil_y=0.45,
                        mouth_curve=-0.15, mouth_w=0.5),
    "asleep": FaceState(eye_l=0.0, eye_r=0.0, brow_y=2.0, mouth_curve=0.2,
                        mouth_w=0.45),
    # Where the face is left once a blow has stopped ringing: lids heavy, brows
    # up in complaint, pupils rolled towards the ceiling, sweating.
    "punched": FaceState(eye_l=0.55, eye_r=0.55, brow_angle=-0.7, brow_y=-0.5,
                         pupil_y=-0.4, mouth_curve=-0.5, mouth_open=0.35,
                         mouth_w=0.55, sweat=0.9),
    "wink": FaceState(eye_r=0.0, mouth_curve=0.85, mouth_w=0.95, brow_y=-0.5),
    "look_left": FaceState(pupil_x=-1.0, brow_y=-0.5),
    "look_right": FaceState(pupil_x=1.0, brow_y=-0.5),
}

# Moods that must not be interrupted by the idle layer: closed eyes cannot
# blink, and a deliberate gaze must not be overridden by a random dart.
NO_BLINK = {"asleep", "wink"}
NO_DART = {"asleep", "sleepy", "look_left", "look_right", "focused"}

TRANSITION = 0.38  # s to cross-fade between two moods


def _lerp(a, b, u):
    return a + (b - a) * u


def _ease(u):
    """Smoothstep - takes the mechanical edge off a mood change."""
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


def blend(a: FaceState, b: FaceState, u: float, theme: dict) -> FaceState:
    """Interpolate every field of two moods; ``tint`` blends via the theme
    colour so a mood that only sets a colour still fades in smoothly."""
    out = {}
    for f in fields(FaceState):
        if f.name == "tint":
            ca = a.tint or theme["face"]
            cb = b.tint or theme["face"]
            out["tint"] = tuple(round(_lerp(ca[i], cb[i], u)) for i in range(3))
        else:
            out[f.name] = _lerp(getattr(a, f.name), getattr(b, f.name), u)
    return FaceState(**out)


# ---------------------------------------------------------------------------
# Pixel buffer and drawing primitives
#
# Coverage-based (3x3 supersampled ellipses, fractional row spans for the
# curves): at 9 px of eye height a hard-edged ellipse looks like a staircase,
# and the LEDs render partial brightness cleanly.
# ---------------------------------------------------------------------------

SQUASH_MAX = 0.8    # hardest compression; a fully flat face is a blank panel
STRETCH_MAX = 0.3   # how far past resting height a rebound may overshoot


def _squash_y(y: float, squash: float) -> float:
    """Map a y through a vertical squash pinned to the panel's bottom row.

    A blow from above drives the face down onto the floor rather than shrinking
    it around its own middle, so row H is the pivot and only the top moves: at
    squash = 0.5 every feature sits at half height, still resting on the bottom.
    Negative values stretch instead, which is what gives the rebound its
    overshoot. Every primitive below routes its y through here, so one number on
    FaceState squashes the whole face and nothing has to know about the others.
    """
    return H - (H - y) * (1.0 - squash)


def _blank():
    return [BG] * (W * H)


def _blend_px(buf, x, y, rgb, a):
    if a <= 0.0 or not (0 <= x < W and 0 <= y < H):
        return
    i = y * W + x
    if a >= 1.0:
        buf[i] = rgb
        return
    r0, g0, b0 = buf[i]
    buf[i] = (round(r0 + (rgb[0] - r0) * a),
              round(g0 + (rgb[1] - g0) * a),
              round(b0 + (rgb[2] - b0) * a))


def _ellipse(buf, cx, cy, rx, ry, rgb, squash=0.0):
    """Filled ellipse with 3x3 coverage antialiasing."""
    if squash:
        cy, ry = _squash_y(cy, squash), ry * (1.0 - squash)
    if rx <= 0.0 or ry <= 0.0:
        return
    for y in range(max(0, int(cy - ry - 1)), min(H, int(cy + ry + 2))):
        for x in range(max(0, int(cx - rx - 1)), min(W, int(cx + rx + 2))):
            hits = 0
            for sy in range(3):
                dy = (y + (sy + 0.5) / 3.0 - cy) / ry
                for sx in range(3):
                    dx = (x + (sx + 0.5) / 3.0 - cx) / rx
                    if dx * dx + dy * dy <= 1.0:
                        hits += 1
            if hits:
                _blend_px(buf, x, y, rgb, hits / 9.0)


def _vspan(buf, x, y_from, y_to, rgb, squash=0.0):
    """Vertical run with fractional ends - the antialiasing for every curve."""
    if squash:
        y_from, y_to = _squash_y(y_from, squash), _squash_y(y_to, squash)
    if y_to <= y_from:
        return
    for y in range(max(0, int(math.floor(y_from))), min(H, int(math.ceil(y_to)))):
        cov = min(y_to, y + 1.0) - max(y_from, float(y))
        if cov > 0.0:
            _blend_px(buf, x, y, rgb, min(1.0, cov))


def _eye(buf, side, open_amt, st, rgb, squash=0.0):
    """One eye: a lit ellipse whose height follows openness, with a dark pupil
    punched out of it. Gaze is that hole moving, which reads far better at this
    size than a lit dot on a dark eye."""
    cx = EYE_CX[side]
    if open_amt <= LID_LINE:
        # A squashed ellipse tapers to points; a shut eye is a straight lid.
        top = EYE_CY - EYE_MIN_RY
        for x in range(int(cx - EYE_RX * 0.9), int(math.ceil(cx + EYE_RX * 0.9))):
            _vspan(buf, x, top, top + 2.0 * EYE_MIN_RY, rgb, squash)
        return
    ry = max(EYE_MIN_RY, EYE_RY * open_amt)
    _ellipse(buf, cx, EYE_CY, EYE_RX, ry, rgb, squash)
    if open_amt <= PUPIL_SHOW:
        return
    # Keep the hole inside the lid: the ellipse narrows towards its ends, so an
    # unclamped gaze bites a notch out of the eye's edge instead of looking away.
    max_off = max(0.0, EYE_RX - PUPIL_R - 1.0)
    off = max(-max_off, min(max_off, st.pupil_x * PUPIL_TRAVEL_X))
    px = cx + off
    py = EYE_CY + st.pupil_y * PUPIL_TRAVEL_Y
    pr_y = min(PUPIL_R, max(0.7, ry - 1.4))
    _ellipse(buf, px, py, PUPIL_R, pr_y, BG, squash)


def _brow(buf, side, st, rgb, squash=0.0):
    """Brow bar; the inner end drops (scowl) or lifts (worry) by brow_angle.
    ``inner`` is mirrored per side so both brows tilt towards the nose."""
    cx = EYE_CX[side]
    x0 = cx - BROW_W / 2.0
    tilt = st.brow_angle * BROW_TILT
    # Row 0 is the ceiling. Push the whole bar down far enough that the raised
    # end clears it too: clipping the tilt away would flatten a worried brow
    # into a straight line and lose the expression.
    top = max(BROW_Y + st.brow_y, -min(0.0, tilt), 0.0)
    for x in range(int(round(x0)), int(round(x0 + BROW_W))):
        u = (x + 0.5 - x0) / BROW_W          # 0 at the left end, 1 at the right
        inner = u if side == 0 else 1.0 - u  # 1 at the end facing the nose
        y = top + tilt * inner
        _vspan(buf, x, y, y + BROW_T, rgb, squash)


def _mouth(buf, st, rgb, squash=0.0):
    """Parabolic mouth: the corners stay put and the centre travels, so one
    number sweeps frown -> flat -> smile. Opening widens the gap towards the
    middle, giving a lens shape rather than a rectangle."""
    half = max(1.5, MOUTH_HALF * st.mouth_w)
    for x in range(int(MOUTH_CX - half), int(math.ceil(MOUTH_CX + half))):
        u = (x + 0.5 - MOUTH_CX) / half
        p = max(0.0, 1.0 - u * u)            # 1 at the centre, 0 at the corners
        t = MOUTH_T + st.mouth_open * MOUTH_MAX_OPEN * p
        yc = MOUTH_Y + st.mouth_curve * MOUTH_AMP * p
        # A wide grin that is also open would run off the bottom row and get
        # clipped into a flat edge; hold the lower lip on the panel instead.
        yc = min(yc, H - 0.4 - t / 2.0)
        _vspan(buf, x, yc - t / 2.0, yc + t / 2.0, rgb, squash)


def _sweat(buf, amount, t, rgb, squash=0.0):
    """2x3 bead that drifts down and fades - the tell for the stressed mood."""
    if amount <= 0.01:
        return
    fall = (t % 1.6) / 1.6
    y0 = SWEAT_Y + fall * SWEAT_FALL
    y1 = y0 + 3.0
    if squash:
        y0, y1 = _squash_y(y0, squash), _squash_y(y1, squash)
    alpha = amount * (1.0 - 0.7 * fall)   # fades as it falls
    for x in (int(SWEAT_X), int(SWEAT_X) + 1):
        for y in range(max(0, int(y0)), min(H, int(math.ceil(y1)))):
            cov = min(y1, y + 1.0) - max(y0, float(y))
            if cov > 0.0:
                _blend_px(buf, x, y, rgb, min(1.0, cov) * alpha)


def render(st: FaceState, theme: dict, t: float = 0.0) -> list:
    """FaceState -> a flat 72x16 list of (r, g, b)."""
    buf = _blank()
    rgb = st.tint or theme["face"]
    sq = max(-STRETCH_MAX, min(SQUASH_MAX, st.squash))
    _brow(buf, 0, st, rgb, sq)
    _brow(buf, 1, st, rgb, sq)
    _eye(buf, 0, max(0.0, min(EYE_MAX, st.eye_l)), st, rgb, sq)
    _eye(buf, 1, max(0.0, min(EYE_MAX, st.eye_r)), st, rgb, sq)
    _mouth(buf, st, rgb, sq)
    _sweat(buf, st.sweat, t, theme["sweat"], sq)
    return buf


def _needs_render(state: FaceState, drawn) -> bool:
    """Whether this frame's pixels can differ from the ones already drawn.

    render() is a pure function of its FaceState (TestRenderPath pins that),
    so a state equal to the one on screen has nothing new to draw - and at
    rest that is every frame between blinks, which used to be rendered anyway
    and then thrown away by the dirty check downstream. Sweat is the one
    feature that moves with time rather than with the state, so a visible
    bead keeps rendering; the threshold is the one below which _sweat() draws
    nothing at all.
    """
    return state != drawn or state.sweat > 0.01


# ---------------------------------------------------------------------------
# The idle layer: blinks and eye darts on top of the current mood.
#
# Both run on irregular intervals. Evenly spaced blinking is the single thing
# that makes a face read as a machine rather than as alive.
# ---------------------------------------------------------------------------

BLINK_DUR = 0.14
BLINK_GAP = (2.4, 6.5)
BLINK_GAP_TENSE = (1.1, 2.6)   # stress blinks faster
DART_GAP = (3.5, 11.0)
DART_HOLD = (0.6, 1.8)
DART_MOVE = 0.11

# Breathing rides the same squash the punch uses. It is the one motion that runs
# when nothing else does, which is most of what separates a face from a picture
# of one. Shallow on purpose - about a pixel of height at the brow, felt rather
# than watched.
#
# Off unless --breathe asks for it, because a face that never stops moving never
# stops pushing frames, and a resting Character otherwise leaves the display to
# whatever else wants it. See BREATH_RATE below and the render skip
# (_needs_render) for what it costs when it is on.
BREATH_DEPTH = 0.03     # squash amplitude at rest
BREATH_PERIOD = 4.6     # s per breath at rest
BREATH_RATE = 5.0       # breath steps per second, which is the knob that
                        # matters. Every step is a changed frame and so a push,
                        # and the depth does not help: measured, a breath
                        # sampled per frame turns a resting face from 0.1 into
                        # 15 pushes/s at any amplitude, because even a
                        # sub-pixel shift moves the antialiasing. At ~50 ms per
                        # full-frame draw that is three quarters of the bar's
                        # time, held forever. Stepping five times a second is
                        # ~23 samples per breath - smooth at this size - for a
                        # third of the cost.
BREATH_TENSE = (2.4, 0.6)    # (period, depth factor) - stress breathes fast and
                             # shallow, the way a held breath actually looks
BREATH_SLEEP = (7.2, 1.9)    # ...and sleep breathes slow and deep


class Idle:
    """Timers for the involuntary motion; ``apply`` mutates a blended state."""

    def __init__(self, now, breathe=False):
        self.breathe = breathe
        self.next_blink = now + random.uniform(*BLINK_GAP)
        self.blink_start = None
        self.next_dart = now + random.uniform(*DART_GAP)
        self.dart_start = 0.0
        self.dart_until = 0.0
        self.dart_x = 0.0

    def apply(self, st: FaceState, mood: str, now: float,
              allow_dart: bool = True) -> FaceState:
        """``allow_dart=False`` is NO_DART for one frame: it lets the reaction
        layer take the eyes while the wheel is steering them, so the user's own
        hand and a random saccade never fight over the same pupil."""
        tense = mood in ("stressed", "surprised")
        if mood not in NO_BLINK:
            if self.blink_start is None and now >= self.next_blink:
                self.blink_start = now
            if self.blink_start is not None:
                u = (now - self.blink_start) / BLINK_DUR
                if u >= 1.0:
                    self.blink_start = None
                    gap = BLINK_GAP_TENSE if tense else BLINK_GAP
                    self.next_blink = now + random.uniform(*gap)
                else:
                    # 1 -> 0 -> 1 over the blink; the lid shuts and reopens.
                    shut = 1.0 - abs(1.0 - 2.0 * u)
                    st = replace(st, eye_l=st.eye_l * (1.0 - shut),
                                 eye_r=st.eye_r * (1.0 - shut))
        else:
            self.next_blink = now + random.uniform(*BLINK_GAP)

        if allow_dart and mood not in NO_DART:
            if now >= self.next_dart and now >= self.dart_until:
                self.dart_x = random.choice((-1.0, -0.65, 0.65, 1.0))
                self.dart_start = now
                self.dart_until = now + random.uniform(*DART_HOLD)
                self.next_dart = self.dart_until + random.uniform(*DART_GAP)
            if now < self.dart_until:
                # A saccade is fast at both ends: ramp out to the target, hold,
                # ramp back. Teleporting the pupil reads as a glitch instead.
                ramp = min(1.0, (now - self.dart_start) / DART_MOVE,
                           (self.dart_until - now) / DART_MOVE)
                st = replace(st, pupil_x=st.pupil_x + self.dart_x * ramp)
        else:
            self.dart_until = 0.0
            self.next_dart = now + random.uniform(*DART_GAP)

        breath = 0.0
        if self.breathe:
            period, depth = BREATH_PERIOD, BREATH_DEPTH
            if tense:
                period, depth = BREATH_TENSE[0], depth * BREATH_TENSE[1]
            elif mood in ("sleepy", "asleep"):
                period, depth = BREATH_SLEEP[0], depth * BREATH_SLEEP[1]
            # Quantised, so the face holds each step and the render skip
            # (_needs_render) still has unchanged frames to skip.
            step = math.floor(now * BREATH_RATE) / BREATH_RATE
            breath = depth * math.sin(step * 2.0 * math.pi / period)
        return replace(st, pupil_x=max(-1.0, min(1.0, st.pupil_x)),
                       squash=st.squash + breath)


# ---------------------------------------------------------------------------
# The reaction layer: what the bar's own buttons and wheel do to the face.
#
# This is a third layer above the existing two, and it outranks both:
#
#   reaction (input)  >  mood (sensors)  ->  blend  ->  idle overlay  ->  render
#
# A press holds its expression for about a second and a half - long enough to
# read as an answer, short enough that the face never freezes in it and stops
# reporting what the machine is doing. The wheel is not a mood at all and does
# not expire: it moves the centre the eyes return to, in the same overlay slot
# Idle's saccade uses, so glances still wander off it and come back.
# ---------------------------------------------------------------------------

REACTION_HOLD = 1.5     # s a button's expression outranks the sensor mood
GAZE_HOLD = 0.8         # s Idle's dart stays out of the way after the last click
GAZE_PER_CLICK = 0.2    # rest gaze per encoder click: five clicks from straight
                        # ahead to hard over, which is aiming rather than flicking
SPIN_WINDOW = 0.4       # s of encoder clicks summed to judge how fast the spin is
SPIN_FAST = 6           # clicks inside that window that read as a fast spin. Above
                        # what aiming the gaze costs, so parking the eyes on
                        # someone does not startle the face on the way there

# Which face each button gets. An unlisted button does nothing rather than
# guessing: only ok/start/back are evidenced, and the full set is undocumented.
BUTTON_MOODS = {"ok": "happy", "start": "punched", "back": "grumpy"}
BUTTON_BLINK = {"ok"}   # ok blinks first - an acknowledgement is a blink, not a
                        # stare, and the blink is what makes the press feel felt
BUTTON_PUNCH = {"start"}  # the loudest button gets hit, not merely reacted to

# The punch: a fist lands on the top of the head, dead centre. The face is
# driven down onto the panel's floor, springs back through its resting height
# into a stretch, and rings down into a daze - the "punched" mood above.
PUNCH_HIT = 0.09        # s of contact, the face held flat
PUNCH_SPRING = 0.55     # s of ringing afterwards, decaying into the daze
PUNCH_SQUASH = 0.6      # compression at the moment of contact (40% height)
PUNCH_RINGS = 2.5       # half-cycles of squash inside the spring: down, up
                        # past resting into a stretch, down again, out
PUNCH_DAMP = 4.5        # how fast the ringing dies; lower lets the stretch grow
                        # far enough to push the brows off the top row
PUNCH_WOBBLE = 0.8      # how far the eyes roll from side to side while dizzy

# The pose at the moment of contact: eyes screwed shut, brows driven onto the
# lids, mouth forced open. The red flash fades out through the spring.
PUNCH_CRUSH = FaceState(eye_l=0.0, eye_r=0.0, brow_angle=1.0, brow_y=2.5,
                        mouth_curve=-0.9, mouth_open=0.85, mouth_w=0.45,
                        pupil_y=0.6, squash=PUNCH_SQUASH, tint=RED)


class Reaction:
    """Input -> expression. Shaped like ``Idle`` - ``now`` arrives as a
    parameter, so every timing here is testable without sleeping - but it
    exposes an extra ``override_mood`` because input beats the sensors."""

    def __init__(self, theme: dict):
        self.theme = theme      # the punch blends two poses, and blend() needs it
        self.mood = None
        self.mood_until = 0.0
        self.blink_start = None
        self.rest = 0.0         # where the face looks when nothing else asks
        self.gaze_until = 0.0
        self.spin = []          # [(t, clicks)] inside SPIN_WINDOW
        self.punch_start = None

    def feed(self, buttons, delta: int, now: float) -> None:
        """One frame's coalesced input. ``delta`` is the whole frame's wheel
        movement summed, not one event; see ``drain_input``."""
        for name in buttons:
            mood = BUTTON_MOODS.get(name)
            if mood is None:
                continue
            self.mood, self.mood_until = mood, now + REACTION_HOLD
            if name in BUTTON_BLINK:
                self.blink_start = now
            if name in BUTTON_PUNCH:
                self.punch_start = now
        if not delta:
            return
        # The wheel aims the face rather than nudging it: this is where the eyes
        # come back to, so it persists instead of expiring. Park it on yourself
        # and the face keeps looking at you between glances elsewhere. Turning
        # back is what re-centres it.
        #
        # The encoder counts up in the opposite direction to the screen's x, so
        # the sign flips here: without it the eyes settle away from the hand that
        # is turning the wheel, which reads as the face avoiding you.
        self.rest = max(-1.0, min(1.0, self.rest - delta * GAZE_PER_CLICK))
        self.gaze_until = now + GAZE_HOLD
        self.spin = [(t, n) for t, n in self.spin if now - t < SPIN_WINDOW]
        self.spin.append((now, abs(delta)))
        if sum(n for _, n in self.spin) >= SPIN_FAST:
            # Amplitude carries surprise: a nudge is a glance, a spin is a jolt.
            self.mood, self.mood_until = "surprised", now + REACTION_HOLD

    def override_mood(self, now: float):
        """The mood input is currently insisting on, or None once it has
        expired and the sensors get to speak again."""
        if self.mood is not None and now >= self.mood_until:
            self.mood = None
        return self.mood

    def owns_eyes(self, now: float) -> bool:
        """True while a hand is on the wheel or a blow is still ringing - Idle's
        dart stands down for as long as this holds, so aiming the face is not
        fought by a random glance. Once it lapses the darts resume, away from
        the rest gaze and back to it."""
        return now < self.gaze_until or self.punching(now)

    def punching(self, now: float) -> bool:
        return (self.punch_start is not None
                and now - self.punch_start < PUNCH_HIT + PUNCH_SPRING)

    def _punch(self, elapsed: float) -> FaceState:
        """The whole face for one frame of the blow.

        The hit owns every feature, so whatever the blend underneath is doing is
        ignored until the ringing dies. By then the mood has long since settled
        on "punched", which is exactly where this leaves off - so handing the
        face back is seamless rather than a jump.
        """
        if elapsed < PUNCH_HIT:
            # Contact is not a fade: the face is flat on the frame the press
            # lands. Ramping it in reads as leaning into the fist, and at 15 fps
            # a ramp short enough to look like a hit is thinner than one frame.
            return PUNCH_CRUSH
        p = (elapsed - PUNCH_HIT) / PUNCH_SPRING
        decay = math.exp(-p * PUNCH_DAMP)
        st = blend(PUNCH_CRUSH, MOODS["punched"], _ease(p), self.theme)
        # A struck panel rings: the squash crosses its resting height, overshoots
        # into a stretch, and comes back smaller each time. The eyes roll with it.
        return replace(
            st,
            squash=PUNCH_SQUASH * math.cos(p * math.pi * PUNCH_RINGS) * decay,
            pupil_x=PUNCH_WOBBLE * math.sin(p * math.pi * PUNCH_RINGS * 2.0) * decay)

    def apply(self, st: FaceState, mood: str, now: float) -> FaceState:
        if self.punch_start is not None:
            elapsed = now - self.punch_start
            if elapsed < PUNCH_HIT + PUNCH_SPRING:
                return self._punch(elapsed)
            self.punch_start = None
        if self.blink_start is not None:
            if mood in NO_BLINK:
                self.blink_start = None       # shut eyes have nothing to blink
            else:
                u = (now - self.blink_start) / BLINK_DUR
                if u >= 1.0:
                    self.blink_start = None
                else:
                    shut = 1.0 - abs(1.0 - 2.0 * u)
                    st = replace(st, eye_l=st.eye_l * (1.0 - shut),
                                 eye_r=st.eye_r * (1.0 - shut))
        # Additive and permanent: the wheel moves the centre the eyes return to,
        # so Idle's dart still carries them away from it and back again.
        if self.rest:
            st = replace(st, pupil_x=max(-1.0, min(1.0, st.pupil_x + self.rest)))
        return st


# ---------------------------------------------------------------------------
# Sensors: what the machine is doing.
#
# Every reader degrades to None/zero rather than raising - a face that dies
# because ``pmset`` changed its output format would be worse than a face that
# just stops reacting to the battery. ``--signals`` prints what they return.
# ---------------------------------------------------------------------------

CLAUDE_PROJECTS = "~/.claude/projects"
SESSION_WINDOW = 300.0   # s; a transcript touched this recently = live session
WORKING_WINDOW = 10.0    # s; touched this recently = a turn is running now
WORKING_TAIL = 25.0      # s the same turn keeps counting once it has started.
                         # One turn can go quiet for longer than WORKING_WINDOW
                         # - a long tool call writes nothing - and without the
                         # wider window on the way out the face flips
                         # focused/happy about ten times a minute in ordinary use.
LOAD_STRESS = 0.85       # load1 / cores at or above this = stressed
LOAD_RELIEF = 0.72       # ...and it has to fall back to here to stop being it
BATTERY_LOW = 20         # % at or below this (and unplugged) = sad
BATTERY_RELIEF = 25      # ...and back up to here to stop being it
SLEEPY_AFTER = 240.0     # s of keyboard idle -> sleepy
ASLEEP_AFTER = 600.0     # s of keyboard idle -> asleep


@dataclass
class Signals:
    load: float = 0.0            # load1 / cpu count
    claude_working: bool = False
    claude_sessions: int = 0
    battery_pct: int = None      # None = unknown / no battery
    charging: bool = False
    user_idle: float = None      # s since the last key/mouse event, None = unknown


def _read_load() -> float:
    try:
        return os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except (OSError, AttributeError):
        return 0.0


def _read_claude(now: float, was_working: bool = False) -> tuple:
    """(sessions, working) from transcript mtimes under ~/.claude/projects.

    Only ``stat`` is used - transcripts run to tens of megabytes and are never
    opened. Mirrors the scan in ../ClaudeLimits/app.py.

    ``was_working`` widens the window: a turn that has already started keeps
    counting through a quiet stretch that would otherwise end it. Hysteresis
    belongs here rather than in mood_for(), because this is the only place that
    can see how long the quiet has lasted.
    """
    root = os.path.expanduser(CLAUDE_PROJECTS)
    sessions, working = 0, False
    try:
        projects = list(os.scandir(root))
    except OSError:
        return 0, False
    for proj in projects:
        try:
            if not proj.is_dir():
                continue
            with os.scandir(proj.path) as entries:
                for entry in entries:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    age = now - entry.stat().st_mtime
                    if age <= SESSION_WINDOW:
                        sessions += 1
                    if age <= (WORKING_TAIL if was_working else WORKING_WINDOW):
                        working = True
        except OSError:
            continue  # one unreadable project must not hide the others
    return sessions, working


def _read_battery() -> tuple:
    """(percent, charging) from ``pmset -g batt``; (None, False) if unavailable.

    Output looks like: ``-InternalBattery-0 (id=...)  84%; discharging; ...``
    Note "discharging" contains "charging", so the state is matched with the
    leading separator.
    """
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None, False
    pct = None
    for token in out.replace(";", " ").split():
        if token.endswith("%") and token[:-1].isdigit():
            pct = int(token[:-1])
            break
    charging = ("AC Power" in out or "; charging" in out or "; charged" in out
                or "finishing charge" in out)
    return pct, charging


IOREG_CMDS = (
    # Whatever is first here is what gets paid on every read, so it is the form
    # that asks for one property and nothing else: -k narrows the dump to
    # HIDIdleTime and -r matches the class rather than rooting the tree at it.
    # Measured on this machine: 16 ms and 4 kB, against 415 ms and 6 MB for the
    # last form, which prints the whole IOHIDSystem subtree and made the render
    # loop drop six frames every five seconds.
    #
    # The fallbacks stay because -k is the part most likely to behave
    # differently across macOS versions. -l is what prints properties at all;
    # without it ioreg lists objects only and HIDIdleTime never appears.
    ["ioreg", "-c", "IOHIDSystem", "-d", "1", "-r", "-k", "HIDIdleTime"],
    ["ioreg", "-c", "IOHIDSystem", "-d", "1", "-l"],
    ["ioreg", "-c", "IOHIDSystem", "-l"],
)


def _read_user_idle() -> tuple:
    """(seconds since the last key/mouse event, note) - None when unreadable.

    ``ioreg`` reports HIDIdleTime in nanoseconds. Unknown stays None so the
    sleepy/asleep rules simply do not fire rather than guessing at presence;
    ``note`` explains why, which ``--signals`` prints.
    """
    last = "no ioreg command ran"
    for cmd in IOREG_CMDS:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            last = f"{cmd[0]} failed: {exc}"
            continue
        for line in proc.stdout.splitlines():
            if "HIDIdleTime" in line:
                digits = "".join(c for c in line.split("=")[-1] if c.isdigit())
                if digits:
                    return int(digits) / 1e9, "ok"
        last = (f"{' '.join(cmd)}: no HIDIdleTime in {len(proc.stdout)} bytes"
                + (f"; stderr: {proc.stderr.strip()[:80]}" if proc.stderr else ""))
    return None, last


def read_signals(now: float, prev: Signals, due: dict) -> Signals:
    """Refresh the signals whose interval elapsed; ``due`` holds next-run times.

    Load is free, the transcript scan is cheap, and the two subprocess readers
    run rarely - polling ``pmset`` at frame rate would cost more than the face.
    """
    sig = replace(prev, load=_read_load())
    if now >= due["claude"]:
        due["claude"] = now + 2.0
        sig.claude_sessions, sig.claude_working = _read_claude(
            now, prev.claude_working)
    if now >= due["idle"]:
        due["idle"] = now + 5.0
        sig.user_idle, _ = _read_user_idle()
    if now >= due["battery"]:
        due["battery"] = now + 60.0
        sig.battery_pct, sig.charging = _read_battery()
    return sig


SENSOR_TICK = 0.25   # s between sweeps in the sensor thread; the readers gate
                     # themselves on top of this, so it only bounds latency


def start_sensors() -> dict:
    """Read the sensors off the render thread; returns a dict whose "signals"
    key always holds the most recent complete reading.

    ``pmset`` and ``ioreg`` are subprocesses, and a subprocess that decides to
    take a second must not land in the middle of a blink - before this, one
    ioreg call cost six frames every five seconds and the face visibly hitched.
    Reading is now a dict lookup, and a reader that hangs costs a stale face
    rather than a frozen one.

    No lock: read_signals() returns a fresh Signals rather than mutating the old
    one, and rebinding a dict key is atomic, so the render loop only ever sees a
    whole snapshot. Daemon, so Ctrl-C still exits promptly.
    """
    state = {"signals": Signals()}

    def run():
        due = {"claude": 0.0, "idle": 0.0, "battery": 0.0}
        sig = Signals()
        while True:
            try:
                sig = read_signals(time.time(), sig, due)
            except Exception:  # noqa: BLE001 - the readers degrade to None on
                pass           # their own; this is so the thread outlives a
                               # surprise and the face keeps its last reading
            state["signals"] = sig
            time.sleep(SENSOR_TICK)

    threading.Thread(target=run, daemon=True, name="busy-sensors").start()
    return state


def mood_for(sig: Signals, current: str = None) -> tuple:
    """(mood, reason) - a single ordered chain, so what is on screen is always
    explainable. Urgency first: a pegged CPU outranks a live Claude turn, which
    outranks a flat battery, which outranks "someone is around".

    ``current`` is the mood already showing, and the numeric thresholds widen
    for it: a mood is harder to leave than it was to enter. A signal parked on a
    bare threshold otherwise flips the face several times a minute, and every
    flip restarts a cross-fade and repaints the panel.

    Only the thresholds that noise sits on get this. The idle ones deliberately
    do not: keyboard idle collapses to zero the instant someone touches a key,
    and a face that took even a second to notice that would read as asleep at
    the wheel. Still pure - the same inputs give the same answer.
    """
    if sig.load >= (LOAD_RELIEF if current == "stressed" else LOAD_STRESS):
        return "stressed", f"load {sig.load:.2f} per core"
    if sig.claude_working:
        return "focused", "claude code turn running"
    if (sig.battery_pct is not None and not sig.charging
            and sig.battery_pct <= (BATTERY_RELIEF if current == "sad"
                                    else BATTERY_LOW)):
        return "sad", f"battery {sig.battery_pct}%"
    if sig.user_idle is not None and sig.user_idle >= ASLEEP_AFTER:
        return "asleep", f"idle {sig.user_idle / 60:.0f} min"
    if sig.user_idle is not None and sig.user_idle >= SLEEPY_AFTER:
        return "sleepy", f"idle {sig.user_idle / 60:.0f} min"
    if sig.claude_sessions:
        s = "session" if sig.claude_sessions == 1 else "sessions"
        return "happy", f"{sig.claude_sessions} claude {s}"
    return "neutral", "nothing going on"


# ---------------------------------------------------------------------------
# BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs)
# ---------------------------------------------------------------------------

DRAW_PATH = "/api/display/draw"
DEVICE_TIMEOUT = 5
DEVICE_BACKOFF_MIN = 1.0   # s before the first retry after the bar goes away
DEVICE_BACKOFF_MAX = 15.0  # ...doubling up to here, as the input thread does
BUSY_RETRY = 2.0           # s between attempts while another app holds the
                           # display; every frame would be shouting at it
ASSET_RING = 4  # the device locks an asset while drawing it; reusing a name
                # too soon returns HTTP 508, so rotate through a few


class DeviceError(Exception):
    """The device is unreachable (network-level failure only)."""


class Bar:
    """Frame pusher: uploads a PNG and draws it as one image element."""

    def __init__(self, host: str, token: str):
        self.base = "http://" + host.strip().replace("http://", "", 1).rstrip("/")
        self.token = token
        self._frame = 0

    def _headers(self, extra=None) -> dict:
        headers = dict(extra or {})
        if self.token:
            headers["X-API-Token"] = self.token
        return headers

    def _send(self, req) -> int:
        try:
            with urllib.request.urlopen(req, timeout=DEVICE_TIMEOUT) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except urllib.error.URLError as exc:
            raise DeviceError(str(getattr(exc, "reason", exc))) from exc
        except (OSError, TimeoutError) as exc:
            raise DeviceError(str(exc)) from exc

    def push(self, pixels) -> int:
        """Upload this frame and draw it; returns the draw HTTP status."""
        name = "frame%d.png" % (self._frame % ASSET_RING)
        self._frame += 1
        up = urllib.request.Request(
            f"{self.base}/api/assets/upload?application_name={APP}&file={name}",
            data=png(pixels), method="POST",
            headers=self._headers({"Content-Type": "application/octet-stream"}))
        status = self._send(up)
        if status in (401, 403):
            return status
        body = json.dumps({
            "application_name": APP,
            "elements": [{"id": "face", "type": "image", "path": name,
                          "x": 0, "y": 0}],
        }).encode()
        draw = urllib.request.Request(
            self.base + DRAW_PATH, data=body, method="POST",
            headers=self._headers({"Content-Type": "application/json"}))
        return self._send(draw)

    def clear(self) -> int:
        req = urllib.request.Request(
            f"{self.base}{DRAW_PATH}?application_name={APP}",
            headers=self._headers(), method="DELETE")
        return self._send(req)


def png(pixels) -> bytes:
    """72x16 flat list of (r, g, b) -> minimal RGBA PNG bytes (stdlib only)."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)  # filter type 0 (none) per scanline
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


AUTH_ERROR = ("error: device rejected the token; set BUSY_HTTP_PASSWORD "
              "or pass --token")

REPUSH_EVERY = 20.0  # s between safety-net re-pushes of an unchanged frame, in
                     # case the firmware expires an element we never refreshed
PUSH_TICK = 0.25     # s the push thread waits before re-checking its own
                     # timers; a fresh frame wakes it immediately, so this
                     # bounds retry latency, never animation latency


class Pusher:
    """The network, off the render thread.

    bar.push() is two HTTP requests with a five-second timeout each. Run
    between two frames, a wedged network stalls the face for up to ten
    seconds in the middle of a blink, and even a healthy full-frame draw
    (~50 ms) makes every cross-fade push-bound - the animation exactly as
    choppy as the link is slow. The sensors came off the render thread for
    the same reason (see start_sensors); frames now follow them out.

    One slot, newest wins: every frame is a complete snapshot, so when the
    device falls behind there is nothing worth queueing - offer() simply
    overwrites the pending frame, the way drain_input() coalesces a spin into
    one delta. Rebinding a dict value is atomic, so no lock, as with the
    sensor snapshot.

    The policy lives in step() - the unchanged-frame skip, the outage
    backoff, the wait while another app holds the display, which statuses are
    fatal - and step() takes ``now`` as a parameter like Idle and Reaction
    do, so all of it is testable without a thread, a sleep, or a device.
    ``fatal`` is how the thread says sys.exit: it cannot end the process from
    here, so the render loop checks it once a frame and does the exiting.
    """

    def __init__(self, bar):
        self.bar = bar
        self.last_sent = None        # the last frame the device accepted
        self.last_push = 0.0
        self.retry_at = 0.0          # holds pushes off a gone or busy bar
        self.backoff = DEVICE_BACKOFF_MIN
        self.offline = None          # the outage, announced once not per retry
        self.blocked = False         # 409: another app holds the display
        self.fatal = None            # auth/protocol failure message, or None
        self._slot = {"frame": None}
        self._wake = threading.Event()
        self._stop = False
        self._thread = None

    def offer(self, pixels) -> None:
        """Hand over the newest frame; never blocks, never queues."""
        self._slot["frame"] = pixels
        self._wake.set()

    def step(self, pixels, now: float) -> None:
        """One push decision. An unchanged frame inside REPUSH_EVERY is
        skipped - the resting face pushes nothing - and only a frame the
        device took is recorded as sent: recording a refused one would leave
        the panel stale until the face happened to change again."""
        if pixels is None or self.fatal is not None or now < self.retry_at:
            return
        if pixels == self.last_sent and now - self.last_push < REPUSH_EVERY:
            return
        try:
            status = self.bar.push(pixels)
        except DeviceError as exc:
            # A bar that went away is not a reason to die: the input thread
            # already reconnects through this, and a one-second USB blip must
            # cost a few skipped pushes, not the face.
            self.backoff = min(DEVICE_BACKOFF_MAX, self.backoff * 2)
            self.retry_at = now + self.backoff
            if self.offline is None:
                self.offline = str(exc)
                print(f"device unreachable: {exc}; retrying", flush=True)
            return
        if self.offline is not None:
            self.offline = None
            print("device back.", flush=True)
        self.backoff = DEVICE_BACKOFF_MIN
        self.last_push = now
        if status == 200:
            self.last_sent = pixels
            self.blocked = False
        elif status in (401, 403):
            self.fatal = AUTH_ERROR
        elif status == 409:
            self.retry_at = now + BUSY_RETRY   # stop shouting at the app that
            if not self.blocked:               # actually holds the display
                print("display busy: another app has priority, waiting...",
                      flush=True)
                self.blocked = True
        elif status == 508:
            pass  # asset still locked; the next push rotates the name
        else:
            self.fatal = f"error: unexpected HTTP {status} from the device"

    def start(self) -> None:
        """The thread half: wait for a frame (or for a timer to come due),
        step, repeat. Daemon, so Ctrl-C still exits promptly."""
        def run():
            while not self._stop and self.fatal is None:
                self._wake.wait(PUSH_TICK)
                self._wake.clear()
                self.step(self._slot["frame"], time.monotonic())

        self._thread = threading.Thread(target=run, daemon=True,
                                        name="busy-push")
        self._thread.start()

    def stop(self) -> None:
        """No more pushes, and wait out one already in the air - a frame that
        lands after the clear in main() would put the face straight back. On
        a healthy link that is milliseconds; on a wedged one the clear was
        going to fail anyway, so the join gives up after one timeout."""
        self._stop = True
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=DEVICE_TIMEOUT)


# ---------------------------------------------------------------------------
# Device input (the one part of Character that is not stdlib-only)
#
# The bar's input arrives as protobuf over a WebSocket, which the standard
# library can neither speak nor decode - so this section, and only this section,
# needs ``busylib``. It imports inside the thread, so every other mode still
# runs on a machine that has never installed it.
#
# busylib hands back plain dicts, so no protobuf surfaces here. The reader lives
# in a daemon thread with its own asyncio loop and posts to a queue.Queue: the
# render loop stays synchronous and never blocks on the network. If input dies
# the thread reconnects on its own and the face keeps running on the sensors -
# degraded, not dead, exactly as every sensor reader degrades to None.
# ---------------------------------------------------------------------------

INPUT_QUEUE_MAX = 256    # a stalled render loop must not grow a backlog forever
INPUT_BACKOFF_MIN = 1.0  # s before the first reconnect attempt
INPUT_BACKOFF_MAX = 15.0  # ...doubling up to here


def parse_input(message: dict) -> list:
    """One WS status message -> [("button", name) | ("encoder", delta)].

    Mirrors ``parse_events`` in ../HomeAssistant/app.py, which is the only
    field-tested reader of this stream. ``action`` is filtered to ""/"press" so
    one physical press is not handled twice on its release.
    """
    events = []
    for update in message.get("updates", []):
        input_update = update.get("input") or {}
        if "button_event" in input_update:
            button = input_update.get("button_event") or {}
            if str(button.get("action", "")).lower() in ("", "press"):
                events.append(("button", str(button.get("button") or "ok").lower()))
        encoder = input_update.get("encoder_event")
        if encoder and int(encoder.get("delta", 0)):
            events.append(("encoder", int(encoder["delta"])))
    return events


def drain_input(q) -> tuple:
    """One frame's worth of input: (buttons pressed, summed wheel delta).

    Summing is the point: at 15 fps a fast spin lands a dozen encoder events
    between two frames, and the face has only one gaze to point.
    """
    buttons, delta = [], 0
    while q is not None:
        try:
            kind, value = q.get_nowait()
        except queue.Empty:
            break
        if kind == "button":
            buttons.append(value)
        else:
            delta += value
    return buttons, delta


def _input_reader(host: str, token: str, q, status: dict) -> None:
    """Thread body: device WebSocket -> queue, reconnecting with backoff."""
    try:
        import asyncio
        from busylib import AsyncBusyBar
    except ImportError as exc:
        status["note"] = (f"off - {exc}; pip install -r Character/requirements.txt, "
                          "or pass --no-input to silence this")
        return

    async def pump() -> bool:
        """Returns whether the stream ever delivered anything."""
        client = AsyncBusyBar(host, token=token or None)
        got = False
        try:
            async for message in client.stream_status_ws():
                if not got:
                    got = True
                    status["note"] = "connected"
                if not isinstance(message, dict):
                    continue
                for event in parse_input(message):
                    try:
                        q.put_nowait(event)
                    except queue.Full:
                        pass  # the render loop is behind; a dropped press beats
                              # an unbounded backlog of stale ones
        finally:
            await client.aclose()
        return got

    delay = INPUT_BACKOFF_MIN
    while True:
        try:
            if asyncio.run(pump()):
                delay = INPUT_BACKOFF_MIN   # a working stream earns a fresh start
            why = "stream closed"
        except Exception as exc:  # noqa: BLE001 - anything the device or the
            # library can raise, from a refused connection to a protocol change,
            # must degrade to "no input" rather than take the face down with it.
            why = f"{type(exc).__name__}: {exc}"
        status["note"] = f"{why}; retrying in {delay:.0f}s"
        time.sleep(delay)
        delay = min(INPUT_BACKOFF_MAX, delay * 2)


def start_input(host: str, token: str) -> tuple:
    """Start the reader thread; returns (queue, status dict).

    Daemon, so Ctrl-C still exits promptly instead of waiting on a socket.
    """
    q = queue.Queue(maxsize=INPUT_QUEUE_MAX)
    status = {"note": "connecting..."}
    threading.Thread(target=_input_reader, args=(host, token, q, status),
                     daemon=True, name="busy-input").start()
    return q, status


# ---------------------------------------------------------------------------
# Terminal preview - lets the face be checked without the hardware.
# ---------------------------------------------------------------------------

def ansi_preview(pixels) -> str:
    """Two pixel rows per text row via the upper half block, truecolor."""
    out = []
    for y in range(0, H, 2):
        row = []
        for x in range(W):
            tr, tg, tb = pixels[y * W + x]
            br, bg_, bb = pixels[(y + 1) * W + x]
            row.append(f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg_};{bb}m▀")
        out.append("".join(row) + "\x1b[0m")
    return "\n".join(out)


PREVIEW_ROWS = H // 2


def _preview_frame(pixels, label: str, first: bool) -> None:
    """Redraw the face in place. Scrolling a new copy every frame would turn
    the terminal into a flipbook and make timing impossible to judge."""
    body = ansi_preview(pixels) + "\n" + label + "\x1b[K"
    if not first:
        body = f"\x1b[{PREVIEW_ROWS + 1}A" + body
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Input simulation - the reaction layer, developable without a bar on the desk.
# ---------------------------------------------------------------------------

SIM_INTERVAL = 1.8   # s between scripted events; longer than REACTION_HOLD so
                     # each reaction is watched out to its settle before the next


def parse_sim(spec: str) -> list:
    """"ok,encoder:+3" -> [("button", "ok"), ("encoder", 3)]. Raises ValueError
    on anything the reaction layer would silently ignore - a typo in a test
    script that does nothing is worse than one that refuses to start."""
    script = []
    for item in spec.split(","):
        item = item.strip().lower()
        if not item:
            continue
        if ":" in item:
            head, _, tail = item.partition(":")
            if head not in ("encoder", "wheel"):
                raise ValueError(f"unknown input {head!r}; use encoder:<signed int>")
            try:
                script.append(("encoder", int(tail)))
            except ValueError:
                raise ValueError(f"{tail!r} is not a wheel delta; "
                                 "use encoder:<signed int>") from None
        elif item in BUTTON_MOODS:
            script.append(("button", item))
        else:
            raise ValueError(f"unknown button {item!r}; use one of "
                             + ", ".join(sorted(BUTTON_MOODS))
                             + ", or encoder:<signed int>")
    if not script:
        raise ValueError("needs at least one event, e.g. ok,encoder:+3")
    return script


class SimulatedInput:
    """Replays a script on a fixed cadence, looping, and hands back the same
    (buttons, delta) pair a real ``drain_input`` returns."""

    def __init__(self, script, now: float):
        self.script = script
        self.i = 0
        self.next_at = now + SIM_INTERVAL

    def poll(self, now: float) -> tuple:
        if now < self.next_at:
            return [], 0
        self.next_at = now + SIM_INTERVAL
        kind, value = self.script[self.i % len(self.script)]
        self.i += 1
        return ([value], 0) if kind == "button" else ([], value)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20",
                   help="BUSY Bar ip[:port] (default 10.0.4.20; emulator: 127.0.0.1:8080)")
    p.add_argument("--token", default=os.environ.get("BUSY_HTTP_PASSWORD"),
                   help="device HTTP API password (prefer BUSY_HTTP_PASSWORD); localhost needs none")
    p.add_argument("--mood", choices=sorted(MOODS),
                   help="hold one expression instead of reading the sensors")
    p.add_argument("--theme", default="amber", choices=sorted(THEMES),
                   help="feature colour (default amber)")
    p.add_argument("--fps", type=float, default=15.0,
                   help="frames per second (default 15; unchanged frames are skipped)")
    p.add_argument("--once", action="store_true",
                   help="draw a single frame and exit")
    p.add_argument("--demo", action="store_true",
                   help="cycle every mood, ~2.2 s each")
    p.add_argument("--preview", action="store_true",
                   help="render in this terminal instead of on a device")
    p.add_argument("--signals", action="store_true",
                   help="print what the sensors report, then exit")
    p.add_argument("--breathe", action="store_true",
                   help="breathe at rest (~5 frames/s pushed instead of ~0, so "
                        "the display stays held; off by default)")
    p.add_argument("--no-input", action="store_true",
                   help="skip the device input stream (keeps this run stdlib-only)")
    p.add_argument("--simulate-input", metavar="SCRIPT",
                   help="replay scripted input instead of reading the device, e.g. "
                        "ok,encoder:+3,back; with --preview it animates in this terminal")
    args = p.parse_args(argv)
    if args.fps <= 0:
        p.error("--fps must be positive")
    if args.simulate_input is not None:   # "" is a typo, not "no simulation"
        try:
            args.simulate_input = parse_sim(args.simulate_input)
        except ValueError as exc:
            p.error(f"--simulate-input: {exc}")
    return args


DEMO_SECONDS = 2.2

# Demo runs as an arc rather than alphabetically: calm, then cheerful, curious,
# tense, and finally asleep. Alphabetical order opens on "asleep", which reads
# as a display that failed to wake up.
DEMO_ORDER = ["neutral", "happy", "wink", "excited", "look_left", "look_right",
              "surprised", "focused", "grumpy", "stressed", "sad", "sleepy",
              "asleep"]


def _demo_mood(t: float) -> str:
    return DEMO_ORDER[int(t / DEMO_SECONDS) % len(DEMO_ORDER)]


def cmd_signals() -> int:
    now = time.time()
    due = {"claude": 0.0, "idle": 0.0, "battery": 0.0}
    sig = read_signals(now, Signals(), due)
    mood, why = mood_for(sig)
    print(f"load          {sig.load:.2f} per core")
    print(f"claude        {sig.claude_sessions} session(s), "
          f"working={sig.claude_working}")
    print("battery       " + ("unknown (no battery, or pmset unavailable)"
                              if sig.battery_pct is None
                              else f"{sig.battery_pct}%, charging={sig.charging}"))
    idle_s, note = _read_user_idle()
    print("user idle     " + (f"{idle_s:.0f} s" if idle_s is not None
                              else f"unknown - {note}"))
    if idle_s is None:
        print("              (sleepy/asleep stay off while this is unknown)")
    print(f"\n-> mood {mood}  ({why})")
    return 0


def cmd_preview(args) -> int:
    """Static ANSI render: one mood, or every mood when none is given."""
    theme = THEMES[args.theme]
    names = [args.mood] if args.mood else sorted(MOODS)
    for name in names:
        print(f"\n\x1b[1m{name}\x1b[0m  ({args.theme}, {W}x{H})")
        print(ansi_preview(render(MOODS[name], theme)))
    return 0


def _sigterm(signum, frame):
    """SIGTERM -> the KeyboardInterrupt path. launchd, systemd and a bare
    ``kill`` all deliver SIGTERM, and a face that dies without the cleanup in
    main() keeps holding the display (and, in preview, a hidden cursor)."""
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.signals:
        return cmd_signals()

    now = time.monotonic()
    sim = SimulatedInput(args.simulate_input, now) if args.simulate_input else None
    if args.preview and sim is None:
        return cmd_preview(args)        # the static mood sheet, as before

    theme = THEMES[args.theme]
    # --preview alone is a still sheet; with input to react to it becomes a live
    # animation in this terminal, which is the only way to develop the reaction
    # layer without a bar on the desk.
    live_preview = bool(args.preview)
    bar = None if live_preview else Bar(args.host, args.token)
    random.seed()

    # Only a deliberate stop clears the face, and SIGTERM is how launchd,
    # systemd and a bare ``kill`` deliver one. Send it down the Ctrl-C path,
    # so every deliberate stop sweeps the panel instead of leaving the last
    # frame behind.
    signal.signal(signal.SIGTERM, _sigterm)

    # --once draws a single frame, so there is nothing for an input stream to
    # react to; a simulated script replaces the device stream rather than joins it.
    inq = in_status = None
    if sim is None and not args.no_input and not args.once:
        inq, in_status = start_input(args.host, args.token)

    # --once has no second frame for a snapshot to arrive in, and --demo/--mood
    # never consult the sensors at all.
    sensors = None if (args.once or args.demo or args.mood) else start_sensors()

    # --once pushes synchronously below - its failures are exits rather than
    # retries, and a single frame has no animation for a slow push to stall.
    pusher = None
    if bar is not None and not args.once:
        pusher = Pusher(bar)
        pusher.start()

    idle = Idle(now, breathe=args.breathe)
    reaction = Reaction(theme)
    due = {"claude": 0.0, "idle": 0.0, "battery": 0.0}
    sig = Signals()
    mood = args.mood or "neutral"
    sensor_mood = "neutral"
    prev_state = MOODS[mood]
    from_state = MOODS[mood]
    trans_start = now - TRANSITION  # start settled, not mid-fade
    interval = 1.0 / args.fps
    drawn_state = None
    pixels = None
    last_label = None
    in_note = None
    first_frame = True
    why = "held" if args.mood else "starting"
    t0 = now

    if live_preview:
        sys.stdout.write("\x1b[?25l")   # a cursor blinking over the face reads
        sys.stdout.flush()              # as a stuck pixel
    else:
        print(f"{APP} -> {bar.base}  (Ctrl-C to stop)", flush=True)
        if args.mood:
            print(f"mood {args.mood} (held; sensors idle)", flush=True)
        if args.no_input:
            print("input off (--no-input)", flush=True)
    try:
        while True:
            now = time.monotonic()

            # Coalesced per frame: one gaze to point, however many events landed.
            buttons, delta = sim.poll(now) if sim else drain_input(inq)
            if buttons or delta:
                reaction.feed(buttons, delta, now)
            if in_status is not None and in_status["note"] != in_note:
                in_note = in_status["note"]
                if not live_preview:
                    print(f"input {in_note}", flush=True)

            if args.demo:
                want, why = _demo_mood(now - t0), "demo"
            elif args.mood:
                want, why = args.mood, "held"
            else:
                sig = (sensors["signals"] if sensors is not None
                       else read_signals(time.time(), sig, due))
                # Tracked apart from `mood`, so a reaction passing through does
                # not reset the hysteresis the sensors are holding.
                sensor_mood, why = mood_for(sig, sensor_mood)
                want = sensor_mood

            # Input outranks both the sensors and a held --mood: it is the one
            # signal that came from someone standing in front of the bar.
            reacting = reaction.override_mood(now)
            if reacting is not None:
                want, why = reacting, "input"

            if want != mood:
                from_state = prev_state
                trans_start = now
                mood = want
                if not live_preview:
                    print(f"mood {mood}  ({why})", flush=True)

            elapsed = now - trans_start
            if elapsed >= TRANSITION:
                base = MOODS[mood]  # settled: a lerp at u=1 is just its target
            else:
                base = blend(from_state, MOODS[mood],
                             _ease(elapsed / TRANSITION), theme)
            prev_state = base                       # blinks stay out of the fade
            state = idle.apply(base, mood, now,
                               allow_dart=not reaction.owns_eyes(now))
            state = reaction.apply(state, mood, now)

            # An unchanged state is not even rendered, let alone pushed: at
            # rest that is every frame between blinks, which used to be drawn
            # 15 times a second and then thrown away by the dirty check.
            changed = _needs_render(state, drawn_state)
            if changed:
                pixels = render(state, theme, now - t0)
                drawn_state = state

            if live_preview:
                label = f"mood {mood}  ({why})"
                if changed or label != last_label:
                    _preview_frame(pixels, label, first_frame)
                    first_frame = False
                    last_label = label
            elif pusher is not None:
                # The newest frame goes to the pusher's thread and the loop
                # gets straight back to animating: a wedged bar costs stale
                # pixels, not a face frozen mid-blink. The pusher keeps the
                # frame, so its own timers cover the outage retries and the
                # periodic re-push without another offer from here.
                if changed:
                    pusher.offer(pixels)
                if pusher.fatal is not None:
                    sys.exit(pusher.fatal)   # a thread cannot exit the
                                             # process; it asks, this does it
            else:
                # --once: one synchronous push, and every failure is an exit
                # rather than a retry - there is no next frame to retry into.
                try:
                    status = bar.push(pixels)
                except DeviceError as exc:
                    sys.exit(f"error: {bar.base} unreachable: {exc}")
                if status in (401, 403):
                    sys.exit(AUTH_ERROR)
                if status == 409:
                    sys.exit("error: display busy, another app has priority")
                if status not in (200, 508):
                    sys.exit(f"error: unexpected HTTP {status} from the device")

            if args.once:
                return 0
            time.sleep(max(0.0, interval - (time.monotonic() - now)))
    except KeyboardInterrupt:
        # Only a deliberate stop clears the face - Ctrl-C, or the SIGTERM
        # that arrives here through _sigterm. --once returns above without
        # clearing, so the frame it drew stays on the display.
        # An impatient second Ctrl-C lands inside this cleanup; swallow it so
        # the exit stays clean instead of ending in a traceback.
        if bar is not None:
            try:
                if pusher is not None:
                    pusher.stop()   # a frame already in the air must land
                                    # first, or it would overdraw the clear
                bar.clear()
            except (DeviceError, KeyboardInterrupt):
                pass
        print("\nstopped.")
        return 0
    finally:
        if live_preview:
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
