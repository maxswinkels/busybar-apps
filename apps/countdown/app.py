#!/usr/bin/env python3
"""Countdown: an animated countdown to any moment, with warning and danger
thresholds, that shows less detail the further away the deadline is.

    python app.py                                       # counts down to the next New Year
    python app.py --host 127.0.0.1:8080                 # emulator or a Wi-Fi bar
    python app.py --label "LAUNCH" --datetime 2026-12-31T23:59
    python app.py --in 90s                              # relative target, handy for testing
    python app.py --warning 1w3d --danger 1d            # amber under 10 days, red under 1 day

The display gets more precise as the deadline approaches: months and days far
out, "in X days" within a month, days and hours within a week, hours and
minutes within a day, and a big mm:ss for the final hour. Past the deadline the
background turns red and the clock counts up.

Press START on the bar to toggle between the countdown and the target date.
"""
import argparse
import calendar
import datetime
import json
import math
import os
import queue
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

APP = "countdown"

W, H = 72, 16
BAR_H = 2               # rows the progress bar takes along the bottom

# --- BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs) ----------


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20",
                   help="bar address; 10.0.4.20 is USB, 127.0.0.1:8080 the emulator")
    p.add_argument("--label", default=None,
                   help="text above the countdown; with none set the countdown "
                        "takes the whole display")
    p.add_argument("--datetime", dest="target", default=None,
                   help="target moment: 2026-12-31T23:59[:59], '2026-12-31 23:59', "
                        "2026-12-31 (midnight) or 23:59 (today, or tomorrow if past)")
    p.add_argument("--in", dest="relative", default=None,
                   help="target as a duration from now, e.g. 90s, 2h30m, 1w3d")
    p.add_argument("--warning", default="1d",
                   help="amber below this much time left (default 1d)")
    p.add_argument("--danger", default="1h",
                   help="red below this much time left (default 1h)")
    p.add_argument("--fps", type=float, default=12.0, help="frames per second (default 12)")
    p.add_argument("--no-buttons", action="store_true",
                   help="skip the START-button listener")
    p.add_argument("--show-target", action="store_true",
                   help="start on the target-date view instead of the countdown")
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


def rect(eid, x, y, w, h, fill_colors, fill="solid", **kw):
    # border_width defaults to 1 on the firmware, which paints a white outline
    # around every rectangle; these are all fills, so it is always 0 here.
    return {"id": eid, "type": "rectangle", "x": x, "y": y, "width": max(1, int(w)),
            "height": max(1, int(h)), "fill": fill, "fill_colors": fill_colors,
            "border_width": 0, **kw}


def hidden_rect(eid):
    """A 1x1 fully transparent rectangle.

    The firmware keeps every id it has ever been sent until the app is cleared,
    so an element cannot be un-drawn by leaving it out of the next frame. The
    fixed id set below is therefore sent on every frame, with the ones a given
    view does not use parked as transparent."""
    return rect(eid, 0, 0, 1, 1, ["#00000000"])


# --- text metrics ----------------------------------------------------------
# Advance widths for printable ASCII, taken from the device font atlas, encoded
# as chr(32 + advance). Knowing how wide a string renders is what lets the app
# pick the largest font that still fits and centre things exactly.

_ADVANCES = {
    "tiny": "#\"$&$%%\"##$$\"$\"%$$$$$$$$$$\"\"$$$$$$$$$$$$$$$%$&%%$%$$$%&&$&%#%#$$#$$$$$##$\"\"$\"&$$$$#$$$$&$$$$\"$%",
    "small": "\"\"$&$%%\"##$$\"#\"#$#$$$$$$$$\"#$$$$%%%%%%%%%\"$%$&%%%%%%$%$&$$$###$$$$$$$$#$$\"#$\"&$$$$#$#$$&$$$$\"$%",
    "condensed": "%#$&&('\"##$&#$$%%%%%%%%%%%\"#$&$&(&&&&&&&&$%&%(&&&&&&&&&(&&&#%#&%&%%%%%%%%$$%$&%%%%$%%%&&&%%$#$&",
    "large": "&#$)(*)\"$$&&#$$&'%''''''''\"#$&$&*(''(''(($'''*(('(''(((*(('#&#&&&&&&&&%&&$$&$(&&&&%&%&&*&&&$\"$(",
    "extra_large": "$#&*)+*#%%''#$#&(%((((((((##%'%',((((''((%'(')(((((('())(('$&$(%'((((''((%'(')(((((('())(('&#&(",
}

# A text element is anchored by its font box, not by its ink, and the box has
# blank rows on top. INK_TOP is how far below y the first lit row actually
# lands, so `y = wanted_row - INK_TOP[font]` puts ink exactly where intended.
# INK_CAPS is the lit height, used to check that a font fits the rows a view has
# left over.
# INK_CAPS is the lit height of capitals and digits, which is what a font has to
# fit in; the few lowercase descenders in these strings ("days", "1y") are
# allowed to reach a row or two further down.
INK_TOP = {"tiny": 1, "small": 2, "condensed": 2, "large": 2, "extra_large": 2}
INK_CAPS = {"tiny": 5, "small": 5, "condensed": 7, "large": 9, "extra_large": 10}


def text_width(txt, font):
    table = _ADVANCES[font]
    total = 0
    for ch in txt:
        i = ord(ch) - 32
        total += ord(table[i]) - 32 if 0 <= i < len(table) else 4
    return total


def fit_font(txt, fonts, max_width=W):
    """The first font in `fonts` that renders `txt` inside max_width."""
    for font in fonts:
        if text_width(txt, font) <= max_width:
            return font
    return fonts[-1]


VALUE_IDS = ("v0", "v1", "v2", "v3", "v4", "v5")
NUMBER_CHARS = "0123456789:+."


def split_runs(txt):
    """Alternating runs of [text, is_number] -- '5h 00m' -> 5 / h / 00 / m."""
    runs = []
    for ch in txt:
        is_number = ch in NUMBER_CHARS
        if runs and runs[-1][1] == is_number:
            runs[-1][0] += ch
        else:
            runs.append([ch, is_number])
    return runs


def value_line(txt, first_row, last_row, fonts, color):
    """The value, as one element per size it needs, padded to VALUE_IDS.

    `extra_large` is a capitals-only face: handed "in 12 days" it prints
    "IN 12 DAYS". So when it is the size in play, the digits go in it and the
    words go in `large` beside them, bottom-aligned onto one baseline. Every
    other size has real lowercase and stays a single element."""
    runs = split_runs(txt)
    if fonts[0] == "extra_large" and len(runs) > 1 and len(runs) <= len(VALUE_IDS):
        width = sum(text_width(part, "extra_large" if num else "large")
                    for part, num in runs)
        if width <= W:
            baseline = first_row + INK_CAPS["extra_large"] - 1
            parts, x = [], (W - width) // 2
            for eid, (part, num) in zip(VALUE_IDS, runs):
                font = "extra_large" if num else "large"
                parts.append(text(eid, part, x=x, align="top_left", font=font,
                                  y=baseline - INK_CAPS[font] + 1 - INK_TOP[font],
                                  color=color))
                x += text_width(part, font)
            return parts

    font = fit_font(txt, [f for f in fonts
                          if first_row + INK_CAPS[f] - 1 <= last_row] or list(fonts))
    return [text(VALUE_IDS[0], txt, x=W // 2, y=first_row - INK_TOP[font],
                 font=font, color=color, align="top_mid")]


# --- colour ----------------------------------------------------------------

PALETTE = {
    # state -> (accent rgb, dim rgb used for the label and the bar track)
    "calm": ("#3FD8FF", "#0E4657"),
    "warning": ("#FFB020", "#4A2E00"),
    "danger": ("#FF3B30", "#4A0B08"),
    "expired": ("#FFFFFF", "#FF2A1F"),
}
LED_ALERT = {"warning": "#FFB020FF", "danger": "#FF3B30FF", "expired": "#FF2A1FFF"}


def rgba(rgb, alpha):
    """'#RRGGBB' plus a 0..1 alpha -> the '#RRGGBBAA' the firmware wants."""
    a = int(round(max(0.0, min(1.0, alpha)) * 255))
    return "%s%02X" % (rgb, a)


def mix(rgb_a, rgb_b, t):
    t = max(0.0, min(1.0, t))
    out = "#"
    for i in (1, 3, 5):
        a, b = int(rgb_a[i:i + 2], 16), int(rgb_b[i:i + 2], 16)
        out += "%02X" % int(round(a + (b - a) * t))
    return out


# --- durations and dates ---------------------------------------------------

_UNIT_SECONDS = {"y": 365 * 86400, "mo": 30 * 86400, "w": 7 * 86400,
                 "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(spec):
    """'1w3d' -> 864000. Accepts y/mo/w/d/h/m/s in any order, or bare seconds."""
    raw = spec.strip().lower().replace(" ", "")
    if not raw:
        raise ValueError("empty duration")
    try:
        return float(raw)          # a bare number means seconds
    except ValueError:
        pass
    total, number, unit, i = 0.0, "", "", 0
    while i < len(raw):
        ch = raw[i]
        if ch.isdigit() or ch == ".":
            if unit:                # a new number closes the previous pair
                number, unit = "", ""
            number += ch
            i += 1
            continue
        # "mo" has to win over "m", so try the two-character unit first.
        unit = raw[i:i + 2] if raw[i:i + 2] in _UNIT_SECONDS else ch
        if unit not in _UNIT_SECONDS or not number:
            raise ValueError("cannot read duration %r" % spec)
        total += float(number) * _UNIT_SECONDS[unit]
        i += len(unit)
        number = ""
    if number:
        raise ValueError("duration %r ends without a unit" % spec)
    return total


def next_new_year(now):
    return now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0,
                       second=0, microsecond=0)


def parse_target(spec, now):
    """Read a target moment, in the machine's local timezone."""
    raw = spec.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:         # a bare clock time means today, else tomorrow
            parsed = now.replace(hour=parsed.hour, minute=parsed.minute,
                                 second=parsed.second, microsecond=0)
            if parsed <= now:
                parsed += datetime.timedelta(days=1)
        return parsed
    try:                            # anything else ISO-ish that the stdlib knows
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        raise SystemExit("countdown: cannot read --datetime %r" % spec)
    if parsed.tzinfo is not None:   # everything downstream is naive local time
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def add_months(when, count):
    month = when.month - 1 + count
    year = when.year + month // 12
    month = month % 12 + 1
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def calendar_split(start, end):
    """Whole calendar months from start to end, plus the leftover timedelta."""
    months = (end.year - start.year) * 12 + (end.month - start.month)
    while months > 0 and add_months(start, months) > end:
        months -= 1
    while add_months(start, months + 1) <= end:
        months += 1
    return months, end - add_months(start, months)


# --- how much detail to show ----------------------------------------------
# The closer the deadline, the more precision is useful; a year out, seconds are
# noise. Each tier also names the unit the progress bar drains through, so the
# bar always measures something the text is not already showing.

def format_gap(now, later, overdue):
    """(text, tier, unit_seconds) for the gap between two moments."""
    seconds = max(0.0, (later - now).total_seconds())
    sign = "+" if overdue else ""
    if seconds < 3600:
        minutes, secs = divmod(int(seconds), 60)
        return "%s%02d:%02d" % (sign, minutes, secs), "second", 60
    if seconds < 86400:
        hours = int(seconds // 3600)
        minutes = int(seconds % 3600) // 60
        return "%s%dh %02dm" % (sign, hours, minutes), "hour", 3600
    if seconds < 7 * 86400:
        days = int(seconds // 86400)
        hours = int(seconds % 86400) // 3600
        return "%s%dd %dh" % (sign, days, hours), "day", 86400
    months, leftover = calendar_split(now, later)
    if months < 1:
        days = int(seconds // 86400)
        return ("+%d days" % days) if overdue else ("in %d days" % days), "days", 86400
    if months < 12:
        return "%s%dmo %dd" % (sign, months, leftover.days), "months", 86400
    years, rest = divmod(months, 12)
    return "%s%dy %dmo" % (sign, years, rest), "years", 86400


def format_target(target):
    """('TUE 31 DEC 2026', '23:59') for the START-button peek view."""
    return (target.strftime("%a %d %b %Y").upper(),
            target.strftime("%H:%M"))


# --- animation -------------------------------------------------------------

def breathe(t, period, amount):
    """A slow sine between 1 - amount and 1."""
    return 1.0 - amount * 0.5 * (1.0 - math.cos(2 * math.pi * t / period))


# Where the two thumps of one beat sit, and how wide they are, in seconds. Each
# beat is a quick lub-dub; how often one comes round is the period in MOOD, and
# the two are independent because these are in seconds rather than in fractions
# of the period.
THUMP_AT, ECHO_AT, THUMP_SPREAD = 0.05, 0.22, 0.045


def _bump(phase, period, centre, spread):
    """A gaussian on a circle, so a thump sitting near phase 0 keeps the half
    of itself that falls off the end of the loop."""
    distance = abs(phase - centre)
    distance = min(distance, period - distance)
    return math.exp(-(distance * distance) / (2 * spread * spread))


def heartbeat(t, period):
    """Two quick thumps per period, for the danger and expired states.

    The thumps are placed in seconds rather than as a fraction of the period,
    so shortening the period closes the gap between beats instead of squeezing
    the beats themselves into a blur."""
    phase = t % period
    thump = _bump(phase, period, THUMP_AT, THUMP_SPREAD)
    echo = 0.55 * _bump(phase, period, ECHO_AT, THUMP_SPREAD)
    return min(1.0, thump + echo)


# How hard the value pulses in each state, and how fast. `period` is the whole
# beat: the calm and warning states breathe on a sine, danger and expired thump
# on the heartbeat above, and expired is the shortest gap of the four.
MOOD = {
    "calm": {"period": 4.2, "amount": 0.18},
    "warning": {"period": 2.2, "amount": 0.32},
    "danger": {"period": 2.60, "amount": 0.55},
    "expired": {"period": 1.90, "amount": 0.75},
}


def state_for(remaining, warning, danger):
    if remaining <= 0:
        return "expired"
    if remaining <= danger:
        return "danger"
    if remaining <= warning:
        return "warning"
    return "calm"


def build_frame(now, target, warning, danger, t, show_target):
    """One frame, as a fixed set of elements always in the same order.

    The firmware paints them in the order it first saw each id, so the
    background has to lead and the text has to trail; sending the same ids in
    the same order every frame keeps that stacking stable and keeps the stored
    element set at its size instead of growing with the clock."""
    remaining = (target - now).total_seconds()
    state = state_for(remaining, warning, danger)
    accent, dim = PALETTE[state]
    mood = MOOD[state]
    overdue = remaining <= 0

    if state in ("danger", "expired"):
        pulse = 0.55 + 0.45 * heartbeat(t, mood["period"])
    else:
        pulse = breathe(t, mood["period"], mood["amount"])

    elements = []

    # 1. background: only the overdue state paints one, throbbing red.
    if overdue:
        # Darkest at the top so the text stays readable, hottest along the
        # bottom where it runs into the bar.
        glow = 0.45 + 0.55 * heartbeat(t, mood["period"])
        elements.append(rect("bg", 0, 0, W, H,
                             [rgba(mix("#280402", dim, glow * 0.25), 1.0),
                              rgba(mix("#6E0D08", dim, glow), 1.0)],
                             fill="gradient_v"))
    else:
        elements.append(hidden_rect("bg"))

    # 2/3. the bottom two rows: a track, and a fill that drains through the
    # current unit -- one minute in the final hour, one day out at month range.
    if overdue:
        text_line, tier, unit = format_gap(target, now, True)
    else:
        text_line, tier, unit = format_gap(now, target, False)

    elements.append(rect("track", 0, H - BAR_H, W, BAR_H, [rgba(dim, 0.5)]))
    if overdue:
        elements.append(hidden_rect("fill"))
    else:
        # Brightest at the leading edge, so where the level currently sits reads
        # as a crisp line rather than fading into the track.
        drain = (remaining % unit) / unit
        elements.append(rect("fill", 0, H - BAR_H, max(1, round(drain * W)), BAR_H,
                             [rgba(accent, 0.28), rgba(accent, 1.0)], fill="gradient_h"))

    # 4/5. label and value.
    if show_target:
        head, clock = format_target(target)
        label_text, value_text = head, clock
        final_hour = False
    else:
        value_text = text_line
        # The final hour drops the label and goes as big as the display allows:
        # at that point the label is the least useful thing on the bar and the
        # mm:ss is the most.
        final_hour = tier == "second" and not overdue
        label_text = "" if final_hour else LABEL

    # With the label line free the value takes the whole display, so it starts
    # from the biggest face and only steps down when a string is too wide.
    value_fonts = (("condensed", "small", "tiny") if label_text
                   else ("extra_large", "large", "condensed", "small", "tiny"))

    # Labels are upper-cased, so nothing on this line has a descender and the
    # taller `small` face still lives inside the five rows above the value.
    label_font = fit_font(label_text, ("small", "tiny"))
    label_y = -INK_TOP[label_font]              # lit rows start at row 0
    if not label_text:
        elements.append(text("label", " ", x=0, y=label_y, font=label_font,
                             color="#00000000"))
    elif text_width(label_text, label_font) > W:
        # Too long to fit: hand it to the firmware's scroller instead of cutting.
        elements.append(text("label", label_text, x=0, y=label_y, font=label_font,
                             color=rgba(mix(dim, accent, 0.55), 0.75 + 0.25 * pulse),
                             align="top_left", width=W, scroll_rate=900,
                             scroll_start_delay=1500, scroll_repeat_delay=2200))
    else:
        elements.append(text("label", label_text, x=W // 2, y=label_y, font=label_font,
                             color=rgba(mix(dim, accent, 0.9 if overdue else 0.75),
                                        0.7 + 0.3 * pulse),
                             align="top_mid"))

    # One blank row under the label, so the two lines are not shoulder to
    # shoulder; with no label the value just centres in what is left.
    first_row = 6 if label_text else 2
    parts = value_line(value_text, first_row, H - BAR_H - 1, value_fonts,
                       rgba(accent, 0.45 + 0.55 * pulse))
    elements.extend(parts)
    # Whatever the line did not need is still sent, blank, so a wide frame
    # cannot leave a stray run of the previous one lit.
    for eid in VALUE_IDS[len(parts):]:
        elements.append(text(eid, " ", x=0, y=0, font="tiny", color="#00000000"))
    return elements, state


# --- START button over the status WebSocket --------------------------------
# The bar streams input on /api/status/ws as protobuf. Decoding the two fields
# this app needs by hand keeps it stdlib-only:
#   State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1
# Button START is enum 2; action PRESS is 0 and RELEASE is 1.


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


def start_pressed(frame):
    """True if this status frame carries a START press."""
    for field, wire, update in _iter_fields(frame):
        if field != 2 or wire != 2:                     # State.updates
            continue
        for ufield, uwire, payload in _iter_fields(update):
            if ufield != 11 or uwire != 2:              # StateUpdate.input
                continue
            for efield, ewire, event in _iter_fields(payload):
                if efield != 1 or ewire != 2:           # InputEvent.button_event
                    continue
                button, action = 0, 0
                for bfield, bwire, value in _iter_fields(event):
                    if bwire:
                        continue
                    if bfield == 1:
                        button = int(value)
                    elif bfield == 2:
                        action = int(value)
                if button == 2 and action == 0:
                    return True
    return False


class ButtonListener(threading.Thread):
    """A minimal RFC 6455 client for /api/status/ws, in a daemon thread.

    Buttons are a bonus here, not the app: the recorder and the emulator do not
    serve this endpoint at all, so every failure is swallowed after one line of
    explanation and retried with a backoff. The countdown keeps drawing either
    way."""

    def __init__(self, host):
        super().__init__(daemon=True)
        self.host = host
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._complained = False
        self._announced = False

    def stop(self):
        self._stop.set()

    def presses(self):
        count = 0
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return count
            count += 1

    def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:                     # noqa: BLE001 - see docstring
                if not self._complained:
                    print("countdown: no button stream (%s); the countdown runs anyway" % exc)
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
                print("countdown: START button connected")
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
                if opcode == 0x2 and start_pressed(payload):
                    self.events.put(True)
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

NOW = datetime.datetime.now().replace(microsecond=0)
if ARGS.relative:
    try:
        TARGET = NOW + datetime.timedelta(seconds=parse_duration(ARGS.relative))
    except ValueError as exc:
        raise SystemExit("countdown: %s" % exc)
elif ARGS.target:
    TARGET = parse_target(ARGS.target, NOW)
else:
    TARGET = next_new_year(NOW)

# No --label means no label line at all, and the countdown gets the full height.
# Bitmap fonts are ASCII only, so anything outside that range becomes a dot
# rather than a 400 from the bar.
LABEL = "".join(ch if 0x20 <= ord(ch) <= 0x7E else "."
                for ch in (ARGS.label or "").upper())

try:
    WARNING = parse_duration(ARGS.warning)
    DANGER = parse_duration(ARGS.danger)
except ValueError as exc:
    raise SystemExit("countdown: %s" % exc)
if DANGER > WARNING:                # a danger window wider than the warning one
    WARNING, DANGER = DANGER, WARNING   # would never let the amber state show


def main():
    listener = None
    if not (ARGS.no_buttons or ARGS.test):
        listener = ButtonListener(ARGS.host.replace("http://", "").rstrip("/"))
        listener.start()

    show_target = ARGS.show_target
    last_state = None
    frame_time = 1.0 / max(1.0, ARGS.fps)
    started = time.monotonic()

    print("countdown: %s%s  (%s)" % (LABEL + " -> " if LABEL else "",
                                     TARGET.strftime("%Y-%m-%d %H:%M:%S"), BASE))
    try:
        while True:
            loop_start = time.monotonic()
            if listener and listener.presses() % 2:
                show_target = not show_target

            elements, state = build_frame(
                datetime.datetime.now(), TARGET, WARNING, DANGER,
                loop_start - started, show_target)

            extra = {}
            # Blink the status LED once on each step up in urgency, not every
            # frame -- a 12 Hz strobe would be unbearable.
            if state != last_state and state in LED_ALERT:
                extra["led_notification_color"] = LED_ALERT[state]
            try:
                draw(elements, **extra)
                last_state = state
            except urllib.error.HTTPError as exc:
                if exc.code != 409:   # 409 = a higher-priority app owns the display
                    raise
            except urllib.error.URLError as exc:
                print("countdown: %s" % exc.reason)
                time.sleep(1.0)

            if ARGS.test:
                return
            elapsed = time.monotonic() - loop_start
            if elapsed < frame_time:
                time.sleep(frame_time - elapsed)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if listener:
            listener.stop()
        if not ARGS.test:
            clear()


if __name__ == "__main__":
    main()
