#!/usr/bin/env python3
"""Clock widget: big time, refreshed every second.

    python app.py                                      # original/default view
    python app.py --host 127.0.0.1:8080                # emulator or a Wi-Fi bar
    python app.py --no-seconds                         # show HH:MM only
    python app.py --color '#00FFAA'                    # custom time color
    python app.py --date                               # add a second date line
    python app.py --date --date-format written --language it
    python app.py --icon clock                         # clock, hourglass, calendar
    python app.py --date --icon calendar --color '#FFD54F'

Icons are true 16x16 RGBA PNG assets generated in memory and uploaded once at
startup. They occupy the rightmost 16 pixels, while text stays in its own area.
"""
import argparse
import json
import math
import re
import signal
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "clock"
ICON_SIZE = 16
ICON_X = 56
ICON_Y = 0
TEXT_AREA_MID_X = 27

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _color(value):
    """Accept RRGGBB or RRGGBBAA, with or without '#', and return #RRGGBBAA."""
    value = value.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
        raise argparse.ArgumentTypeError("color must be RRGGBB or RRGGBBAA")
    if len(value) == 6:
        value += "FF"
    return "#" + value.upper()


def _args():
    # argparse lets busybar-manager auto-discover options by parsing --help.
    p = argparse.ArgumentParser(description="Clock widget for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--no-seconds", action="store_true",
                   help="show HH:MM only, hide the seconds")
    p.add_argument("--color", type=_color, default="#FFFFFFFF",
                   help="time color as RRGGBB or RRGGBBAA (default: FFFFFFFF)")
    p.add_argument("--date", action="store_true",
                   help="show the date on a second line")
    p.add_argument("--date-format", choices=["numeric", "written"], default="numeric",
                   help="second-line date format (default: numeric)")
    p.add_argument("--language", choices=["en", "it", "fr", "de", "es", "pt"], default="en",
                   help="language used by --date-format written (default: en)")
    p.add_argument("--date-color", type=_color, default=None,
                   help="date color; defaults to --color")
    p.add_argument("--icon", choices=["clock", "hourglass", "calendar"], default=None,
                   help="show a large 16x16 color icon on the right")
    p.add_argument("--icon-color", type=_color, default=None,
                   help="override the icon's main accent color")
    return p.parse_args()


_ARGS = _args()
BASE = "http://" + _ARGS.host.replace("http://", "").rstrip("/")
TIME_FMT = "%H:%M" if _ARGS.no_seconds else "%H:%M:%S"
DATE_COLOR = _ARGS.date_color or _ARGS.color


# ---------------------------------------------------------------------------
# Localized date names
# ---------------------------------------------------------------------------

WEEKDAYS = {
    "en": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
    "it": ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"],
    "fr": ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"],
    "de": ["MO", "DI", "MI", "DO", "FR", "SA", "SO"],
    "es": ["LUN", "MAR", "MIE", "JUE", "VIE", "SAB", "DOM"],
    "pt": ["SEG", "TER", "QUA", "QUI", "SEX", "SAB", "DOM"],
}

MONTHS = {
    "en": ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
    "it": ["GEN", "FEB", "MAR", "APR", "MAG", "GIU", "LUG", "AGO", "SET", "OTT", "NOV", "DIC"],
    "fr": ["JAN", "FEV", "MAR", "AVR", "MAI", "JUN", "JUL", "AOU", "SEP", "OCT", "NOV", "DEC"],
    "de": ["JAN", "FEB", "MAR", "APR", "MAI", "JUN", "JUL", "AUG", "SEP", "OKT", "NOV", "DEZ"],
    "es": ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"],
    "pt": ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN", "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"],
}


def format_date(now):
    if _ARGS.date_format == "numeric":
        return time.strftime("%d/%m/%Y", now)
    weekday = WEEKDAYS[_ARGS.language][now.tm_wday]
    month = MONTHS[_ARGS.language][now.tm_mon - 1]
    return f"{weekday} {now.tm_mday:02d} {month}"


# ---------------------------------------------------------------------------
# BUSY Bar HTTP helpers
# ---------------------------------------------------------------------------


def draw(elements, **extra):
    body = {"application_name": APP, "elements": elements, **extra}
    req = urllib.request.Request(
        BASE + "/api/display/draw",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5):
        pass


def clear():
    """Remove every element owned by this app from the display."""
    query = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(BASE + "/api/display/draw?" + query, method="DELETE")
    with urllib.request.urlopen(req, timeout=5):
        pass


def _safe_clear():
    try:
        clear()
    except urllib.error.HTTPError as exc:
        # 404/409/410 are harmless during shutdown or when another app owns the bar.
        if exc.code not in (404, 409, 410):
            print(f"clear failed: HTTP {exc.code}", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"clear failed: {exc.reason}", file=sys.stderr)


def _upload_asset(filename, data):
    query = urllib.parse.urlencode({"application_name": APP, "file": filename})
    url = BASE + "/api/assets/upload?" + query

    # A just-cleared asset can briefly still be locked by the display. Retry 508
    # a few times rather than failing startup.
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5):
                return
        except urllib.error.HTTPError as exc:
            if exc.code == 508 and attempt < 2:
                time.sleep(0.1)
                continue
            raise


def text(txt, x=0, y=0, font="normal", color="#FFFFFFFF", el_id="0", **kw):
    return {
        "id": el_id,
        "type": "text",
        "text": str(txt),
        "x": x,
        "y": y,
        "font": font,
        "color": color,
        **kw,
    }


def image(path, x, y, el_id="icon"):
    return {"id": el_id, "type": "image", "path": path, "x": x, "y": y}


# ---------------------------------------------------------------------------
# 16x16 color icon generation (stdlib only)
# ---------------------------------------------------------------------------


def _rgba(hex_color):
    value = hex_color.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in range(0, 8, 2))


def _blank_icon():
    return [(0, 0, 0, 0)] * (ICON_SIZE * ICON_SIZE)


def _px(buf, x, y, color):
    if 0 <= x < ICON_SIZE and 0 <= y < ICON_SIZE:
        buf[y * ICON_SIZE + x] = color


def _rect(buf, x, y, width, height, color):
    for yy in range(y, y + height):
        for xx in range(x, x + width):
            _px(buf, xx, yy, color)


def _line(buf, x0, y0, x1, y1, color):
    """Small integer Bresenham line for clock hands."""
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        _px(buf, x0, y0, color)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def _clock_icon(accent=None):
    buf = _blank_icon()
    ring = _rgba(accent or "#2F80EDFF")
    face = _rgba("#D9F1FFFF")
    shadow = _rgba("#123A5AFF")
    tick = _rgba("#3F5D73FF")
    hands = _rgba("#102A43FF")
    second = _rgba("#FF4D5AFF")
    center = _rgba("#FFD54FFF")

    # Antialiased-looking circular face from two crisp pixel radii.
    for y in range(ICON_SIZE):
        for x in range(ICON_SIZE):
            d = math.hypot(x - 7.5, y - 7.5)
            if d <= 7.3:
                _px(buf, x, y, shadow)
            if d <= 6.5:
                _px(buf, x, y, ring)
            if d <= 5.5:
                _px(buf, x, y, face)

    for x, y in ((7, 3), (12, 7), (7, 12), (3, 7)):
        _px(buf, x, y, tick)
    _line(buf, 7, 7, 7, 4, hands)
    _line(buf, 7, 7, 11, 8, hands)
    _line(buf, 7, 7, 5, 11, second)
    _rect(buf, 7, 7, 2, 2, center)
    return buf


def _hourglass_icon(accent=None):
    buf = _blank_icon()
    frame_dark = _rgba("#6B451AFF")
    frame = _rgba(accent or "#D99A2BFF")
    frame_light = _rgba("#FFD36AFF")
    glass_edge = _rgba("#4B9FC5FF")
    glass = _rgba("#BFEFFFFF")
    sand = _rgba("#FFB72BFF")
    sand_light = _rgba("#FFD56AFF")

    # Wooden/golden caps and narrow side supports.
    _rect(buf, 2, 1, 12, 2, frame_dark)
    _rect(buf, 3, 2, 10, 1, frame)
    _rect(buf, 3, 13, 10, 1, frame)
    _rect(buf, 2, 14, 12, 1, frame_dark)
    _rect(buf, 3, 3, 1, 10, frame)
    _rect(buf, 12, 3, 1, 10, frame)
    _px(buf, 4, 2, frame_light)
    _px(buf, 11, 13, frame_light)

    # Glass narrows toward the neck.
    for x in range(4, 12):
        _px(buf, x, 4, glass_edge)
    for x in range(5, 11):
        _px(buf, x, 5, glass)
    for x in range(6, 10):
        _px(buf, x, 6, glass)
    _rect(buf, 7, 7, 2, 2, glass)
    for x in range(6, 10):
        _px(buf, x, 9, glass)
    for x in range(5, 11):
        _px(buf, x, 10, glass)
    for x in range(4, 12):
        _px(buf, x, 11, glass_edge)

    # Sand: a small upper pile, falling stream, and lower mound.
    _rect(buf, 5, 5, 6, 1, sand_light)
    _rect(buf, 6, 6, 4, 1, sand)
    _rect(buf, 7, 7, 2, 3, sand)
    _rect(buf, 6, 10, 4, 1, sand_light)
    _rect(buf, 5, 11, 6, 1, sand)
    return buf


def _calendar_icon(accent=None):
    buf = _blank_icon()
    outline = _rgba("#294A63FF")
    header = _rgba(accent or "#E64B4BFF")
    header_light = _rgba("#FF7777FF")
    paper = _rgba("#F8FCFFFF")
    paper_shadow = _rgba("#DCE8F0FF")
    ring = _rgba("#6F8799FF")
    ink = _rgba("#245D9CFF")

    # Rounded page silhouette with binding rings.
    _rect(buf, 2, 2, 12, 13, outline)
    _rect(buf, 1, 4, 14, 9, outline)
    _rect(buf, 2, 4, 12, 10, paper)
    _rect(buf, 2, 4, 12, 3, header)
    _rect(buf, 3, 4, 10, 1, header_light)
    _rect(buf, 4, 0, 2, 4, ring)
    _rect(buf, 10, 0, 2, 4, ring)
    _px(buf, 4, 1, paper_shadow)
    _px(buf, 10, 1, paper_shadow)

    # Two rows of date cells; one highlighted day makes the icon read clearly
    # as a calendar even at the native 16x16 resolution.
    cell = _rgba("#91A9BAFF")
    for x in (4, 7, 10):
        _rect(buf, x, 8, 2, 2, cell)
        _rect(buf, x, 11, 2, 2, cell)
    _rect(buf, 7, 11, 2, 2, ink)

    _rect(buf, 3, 13, 10, 1, paper_shadow)
    return buf


ICON_BUILDERS = {
    "clock": _clock_icon,
    "hourglass": _hourglass_icon,
    "calendar": _calendar_icon,
}


def _png_rgba(pixels):
    """Encode a 16x16 RGBA pixel list as PNG bytes, without Pillow."""
    raw = bytearray()
    for y in range(ICON_SIZE):
        raw.append(0)  # PNG filter: none
        for x in range(ICON_SIZE):
            raw += bytes(pixels[y * ICON_SIZE + x])

    def chunk(tag, data):
        payload = tag + data
        return (struct.pack(">I", len(data)) + payload +
                struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", ICON_SIZE, ICON_SIZE, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
            chunk(b"IEND", b""))


def prepare_icon():
    if not _ARGS.icon:
        return None
    pixels = ICON_BUILDERS[_ARGS.icon](_ARGS.icon_color)
    accent = (_ARGS.icon_color or "native").replace("#", "")
    filename = f"icon_{_ARGS.icon}_{accent}.png"
    _upload_asset(filename, _png_rgba(pixels))
    return filename


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------

_ICON_PATH = None


def build_elements(now):
    hhmm = time.strftime(TIME_FMT, now)

    # Preserve the original output when no new feature is enabled.
    if not _ARGS.date and not _ARGS.icon and _ARGS.color == "#FFFFFFFF":
        return [text(hhmm, x=36, y=15, font="extra_large", align="bottom_mid")]

    # Color-only customization keeps the original one-line layout and font.
    if not _ARGS.date and not _ARGS.icon:
        return [text(hhmm, x=36, y=15, font="extra_large",
                     color=_ARGS.color, align="bottom_mid")]

    elements = []
    if _ARGS.icon and _ICON_PATH:
        elements.append(image(_ICON_PATH, ICON_X, ICON_Y))

    if _ARGS.date:
        # Two lines. With an icon, the text is centered inside x=0..54 and never
        # gets shifted by the icon itself; without an icon it uses the full bar.
        center_x = TEXT_AREA_MID_X if _ARGS.icon else 36
        elements.append(text(hhmm, x=center_x, y=0, font="normal",
                             color=_ARGS.color, el_id="time", align="top_mid"))
        elements.append(text(format_date(now), x=center_x, y=9, font="small",
                             color=DATE_COLOR, el_id="date", align="top_mid"))
        return elements

    # Icon-only view. HH:MM:SS uses normal so it fits cleanly in the 55-pixel
    # text area. HH:MM has enough room for large.
    font = "large" if _ARGS.no_seconds else "normal"
    elements.append(text(hhmm, x=TEXT_AREA_MID_X, y=8, font=font,
                         color=_ARGS.color, el_id="time", align="center"))
    return elements


def tick():
    now = time.localtime()
    try:
        draw(build_elements(now))
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise
        print("display busy (409), retrying...")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

_STOP = False


def _request_stop(_signum, _frame):
    # busybar-manager may use SIGTERM when switching applications. Catch it so
    # the finally block runs and the clock clears its elements cleanly.
    global _STOP
    _STOP = True


def main():
    global _ICON_PATH

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)

    print(f"clock -> {BASE}  (Ctrl-C to stop)")

    try:
        # Always start from a known clean state before uploading/drawing anything.
        _safe_clear()
        _ICON_PATH = prepare_icon()

        while not _STOP:
            tick()
            time.sleep(1.0)

    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as exc:
        sys.exit(f"error: HTTP {exc.code} - {exc.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as exc:
        sys.exit(f"error: cannot reach {BASE} - {exc.reason}")
    finally:
        # Also runs on Ctrl+C, normal exit, errors, and SIGTERM from an app switch.
        _safe_clear()


if __name__ == "__main__":
    main()
