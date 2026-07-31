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

Each frame is rendered to a single 72x16 image and pushed as one image element.
Individual rectangles cost ~3.6 ms each on the device, so a busy style (segments
can reach 100+ rects) either crawls or trips the element cap; one full-frame
image is a flat ~50 ms regardless, so every style runs smoothly.

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
import zlib

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

W, H = 72, 16
DISPLAY_H = H

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
PEAK_COLOR = (255, 255, 255)
# Peak caps fall this many pixels per second, then are scaled to the frame rate.
PEAK_FALL_PER_SEC = 9.0


# ---------------------------------------------------------------------------
# HTTP helpers — push one full-frame image per frame
# ---------------------------------------------------------------------------

def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _png(pixels):
    """72x16 flat list of (r, g, b) -> minimal RGBA PNG bytes (stdlib only)."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        base = y * W
        for x in range(W):
            r, g, b = pixels[base + x]
            raw += bytes((r, g, b, 255))

    def _chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


# The device briefly locks an asset while a draw reads it; re-uploading the same
# name too soon returns HTTP 508, so rotate through a few filenames.
_RING = 4
_frame_no = 0


def _post(host, path, data, content_type):
    req = urllib.request.Request(_base(host) + path, data=data, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode()


def show(host, pixels):
    """Upload the frame PNG and draw it as one image. Returns the draw status."""
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    try:
        _post(host, "/api/assets/upload?application_name=%s&file=%s" % (APP, fn),
              _png(pixels), "application/octet-stream")
        body = {"application_name": APP,
                "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}]}
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise RuntimeError(f"push failed: {e}") from e


def _clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
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
    """Single (r, g, b) in 0..1 for a band at absolute height fraction t in 0..1."""
    t = max(0.0, min(1.0, t))
    if theme == "rainbow":
        return _hsv(band_i / max(1, num_bands), 1.0, 0.35 + 0.65 * t)
    return _sample(THEMES.get(theme, THEMES["classic"]), t)


def _c(band_i, t, theme, n):
    """Theme colour as an (r, g, b) 0..255 tuple."""
    r, g, b = _theme_rgb(band_i, t, theme, n)
    return (max(0, min(255, int(r * 255 + 0.5))),
            max(0, min(255, int(g * 255 + 0.5))),
            max(0, min(255, int(b * 255 + 0.5))))


# ---------------------------------------------------------------------------
# Pixel buffer + per-style rasterisers (write pixels, not rect elements)
# ---------------------------------------------------------------------------

def _blank():
    return [(0, 0, 0)] * (W * H)


def _px(buf, x, y, rgb):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = rgb


def _col2(buf, x, y, rgb):
    _px(buf, x, y, rgb)
    _px(buf, x + 1, y, rgb)


def _raster_bars(buf, heights, peaks, theme):
    """Vertical gradient bars anchored at the bottom, floating peak-hold caps."""
    n = len(heights)
    for i, h in enumerate(heights):
        x = i * 3
        for y in range(H - h, H):
            _col2(buf, x, y, _c(i, (H - 1 - y) / (H - 1), theme, n))
        ph = int(peaks[i])
        if ph > h and ph > 0:
            _col2(buf, x, H - ph, PEAK_COLOR)


def _raster_mirror(buf, heights, peaks, theme):
    """Bars grow symmetrically from the horizontal centre, up and down."""
    n = len(heights)
    mid = H // 2
    for i, h in enumerate(heights):
        x = i * 3
        half = h // 2
        for y in range(mid - half, mid):
            t = (mid - y) / max(1, half) * (h / H)
            _col2(buf, x, y, _c(i, t, theme, n))
        for y in range(mid, mid + half):
            t = (y - mid + 1) / max(1, half) * (h / H)
            _col2(buf, x, y, _c(i, t, theme, n))
        phalf = int(peaks[i]) // 2
        if phalf > half and phalf > 0:
            _col2(buf, x, mid - phalf, PEAK_COLOR)
            _col2(buf, x, mid + phalf - 1, PEAK_COLOR)


# LED-block geometry for the segmented style: 2 px lit block, 1 px dark gap.
SEG_BLOCK, SEG_GAP = 2, 1
SEG_PITCH = SEG_BLOCK + SEG_GAP
SEG_SLOTS = 6


def _seg_block(k):
    """(y, height) of block slot k (0 = bottom), clamped into the display."""
    y = H - SEG_BLOCK - k * SEG_PITCH
    bh = SEG_BLOCK
    if y < 0:
        bh += y
        y = 0
    return y, bh


def _raster_segments(buf, heights, peaks, theme):
    """Discrete stacked LED blocks per band (classic hardware VU meter)."""
    n = len(heights)
    for i, h in enumerate(heights):
        x = i * 3
        lit = int(round(h / H * SEG_SLOTS))
        for k in range(lit):
            y, bh = _seg_block(k)
            if bh <= 0:
                continue
            t = 1.0 - (y + bh / 2.0) / H
            col = _c(i, t, theme, n)
            for yy in range(y, y + bh):
                _col2(buf, x, yy, col)
        peak_slot = int(round(peaks[i] / H * SEG_SLOTS))
        if peak_slot > lit and peak_slot > 0:
            y, bh = _seg_block(peak_slot - 1)
            for yy in range(y, y + bh):
                _col2(buf, x, yy, PEAK_COLOR)


def _raster_dots(buf, heights, peaks, theme):
    """Only a bouncing dot at each band top, with a falling peak-hold dot."""
    n = len(heights)
    for i, h in enumerate(heights):
        x = i * 3
        if h > 0:
            dh = 2
            y0 = min(H - dh, H - h)
            col = _c(i, h / H, theme, n)
            for yy in range(y0, y0 + dh):
                _col2(buf, x, yy, col)
        ph = int(peaks[i])
        if ph > h and ph > 0:
            _col2(buf, x, H - ph, PEAK_COLOR)


def _raster_wave(buf, heights, peaks, theme):
    """A continuous contour line connecting the band tops (oscilloscope-style)."""
    n = len(heights)
    tops = [H - max(1, h) for h in heights]
    for i, h in enumerate(heights):
        x = i * 3
        col = _c(i, max(1, h) / H, theme, n)
        _col2(buf, x, tops[i], col)
        if i < n - 1:
            lo, hi = min(tops[i], tops[i + 1]), max(tops[i], tops[i + 1])
            for yy in range(lo, hi + 1):
                _px(buf, x + 2, yy, col)


_RASTERISERS = {
    "bars": _raster_bars,
    "mirror": _raster_mirror,
    "segments": _raster_segments,
    "dots": _raster_dots,
    "wave": _raster_wave,
}


def build_pixels(heights, peaks, theme, style="bars"):
    """Rasterise the chosen style into a flat 72x16 pixel buffer."""
    buf = _blank()
    _RASTERISERS.get(style, _raster_bars)(buf, heights, peaks, theme)
    return buf


def magnitudes_to_heights(magnitudes, running_max):
    """Convert raw magnitudes to pixel heights 0..16, updating running_max in place."""
    heights = []
    for mag in magnitudes:
        running_max[0] = max(running_max[0] * 0.995, 1.0)
        if mag > running_max[0]:
            running_max[0] = mag
        h = int(16.0 * math.log1p(mag) / math.log1p(running_max[0]))
        heights.append(max(0, min(16, h)))
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
    p.add_argument("--fps", type=int, default=15, help="draw updates per second (default: 15)")
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
    peaks = [min(H, h + 3) for h in heights]
    status = show(host, build_pixels(heights, peaks, theme, style))
    print(f"test: drew 1 frame ({style}/{theme}) status {status}")
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
    """Music-like spectrum for band 0..23 at time t seconds (no audio needed)."""
    phase = (t * _DEMO_BEAT_HZ) % 1.0
    kick = math.exp(-6.0 * phase)
    off = (phase + 0.5) % 1.0
    snare = math.exp(-9.0 * off)
    swell = 0.85 + 0.15 * math.sin(t * 0.5)
    mel_center = 6.0 + 8.0 * (0.5 + 0.5 * math.sin(t * 0.8))

    heights = []
    for i in range(NUM_BANDS):
        if i <= 5:
            wob = 0.5 + 0.5 * math.sin(t * 2.0 + i * 0.7)
            val = (0.55 + 0.45 * wob) * (0.30 + 0.80 * kick)
        elif i <= 14:
            mel = math.exp(-((i - mel_center) ** 2) / 6.0)
            groove = 0.5 + 0.5 * math.sin(t * 3.3 + i * 0.5)
            val = 0.20 + 0.75 * mel * (0.4 + 0.6 * groove) + 0.20 * snare
        else:
            shimmer = 0.5 + 0.5 * math.sin(t * 11.0 + i * 1.7)
            hat = math.exp(-12.0 * off)
            val = (0.10 + 0.35 * shimmer) * (0.5 + 0.9 * hat)
        val *= swell
        heights.append(max(0, min(H, int(round(val * H)))))
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
            try:
                show(host, build_pixels(smooth, peaks, theme, style))
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
            chunk_bytes = ffmpeg_proc.stdout.read(CHUNK_SAMPLES * 2)
            if chunk_bytes and len(chunk_bytes) == CHUNK_SAMPLES * 2:
                last_chunk = chunk_bytes
                _hb_count = globals().setdefault("_hb", [0])
                _hb_count[0] += 1
                if _hb_count[0] % 22 == 0:
                    samples = struct.unpack(f"<{CHUNK_SAMPLES}h", chunk_bytes)
                    rms = (sum(v * v for v in samples) / CHUNK_SAMPLES) ** 0.5
                    note = "" if rms >= 20 else "  (stilte: check microfoontoestemming)"
                    print(f"audio niveau: {rms:7.1f}{note}")
            elif ffmpeg_proc.poll() is not None:
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
                    try:
                        status = show(args.host, build_pixels(smooth, peaks, args.theme, args.style))
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
