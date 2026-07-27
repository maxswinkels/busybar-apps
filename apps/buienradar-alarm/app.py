#!/usr/bin/env python3
"""Buienradar rain alarm: takes the screen only when rain is coming, with a 2-hour intensity timeline.

    python3 apps/local/buienradar-alarm/app.py [--host 127.0.0.1:8080] [--lat 52.37] [--lon 4.89] [--test]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

APP = "maxswinkels.buienradar-alarm"
BUIENRADAR_URL = "https://gpsgadget.buienradar.nl/data/raintext"


def parse_args():
    p = argparse.ArgumentParser(description="Buienradar rain alarm for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--lat", type=float, default=52.37)
    p.add_argument("--lon", type=float, default=4.89)
    p.add_argument("--interval", type=int, default=60, help="seconds between redraws")
    p.add_argument("--refresh", type=int, default=300, help="seconds between data fetches")
    p.add_argument("--test", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _draw(host, elements, led_notification=False):
    """POST /api/display/draw. Returns (status_code, body_text)."""
    body = {
        "application_name": APP,
        "priority": 60,
        "elements": elements,
    }
    if led_notification:
        body["led_notification_color"] = "0x2196F3FF"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _base(host) + "/api/display/draw",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
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
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise RuntimeError(f"clear failed: {e}") from e


def _fetch_rain(lat, lon):
    """Fetch rain data from Buienradar. Returns list of (minutes_from_now, value) tuples."""
    url = BUIENRADAR_URL + "?" + urllib.parse.urlencode({"lat": lat, "lon": lon})
    req = urllib.request.Request(url, headers={"User-Agent": "busybar-rain-alarm/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", "ignore")
    return _parse_rain(raw)


def _parse_rain(raw):
    """Parse plain-text lines 'NNN|HH:MM' into (minutes_from_now, value) list."""
    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute
    result = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 2:
            continue
        try:
            value = int(parts[0].strip())
            hh, mm = parts[1].strip().split(":")
            entry_minutes = int(hh) * 60 + int(mm)
        except (ValueError, IndexError):
            continue
        delta = entry_minutes - now_minutes
        # entries can cross midnight; if more than 60 min in the past, add 24h
        if delta < -60:
            delta += 24 * 60
        result.append((delta, value))
    return result


def _fake_series():
    """Test data: 2 dry entries, then rain ramp, then dry to 24 total."""
    values = [0, 0, 40, 80, 120, 180, 220, 180, 120, 80, 40]
    series = []
    for i in range(24):
        v = values[i] if i < len(values) else 0
        series.append((i * 5, v))
    return series


# ---------------------------------------------------------------------------
# Display logic
# ---------------------------------------------------------------------------

def _build_headline(series):
    """Return (headline_str, is_raining_now)."""
    if not series:
        return None, False
    first_minutes, first_value = series[0]
    if first_value <= 0:
        # Dry now -- find when rain starts
        for minutes, value in series:
            if value > 0:
                return f"REGEN IN {max(minutes, 0)} MIN", False
        return None, False  # no rain in entire series
    else:
        # Raining now -- find when it becomes dry
        for minutes, value in series:
            if minutes >= 0 and value <= 0:
                return f"DROOG IN {minutes} MIN", True
        return "REGEN", True


def _rect(x, y, w, h, color):
    return {"type": "rectangle", "x": x, "y": y, "width": w, "height": h,
            "border_width": 0, "fill": "solid", "fill_colors": [color]}


def _bar_color(value):
    """Buienradar-style intensity colors: drizzle cyan, showers blue, downpour purple.
    Saturated hues with zeroed channels survive the display's gamma curve."""
    if value < 70:
        return "0x00C8FFFF"
    if value < 150:
        return "0x0064FFFF"
    return "0x9600FFFF"


def _build_elements(headline, series):
    """Droplet icon + headline on top, gapped intensity bars over a baseline below."""
    elements = []
    # Droplet icon (5x6) at the top left, with a light catch-light pixel
    drop = "0x00A8FFFF"
    elements += [
        _rect(4, 0, 1, 2, drop),   # tip
        _rect(3, 2, 3, 1, drop),
        _rect(2, 3, 5, 2, drop),   # body
        _rect(3, 5, 3, 1, drop),
        _rect(3, 3, 1, 1, "0xB3E5FCFF"),
    ]
    # Headline, centered in the space right of the icon (x=10..72; the widest
    # case "REGEN IN 120 MIN" is 62px in the small font, measured via the atlas)
    elements.append({
        "type": "text",
        "text": headline,
        "x": 41,
        "y": 0,
        "font": "small",
        "color": "0xEAF6FFFF",
        "align": "top_mid",
    })
    # Baseline with subtle half-hour ticks
    elements.append(_rect(0, 15, 72, 1, "0x102027FF"))
    for tick_x in (18, 36, 54):
        elements.append(_rect(tick_x, 15, 1, 1, "0x3A4A55FF"))
    # Timeline: 24 slots of 5 min, 2px bars with a 1px gap, colored by intensity
    for slot in range(24):
        if slot < len(series):
            _, value = series[slot]
        else:
            value = 0
        if value <= 0:
            continue
        h = max(1, round(value / 255 * 7))
        elements.append(_rect(slot * 3, 16 - h, 2, h, _bar_color(value)))
    return elements


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    series = []
    last_fetch = 0.0
    last_clear_sent = False  # True after we sent a DELETE for a dry period
    first_alert = True       # True until we draw the first rain alert

    if args.test:
        series = _fake_series()
        headline, _ = _build_headline(series)
        if headline is None:
            print("test: no rain in series -- nothing to draw")
            sys.exit(0)
        elements = _build_elements(headline, series)
        status, body = _draw(args.host, elements, led_notification=True)
        print(f"drew: '{headline}', {len(elements)} elements (status {status})")
        sys.exit(0)

    print(f"buienradar-alarm -> {_base(args.host)}  (Ctrl-C to stop)")

    try:
        while True:
            now = time.monotonic()

            # Fetch/refresh data
            if now - last_fetch >= args.refresh or last_fetch == 0.0:
                try:
                    series = _fetch_rain(args.lat, args.lon)
                    last_fetch = now
                except Exception as e:
                    print(f"fetch error (reusing previous data): {e}")
                    if last_fetch == 0.0:
                        last_fetch = now  # avoid hammering on boot error

            # Determine what to display
            any_rain = any(v > 0 for _, v in series)

            if not any_rain:
                if not last_clear_sent:
                    try:
                        _clear(args.host)
                        print("droog, scherm vrijgegeven")
                    except Exception as e:
                        print(f"clear error: {e}")
                    last_clear_sent = True
                    first_alert = True  # next rain alert gets LED notification
            else:
                headline, _ = _build_headline(series)
                if headline is None:
                    # Shouldn't happen when any_rain is True, but be safe
                    time.sleep(args.interval)
                    continue
                elements = _build_elements(headline, series)
                notify = first_alert
                try:
                    status, body = _draw(args.host, elements, led_notification=notify)
                    if status == 409:
                        print(f"display busy (409), retrying next cycle")
                    else:
                        if notify:
                            first_alert = False
                        last_clear_sent = False
                        print(f"drew: '{headline}' ({len(elements)} elements)")
                except Exception as e:
                    print(f"draw error: {e}")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nstopped.")
        try:
            _clear(args.host)
        except Exception:
            pass


if __name__ == "__main__":
    main()
