#!/usr/bin/env python3
"""Queue management system for BUSY Bar.

Large, centered "NOW SERVING" header plus a ticket number from 1 to 99.
START advances to the next ticket and plays the characteristic queue-system tone sequence.
The rotary encoder corrects the currently served number manually without sound.

    python3 app.py
    python3 app.py --host 127.0.0.1:8080
    python3 app.py --start-number 42

Requires:
    pip install websockets
"""
from __future__ import annotations

import argparse
import asyncio
import io
import math
import queue
import struct
import threading
import time
import json
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Iterable


APP = "queue-mgnt-system"
DEFAULT_HOST = "10.0.4.20"
W, H = 72, 16
MIN_NUMBER = 1
MAX_NUMBER = 99
FRAME_FILE = "queue-frame.png"
CHIME_FILE = "queue-chime.wav"

BG = (0, 0, 0)
TITLE_COLOR = (255, 255, 255)
NUMBER_COLOR = (255, 210, 40)

# 3x5 font, deliberately compact so "NOW SERVING" is prominent but leaves
# enough vertical room for a very large ticket number below it.
FONT_3X5 = {
    " ": ["000", "000", "000", "000", "000"],
    "A": ["010", "101", "111", "101", "101"],
    "E": ["111", "100", "110", "100", "111"],
    "G": ["011", "100", "101", "101", "011"],
    "I": ["111", "010", "010", "010", "111"],
    "N": ["101", "111", "111", "111", "101"],
    "O": ["010", "101", "101", "101", "010"],
    "R": ["110", "101", "110", "101", "101"],
    "S": ["011", "100", "010", "001", "110"],
    "V": ["101", "101", "101", "101", "010"],
    "W": ["101", "101", "111", "111", "101"],
    "C": ["011", "100", "100", "100", "011"],
    "D": ["110", "101", "101", "101", "110"],
    "J": ["001", "001", "001", "101", "010"],
    "L": ["100", "100", "100", "100", "111"],
    "M": ["101", "111", "111", "101", "101"],
    "T": ["111", "010", "010", "010", "010"],
    "U": ["101", "101", "101", "101", "111"],
    "Y": ["101", "101", "010", "010", "010"],
}

# 6x9 digits. Each digit is intentionally wide and heavy for readability on
# the 72x16 matrix. Two digits including the gap use only 14 px, so the number
# remains visually dominant while perfectly centered.
DIGITS_6X9 = {
    "0": [
        "011110", "110011", "110111", "111011", "111011",
        "110111", "110011", "110011", "011110",
    ],
    "1": [
        "001100", "011100", "111100", "001100", "001100",
        "001100", "001100", "001100", "111111",
    ],
    "2": [
        "011110", "110011", "000011", "000110", "001100",
        "011000", "110000", "110000", "111111",
    ],
    "3": [
        "111110", "000011", "000011", "001110", "000011",
        "000011", "000011", "110011", "011110",
    ],
    "4": [
        "000110", "001110", "011110", "110110", "110110",
        "111111", "000110", "000110", "000110",
    ],
    "5": [
        "111111", "110000", "110000", "111110", "000011",
        "000011", "000011", "110011", "011110",
    ],
    "6": [
        "001110", "011000", "110000", "111110", "110011",
        "110011", "110011", "110011", "011110",
    ],
    "7": [
        "111111", "000011", "000110", "001100", "001100",
        "011000", "011000", "110000", "110000",
    ],
    "8": [
        "011110", "110011", "110011", "110011", "011110",
        "110011", "110011", "110011", "011110",
    ],
    "9": [
        "011110", "110011", "110011", "110011", "011111",
        "000011", "000011", "000110", "111100",
    ],
}


TITLES = {
    "en": "NOW SERVING",
    "fr": "A VOUS",
    "it": "ORA SERVIAMO",
    "es": "SU TURNO",
    "de": "AN DER REIHE",
    "nl": "AAN DE BEURT",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Queue management system for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST, help="BUSY Bar host ip[:port]")
    p.add_argument("--token", default=None, help="Wi-Fi access PIN, if required")
    p.add_argument("--lang", choices=["en", "fr", "it", "es", "de", "nl"], default="en",
                   help="display language (default: en)")
    p.add_argument(
        "--start-number", type=int, default=1,
        help="initial ticket number, clamped to 1..99 (default: 1)",
    )
    p.add_argument("--no-sound", action="store_true", help="disable queue chime")
    p.add_argument(
        "--invert-dial", action="store_true",
        help="invert the rotary encoder direction",
    )
    p.add_argument(
        "--volume", type=int, default=70, metavar="0-100",
        help="chime volume percentage, 0..100 (default: 70)",
    )
    p.add_argument("--test", action="store_true", help="render a local PNG and exit")
    return p.parse_args()


def _put(buf: list[tuple[int, int, int]], x: int, y: int, rgb: tuple[int, int, int]) -> None:
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = rgb


def _draw_bitmap(
    buf: list[tuple[int, int, int]],
    bitmap: Iterable[str],
    x0: int,
    y0: int,
    rgb: tuple[int, int, int],
) -> None:
    for y, row in enumerate(bitmap):
        for x, bit in enumerate(row):
            if bit == "1":
                _put(buf, x0 + x, y0 + y, rgb)


def _title_width(text: str) -> int:
    # 3 px glyph + 1 px gap, except after final glyph.
    return len(text) * 4 - 1


def render_frame(number: int, lang: str = "en") -> list[tuple[int, int, int]]:
    """Render a full 72x16 frame with guaranteed non-overlapping layout."""
    number = max(MIN_NUMBER, min(MAX_NUMBER, int(number)))
    buf = [BG] * (W * H)

    title = TITLES.get(lang, TITLES["en"])
    title_w = _title_width(title)
    tx = (W - title_w) // 2
    ty = 0
    x = tx
    for ch in title:
        glyph = FONT_3X5[ch]
        _draw_bitmap(buf, glyph, x, ty, TITLE_COLOR)
        x += 4

    # Row 5 is intentionally blank. Digits occupy rows 7..15, leaving row 6
    # blank as well: two full black rows separate the header from the number.
    s = str(number)
    digit_w = 6
    gap = 2
    total_w = len(s) * digit_w + (len(s) - 1) * gap
    nx = (W - total_w) // 2
    ny = 7
    for ch in s:
        _draw_bitmap(buf, DIGITS_6X9[ch], nx, ny, NUMBER_COLOR)
        nx += digit_w + gap

    return buf


def png_bytes(pixels: list[tuple[int, int, int]]) -> bytes:
    """Encode the 72x16 RGB frame as a minimal PNG using only stdlib."""
    import zlib

    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            r, g, b = pixels[y * W + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        import binascii
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", binascii.crc32(c) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


# Pitch/duration sequence measured from the reference queue chime.
#
# Important: the first/last sound has its strongest spectral peak near 2890 Hz,
# but that is the third harmonic. Autocorrelation shows a fundamental around
# 960-963 Hz, which is the perceived pitch. Synthesizing a pure 2890 Hz sine
# therefore sounds much too high. We reproduce those tones as a 963 Hz
# fundamental with strong 3rd and 5th harmonics, closer to the source timbre.
CHIME_SEQUENCE = [
    (963.0, 0.060, "queue"),
    (1225.0, 0.070, "tone"),
    (1300.0, 0.070, "tone"),
    (1455.0, 0.060, "tone"),
    (1650.0, 0.060, "tone"),
    (963.0, 0.205, "queue"),
]


def make_chime_wav(volume: int = 70) -> bytes:
    """Synthesize the measured queue chime.

    The source's first/last note is a ~963 Hz fundamental whose 3rd harmonic
    (~2889 Hz) is substantially stronger than the fundamental. That harmonic
    structure is preserved here so the perceived pitch stays low while the
    characteristic bright queue-system timbre remains.

    ``volume`` is a 0..100 percentage applied during WAV generation.
    """
    sample_rate = 44100
    fade_s = 0.002
    gain = max(0.0, min(1.0, volume / 100.0))
    samples: list[int] = []

    for freq, duration, timbre in CHIME_SEQUENCE:
        count = max(1, round(duration * sample_rate))
        fade_n = max(1, min(count // 2, round(fade_s * sample_rate)))

        for i in range(count):
            t = i / sample_rate
            envelope = 1.0
            if i < fade_n:
                envelope = i / fade_n
            elif i >= count - fade_n:
                envelope = (count - 1 - i) / fade_n

            if timbre == "queue":
                # Approximate the measured spectrum: weak fundamental, very
                # strong 3rd harmonic, and a smaller 5th harmonic. Normalize
                # the mixture to leave headroom and avoid clipping.
                value = (
                    0.20 * math.sin(2.0 * math.pi * freq * t)
                    + 1.00 * math.sin(2.0 * math.pi * (freq * 3.0) * t)
                    + 0.18 * math.sin(2.0 * math.pi * (freq * 5.0) * t)
                ) / 1.38
            else:
                # Middle notes are correctly identified by their fundamentals.
                # A light 3rd harmonic adds some of the buzzy source character
                # without changing the perceived pitch.
                value = (
                    math.sin(2.0 * math.pi * freq * t)
                    + 0.10 * math.sin(2.0 * math.pi * (freq * 3.0) * t)
                ) / 1.10

            value *= gain * envelope
            samples.append(int(max(-1.0, min(1.0, value)) * 32767))

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return out.getvalue()


def _base(host: str) -> str:
    raw = host.rstrip("/")
    return raw if raw.startswith(("http://", "https://")) else "http://" + raw


def _ws_url(host: str, token: str | None = None) -> str:
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


def _api_request(
    host: str,
    method: str,
    path: str,
    data: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(_base(host) + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class BusyIO:
    """Minimal HTTP client for display/assets/audio. No busylib dependency."""

    def __init__(self, host: str, lang: str = "en") -> None:
        self.host = host
        self.lang = lang
        self._frame_no = 0

    def upload(self, filename: str, payload: bytes) -> int:
        q = urllib.parse.urlencode({"application_name": APP, "file": filename})
        status, _ = _api_request(
            self.host,
            "POST",
            "/api/assets/upload?" + q,
            payload,
            "application/octet-stream",
            timeout=8,
        )
        return status

    def draw_number(self, number: int) -> int:
        # Rotate filenames because the device can briefly lock an asset while
        # the display is reading it; re-uploading the same name may return 508.
        filename = f"queue-frame-{self._frame_no % 4}.png"
        self._frame_no += 1
        status = self.upload(filename, png_bytes(render_frame(number, self.lang)))
        if status not in (200, 201, 204):
            return status
        body = json.dumps({
            "application_name": APP,
            "elements": [{"id": "frame", "type": "image", "path": filename, "x": 0, "y": 0}],
        }).encode("utf-8")
        status, _ = _api_request(self.host, "POST", "/api/display/draw", body, "application/json")
        return status

    def clear(self) -> None:
        q = urllib.parse.urlencode({"application_name": APP})
        try:
            _api_request(self.host, "DELETE", "/api/display/draw?" + q)
        except Exception:
            pass

    def delete_assets(self) -> None:
        q = urllib.parse.urlencode({"application_name": APP})
        try:
            _api_request(self.host, "DELETE", "/api/assets/upload?" + q)
        except Exception:
            pass

    def upload_chime(self, volume: int) -> int:
        return self.upload(CHIME_FILE, make_chime_wav(volume))

    def play_chime(self) -> int:
        body = json.dumps({"application_name": APP, "path": CHIME_FILE}).encode("utf-8")
        status, _ = _api_request(self.host, "POST", "/api/audio/play", body, "application/json")
        return status


# Minimal BUSY Bar protobuf decoder, mirrored from the media-player implementation.
def _read_varint(buf: bytes, pos: int):
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


def _iter_proto_fields(buf: bytes):
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


def _zigzag32(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


def _decode_input_event(buf: bytes):
    for field_no, wire, value in _iter_proto_fields(buf):
        if wire != 2:
            continue
        if field_no == 1:  # button_event
            button = 0
            action = 0
            for f, w, v in _iter_proto_fields(value):
                if w == 0 and f == 1:
                    button = int(v)
                elif w == 0 and f == 2:
                    action = int(v)
            return {"button_event": {"button": button, "action": action}}
        if field_no == 3:  # encoder_event
            raw_delta = 0
            for f, w, v in _iter_proto_fields(value):
                if w == 0 and f == 1:
                    raw_delta = int(v)
            return {"encoder_event": {"delta": _zigzag32(raw_delta)}}
    return None


def _decode_state_inputs(frame: bytes):
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


class InputListener:
    """Direct WebSocket listener with automatic connection recovery."""

    def __init__(self, address: str, token: str | None) -> None:
        self._address = address
        self._token = token
        self._queue: queue.Queue[dict] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = True
        self.error: str | None = None

    def start(self) -> None:
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            self.available = False
            self.error = f"websockets not installed: {exc}"
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll(self) -> list[dict]:
        events: list[dict] = []
        try:
            while True:
                events.append(self._queue.get_nowait())
        except queue.Empty:
            return events

    def _run(self) -> None:
        try:
            asyncio.run(self._listen_forever())
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            print(f"controls: listener stopped: {exc}")

    async def _listen_forever(self) -> None:
        import websockets

        url = _ws_url(self._address, self._token)
        backoff = 0.5
        max_backoff = 3.0
        connected_once = False

        while not self._stop.is_set():
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
                        print("controls: BUSY Bar input connected")
                    connected_once = True
                    self.available = True
                    self.error = None
                    backoff = 0.5

                    async for message in ws:
                        if self._stop.is_set():
                            return
                        if isinstance(message, str):
                            continue
                        try:
                            for event in _decode_state_inputs(bytes(message)):
                                self._queue.put(event)
                        except Exception as exc:
                            print(f"controls: ignored malformed status frame: {exc}")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.available = False
                self.error = str(exc)
                print(
                    f"controls: BUSY Bar input disconnected: {exc}; "
                    f"reconnecting in {backoff:g}s"
                )
                deadline = time.monotonic() + backoff
                while not self._stop.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(min(0.25, deadline - time.monotonic()))
                backoff = min(max_backoff, backoff * 2.0)

def run_test(number: int, lang: str) -> None:
    from pathlib import Path
    target = Path("queue-mgnt-system-preview.png")
    target.write_bytes(png_bytes(render_frame(number, lang)))
    print(f"wrote {target} for ticket {number}")


def main() -> None:
    args = parse_args()
    number = max(MIN_NUMBER, min(MAX_NUMBER, args.start_number))
    volume = max(0, min(100, args.volume))
    if volume != args.volume:
        print(f"volume clamped to {volume}%")

    if args.test:
        run_test(number, args.lang)
        return

    controls = InputListener(args.host, args.token)
    controls.start()
    io_client = BusyIO(args.host, args.lang)

    print(f"queue-mgnt-system -> {args.host}  (Ctrl-C to stop)")
    sound_info = "off" if args.no_sound else f"{volume}%"
    dial_info = "inverted" if args.invert_dial else "normal"
    print(f"{TITLES[args.lang]} {number:02d}  |  START = next  |  wheel = correction ({dial_info})  |  volume = {sound_info}")

    try:
        io_client.clear()
        if not args.no_sound:
            status = io_client.upload_chime(volume)
            if status not in (200, 201, 204):
                print(f"warning: chime upload returned HTTP {status}")

        status = io_client.draw_number(number)
        if status not in (200, 201, 204):
            print(f"warning: initial draw returned HTTP {status}")

        while True:
            changed = False
            ring = False

            for event in controls.poll():
                encoder = event.get("encoder_event")
                if encoder:
                    delta = int(encoder.get("delta", 0) or 0)
                    if delta:
                        # Default direction is intentionally the inverse of v4.
                        # --invert-dial flips it back when the physical mounting
                        # or user preference requires the opposite direction.
                        step = -delta if args.invert_dial else delta
                        new_number = max(MIN_NUMBER, min(MAX_NUMBER, number + step))
                        if new_number != number:
                            number = new_number
                            changed = True

                button = event.get("button_event")
                if button:
                    # Firmware enum: START=2, PRESS/default action=0.
                    if button.get("button") == 2 and button.get("action") == 0:
                        number = MIN_NUMBER if number >= MAX_NUMBER else number + 1
                        changed = True
                        ring = True

            if changed:
                try:
                    status = io_client.draw_number(number)
                    if status in (200, 201, 204):
                        print(f"{TITLES[args.lang]} {number:02d}")
                        if ring and not args.no_sound:
                            audio_status = io_client.play_chime()
                            if audio_status not in (200, 201, 204):
                                print(f"  (chime skipped: HTTP {audio_status})")
                    elif status == 409:
                        print("display busy (409); keeping current ticket state")
                    elif status == 508:
                        print("display asset busy (508); next update will retry")
                    else:
                        print(f"display update returned HTTP {status}")
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    print(f"display update failed: {exc}")

            time.sleep(0.03)

    except KeyboardInterrupt:
        pass
    finally:
        controls.stop()
        io_client.clear()
        io_client.delete_assets()
        print("\nstopped.")

if __name__ == "__main__":
    main()
