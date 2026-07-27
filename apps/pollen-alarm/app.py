#!/usr/bin/env python3
"""Pollen alarm: takes the screen only when hay fever pollen is high, with a 24-hour timeline.

    python3 app.py [--host 127.0.0.1:8080] [--lat 52.37] [--lon 4.89] [--test]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APP = "pollen-alarm"
METEO_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Species key -> Dutch display name
SPECIES = [
    ("alder_pollen",    "ELS"),
    ("birch_pollen",    "BERK"),
    ("grass_pollen",    "GRAS"),
    ("mugwort_pollen",  "BIJVOET"),
    ("olive_pollen",    "OLIJF"),
    ("ragweed_pollen",  "AMBROSIA"),
]

# Level thresholds
LEVEL_LAAG   = "laag"
LEVEL_MIDDEL = "middel"
LEVEL_HOOG   = "hoog"

COLOR_MIDDEL   = "0xFFC800FF"
COLOR_HOOG     = "0xFF3C00FF"
COLOR_BASELINE = "0x2A2A1AFF"
COLOR_WHITE    = "0xEAF6FFFF"
COLOR_AMBER    = "0xFFC800FF"
COLOR_GREEN    = "0x00C800FF"


def parse_args():
    p = argparse.ArgumentParser(description="Pollen alarm for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--lat", type=float, default=52.37)
    p.add_argument("--lon", type=float, default=4.89)
    p.add_argument("--interval", type=int, default=60, help="seconds between redraws (default: 60)")
    p.add_argument("--refresh", type=int, default=3600, help="seconds between data fetches (default: 3600)")
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
        body["led_notification_color"] = COLOR_AMBER
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


def _fetch_pollen(lat, lon):
    """Fetch pollen data from Open-Meteo. Returns dict of hourly arrays keyed by species key."""
    species_keys = [s[0] for s in SPECIES]
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(species_keys),
        "forecast_days": 2,
    }
    url = METEO_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "busybar-pollen-alarm/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", "ignore")
    parsed = json.loads(raw)
    hourly = parsed.get("hourly", {})
    return {
        "time": hourly.get("time", []),
        **{key: hourly.get(key, []) for key, _ in SPECIES},
    }


def _fake_data():
    """Test data: hours 0-3 GRAS laag (5), hours 4-9 GRAS rising, hours 10-23 BERK middel (25)."""
    now = datetime.now()
    base_hour = now.replace(minute=0, second=0, microsecond=0)
    times = []
    for i in range(48):
        h = base_hour.replace(hour=(base_hour.hour + i) % 24)
        if i >= 24:
            # second day -- simple offset
            from datetime import timedelta
            h = base_hour + timedelta(hours=i)
        times.append(h.strftime("%Y-%m-%dT%H:00"))

    grass_vals  = [5, 5, 5, 5, 30, 60, 90, 120, 90, 60] + [0] * 38
    birch_vals  = [0] * 10 + [25] * 38

    data = {"time": times}
    for key, _ in SPECIES:
        if key == "grass_pollen":
            data[key] = grass_vals
        elif key == "birch_pollen":
            data[key] = birch_vals
        else:
            data[key] = [0] * 48
    return data


# ---------------------------------------------------------------------------
# Pollen logic
# ---------------------------------------------------------------------------

def _level(value):
    if value is None or value < 20:
        return LEVEL_LAAG
    if value < 80:
        return LEVEL_MIDDEL
    return LEVEL_HOOG


def _level_color(level):
    if level == LEVEL_HOOG:
        return COLOR_HOOG
    return COLOR_MIDDEL


def _dominant(values_by_species):
    """Given dict {key: value}, return (key, value) for species with highest value."""
    best_key, best_val = None, -1
    for key, val in values_by_species.items():
        if val is not None and val > best_val:
            best_val = val
            best_key = key
    return best_key, best_val


def _build_24h(data):
    """Extract next 24 hours starting from current hour.

    Returns list of 24 dicts: {hour, dominant_label, dominant_value, level}.
    """
    now = datetime.now()
    current_hour_str = now.strftime("%Y-%m-%dT%H:00")
    times = data.get("time", [])

    # Find index of current hour
    start_idx = None
    for i, t in enumerate(times):
        if t == current_hour_str:
            start_idx = i
            break

    if start_idx is None:
        # Fallback: find nearest future hour
        for i, t in enumerate(times):
            if t >= current_hour_str:
                start_idx = i
                break

    if start_idx is None:
        return []

    slots = []
    for offset in range(24):
        idx = start_idx + offset
        if idx >= len(times):
            break
        t = times[idx]
        # Parse hour for display
        try:
            slot_hour = int(t[11:13])
        except (ValueError, IndexError):
            slot_hour = 0

        vals = {}
        for key, _ in SPECIES:
            arr = data.get(key, [])
            v = arr[idx] if idx < len(arr) else None
            vals[key] = v

        dom_key, dom_val = _dominant(vals)
        if dom_key is None or dom_val <= 0:
            dom_label = None
            dom_val = 0
        else:
            dom_label = dict(SPECIES)[dom_key]

        level = _level(dom_val)
        slots.append({
            "hour": slot_hour,
            "dominant_label": dom_label,
            "dominant_value": dom_val,
            "level": level,
        })

    return slots


def _build_headline(slots):
    """Return (text, level, slot_index) for the announced hour, or None if
    everything is laag. The level colors the headline; the index marks the
    matching bar in the timeline."""
    if not slots:
        return None
    current = slots[0]
    if current["level"] != LEVEL_LAAG:
        label = current["dominant_label"] or "POLLEN"
        return (f"{label} {current['level'].upper()}", current["level"], 0)
    # Current hour is laag -- find first non-laag hour
    for i, slot in enumerate(slots[1:], start=1):
        if slot["level"] != LEVEL_LAAG:
            label = slot["dominant_label"] or "POLLEN"
            text = f"{label} OM {slot['hour']:02d}:00"
            if len(text) > 15:  # long species names (AMBROSIA, BIJVOET) drop the OM to fit
                text = f"{label} {slot['hour']:02d}:00"
            return (text, slot["level"], i)
    return None


# ---------------------------------------------------------------------------
# Display building
# ---------------------------------------------------------------------------

def _rect(x, y, w, h, color):
    return {"type": "rectangle", "x": x, "y": y, "width": w, "height": h,
            "border_width": 0, "fill": "solid", "fill_colors": [color]}


def _grad(x, y, w, h, top, bottom):
    return {"type": "rectangle", "x": x, "y": y, "width": w, "height": h,
            "border_width": 0, "fill": "gradient_v", "fill_colors": [top, bottom]}


# Pollen specks drifting right to left through the free lane (rows 7-8):
# (start_x, row, step, color). The tick advances them every redraw.
DRIFT = [(4, 7, 3, "0xC89600FF"), (17, 8, 2, "0x8C6E00FF"), (31, 7, 4, "0x8C6E00FF"),
         (45, 8, 3, "0xC89600FF"), (58, 7, 2, "0x8C6E00FF"), (68, 8, 5, "0xC89600FF")]


def _drift(tick):
    return [_rect((sx - tick * step) % 72, row, 1, 1, color)
            for sx, row, step, color in DRIFT]


def _build_elements(headline, headline_level, marker_idx, slots, tick=0):
    elements = []

    # Flower icon (7x7): petal ring around a white heart, green stem with a
    # leaf. The petals breathe between amber and bright yellow every redraw.
    petal = COLOR_AMBER if tick % 2 == 0 else "0xFFE13CFF"
    elements += [
        _rect(2, 0, 3, 1, petal),                 # petals top
        _rect(1, 1, 1, 1, petal),                 # petal left
        _rect(5, 1, 1, 1, petal),                 # petal right
        _rect(2, 2, 3, 1, petal),                 # petals bottom
        _rect(3, 1, 1, 1, COLOR_WHITE),           # heart
        _rect(3, 3, 1, 4, COLOR_GREEN),           # stem
        _rect(1, 4, 2, 1, COLOR_GREEN),           # leaf
    ]

    # Drifting pollen specks in the free lane between text and chart
    elements += _drift(tick)

    # Headline always white; the bars carry the level colors
    elements.append({
        "type": "text",
        "text": headline,
        "x": 41,
        "y": 0,
        "font": "small",
        "color": COLOR_WHITE,
        "align": "top_mid",
    })

    # Quarter-day ticks in the gap columns so the 24h axis has anchor points
    for tick_slot in (6, 12, 18):
        elements.append(_rect(tick_slot * 3 - 1, 14, 1, 2, "0x5A5A5AFF"))

    # Timeline: 24 hourly slots, 2px wide bars with 1px gap, max height 7,
    # anchored at bottom (y=9..15), with a vertical gradient for depth and a
    # bright cap on the peak hour.
    peak_idx, peak_h = None, 0
    for slot_idx, slot in enumerate(slots[:24]):
        x = slot_idx * 3
        val = slot["dominant_value"]
        level = slot["level"]
        if level == LEVEL_LAAG:
            # Dim 1px baseline
            elements.append(_rect(x, 15, 2, 1, COLOR_BASELINE))
        else:
            h = max(1, min(7, round(val / 120 * 7)))
            if level == LEVEL_HOOG:
                top, bottom = "0xFF5A14FF", "0x781400FF"
            else:
                top, bottom = "0xFFD23CFF", "0x785000FF"
            elements.append(_grad(x, 16 - h, 2, h, top, bottom))
            if h > peak_h:
                peak_idx, peak_h = slot_idx, h
    if peak_idx is not None and peak_h >= 2:
        elements.append(_rect(peak_idx * 3, 16 - peak_h, 2, 1, "0xFFF0A0FF"))

    # White marker on the bottom row of the bar the headline points at
    if marker_idx is not None and 0 <= marker_idx < 24:
        elements.append(_rect(marker_idx * 3, 15, 2, 1, COLOR_WHITE))

    return elements


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    data = {}
    last_fetch = 0.0
    last_clear_sent = False
    first_alert = True
    tick = 0

    if args.test:
        data = _fake_data()
        slots = _build_24h(data)
        result = _build_headline(slots)
        if result is None:
            print("test: pollen laag in alle slots -- niets te tekenen")
            sys.exit(0)
        text, level, marker = result
        elements = _build_elements(text, level, marker, slots, tick=0)
        status, body = _draw(args.host, elements, led_notification=True)
        print(f"drew: '{text}' ({level}, marker slot {marker}), {len(elements)} elements (status {status})")
        sys.exit(0)

    print(f"pollen-alarm -> {_base(args.host)}  (Ctrl-C to stop)")

    try:
        while True:
            now = time.monotonic()

            # Fetch / refresh
            if now - last_fetch >= args.refresh or last_fetch == 0.0:
                try:
                    data = _fetch_pollen(args.lat, args.lon)
                    last_fetch = now
                except Exception as e:
                    print(f"fetch error (reusing previous data): {e}")
                    if last_fetch == 0.0:
                        last_fetch = now  # avoid hammering on boot error

            slots = _build_24h(data)
            any_alert = any(s["level"] != LEVEL_LAAG for s in slots)
            result = _build_headline(slots)

            if not any_alert or result is None:
                if not last_clear_sent:
                    try:
                        _clear(args.host)
                        print("pollen laag, scherm vrijgegeven")
                    except Exception as e:
                        print(f"clear error: {e}")
                    last_clear_sent = True
                    first_alert = True
            else:
                text, level, marker = result
                elements = _build_elements(text, level, marker, slots, tick=tick)
                notify = first_alert
                try:
                    status, body = _draw(args.host, elements, led_notification=notify)
                    if status == 409:
                        print(f"display busy (409), retrying next cycle")
                    else:
                        if notify:
                            first_alert = False
                        last_clear_sent = False
                        print(f"drew: '{text}' ({len(elements)} elements)")
                except Exception as e:
                    print(f"draw error: {e}")

            tick += 1
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\nstopped.")
        try:
            _clear(args.host)
        except Exception:
            pass


if __name__ == "__main__":
    main()
