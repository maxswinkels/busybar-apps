#!/usr/bin/env python3
"""Counter: a persistent tally on the bar, in any format you can spell.

    python app.py                                  # 000, START counts up
    python app.py --host 127.0.0.1:8080            # emulator or a Wi-Fi bar
    python app.py --title "VISITORS" --format 9999
    python app.py --format "#HH"                   # #00 .. #FF
    python app.py --format "A-99" --start A-42     # A-42 .. A-99 -> B-00
    python app.py --format "999?"                  # 0, 1, 2 ... 999, unpadded
    python app.py --format "A-99+"                 # A-01 .. A-99 -> B-01
    python app.py --id lab --step 5                # its own saved profile

The format string is a mask that looks like the thing it counts: every mask
character is either a counting position or a literal that just sits there.

    9  decimal digit   0-9        A  letter   A-Z
    H  hex digit       0-9A-F     \\  next character is a literal

So `99:99` counts 00:00 to 99:99, `HHHH` counts 0000 to FFFF, and `AA-999`
counts AA-000 all the way to ZZ-999 before wrapping back to AA-000. Positions
carry into each other like an odometer, whatever mix of bases they are.

Neighbouring positions in the same alphabet are one number, and two modifiers
written after that number change how it is counted:

    ?  drop the leading zeros, so `999?` counts 0, 1, 2 ... 999
    +  skip zero and count from 1, so `A-99+` turns over A-99 -> B-01

They combine: `A-99?+` counts A-1 .. A-99 and then B-1. Write `\\?` or `\\+` for
either of them as a literal.

START on the bar advances by --step; the wheel corrects by one. With --id the
count is kept in counter-state.json next to this file and picked up again on the
next run -- as a plain number, so the same profile can be reprinted in a
different --format tomorrow. Passing --start ignores whatever was saved.
"""
import argparse
import json
import os
import queue
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

APP = "counter"

W, H = 72, 16

# Counts live next to the app, so a copy of this folder carries them with it and
# nothing is left behind elsewhere on the machine. The file is only touched when
# --id names a profile to keep.
STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "counter-state.json")

# --- BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs) ----------


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="mask characters: 9 = 0-9, H = 0-9A-F, A = A-Z, \\x = literal x, "
               "anything else = literal. After a number: ? = no leading zeros, "
               "+ = count from 1 instead of 0",
    )
    p.add_argument("--host", default="10.0.4.20",
                   help="bar address; 10.0.4.20 is USB, 127.0.0.1:8080 the emulator")
    p.add_argument("--title", default=None,
                   help="text above the counter; with none set the counter is "
                        "centred on the whole display")
    p.add_argument("--format", dest="mask", default="999",
                   help="counter mask, e.g. 999, '999?', 'A-99+', '#HH' "
                        "(default 999)")
    p.add_argument("--start", default=None,
                   help="start here instead of at the saved value, written in "
                        "--format (e.g. A-42); a bare number is taken as a raw count")
    p.add_argument("--step", type=int, default=1,
                   help="how far START advances; negative counts down (default 1)")
    p.add_argument("--id", dest="profile", default=None,
                   help="remember the count under this name in counter-state.json, "
                        "and pick it up again on the next run; without it nothing "
                        "is saved")
    p.add_argument("--color", default="#FFD228FF",
                   help="counter colour as #RGB, #RRGGBB or #RRGGBBAA (default #FFD228FF)")
    p.add_argument("--title-color", default="#C4C4C4FF",
                   help="title colour as #RGB, #RRGGBB or #RRGGBBAA (default #C4C4C4FF)")
    p.add_argument("--invert-dial", action="store_true",
                   help="invert the wheel direction")
    p.add_argument("--test", action="store_true", help="draw a single frame and exit")
    return p


ARGS = build_parser().parse_args()
BASE = "http://" + ARGS.host.replace("http://", "").rstrip("/")


def draw(elements, **extra):
    body = {"application_name": APP, "elements": elements, **extra}
    req = urllib.request.Request(BASE + "/api/display/draw",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5):
        pass


def clear():
    req = urllib.request.Request(
        BASE + "/api/display/draw?application_name=" + urllib.parse.quote(APP),
        method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, OSError):
        pass


def text(eid, txt, x=0, y=0, font="normal", color="#FFFFFFFF", **kw):
    # Every element needs an id, and colors are #RRGGBBAA (API 25.0.0+).
    return {"id": eid, "type": "text", "text": str(txt), "x": x, "y": y,
            "font": font, "color": color, **kw}


def blank(eid):
    """A fully transparent element.

    The firmware keeps every id it has been sent until the app is cleared, so an
    element cannot be un-drawn by leaving it out of the next frame. Both ids go
    out on every frame, with the unused one parked as invisible."""
    return text(eid, " ", x=0, y=0, font=TITLE_FONT, color="#00000000")


def parse_color(value):
    """#RGB / #RRGGBB / #RRGGBBAA -> the #RRGGBBAA the firmware wants."""
    raw = value.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", raw):
        raw = "".join(ch * 2 for ch in raw)
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        raw += "FF"
    if not re.fullmatch(r"[0-9a-fA-F]{8}", raw):
        raise argparse.ArgumentTypeError(
            "colour must be #RGB, #RRGGBB or #RRGGBBAA, got %r" % value)
    return "#" + raw.upper()


# --- text metrics ----------------------------------------------------------
# Advance widths for printable ASCII, taken from the device font atlas, encoded
# as chr(32 + advance). Knowing how wide a string renders is what tells the app
# whether a title has to be handed to the firmware's scroller.

_ADVANCES = {
    "small": "\"\"$&$%%\"##$$\"#\"#$#$$$$$$$$\"#$$$$%%%%%%%%%\"$%$&%%%%%%$%$&$$$###$$$$$$$$#$$\"#$\"&$$$$#$#$$&$$$$\"$%",
    "extra_large": "$#&*)+*#%%''#$#&(%((((((((##%'%',((((''((%'(')(((((('())(('$&$(%'((((''((%'(')(((((('())(('&#&(",
}

# The two faces this app uses, and nothing else: the value is always as big as
# the display can print, and the title is the small line above it.
TITLE_FONT = "small"
VALUE_FONT = "extra_large"

# A text element is anchored by its font box, not by its ink, and the box has
# blank rows on top. INK_TOP is how far below y the first lit row lands, so
# `y = wanted_row - INK_TOP[font]` puts ink exactly where intended. INK_CAPS is
# the lit height of capitals and digits.
INK_TOP = {"small": 2, "extra_large": 2}
INK_CAPS = {"small": 5, "extra_large": 10}


def text_width(txt, font):
    table = _ADVANCES[font]
    total = 0
    for ch in txt:
        i = ord(ch) - 32
        total += ord(table[i]) - 32 if 0 <= i < len(table) else 4
    return total


# --- the counter format ----------------------------------------------------
# A mask is a run of groups and literals. A group is one number -- neighbouring
# positions in the same alphabet, counted together -- and a literal is printed
# as-is and never changes. Turning the whole counter into a single integer is
# what makes carrying, wrapping, --step and the wheel work identically no matter
# which bases are mixed together.

# Only capitals: extra_large, the face the value is drawn in, has no lowercase.
DIGITS_DEC = "0123456789"
DIGITS_HEX = "0123456789ABCDEF"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

ALPHABETS = {
    "9": DIGITS_DEC,
    "H": DIGITS_HEX,
    "A": LETTERS,
}

# Modifiers, written straight after the group they change.
PAD_OFF = "?"          # 999? counts 0, 1, ... 999 instead of 000, 001, ... 999
SKIP_ZERO = "+"        # 99+ counts 01 .. 99, so A-99+ wraps A-99 -> B-01
MODIFIERS = PAD_OFF + SKIP_ZERO


class Group:
    """One number in the mask: a run of positions sharing an alphabet.

    `padded` prints every position, so the width never changes; without it the
    leading zeros are dropped and the value gets shorter near the bottom of its
    range. `one_based` takes the all-zero value out of the count, which is what
    turns A-99+ over from A-99 into B-01 rather than B-00."""

    def __init__(self, alphabet, mask_char):
        self.alphabet = alphabet
        self.mask_char = mask_char
        self.width = 1
        self.padded = True
        self.one_based = False

    @property
    def capacity(self):
        return len(self.alphabet) ** self.width

    @property
    def radix(self):
        """How many distinct values this group takes."""
        return self.capacity - 1 if self.one_based else self.capacity

    def render(self, value):
        number = value + 1 if self.one_based else value
        base = len(self.alphabet)
        digits = []
        for _ in range(self.width):
            number, digit = divmod(number, base)
            digits.append(self.alphabet[digit])
        digits.reverse()
        out = "".join(digits)
        if not self.padded:
            out = out.lstrip(self.alphabet[0]) or self.alphabet[0]
        return out

    def pattern(self):
        """This group as a regex, for reading a written value back."""
        count = "{%d}" % self.width if self.padded else "{1,%d}" % self.width
        return "([%s]%s)" % (re.escape(self.alphabet), count)

    def parse(self, text):
        base = len(self.alphabet)
        number = 0
        for ch in text:
            digit = self.alphabet.find(ch.upper())
            if digit < 0:
                raise ValueError("%r is not a %s position" % (ch, self.mask_char))
            number = number * base + digit
        if self.one_based:
            if not 1 <= number < self.capacity:
                raise ValueError("%r is outside 1..%d" % (text, self.capacity - 1))
            return number - 1
        return number


class Format:
    """A parsed counter mask, plus the count <-> text conversions."""

    def __init__(self, mask):
        self.mask = mask
        self.items = []          # Group objects and literal strings, in order
        escaped = False
        open_group = None        # the group further positions would extend
        last_group = None        # the group a modifier would apply to
        for ch in mask:
            if escaped:
                self.items.append(ch)
                open_group = last_group = None
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch in ALPHABETS:
                if open_group is not None and open_group.mask_char == ch:
                    open_group.width += 1
                else:
                    open_group = last_group = Group(ALPHABETS[ch], ch)
                    self.items.append(open_group)
            elif ch in MODIFIERS:
                if last_group is None:
                    raise ValueError(
                        "%r in mask %r has no group in front of it" % (ch, mask))
                if ch == PAD_OFF:
                    last_group.padded = False
                else:
                    last_group.one_based = True
                open_group = None            # a further position starts a new group
            else:
                self.items.append(ch)
                open_group = last_group = None
        if escaped:
            raise ValueError("mask %r ends with a dangling backslash" % mask)
        if not self.items:
            raise ValueError("empty counter format")
        for item in self.items:
            if isinstance(item, str) and not 0x20 <= ord(item) <= 0x7E:
                # Bitmap fonts are ASCII only; a stray character would come back
                # as a 400 from the bar rather than as anything visible.
                raise ValueError("mask %r has a non-ASCII literal %r" % (mask, item))
        self.groups = [item for item in self.items if isinstance(item, Group)]
        self.total = 1
        for group in self.groups:
            self.total *= group.radix

    @property
    def counting(self):
        return bool(self.groups)

    def render(self, count):
        """The value at `count`, as it appears on the display."""
        values = []
        remaining = count % self.total
        for group in reversed(self.groups):
            remaining, value = divmod(remaining, group.radix)
            values.append(value)
        values.reverse()
        out, i = [], 0
        for item in self.items:
            if isinstance(item, Group):
                out.append(item.render(values[i]))
                i += 1
            else:
                out.append(item)
        return "".join(out)

    def parse(self, text):
        """A written value -> its count. Case-insensitive on hex and letters."""
        pattern = "".join(item.pattern() if isinstance(item, Group) else re.escape(item)
                          for item in self.items)
        match = re.fullmatch(pattern, text.strip(), re.IGNORECASE)
        if not match:
            raise ValueError("%r does not fit the format %r" % (text, self.mask))
        count = 0
        for group, part in zip(self.groups, match.groups()):
            count = count * group.radix + group.parse(part)
        return count


def widest_value(fmt):
    """The widest string this format can ever produce, for the fit warning."""
    out = []
    for item in fmt.items:
        if isinstance(item, Group):
            widest = max(item.alphabet, key=lambda ch: text_width(ch, VALUE_FONT))
            out.append(widest * item.width)
        else:
            out.append(item)
    return "".join(out)


def parse_start(fmt, spec):
    """--start, which is either a value written in the format or a raw count."""
    if spec is None:
        return 0
    spec = spec.strip()
    try:
        return fmt.parse(spec) % fmt.total
    except ValueError:
        pass
    if re.fullmatch(r"-?\d+", spec):
        return int(spec) % fmt.total
    raise SystemExit("counter: cannot read --start %r for format %r" % (spec, fmt.mask))


# --- saved counts ----------------------------------------------------------
# One number per profile and nothing else. A count is just how many steps in the
# profile is, so the same file survives a change of --format: run the same --id
# with a different mask and the count is simply reprinted in the new one.


def load_counts():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            counts = json.load(fh)
        return counts if isinstance(counts, dict) else {}
    except (OSError, ValueError):
        return {}


def load_count(profile, fmt, fallback):
    count = load_counts().get(profile)
    return count % fmt.total if isinstance(count, int) else fallback


def save_count(profile, count):
    counts = load_counts()
    counts[profile] = count
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(counts, fh, indent=1, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError as exc:
        print("counter: cannot write %s (%s); the count is not being saved"
              % (STATE_PATH, exc))


# --- frame -----------------------------------------------------------------


def title_element(title, title_color):
    """The title line, drawn once and then left alone.

    The firmware restarts a scrolling element from the beginning every time it
    is sent, so a title too long to fit would never get past its first few
    characters if it went out alongside each new value. It never changes, so it
    never needs to be sent twice."""
    if not title:
        return blank("title")
    y = -INK_TOP[TITLE_FONT]                            # lit rows start at row 0
    if text_width(title, TITLE_FONT) > W:
        # Too long to fit: hand it to the firmware's scroller, not a knife.
        return text("title", title, x=0, y=y, font=TITLE_FONT, color=title_color,
                    align="top_left", width=W, scroll_rate=900,
                    scroll_start_delay=1500, scroll_repeat_delay=2200)
    return text("title", title, x=W // 2, y=y, font=TITLE_FONT, color=title_color,
                align="top_mid")


def value_element(value, has_title, color):
    """The value in extra_large, centred on the rows it has been left.

    With a title that is rows 6..15, which is exactly the ten rows the face
    needs; with none it is all sixteen and the value sits in the middle."""
    first_row = 6 if has_title else 0
    rows = H - first_row
    top = first_row + max(0, (rows - INK_CAPS[VALUE_FONT]) // 2)
    return text("value", value, x=W // 2, y=top - INK_TOP[VALUE_FONT],
                font=VALUE_FONT, color=color, align="top_mid")


# --- buttons and wheel over the status WebSocket ---------------------------
# The bar streams input on /api/status/ws as protobuf. Decoding the fields this
# app needs by hand keeps it stdlib-only:
#   State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1
#                                           -> InputEvent.encoder_event=3
# Button START is enum 2; action PRESS is 0 and RELEASE is 1. The encoder's
# delta is a zigzag-encoded signed varint.


def _read_varint(buf, pos):
    value, shift = 0, 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


def _iter_fields(buf):
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field, wire, value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            end = pos + length
            if end > len(buf):
                return
            yield field, wire, buf[pos:end]
            pos = end
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > len(buf):
                return
            yield field, wire, buf[pos:pos + width]
            pos += width
        else:
            return


def _zigzag32(value):
    return (value >> 1) ^ -(value & 1)


def input_events(frame):
    """Every ('start', 1) and ('wheel', delta) carried by a status frame."""
    events = []
    for field, wire, update in _iter_fields(frame):
        if field != 2 or wire != 2:                     # State.updates
            continue
        for ufield, uwire, payload in _iter_fields(update):
            if ufield != 11 or uwire != 2:              # StateUpdate.input
                continue
            for efield, ewire, event in _iter_fields(payload):
                if ewire != 2:
                    continue
                if efield == 1:                         # button_event
                    button, action = 0, 0
                    for bfield, bwire, value in _iter_fields(event):
                        if bwire:
                            continue
                        if bfield == 1:
                            button = int(value)
                        elif bfield == 2:
                            action = int(value)
                    if button == 2 and action == 0:
                        events.append(("start", 1))
                elif efield == 3:                       # encoder_event
                    raw = 0
                    for dfield, dwire, value in _iter_fields(event):
                        if dwire == 0 and dfield == 1:
                            raw = int(value)
                    delta = _zigzag32(raw)
                    if delta:
                        events.append(("wheel", delta))
    return events


class InputListener(threading.Thread):
    """A minimal RFC 6455 client for /api/status/ws, in a daemon thread.

    This is the only way the counter moves, but a bar that is not reachable is
    still not a reason to die: every failure is swallowed after one line of
    explanation and retried with a backoff, and the display keeps showing the
    value it is on."""

    def __init__(self, host):
        super().__init__(daemon=True)
        self.host = host
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._complained = False
        self._announced = False

    def stop(self):
        self._stop.set()

    def poll(self):
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:                     # noqa: BLE001 - see docstring
                if not self._complained:
                    print("counter: no input stream (%s); retrying in the "
                          "background" % exc)
                    self._complained = True
                backoff = min(30.0, backoff * 2)
            deadline = time.monotonic() + backoff
            while not self._stop.is_set() and time.monotonic() < deadline:
                time.sleep(0.2)

    def _session(self):
        host, _, port = self.host.partition(":")
        sock = socket.create_connection((host, int(port or 80)), timeout=8)
        try:
            sock.settimeout(60)
            key = b64encode(os.urandom(16)).decode()
            sock.sendall((
                "GET /api/status/ws HTTP/1.1\r\n"
                "Host: %s\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n" % (self.host, key)).encode())
            stream = sock.makefile("rb")
            status = stream.readline()
            if b"101" not in status:
                raise OSError("handshake refused: %s" % status.decode(errors="replace").strip())
            while stream.readline() not in (b"\r\n", b"\n", b""):
                pass
            self._send(sock, 0x1, json.dumps({"enable": True}).encode())
            if not self._announced:
                print("counter: START button and wheel connected")
                self._announced = True

            payload = b""
            while not self._stop.is_set():
                fin, opcode, chunk = self._read_frame(stream)
                if opcode == 0x8:                        # close
                    return
                if opcode == 0x9:                        # ping
                    self._send(sock, 0xA, chunk)
                    continue
                if opcode == 0xA:
                    continue
                payload = chunk if opcode else payload + chunk
                if not fin:
                    continue
                if opcode == 0x2:
                    try:
                        for event in input_events(payload):
                            self.events.put(event)
                    except ValueError:
                        pass                             # a malformed frame costs one event
                payload = b""
        finally:
            sock.close()

    @staticmethod
    def _read_frame(stream):
        head = stream.read(2)
        if len(head) < 2:
            raise OSError("stream closed")
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, length = head[1] & 0x80, head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", stream.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", stream.read(8))[0]
        mask = stream.read(4) if masked else b""
        body = stream.read(length) if length else b""
        if len(body) < length:
            raise OSError("stream closed mid-frame")
        if mask:
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        return fin, opcode, body

    @staticmethod
    def _send(sock, opcode, data):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        header += mask
        header += bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        sock.sendall(bytes(header))


# --- app -------------------------------------------------------------------

try:
    FORMAT = Format(ARGS.mask)
except ValueError as exc:
    raise SystemExit("counter: %s" % exc)

try:
    COLOR = parse_color(ARGS.color)
    TITLE_COLOR = parse_color(ARGS.title_color)
except argparse.ArgumentTypeError as exc:
    raise SystemExit("counter: %s" % exc)

# Bitmap fonts are ASCII only, so anything outside that range becomes a dot
# rather than a 400 from the bar.
TITLE = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "."
                for ch in (ARGS.title or "").upper())

START_INDEX = parse_start(FORMAT, ARGS.start)
SAVING = ARGS.profile is not None and not ARGS.test


def main():
    # An explicit --start is the whole point of passing it, so it wins over
    # whatever the profile last counted to.
    index = START_INDEX
    if SAVING and ARGS.start is None:
        index = load_count(ARGS.profile, FORMAT, START_INDEX)

    listener = None
    if not ARGS.test:
        listener = InputListener(ARGS.host.replace("http://", "").rstrip("/"))
        listener.start()

    print("counter: %s%s  format %s, step %+d%s  (%s)" % (
        TITLE + " " if TITLE else "", FORMAT.render(index), FORMAT.mask,
        ARGS.step, ", profile %r" % ARGS.profile if SAVING else "", BASE))
    if not FORMAT.counting:
        print("counter: %r has no counting positions; it will not move" % FORMAT.mask)
    widest = widest_value(FORMAT)
    if text_width(widest, VALUE_FONT) > W:
        print("counter: %r can reach %r, which is wider than the display; use "
              "fewer positions" % (FORMAT.mask, widest))
    print("START = %+d  |  wheel = correct by one" % ARGS.step)

    dirty = True
    title_sent = False
    saved = None            # so the count on screen is written out even if the
    next_refresh = 0.0      # run never advances past --start
    try:
        while True:
            now = time.monotonic()

            for kind, delta in (listener.poll() if listener else []):
                if kind == "start":
                    index += ARGS.step
                else:
                    # The wheel is a correction, so it moves one position at a
                    # time regardless of how big --step is.
                    index += -delta if ARGS.invert_dial else delta
                dirty = True

            index %= FORMAT.total
            value = FORMAT.render(index)

            # A redraw every few seconds costs nothing and takes the display
            # back after a higher-priority app has been over the top of it. Only
            # the value goes out: the firmware keeps every id it has been sent,
            # so the title stays where it was put, and a scrolling one is left
            # to finish its run.
            if dirty or now >= next_refresh:
                elements = [value_element(value, bool(TITLE), COLOR)]
                if not title_sent:
                    elements.insert(0, title_element(TITLE, TITLE_COLOR))
                try:
                    draw(elements)
                    title_sent = True
                    next_refresh = now + 5.0
                    dirty = False
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:   # 409 = a higher-priority app owns the display
                        raise
                    next_refresh = now + 1.0
                except (urllib.error.URLError, OSError) as exc:
                    print("counter: %s" % exc)
                    next_refresh = now + 1.0

            if SAVING and index != saved:
                save_count(ARGS.profile, index)
                saved = index

            if ARGS.test:
                return
            time.sleep(0.03)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if listener:
            listener.stop()
        if not ARGS.test:
            clear()


if __name__ == "__main__":
    main()
