#!/usr/bin/env python3
"""Ambient ocean waves for BUSY Bar.

A 1-D ocean surface rendered as a full 72x16 frame. Five sea states
progress from calm water to storm, with smooth timed transitions between them.
The left and right display edges behave like container walls: incoming waves are
partly reflected, the water piles up against the wall, and a slow slosh mode tilts
the surface like liquid moving inside a bottle.

    python3 app.py
    python3 app.py --host 127.0.0.1:8080
    python3 app.py --state auto
    python3 app.py --state moderate
    python3 app.py --state-seconds 24 --transition-seconds 10
    python3 app.py --animation-speed 0.65
    python3 app.py --cycle calm,breeze,moderate,rough,storm,rough,moderate,breeze
    python3 app.py --test

States: calm, breeze, moderate, rough, storm.

In auto mode the state machine moves through adjacent sea states instead of
jumping randomly. Each state is held for --state-seconds, then the physical wave
parameters are interpolated over --transition-seconds using a smoothstep curve.
"""
import argparse
import json
import math
import random
import struct
import wave
import io
import hashlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "waves"
W, H = 72, 16
_RING = 4
_frame_no = 0

STATE_NAMES = ["calm", "breeze", "moderate", "rough", "storm"]

# Parameters are intentionally physical-ish rather than arbitrary animation
# knobs. Wave families deliberately travel in both directions and reflect from the
# side walls. Higher states increase amplitude, steepness, high-frequency energy
# and propagation speed together.
SEA_STATES = {
    "calm": {
        "level": 0.00, "base_y": 6.0, "amp": 0.55, "speed": 0.72,
        "steep": 0.08, "chop": 0.10, "foam": 0.00, "spray": 0.00,
    },
    "breeze": {
        "level": 0.25, "base_y": 6.0, "amp": 0.95, "speed": 0.86,
        "steep": 0.13, "chop": 0.20, "foam": 0.08, "spray": 0.00,
    },
    "moderate": {
        "level": 0.50, "base_y": 6.1, "amp": 1.55, "speed": 1.03,
        "steep": 0.20, "chop": 0.36, "foam": 0.28, "spray": 0.02,
    },
    "rough": {
        "level": 0.75, "base_y": 6.3, "amp": 2.25, "speed": 1.20,
        "steep": 0.28, "chop": 0.55, "foam": 0.58, "spray": 0.12,
    },
    "storm": {
        "level": 1.00, "base_y": 6.6, "amp": 3.00, "speed": 1.38,
        "steep": 0.36, "chop": 0.72, "foam": 0.90, "spray": 0.25,
    },
}


def parse_args():
    p = argparse.ArgumentParser(description="Ambient realistic ocean waves for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--fps", type=float, default=12.0, help="frames per second / smoothness (default: 12)")
    p.add_argument("--animation-speed", dest="animation_speed", type=float, default=2.0,
                   help="global animation speed multiplier; 0.5=half speed, 2=double (default: 2.0)")
    p.add_argument("--state", dest="state",
                   choices=["auto"] + STATE_NAMES, default="auto",
                   help="sea state (default: auto)")
    p.add_argument("--state-seconds", type=float, default=24.0,
                   help="time spent at each auto state before changing (default: 24)")
    p.add_argument("--transition-seconds", type=float, default=10.0,
                   help="duration of each smooth state transition (default: 10)")
    p.add_argument("--cycle", default=None,
                   help="comma-separated auto sequence, e.g. calm,breeze,moderate,rough,storm")
    p.add_argument("--wall-feedback", type=float, default=0.72,
                   help="edge reflection/pile-up strength, 0..1 (default: 0.72)")
    p.add_argument("--sloshing", type=float, default=0.70,
                   help="slow bottle-like left/right slosh strength, 0..1 (default: 0.70)")
    p.add_argument("--muted", action="store_true", help="disable thunder audio during lightning")
    p.add_argument("--thunder-test", action="store_true", help="upload and play one thunderclap immediately, then exit")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    return p.parse_args()


THUNDER_ASSET = None  # resolved from the WAV content hash at runtime


def _thunder_wav():
    """Generate a thunderclap tuned for the BUSY Bar's tiny loudspeaker.

    A physically realistic thunder recording carries a lot of energy below
    100 Hz, which a very small speaker mostly cannot reproduce.  This version
    deliberately moves the perceived weight upward into roughly 140-1200 Hz,
    keeps several rolling bursts audible for seconds, and uses a short fade-in
    to avoid the click produced by an abrupt first sample.
    """
    rate = 44100
    duration = 1.25
    count = int(rate * duration)
    rng = random.Random(0xB05712)
    out = bytearray()

    # One-pole low-pass states. Differences between them form broad band-pass
    # signals without external DSP dependencies:
    #   presence: ~500-2200 Hz, body: ~150-650 Hz, weight: <~180 Hz.
    lp_fast = 0.0
    lp_mid = 0.0
    lp_low = 0.0

    # Irregular rolling thunder events.  The later events are intentionally
    # strong enough to remain audible on the BUSY Bar speaker instead of
    # disappearing into an inaudible sub-bass tail.
    rolls = (
        (0.05, 1.00, 3.2),
        (0.42, 0.88, 2.6),
        (0.92, 0.72, 2.2),
        (1.48, 0.52, 1.9),
    )

    for i in range(count):
        t = i / rate
        white = rng.random() * 2.0 - 1.0

        # Broad spectral bands with substantially more midrange than v11.
        lp_fast += 0.23 * (white - lp_fast)
        lp_mid += 0.060 * (white - lp_mid)
        lp_low += 0.016 * (white - lp_low)
        presence = lp_fast - lp_mid
        body_noise = lp_mid - lp_low
        weight_noise = lp_low

        # A thunder crack rather than a digital click: it rises over ~8 ms,
        # then decays quickly, with noisy midrange and a few resonances.
        crack_attack = 1.0 - math.exp(-t * 125.0)
        crack_decay = math.exp(-t * 8.5)
        crack_env = crack_attack * crack_decay
        crack = (
            1.00 * presence
            + 0.55 * body_noise
            + 0.14 * math.sin(2.0 * math.pi * 720.0 * t)
            + 0.12 * math.sin(2.0 * math.pi * 410.0 * t + 0.7)
        ) * crack_env

        # Each roll has a quick but non-instantaneous attack and a compact decay.
        # Slight amplitude modulation makes the tail breathe and break up.
        body_env = 0.0
        presence_env = 0.0
        for onset, strength, decay in rolls:
            dt = t - onset
            if dt > 0.0:
                rise = 1.0 - math.exp(-dt * 12.0)
                fall = math.exp(-dt * decay)
                e = strength * rise * fall
                body_env += e
                presence_env += e * math.exp(-dt * 0.55)

        flutter = (
            0.78
            + 0.11 * math.sin(2.0 * math.pi * 1.3 * t + 0.3)
            + 0.07 * math.sin(2.0 * math.pi * 3.1 * t + 1.2)
            + 0.04 * math.sin(2.0 * math.pi * 6.8 * t + 0.5)
        )

        # Speaker-friendly body: most energy lives above 120 Hz.  The tones are
        # intentionally not pure fundamentals; they reinforce perceived bass on
        # a speaker that cannot reproduce true 40-70 Hz thunder energy.
        resonances = (
            0.24 * math.sin(2.0 * math.pi * 148.0 * t + 0.2)
            + 0.17 * math.sin(2.0 * math.pi * 196.0 * t + 1.0)
            + 0.11 * math.sin(2.0 * math.pi * 286.0 * t + 2.1)
            + 0.055 * math.sin(2.0 * math.pi * 430.0 * t + 0.6)
        )
        rumble = (
            1.45 * body_noise
            + 0.72 * weight_noise
            + 0.38 * presence * presence_env
            + resonances
        ) * body_env * flutter

        # A quieter, grainy high-mid layer keeps the short tail perceptible.
        air = presence * (0.13 + 0.10 * math.sin(2.0 * math.pi * 0.73 * t)) * body_env

        # Fade the final ~0.45 second smoothly.  A tiny master fade-in eliminates any
        # discontinuity at sample zero even if the decoder starts immediately.
        fade_in = min(1.0, t / 0.012)
        fade_out = clamp((duration - t) / 0.25)
        sample = (0.62 * crack + 0.84 * rumble + 0.30 * air) * fade_in * fade_out

        # Strong soft compression is intentional: it keeps later rolls audible
        # while preventing the initial crack from clipping.
        sample = math.tanh(sample * 2.25) * 0.88
        out += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))

    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(out))
    return bio.getvalue()

def _upload_audio(host):
    """Upload thunder under a content-addressed filename.

    Reusing a fixed path such as thunder.wav can leave the device playing a
    previously decoded/cached asset even after new bytes were uploaded.  The
    SHA-256 suffix makes every materially different thunder waveform a new
    device path, while identical launches reuse the same stable name.
    """
    global THUNDER_ASSET
    wav = _thunder_wav()
    digest = hashlib.sha256(wav).hexdigest()[:12]
    THUNDER_ASSET = f"thunder_{digest}.wav"
    path = "/api/assets/upload?" + urllib.parse.urlencode({"application_name": APP, "file": THUNDER_ASSET})
    req = urllib.request.Request(_base(host) + path, data=wav, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=15):
        pass
    return THUNDER_ASSET, len(wav)


def _play_thunder(host):
    if not THUNDER_ASSET:
        raise RuntimeError("thunder asset has not been uploaded")
    body = json.dumps({"application_name": APP, "path": THUNDER_ASSET}).encode()
    req = urllib.request.Request(_base(host) + "/api/audio/play", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.HTTPError as e:
        # Audio already busy should not interrupt the animation.
        if e.code not in (409, 410):
            raise


def _stop_audio(host):
    req = urllib.request.Request(_base(host) + "/api/audio/play", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.HTTPError as e:
        if e.code not in (404, 410):
            raise


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


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


def _post(host, path, data, content_type):
    req = urllib.request.Request(_base(host) + path, data=data, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode()


def show(host, pixels):
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    try:
        _post(host, "/api/assets/upload?application_name=%s&file=%s" % (APP, fn),
              _png(pixels), "application/octet-stream")
        body = {
            "application_name": APP,
            "priority": 30,
            "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}],
        }
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return 409
        raise


def clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception:
        pass


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def smoothstep(t):
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)


def mix_color(a, b, t):
    t = clamp(t)
    return tuple(int(a[i] + (b[i] - a[i]) * t + 0.5) for i in range(3))


def lerp_params(a, b, t):
    t = smoothstep(t)
    return {k: a[k] + (b[k] - a[k]) * t for k in a}


# Ocean palette tuned for the BUSY Bar's tiny, high-contrast LED matrix.
# The important change from v4 is that water is no longer a single linear
# mid->deep gradient. Real water loses red first, then green, while surface
# scattering introduces a brighter cyan/green band only in the top pixels.
SKY_CALM = (2, 9, 18)
SKY_STORM = (5, 8, 16)

# Calm water is slightly clearer/greener; rough water becomes darker and
# greyer as the apparent surface reflection increases.
DEEP_CALM = (0, 11, 30)
DEEP_STORM = (1, 7, 22)
LOW_CALM = (0, 30, 58)
LOW_STORM = (1, 22, 47)
MID_CALM = (0, 73, 105)
MID_STORM = (3, 48, 76)
UPPER_CALM = (5, 113, 132)
UPPER_STORM = (7, 78, 100)
SHALLOW_CALM = (22, 145, 151)
SHALLOW_STORM = (25, 107, 119)
CREST_CALM = (103, 205, 195)
CREST_STORM = (105, 177, 176)

# Foam is deliberately not pure white. A cold grey/cyan survives the LED
# display better and reads more like aerated seawater than a glowing stripe.
FOAM_CALM = (205, 232, 222)
FOAM_STORM = (188, 211, 208)
LIGHTNING = (226, 239, 244)


def water_color(depth, level):
    """Depth-aware ocean colour with non-linear optical attenuation.

    depth is measured in pixels below the instantaneous surface. The first
    ~2 px contain most of the turquoise surface scattering; below that the
    gradient compresses rapidly toward dark blue. This gives the 16px display
    much more perceived depth than a linear RGB interpolation.
    """
    deep = mix_color(DEEP_CALM, DEEP_STORM, level)
    low = mix_color(LOW_CALM, LOW_STORM, level)
    mid = mix_color(MID_CALM, MID_STORM, level)
    upper = mix_color(UPPER_CALM, UPPER_STORM, level)
    shallow = mix_color(SHALLOW_CALM, SHALLOW_STORM, level)
    crest = mix_color(CREST_CALM, CREST_STORM, level)

    d = max(0.0, depth)
    if d < 0.55:
        return mix_color(crest, shallow, smoothstep(d / 0.55))
    if d < 1.8:
        return mix_color(shallow, upper, smoothstep((d - 0.55) / 1.25))
    if d < 4.0:
        return mix_color(upper, mid, smoothstep((d - 1.8) / 2.2))
    if d < 7.5:
        return mix_color(mid, low, smoothstep((d - 4.0) / 3.5))
    return mix_color(low, deep, smoothstep((d - 7.5) / 5.5))


def _set(buf, x, y, c):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = c


def _add(buf, x, y, c, alpha):
    if not (0 <= x < W and 0 <= y < H):
        return
    old = buf[y * W + x]
    a = clamp(alpha)
    buf[y * W + x] = tuple(
        min(255, int(old[i] * (1.0 - a) + c[i] * a + 0.5)) for i in range(3)
    )


class SeaStateMachine:
    """Timed state transitions with adjacent-state motion and smooth interpolation."""

    def __init__(self, state, state_seconds, transition_seconds, cycle=None):
        self.fixed = state != "auto"
        self.state_seconds = max(1.0, state_seconds)
        self.transition_seconds = max(0.1, transition_seconds)
        self.cycle = self._parse_cycle(cycle)
        self.cycle_pos = 0
        self.direction = 1

        initial = state if self.fixed else (self.cycle[0] if self.cycle else "calm")
        self.current_name = initial
        self.target_name = initial
        self.from_params = dict(SEA_STATES[initial])
        self.to_params = dict(SEA_STATES[initial])
        now = time.monotonic()
        self.transition_started = now
        self.transitioning = False
        self.next_change = now + self.state_seconds

        self.flash_until = 0.0
        self.next_flash = now + random.uniform(18.0, 35.0)

    @staticmethod
    def _parse_cycle(spec):
        if not spec:
            return None
        names = [s.strip().lower() for s in spec.split(",") if s.strip()]
        bad = [s for s in names if s not in SEA_STATES]
        if bad:
            raise ValueError("unknown --cycle state(s): " + ", ".join(bad))
        if not names:
            raise ValueError("--cycle cannot be empty")
        return names

    def _choose_next(self):
        if self.cycle:
            self.cycle_pos = (self.cycle_pos + 1) % len(self.cycle)
            return self.cycle[self.cycle_pos]

        # Natural random walk: move only one Beaufort-like step at a time.
        i = STATE_NAMES.index(self.current_name)
        if i == 0:
            self.direction = 1
        elif i == len(STATE_NAMES) - 1:
            self.direction = -1
        elif random.random() < 0.22:
            # Occasionally reverse the trend, but never jump across states.
            self.direction *= -1
        return STATE_NAMES[i + self.direction]

    def update(self, now):
        if self.fixed:
            params = SEA_STATES[self.current_name]
        else:
            if not self.transitioning and now >= self.next_change:
                self.target_name = self._choose_next()
                self.from_params = dict(SEA_STATES[self.current_name])
                self.to_params = dict(SEA_STATES[self.target_name])
                self.transition_started = now
                self.transitioning = True
                print(f"transition: {self.current_name} -> {self.target_name} "
                      f"({self.transition_seconds:g}s)")

            if self.transitioning:
                p = (now - self.transition_started) / self.transition_seconds
                params = lerp_params(self.from_params, self.to_params, p)
                if p >= 1.0:
                    self.current_name = self.target_name
                    self.transitioning = False
                    self.next_change = now + self.state_seconds
                    params = SEA_STATES[self.current_name]
                    print(f"state: {self.current_name} ({self.state_seconds:g}s)")
            else:
                params = SEA_STATES[self.current_name]

        level = params["level"]
        if level > 0.88 and now >= self.next_flash and random.random() < 0.035:
            self.flash_until = now + random.uniform(0.05, 0.10)
            self.next_flash = now + random.uniform(13.0, 30.0)

        return params, now < self.flash_until


class SloshDynamics:
    """Low-frequency liquid inertia with damped, irregular re-excitation.

    This is deliberately not a sine oscillator.  A velocity-like state carries
    the water toward one wall, loses energy through damping, reverses naturally,
    and receives small state-dependent impulses at irregular intervals.  The
    result is a bottle/tank motion whose period and amplitude slowly wander.
    """

    def __init__(self):
        self.angle = 0.0
        self.velocity = 0.0
        self.drive = 0.0
        self.last_t = None
        self.next_impulse = 0.0
        self.rng = random.Random(41723)

    def update(self, t, level, strength):
        if self.last_t is None:
            self.last_t = t
            self.next_impulse = t + self.rng.uniform(2.5, 5.5)
            return self.angle, self.velocity

        dt = min(0.10, max(0.0, t - self.last_t))
        self.last_t = t
        strength = clamp(strength)

        # Irregular external nudges emulate the bottle being disturbed. Stronger
        # sea states receive slightly larger and more frequent pushes.
        if t >= self.next_impulse:
            impulse = self.rng.uniform(-1.0, 1.0)
            self.velocity += impulse * (0.12 + 0.20 * level) * strength
            self.drive = self.rng.uniform(-1.0, 1.0) * (0.015 + 0.025 * level)
            self.next_impulse = t + self.rng.uniform(2.2, 5.8 - 1.4 * level)

        # Damped spring-like bulk liquid motion.  The nonlinear restoring term
        # keeps large excursions soft instead of perfectly harmonic.
        restoring = -0.62 * self.angle - 0.16 * self.angle * abs(self.angle)
        damping = -0.46 * self.velocity
        accel = restoring + damping + self.drive
        self.velocity += accel * dt
        self.angle += self.velocity * dt
        self.angle = max(-1.25, min(1.25, self.angle))
        self.drive *= math.exp(-dt * 0.55)
        return self.angle, self.velocity


_SLOSH = SloshDynamics()


def wave_surface(x, t, p, wall_feedback=0.72, sloshing=0.70, slosh_state=None):
    """Bidirectional bounded-liquid surface.

    The surface is the sum of independent right- and left-going wave families,
    their wall-reflected copies, a low-frequency inertial tank mode and local
    wind chop.  No component wraps around the display.  Because the two travelling
    families have different wavelengths and phase velocities, individual crests
    naturally overtake, cancel and reverse locally instead of the whole surface
    appearing to march in one direction.
    """
    amp = p["amp"]
    speed = p["speed"]
    steep = p["steep"]
    chop = p["chop"]
    level = p["level"]
    wall_feedback = clamp(wall_feedback)
    sloshing = clamp(sloshing)
    L = W - 1.0

    if slosh_state is None:
        slosh_pos, slosh_vel = _SLOSH.update(t, level, sloshing)
    else:
        slosh_pos, slosh_vel = slosh_state

    # Bulk liquid inertia.  cos(pi*x/L) puts opposite displacement at the two
    # walls and a node near the centre. Velocity adds a slight dynamic skew, so
    # the surface keeps moving through the neutral position instead of stopping.
    tank_shape = math.cos(math.pi * x / L)
    tank_shape2 = math.sin(2.0 * math.pi * x / L)
    slosh = amp * sloshing * (
        (0.28 + 0.20 * level) * slosh_pos * tank_shape
        + (0.055 + 0.050 * level) * slosh_vel * tank_shape2
    )

    # Reflection envelope: wall interaction is strong near the sides but the
    # reflected waves remain visible in the middle, which prevents a single
    # dominant propagation direction.
    edge_d = min(x, L - x)
    edge_env = math.exp(-edge_d / 13.0)
    refl = wall_feedback * (0.34 + 0.66 * edge_env)

    y = p["base_y"] + slosh

    # Long right-going swell and its left-going reflection.
    k1 = 2.0 * math.pi / 43.0
    f1 = k1 * x - speed * 0.90 * t + 0.10
    r1 = k1 * x + speed * 0.84 * t + 1.05
    y += amp * 0.66 * math.sin(f1)
    y += amp * 0.42 * refl * math.sin(r1)
    y += amp * steep * 0.34 * math.sin(2.0 * f1 + 0.35)
    y += amp * steep * 0.17 * refl * math.sin(2.0 * r1 - 0.20)

    # Independent left-going swell.  This is not merely the reflection of k1;
    # its different wavelength and phase speed create natural crossing patterns.
    k2 = 2.0 * math.pi / 61.0
    l2 = k2 * x + speed * 0.63 * t + 2.15
    rr2 = k2 * x - speed * 0.58 * t + 0.72
    y += amp * 0.31 * math.sin(l2)
    y += amp * 0.20 * refl * math.sin(rr2)
    y += amp * steep * 0.10 * math.sin(2.0 * l2 + 0.50)

    # Mid-scale crossing waves. Their amplitudes stay below the long swell so
    # storm mode remains recognisably fluid rather than random/noisy.
    k3 = 2.0 * math.pi / 25.0
    a3 = k3 * x - speed * 1.18 * t + 1.40
    b3 = k3 * x + speed * 0.96 * t + 3.00
    y += amp * chop * 0.18 * math.sin(a3)
    y += amp * chop * 0.14 * math.sin(b3)

    # Wall pile-up is coupled to actual inertial direction rather than a fixed
    # clock. Positive velocity drives water toward one side; negative toward the
    # other. The exponential shape makes the fluid climb the wall smoothly.
    push_r = clamp(max(0.0, slosh_vel) * 2.2)
    push_l = clamp(max(0.0, -slosh_vel) * 2.2)
    pile = amp * wall_feedback * (0.20 + 0.20 * level)
    y -= pile * (push_l * math.exp(-x / 5.0)
                 + push_r * math.exp(-(L - x) / 5.0))

    # Short capillary/chop components run in both directions.  Their amplitudes
    # are kept small, especially in calm states, to avoid the old random look.
    k4 = 2.0 * math.pi / 13.5
    y += amp * chop * 0.080 * math.sin(k4 * x - speed * 1.72 * t + 0.25)
    y += amp * chop * 0.060 * math.sin(k4 * x + speed * 1.49 * t + 2.30)

    # Very subtle vertical breathing avoids a perfectly fixed mean water volume.
    y += (0.025 + 0.045 * level) * math.sin(t * 0.19 + 0.7)
    return y

def render(t, p, flash=False, wall_feedback=0.72, sloshing=0.70):
    level = p["level"]
    sky = mix_color(SKY_CALM, SKY_STORM, level)
    crest_col = mix_color(CREST_CALM, CREST_STORM, level)
    foam_col = mix_color(FOAM_CALM, FOAM_STORM, level)
    shallow_col = mix_color(SHALLOW_CALM, SHALLOW_STORM, level)
    buf = [sky] * (W * H)

    # Update the bulk-liquid inertia exactly once per frame, then reuse that
    # state for every column.  Calling it per x would introduce artificial
    # phase/energy differences across the surface.
    slosh_state = _SLOSH.update(t, level, sloshing)
    surface = [wave_surface(x, t, p, wall_feedback, sloshing, slosh_state) for x in range(W)]

    # Water volume and subtle moving caustic texture.
    for x in range(W):
        sy = surface[x]
        for y in range(H):
            depth = y - sy
            if depth < 0:
                continue
            col = water_color(depth, level)

            # Moving caustics/specular modulation. Keep it subtle and strongest
            # near the surface; deeper water receives almost no brightening.
            shimmer_a = 0.5 + 0.5 * math.sin(x * 0.31 + y * 0.53 - t * (0.72 + level * 0.22))
            shimmer_b = 0.5 + 0.5 * math.sin(x * 0.13 - y * 0.37 + t * 0.41)
            shimmer = shimmer_a * shimmer_b
            near_surface = math.exp(-max(0.0, depth) / 3.4)
            col = mix_color(col, shallow_col, (0.035 + 0.025 * (1.0 - level)) * shimmer * near_surface)

            # Very mild blue absorption with depth. This darkens green before
            # blue and prevents the bottom rows from looking uniformly painted.
            absorb = clamp((depth - 4.0) / 10.0)
            if absorb > 0:
                col = (
                    int(col[0] * (1.0 - 0.34 * absorb)),
                    int(col[1] * (1.0 - 0.20 * absorb)),
                    int(col[2] * (1.0 - 0.07 * absorb)),
                )
            _set(buf, x, y, col)

    # Foam is based on crest height + negative curvature. This makes it gather
    # near physically plausible breaking crests instead of appearing on arbitrary
    # steep slopes all over the surface.
    foam_amount = p["foam"]
    for x in range(W):
        xm = max(0, x - 1)
        xp = min(W - 1, x + 1)
        s = surface[x]
        curvature = surface[xm] - 2.0 * s + surface[xp]
        mean_y = p["base_y"]
        crest_height = clamp((mean_y - s) / max(0.5, p["amp"] * 1.15))
        crest_curve = clamp((-curvature - 0.035) * 2.6)
        breaking = clamp(foam_amount * (0.62 * crest_height + 0.95 * crest_curve))

        yi = int(round(s))
        _add(buf, x, yi, crest_col, 0.58 + 0.17 * level)
        if breaking > 0.10:
            _add(buf, x, yi - 1, foam_col, 0.34 + breaking * 0.52)
        if breaking > 0.52:
            # A short lee-side streak reads better as foam than isolated speckles.
            if x + 1 < W:
                _add(buf, x + 1, yi, foam_col, breaking * 0.44)
            if x + 2 < W:
                _add(buf, x + 2, yi, crest_col, breaking * 0.28)

    # Only rough/storm seas produce detached spray. Particles are derived from
    # current breaking crests and advect consistently left/up for several frames.
    if p["spray"] > 0.0:
        tick = int(t * 8.0)
        rnd = random.Random(1709 + tick // 3)
        candidates = []
        for x in range(1, W - 1):
            s = surface[x]
            curvature = surface[x - 1] - 2.0 * s + surface[x + 1]
            if curvature < -0.20 and s < p["base_y"] - p["amp"] * 0.28:
                candidates.append(x)
        count = min(len(candidates), int(round(p["spray"] * 8)))
        if count:
            for x0 in rnd.sample(candidates, count):
                age = tick % 3
                x = max(0, x0 - age)
                y = int(round(surface[x0])) - 2 - age // 2
                _add(buf, x, y, foam_col, 0.46 + 0.22 * p["spray"])

    if flash:
        for y in range(H):
            a = 0.66 if y < 7 else 0.20
            for x in range(W):
                _add(buf, x, y, LIGHTNING, a)

    return buf


def main():
    args = parse_args()
    fps = max(2.0, min(20.0, args.fps))
    interval = 1.0 / fps

    try:
        sea = SeaStateMachine(args.state, args.state_seconds, args.transition_seconds, args.cycle)
    except ValueError as e:
        sys.exit(f"error: {e}")

    animation_speed = max(0.05, min(4.0, args.animation_speed))
    started = time.monotonic()
    thunder_due = None
    flash_was_on = False

    if not args.muted and not args.test:
        try:
            asset_name, asset_bytes = _upload_audio(args.host)
            print(f"thunder audio: ready ({asset_name}, {asset_bytes} bytes)")
        except Exception as e:
            print(f"warning: thunder audio unavailable ({e}); continuing silently")
            args.muted = True
    if args.thunder_test:
        if args.muted:
            sys.exit("error: --thunder-test cannot be used with --muted")
        try:
            print(f"playing thunder test from {THUNDER_ASSET}")
            _play_thunder(args.host)
            time.sleep(6.8)
        finally:
            try:
                _stop_audio(args.host)
            except Exception:
                pass
        return

    print(f"waves -> {_base(args.host)}  state={args.state} fps={fps:g} speed={animation_speed:g}x "
          f"hold={max(1.0,args.state_seconds):g}s transition={max(0.1,args.transition_seconds):g}s "
          f"walls={clamp(args.wall_feedback):.2f} slosh={clamp(args.sloshing):.2f} "
          "(Ctrl-C to stop)")

    try:
        if args.test:
            p = SEA_STATES["rough"]
            show(args.host, render(2.0, p, False, args.wall_feedback, args.sloshing))
            print("test: drew one rough-sea frame")
            return

        while True:
            frame_start = time.monotonic()
            p, flash = sea.update(frame_start)
            # Trigger one thunderclap per lightning event. A short random delay
            # gives the flash/sound pairing a little depth without blocking frames.
            if flash and not flash_was_on and not args.muted:
                thunder_due = frame_start + random.uniform(0.10, 0.38)
            flash_was_on = flash
            if thunder_due is not None and frame_start >= thunder_due:
                try:
                    _play_thunder(args.host)
                except Exception as e:
                    print(f"warning: thunder playback failed ({e})")
                thunder_due = None
            # Scale only the visual simulation clock. State hold/transition timers
            # intentionally remain in real seconds, so --animation-speed changes
            # how fast the water moves without changing the auto-state schedule.
            t = (frame_start - started) * animation_speed
            show(args.host, render(t, p, flash, args.wall_feedback, args.sloshing))
            elapsed = time.monotonic() - frame_start
            if elapsed < interval:
                time.sleep(interval - elapsed)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} - {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {_base(args.host)} - {e.reason}")
    finally:
        if not args.muted:
            try:
                _stop_audio(args.host)
            except Exception:
                pass
        clear(args.host)


if __name__ == "__main__":
    main()
