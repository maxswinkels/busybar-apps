#!/usr/bin/env python3
"""BUSY Bar weather forecast.

Shows current weather and the next days on a 72x16 BUSY Bar display.
Weather data comes from Open-Meteo and requires no API key.

Examples:
    python3 weather_forecast.py
    python3 weather_forecast.py --city Roma --lat 41.9028 --lon 12.4964
    python3 weather_forecast.py --host 127.0.0.1:8080 --days 4
    python3 weather_forecast.py --unit fahrenheit
    python3 weather_forecast.py --lang en
    python3 weather_forecast.py --test

Default location is Rome, Italy. Change --city/--lat/--lon for your location.
"""

import argparse
import datetime as dt
import json
import math
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "weather-forecast"
W, H = 72, 16
DEFAULT_HOST = "10.0.4.20"
DEFAULT_CITY = "Roma"
DEFAULT_LAT = 41.9028
DEFAULT_LON = 12.4964
DEFAULT_REFRESH = 15 * 60
DEFAULT_DWELL = 6

WHITE = "#FFFFFFFF"
YELLOW = "#FFD54FFF"
CYAN = "#64D8FFFF"
BLUE = "#42A5F5FF"
GRAY = "#B0BEC5FF"

LANGUAGES = {

    "en": {
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "today": "Today", "tomorrow": "Tomorrow", "wind": "Wind", "rain": "Rain",
        "weather": {
            "sun": "Clear", "partly": "Partly cloudy", "cloud": "Cloudy",
            "fog": "Fog", "rain": "Rain", "snow": "Snow",
        },
    },
    "it": {
        "weekdays": ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"],
        "today": "Oggi", "tomorrow": "Domani", "wind": "Vento", "rain": "Pioggia",
        "weather": {
            "sun": "Sereno", "partly": "Poco nuvoloso", "cloud": "Nuvoloso",
            "fog": "Nebbia", "rain": "Pioggia", "snow": "Neve",
        },
    },
    "de": {
        "weekdays": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
        "today": "Heute", "tomorrow": "Morgen", "wind": "Wind", "rain": "Regen",
        "weather": {
            "sun": "Klar", "partly": "Teilw. bewoelkt", "cloud": "Bewoelkt",
            "fog": "Nebel", "rain": "Regen", "snow": "Schnee",
        },
    },
    "fr": {
        "weekdays": ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"],
        "today": "Auj.", "tomorrow": "Demain", "wind": "Vent", "rain": "Pluie",
        "weather": {
            "sun": "Clair", "partly": "Peu nuageux", "cloud": "Nuageux",
            "fog": "Brouillard", "rain": "Pluie", "snow": "Neige",
        },
    },
    "es": {
        "weekdays": ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"],
        "today": "Hoy", "tomorrow": "Manana", "wind": "Viento", "rain": "Lluvia",
        "weather": {
            "sun": "Despejado", "partly": "Poco nuboso", "cloud": "Nublado",
            "fog": "Niebla", "rain": "Lluvia", "snow": "Nieve",
        },
    },
}


def parse_args():
    p = argparse.ArgumentParser(description="Weather forecast for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"BUSY Bar host (default: {DEFAULT_HOST})")
    p.add_argument("--city", default=DEFAULT_CITY,
                   help=f"display city name (default: {DEFAULT_CITY})")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT,
                   help=f"latitude (default: {DEFAULT_LAT})")
    p.add_argument("--lon", type=float, default=DEFAULT_LON,
                   help=f"longitude (default: {DEFAULT_LON})")
    p.add_argument("--days", type=int, default=4,
                   help="number of forecast days including today, 1-7 (default: 4)")
    p.add_argument("--refresh", type=int, default=DEFAULT_REFRESH,
                   help=f"weather refresh seconds (default: {DEFAULT_REFRESH})")
    p.add_argument("--dwell", type=float, default=DEFAULT_DWELL,
                   help=f"seconds per screen (default: {DEFAULT_DWELL})")
    p.add_argument("--unit", choices=["celsius", "fahrenheit"], default="celsius",
                   help="temperature unit (default: celsius)")
    p.add_argument("--lang", choices=sorted(LANGUAGES), default="en",
                   help="display language: it, en, de, fr, es (default: en)")
    p.add_argument("--priority", type=int, default=30,
                   help="BUSY Bar display priority (default: 30)")
    p.add_argument("--test", action="store_true",
                   help="fetch weather, print it, draw one cycle, then exit")
    return p.parse_args()


def base_url(host):
    host = host.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return "http://" + host


def http_json(url, params, timeout=10):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url + "?" + query,
        headers={"User-Agent": "busybar-weather-forecast/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def weather_kind(code):
    if code == 0:
        return "sun"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
        return "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    return "cloud"


def weather_label(code, lang="en"):
    kind = weather_kind(code)
    return LANGUAGES[lang]["weather"][kind]


def fetch_weather(lat, lon, days, unit):
    payload = http_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit" if unit == "fahrenheit" else "celsius",
            "wind_speed_unit": "kmh",
            "timezone": "auto",
            "forecast_days": max(1, min(7, days)),
        },
    )

    cur = payload["current"]
    daily = payload["daily"]
    out_days = []
    for i, date_s in enumerate(daily["time"]):
        date = dt.date.fromisoformat(date_s)
        out_days.append({
            "date": date,
            "code": int(daily["weather_code"][i]),
            "high": round(daily["temperature_2m_max"][i]),
            "low": round(daily["temperature_2m_min"][i]),
            "rain": round(daily["precipitation_probability_max"][i] or 0),
        })

    return {
        "current": {
            "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]),
            "code": int(cur["weather_code"]),
            "wind": round(cur["wind_speed_10m"]),
        },
        "days": out_days,
    }


# ---------------------------------------------------------------------------
# Minimal 16x16 weather icons, generated as PNG with stdlib only.
# ---------------------------------------------------------------------------

BLACK = (0, 0, 0)
ICON_COLORS = {
    "sun": (255, 205, 55),
    "cloud": (180, 195, 205),
    "partly": (255, 205, 55),
    "rain": (80, 170, 255),
    "snow": (220, 245, 255),
    "fog": (150, 165, 175),
}


def blank_pixels():
    return [BLACK] * (16 * 16)


def px(buf, x, y, c):
    if 0 <= x < 16 and 0 <= y < 16:
        buf[y * 16 + x] = c


def rect(buf, x, y, w, h, c):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            px(buf, xx, yy, c)


def circle(buf, cx, cy, r, c):
    rr = r * r
    for y in range(cy - r, cy + r + 1):
        for x in range(cx - r, cx + r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= rr:
                px(buf, x, y, c)


def draw_cloud(buf, xoff=0, yoff=0, c=(180, 195, 205)):
    circle(buf, 6 + xoff, 8 + yoff, 3, c)
    circle(buf, 10 + xoff, 7 + yoff, 4, c)
    rect(buf, 3 + xoff, 8 + yoff, 11, 4, c)


def build_icon(kind):
    b = blank_pixels()
    if kind == "sun":
        c = ICON_COLORS["sun"]
        circle(b, 8, 8, 4, c)
        for x, y in ((8,1),(8,15),(1,8),(15,8),(3,3),(13,3),(3,13),(13,13)):
            px(b, x, y, c)
    elif kind == "cloud":
        draw_cloud(b)
    elif kind == "partly":
        sun = ICON_COLORS["sun"]
        circle(b, 5, 5, 3, sun)
        draw_cloud(b, 1, 2)
    elif kind == "rain":
        draw_cloud(b, 0, -2)
        c = ICON_COLORS["rain"]
        for x in (5, 9, 13):
            px(b, x, 12, c)
            px(b, x - 1, 13, c)
            px(b, x - 1, 14, c)
    elif kind == "snow":
        draw_cloud(b, 0, -2)
        c = ICON_COLORS["snow"]
        for x in (5, 10, 14):
            px(b, x, 12, c); px(b, x-1, 13, c); px(b, x+1, 13, c); px(b, x, 14, c)
    elif kind == "fog":
        c = ICON_COLORS["fog"]
        for y in (5, 8, 11):
            rect(b, 2, y, 12, 1, c)
    return b


def png_bytes(pixels, width=16, height=16):
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            r, g, b = pixels[y * width + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def api_request(base, method, path, body=None, content_type=None, timeout=8):
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upload_icons(base):
    files = {}
    for kind in ("sun", "partly", "cloud", "fog", "rain", "snow"):
        name = f"weather_{kind}.png"
        qs = urllib.parse.urlencode({"application_name": APP, "file": name})
        status, body = api_request(
            base, "POST", "/api/assets/upload?" + qs,
            body=png_bytes(build_icon(kind)),
            content_type="application/octet-stream",
        )
        if status not in (200, 201, 204):
            raise RuntimeError(f"icon upload {kind}: HTTP {status} {body[:120]!r}")
        files[kind] = name
    return files


def text_el(el_id, text, x, y, font="small", color=WHITE, align="top_left", width=None):
    e = {
        "id": el_id,
        "type": "text",
        "text": str(text),
        "x": x,
        "y": y,
        "font": font,
        "color": color,
        "align": align,
    }
    if width is not None:
        e.update({
            "width": width,
            "scroll_rate": 360,
            "scroll_start_delay": 700,
            "scroll_repeat_delay": 1200,
        })
    return e


def image_el(el_id, path):
    return {"id": el_id, "type": "image", "path": path, "x": 0, "y": 0}


def busy_draw(base, elements, priority):
    payload = json.dumps({
        "application_name": APP,
        "priority": priority,
        "elements": elements,
    }).encode("utf-8")
    status, body = api_request(base, "POST", "/api/display/draw", payload, "application/json")
    if status == 409:
        return False
    if status not in (200, 201, 204):
        raise RuntimeError(f"BUSY Bar draw HTTP {status}: {body[:200]!r}")
    return True


def busy_clear(base):
    qs = urllib.parse.urlencode({"application_name": APP})
    try:
        api_request(base, "DELETE", "/api/display/draw?" + qs)
    except Exception:
        pass


def temp_suffix(unit):
    return "F" if unit == "fahrenheit" else "C"


def current_elements(data, city, icons, unit, lang):
    c = data["current"]
    kind = weather_kind(c["code"])
    suffix = temp_suffix(unit)
    top = f"{city} {c['temp']}°{suffix}"
    tr = LANGUAGES[lang]
    bottom = f"{weather_label(c['code'], lang)}  {tr['wind']} {c['wind']}km/h"
    return [
        image_el("icon", icons[kind]),
        text_el("top", top, 18, 0, font="small", color=YELLOW, width=54),
        text_el("bottom", bottom, 18, 8, font="small", color=CYAN, width=54),
    ]


def forecast_elements(day, index, icons, unit, lang):
    kind = weather_kind(day["code"])
    suffix = temp_suffix(unit)
    tr = LANGUAGES[lang]
    if index == 0:
        day_name = tr["today"]
    elif index == 1:
        day_name = tr["tomorrow"]
    else:
        day_name = tr["weekdays"][day["date"].weekday()]
    top = f"{day_name} {day['high']}°/{day['low']}°{suffix}"
    bottom = f"{weather_label(day['code'], lang)} | {tr['rain']} {day['rain']}%"
    color = BLUE if kind in ("rain", "snow") else GRAY
    return [
        image_el("icon", icons[kind]),
        text_el("top", top, 18, 0, font="small", color=WHITE, width=54),
        text_el("bottom", bottom, 18, 8, font="small", color=color, width=54),
    ]


def print_weather(data, city, unit, lang):
    suffix = temp_suffix(unit)
    c = data["current"]
    tr = LANGUAGES[lang]
    print(f"{city}: {c['temp']}°{suffix}, {weather_label(c['code'], lang)}, {tr['wind'].lower()} {c['wind']} km/h")
    for i, d in enumerate(data["days"]):
        label = tr["today"] if i == 0 else tr["tomorrow"] if i == 1 else tr["weekdays"][d["date"].weekday()]
        print(f"  {label}: {d['low']}..{d['high']}°{suffix}, {weather_label(d['code'], lang)}, {tr['rain'].lower()} {d['rain']}%")


def main():
    args = parse_args()
    args.days = max(1, min(7, args.days))
    args.refresh = max(60, args.refresh)
    args.dwell = max(1.0, args.dwell)
    base = base_url(args.host)

    print(f"weather-forecast -> {base}")
    print(f"location: {args.city} ({args.lat:.4f}, {args.lon:.4f})")
    print("Ctrl-C to stop.")

    try:
        icons = upload_icons(base)
    except Exception as e:
        sys.exit(f"BUSY Bar setup failed: {e}")

    data = None
    last_fetch = 0.0

    try:
        while True:
            now = time.monotonic()
            if data is None or now - last_fetch >= args.refresh:
                try:
                    fresh = fetch_weather(args.lat, args.lon, args.days, args.unit)
                    data = fresh
                    last_fetch = now
                    print_weather(data, args.city, args.unit, args.lang)
                except Exception as e:
                    print(f"weather fetch failed: {e}")
                    if data is None:
                        time.sleep(10)
                        continue

            screens = [current_elements(data, args.city, icons, args.unit, args.lang)]
            screens.extend(forecast_elements(day, i, icons, args.unit, args.lang)
                           for i, day in enumerate(data["days"]))

            for elements in screens:
                try:
                    drawn = busy_draw(base, elements, args.priority)
                    if not drawn:
                        print("display busy (409), retrying next screen")
                except Exception as e:
                    print(f"draw failed: {e}")
                time.sleep(args.dwell)

            if args.test:
                break

    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        busy_clear(base)


if __name__ == "__main__":
    main()
