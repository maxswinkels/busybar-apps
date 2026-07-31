#!/usr/bin/env python3
"""Audio visualizer: live spectrum bars from your microphone.

Live spectrum from the mic in several render styles (bars, mirror, segments,
dots, wave), each recolourable with a theme, plus floating peak-hold caps.

    python3 app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
    python3 app.py --style segments --theme fire
    python3 app.py --demo                 # no mic/ffmpeg: cycles every style + theme

Options: --style {bars,mirror,segments,dots,wave}
         --theme {classic,fire,ocean,aurora,rainbow}  --fps N

Live capture needs macOS with `ffmpeg` installed (`brew install ffmpeg`) and
microphone access; it reads the built-in mic via avfoundation. The --demo mode
needs neither and is handy for a quick look.
"""
import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

APP = "audio-visualizer"

SAMPLE_RATE = 22050
CHUNK_SAMPLES = 2048
NUM_BANDS = 24
FREQ_MIN = 60.0
FREQ_MAX = 8000.0

# 24 target frequencies, log-spaced from 60 Hz to 8000 Hz
_log_min = math.log(FREQ_MIN)
_log_max = math.log(FREQ_MAX)
BAND_FREQS = [
    math.exp(_log_min + (_log_max - _log_min) * i / (NUM_BANDS - 1))
    for i in range(NUM_BANDS)
]

DISPLAY_H = 16

# Colour themes as vertical gradient stops: (position 0..1, (r, g, b) in 0..1).
# Position 0 is the bottom row of the display, 1 is the top. A bar samples the
# palette from its base up to its current peak, so quiet bands stay in the cool
# low colours and loud bands climb into the hot top colours.
THEMES = {
    "classic": [(0.0, (0.00, 0.78, 0.00)), (0.5, (1.00, 0.78, 0.00)), (1.0, (1.00, 0.12, 0.00))],
    "fire":    [(0.0, (0.45, 0.00, 0.00)), (0.35, (1.00, 0.25, 0.00)), (0.7, (1.00, 0.65, 0.00)), (1.0, (1.00, 1.00, 0.75))],
    "ocean":   [(0.0, (0.00, 0.10, 0.55)), (0.45, (0.00, 0.50, 0.95)), (0.8, (0.00, 0.90, 0.95)), (1.0, (0.80, 1.00, 1.00))],
    "aurora":  [(0.0, (0.00, 0.35, 0.20)), (0.4, (0.00, 0.85, 0.50)), (0.7, (0.20, 0.95, 0.75)), (1.0, (0.65, 0.30, 0.95))],
}
THEME_NAMES = list(THEMES.keys()) + ["rainbow"]

# Render styles: the shape/layout of the visualiser (independent of --theme).
STYLE_NAMES = ["bars", "mirror", "segments", "dots", "wave"]

# Bright cap that floats on top of each bar at its recent peak.
PEAK_COLOR = "#FFFFFFFF"
# Peak caps fall this many pixels per second, then are scaled to the frame rate.
PEAK_FALL_PER_SEC = 9.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _draw(host, elements):
    """POST /api/display/draw. Returns (status_code, body_text)."""
    body = {"application_name": APP, "elements": elements}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _base(host) + "/api/display/draw",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode(), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except urllib.error.URLError as e:
        raise RuntimeError(f"draw failed: {e}") from e


def _clear(host):
    """DELETE /api/display/draw?application_name=..."""
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(
        _base(host) + "/api/display/draw?" + qs,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise RuntimeError(f"clear failed: {e}") from e


# ---------------------------------------------------------------------------
# Spectrum: Goertzel algorithm (pure Python, no numpy)
# ---------------------------------------------------------------------------

def goertzel_magnitude(samples, freq, sample_rate):
    """Compute the magnitude of a single frequency bin using the Goertzel algorithm."""
    n = len(samples)
    k = freq * n / sample_rate
    omega = 2.0 * math.pi * k / n
    cos_w = math.cos(omega)
    coeff = 2.0 * cos_w
    sin_w = math.sin(omega)
    q1 = 0.0
    q2 = 0.0
    for s in samples:
        q0 = coeff * q1 - q2 + s
        q2 = q1
        q1 = q0
    real = q1 - q2 * math.cos(omega)
    imag = q2 * math.sin(omega)
    return math.sqrt(real * real + imag * imag)


def compute_band_magnitudes(samples):
    """Return list of 24 raw magnitudes, one per band."""
    return [goertzel_magnitude(samples, f, SAMPLE_RATE) for f in BAND_FREQS]


# ---------------------------------------------------------------------------
# Colour / palette
# ---------------------------------------------------------------------------

def _hex(r, g, b, a=1.0):
    """(r, g, b[, a]) in 0..1 -> '#RRGGBBAA'."""
    clamp = lambda v: max(0, min(255, int(v * 255 + 0.5)))
    return "#%02X%02X%02X%02X" % (clamp(r), clamp(g), clamp(b), clamp(a))


def _hsv(h, s, v):
    """HSV (all 0..1) -> (r, g, b) in 0..1."""
    i = int(h * 6.0) % 6
    f = h * 6.0 - int(h * 6.0)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    return [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]


def _sample(stops, t):
    """Linearly interpolate a list of (pos, (r, g, b)) gradient stops at t in 0..1."""
    t = max(0.0, min(1.0, t))
    prev = stops[0]
    for stop in stops:
        if t <= stop[0]:
            (p0, c0), (p1, c1) = prev, stop
            span = (p1 - p0) or 1.0
            k = (t - p0) / span
            return tuple(c0[j] + (c1[j] - c0[j]) * k for j in range(3))
        prev = stop
    return stops[-1][1]


def _theme_rgb(band_i, t, theme, num_bands):
    """Single (r, g, b) for a band at absolute height fraction t in 0..1."""
    t = max(0.0, min(1.0, t))
    if theme == "rainbow":
        return _hsv(band_i / max(1, num_bands), 1.0, 0.35 + 0.65 * t)
    return _sample(THEMES.get(theme, THEMES["classic"]), t)


def _bar_gradient(band_i, h, theme, num_bands):
    """(top_color, base_color) hex strings for a bar of height h (top-of-bar first)."""
    return (_hex(*_theme_rgb(band_i, h / DISPLAY_H, theme, num_bands)),
            _hex(*_theme_rgb(band_i, 0.0, theme, num_bands)))


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _rect(el_id, x, y, w, h, colors, fill="solid"):
    return {
        "id": str(el_id),
        "type": "rectangle",
        "x": x, "y": y, "width": w, "height": h,
        "border_width": 0,
        "fill": fill,
        "fill_colors": colors,
    }


# Each style is a separate builder. Bands are 2 px wide on a 3 px pitch.

def _build_bars(heights, peaks, theme):
    """Vertical gradient bars anchored at the bottom, floating peak-hold caps."""
    n = len(heights)
    els = []
    for i, h in enumerate(heights):
        x = i * 3
        if h > 0:
            top_hex, base_hex = _bar_gradient(i, h, theme, n)
            # renderer's gradient_v lerps fill_colors[0] (top row) -> [1] (bottom row)
            els.append(_rect(f"bar{i}", x, DISPLAY_H - h, 2, h, [top_hex, base_hex], fill="gradient_v"))
        ph = int(peaks[i])
        if ph > h and ph > 0:
            els.append(_rect(f"peak{i}", x, DISPLAY_H - ph, 2, 1, [PEAK_COLOR]))
    return els


def _build_mirror(heights, peaks, theme):
    """Bars grow symmetrically from the horizontal centre, up and down."""
    n = len(heights)
    mid = DISPLAY_H // 2  # 8
    els = []
    for i, h in enumerate(heights):
        x = i * 3
        half = h // 2
        if half > 0:
            hot = _hex(*_theme_rgb(i, h / DISPLAY_H, theme, n))
            base = _hex(*_theme_rgb(i, 0.0, theme, n))
            els.append(_rect(f"bar{i}t", x, mid - half, 2, half, [hot, base], fill="gradient_v"))   # top half
            els.append(_rect(f"bar{i}b", x, mid, 2, half, [base, hot], fill="gradient_v"))           # bottom half
        phalf = int(peaks[i]) // 2
        if phalf > half and phalf > 0:
            els.append(_rect(f"peak{i}t", x, mid - phalf, 2, 1, [PEAK_COLOR]))
            els.append(_rect(f"peak{i}b", x, mid + phalf - 1, 2, 1, [PEAK_COLOR]))
    return els


# LED-block geometry for the segmented style: 2 px lit block, 1 px dark gap.
SEG_BLOCK, SEG_GAP = 2, 1
SEG_PITCH = SEG_BLOCK + SEG_GAP
SEG_SLOTS = 6


def _seg_block(k):
    """(y, height) of block slot k (0 = bottom), clamped into the display."""
    y = DISPLAY_H - SEG_BLOCK - k * SEG_PITCH
    bh = SEG_BLOCK
    if y < 0:
        bh += y
        y = 0
    return y, bh


def _build_segments(heights, peaks, theme):
    """Discrete stacked LED blocks per band (classic hardware VU meter)."""
    n = len(heights)
    els = []
    for i, h in enumerate(heights):
        x = i * 3
        lit = int(round(h / DISPLAY_H * SEG_SLOTS))
        for k in range(lit):
            y, bh = _seg_block(k)
            if bh <= 0:
                continue
            t = 1.0 - (y + bh / 2.0) / DISPLAY_H
            els.append(_rect(f"bar{i}_{k}", x, y, 2, bh, [_hex(*_theme_rgb(i, t, theme, n))]))
        peak_slot = int(round(peaks[i] / DISPLAY_H * SEG_SLOTS))
        if peak_slot > lit and peak_slot > 0:
            y, bh = _seg_block(peak_slot - 1)
            if bh > 0:
                els.append(_rect(f"peak{i}", x, y, 2, bh, [PEAK_COLOR]))
    return els


def _build_dots(heights, peaks, theme):
    """Only a bouncing dot at each band top, with a falling peak-hold dot."""
    n = len(heights)
    els = []
    for i, h in enumerate(heights):
        x = i * 3
        if h > 0:
            dh = 2
            y = min(DISPLAY_H - dh, DISPLAY_H - h)
            els.append(_rect(f"bar{i}", x, y, 2, dh, [_hex(*_theme_rgb(i, h / DISPLAY_H, theme, n))]))
        ph = int(peaks[i])
        if ph > h and ph > 0:
            els.append(_rect(f"peak{i}", x, DISPLAY_H - ph, 2, 1, [PEAK_COLOR]))
    return els


def _build_wave(heights, peaks, theme):
    """A continuous contour line connecting the band tops (oscilloscope-style)."""
    n = len(heights)
    tops = [DISPLAY_H - max(1, h) for h in heights]
    els = []
    for i, h in enumerate(heights):
        x = i * 3
        col = _hex(*_theme_rgb(i, max(1, h) / DISPLAY_H, theme, n))
        els.append(_rect(f"bar{i}", x, tops[i], 2, 1, [col]))  # cap at the band top
        if i < n - 1:                                 # vertical connector to the next band
            lo, hi = min(tops[i], tops[i + 1]), max(tops[i], tops[i + 1])
            els.append(_rect(f"conn{i}", x + 2, lo, 1, hi - lo + 1, [col]))
    return els


_STYLE_BUILDERS = {
    "bars": _build_bars,
    "mirror": _build_mirror,
    "segments": _build_segments,
    "dots": _build_dots,
    "wave": _build_wave,
}


def build_elements(heights, peaks, theme, style="bars"):
    """Dispatch to the chosen render style."""
    return _STYLE_BUILDERS.get(style, _build_bars)(heights, peaks, theme)


def magnitudes_to_heights(magnitudes, running_max):
    """Convert raw magnitudes to pixel heights 0..16, updating running_max in place."""
    heights = []
    for mag in magnitudes:
        # decay running max
        running_max[0] = max(running_max[0] * 0.995, 1.0)
        if mag > running_max[0]:
            running_max[0] = mag
        h = int(16.0 * math.log1p(mag) / math.log1p(running_max[0]))
        h = max(0, min(16, h))
        heights.append(h)
    return heights


def smooth_heights(new_heights, old_heights, decay=0.75):
    """Attack/decay smoothing: bars fall at decay rate per frame."""
    return [max(n, int(o * decay)) for n, o in zip(new_heights, old_heights)]


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Audio visualizer for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--fps", type=int, default=8, help="draw updates per second (default: 8)")
    p.add_argument("--style", default="bars", choices=STYLE_NAMES,
                   help="render style / shape (default: bars)")
    p.add_argument("--theme", default="classic", choices=THEME_NAMES,
                   help="colour theme (default: classic)")
    p.add_argument("--device", default="auto", help="ffmpeg avfoundation audio input, e.g. :1 (default: auto)")
    p.add_argument("--demo", action="store_true",
                   help="run a synthetic-audio demo (no mic/ffmpeg) that cycles the styles and themes")
    p.add_argument("--test", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _test_mode(host, theme, style):
    """Draw one synthetic frame and exit."""
    heights = [3, 5, 8, 12, 16, 14, 10, 7, 9, 13, 15, 11, 8, 6, 4, 7, 10, 14, 12, 9, 6, 4, 3, 2]
    peaks = [min(DISPLAY_H, h + 3) for h in heights]
    elements = build_elements(heights, peaks, theme, style)
    status, _ = _draw(host, elements)
    print(f"test: drew 1 frame ({style}/{theme}) with {len(elements)} elements")
    sys.exit(0)


# One (style, theme) pair per style so a viewer sees every look once.
DEMO_COMBOS = [
    ("bars", "fire"),
    ("mirror", "aurora"),
    ("segments", "classic"),
    ("dots", "ocean"),
    ("wave", "rainbow"),
]
DEMO_SECONDS_PER_COMBO = 1.6

# ~100 BPM groove; the whole cycle equals len(DEMO_COMBOS) * DEMO_SECONDS_PER_COMBO.
_DEMO_BEAT_HZ = 100.0 / 60.0


def _synth_heights(t):
    """Music-like spectrum for band 0..23 at time t seconds (no audio needed).

    Models a simple track: a kick that pumps the bass on every beat, a melodic
    peak that sweeps the mids, and hi-hat shimmer up top that flicks on the
    off-beats. Layered so it reads as music, not a mechanical wave.
    """
    phase = (t * _DEMO_BEAT_HZ) % 1.0              # position within the current beat
    kick = math.exp(-6.0 * phase)                  # sharp attack, quick decay each beat
    off = (phase + 0.5) % 1.0
    snare = math.exp(-9.0 * off)                   # back-beat on the off-beat
    swell = 0.85 + 0.15 * math.sin(t * 0.5)        # slow overall breathing
    mel_center = 6.0 + 8.0 * (0.5 + 0.5 * math.sin(t * 0.8))   # melody drifts across the mids

    heights = []
    for i in range(NUM_BANDS):
        if i <= 5:                                 # bass: kick-driven wobble
            wob = 0.5 + 0.5 * math.sin(t * 2.0 + i * 0.7)
            val = (0.55 + 0.45 * wob) * (0.30 + 0.80 * kick)
        elif i <= 14:                              # mids: sweeping melodic peak + groove
            mel = math.exp(-((i - mel_center) ** 2) / 6.0)
            groove = 0.5 + 0.5 * math.sin(t * 3.3 + i * 0.5)
            val = 0.20 + 0.75 * mel * (0.4 + 0.6 * groove) + 0.20 * snare
        else:                                      # highs: hi-hat shimmer on the off-beats
            shimmer = 0.5 + 0.5 * math.sin(t * 11.0 + i * 1.7)
            hat = math.exp(-12.0 * off)
            val = (0.10 + 0.35 * shimmer) * (0.5 + 0.9 * hat)
        val *= swell
        heights.append(max(0, min(DISPLAY_H, int(round(val * DISPLAY_H)))))
    return heights


def _demo_mode(host, fps):
    """Drive the real render pipeline with a synthetic spectrum, cycling looks."""
    interval = 1.0 / max(1, fps)
    peak_fall = max(0.3, PEAK_FALL_PER_SEC / max(1, fps))
    smooth = [0] * NUM_BANDS
    peaks = [0.0] * NUM_BANDS
    t0 = time.monotonic()
    print(f"audio-visualizer demo -> {_base(host)}  (Ctrl-C to stop)")
    try:
        while True:
            t = time.monotonic() - t0
            style, theme = DEMO_COMBOS[int(t / DEMO_SECONDS_PER_COMBO) % len(DEMO_COMBOS)]
            new_h = _synth_heights(t)
            smooth = smooth_heights(new_h, smooth)
            for i in range(NUM_BANDS):
                peaks[i] = max(float(smooth[i]), peaks[i] - peak_fall)
            elements = build_elements(smooth, peaks, theme, style)
            if elements:
                try:
                    _draw(host, elements)
                except RuntimeError:
                    pass
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            _clear(host)
        except Exception:
            pass
        print("stopped.")


def pick_device():
    """List avfoundation audio devices and pick the built-in microphone:
    prefer a name containing MacBook, else the first non-iPhone device."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation",
                        "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True, timeout=10)
    devices = []
    in_audio = False
    for line in r.stderr.splitlines():
        if "audio devices" in line.lower():
            in_audio = True
            continue
        if in_audio:
            m = __import__("re").search(r"\[(\d+)\] (.+)$", line)
            if m:
                devices.append((m.group(1), m.group(2).strip()))
            elif "]" not in line:
                break
    if not devices:
        return ":0"
    for idx, name in devices:
        if "macbook" in name.lower():
            print(f"microfoon: [{idx}] {name}")
            return f":{idx}"
    for idx, name in devices:
        if "iphone" not in name.lower():
            print(f"microfoon: [{idx}] {name}")
            return f":{idx}"
    return f":{devices[0][0]}"


def main():
    args = parse_args()

    if args.test:
        _test_mode(args.host, args.theme, args.style)
        return

    if args.demo:
        _demo_mode(args.host, args.fps)
        return

    # Check ffmpeg availability
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found. Install it with: brew install ffmpeg")
        sys.exit(1)

    frame_interval = 1.0 / max(1, args.fps)
    peak_fall = max(0.3, PEAK_FALL_PER_SEC / max(1, args.fps))
    running_max = [1.0]
    smooth = [0] * NUM_BANDS
    peaks = [0.0] * NUM_BANDS
    last_draw = 0.0
    last_chunk = None
    ffmpeg_proc = None
    _draw_error_printed = False

    if args.device == "auto":
        try:
            args.device = pick_device()
        except Exception as e:
            print(f"device detection failed ({e}), using :0")
            args.device = ":0"

    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation",
        "-i", args.device,
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]

    print(f"audio-visualizer -> {_base(args.host)}  (Ctrl-C to stop)")

    try:
        ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        while True:
            # Drain the pipe: read as many chunks as available, keep the latest
            chunk_bytes = ffmpeg_proc.stdout.read(CHUNK_SAMPLES * 2)
            if chunk_bytes and len(chunk_bytes) == CHUNK_SAMPLES * 2:
                last_chunk = chunk_bytes
                # heartbeat: print the input level every ~2s so silence
                # (e.g. denied mic permission) is visible instead of a mystery
                _hb_count = globals().setdefault("_hb", [0])
                _hb_count[0] += 1
                if _hb_count[0] % 22 == 0:
                    samples = struct.unpack(f"<{CHUNK_SAMPLES}h", chunk_bytes)
                    rms = (sum(v * v for v in samples) / CHUNK_SAMPLES) ** 0.5
                    note = "" if rms >= 20 else "  (stilte: check microfoontoestemming)"
                    print(f"audio niveau: {rms:7.1f}{note}")
            elif ffmpeg_proc.poll() is not None:
                # ffmpeg died (broken install, mic permission, wrong device):
                # surface its stderr instead of sitting silent forever
                err = ffmpeg_proc.stderr.read().decode("utf-8", "ignore").strip()
                print("ffmpeg stopped unexpectedly:")
                for line in err.splitlines()[-5:]:
                    print(f"  {line}")
                sys.exit(1)

            now = time.monotonic()
            if now - last_draw >= frame_interval:
                last_draw = now
                if last_chunk is not None:
                    samples = list(struct.unpack(f"<{CHUNK_SAMPLES}h", last_chunk))
                    mags = compute_band_magnitudes(samples)
                    new_h = magnitudes_to_heights(mags, running_max)
                    smooth = smooth_heights(new_h, smooth)
                    for i in range(NUM_BANDS):
                        peaks[i] = max(float(smooth[i]), peaks[i] - peak_fall)
                    elements = build_elements(smooth, peaks, args.theme, args.style)
                    if elements:
                        try:
                            status, _ = _draw(args.host, elements)
                            if status == 409:
                                if not _draw_error_printed:
                                    print("display busy (409), retrying")
                                    _draw_error_printed = True
                            else:
                                _draw_error_printed = False
                        except RuntimeError as e:
                            if not _draw_error_printed:
                                print(f"draw error: {e}")
                                _draw_error_printed = True

    except KeyboardInterrupt:
        pass
    finally:
        if ffmpeg_proc is not None:
            ffmpeg_proc.terminate()
            try:
                ffmpeg_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
        try:
            _clear(args.host)
        except Exception:
            pass
        print("stopped.")


if __name__ == "__main__":
    main()
