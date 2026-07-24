#!/usr/bin/env python3
"""Tide Clock: alternates between a big clock and the next tide event
(high/low, height, and time) for any NOAA Tides & Currents station.

    python app.py                                  # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080             # emulator or a Wi-Fi bar
    python app.py --station 8518750                 # a different NOAA station (default: Charleston, SC)
"""
import datetime
import json
import sys
import time
import urllib.error
import urllib.request

APP = "daninsc.tideclock"

# ---------------------------------------------------------------------------
# BUSY Bar HTTP API — self-contained, stdlib only.
# Over USB the bar is always at 10.0.4.20; --host targets a Wi-Fi bar or the
# emulator. Full API docs are served by the device: http://10.0.4.20/docs
# ---------------------------------------------------------------------------

def _arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

BASE = "http://" + _arg("--host", "10.0.4.20").replace("http://", "").rstrip("/")
STATION = _arg("--station", "8665530")  # NOAA station ID; default is Charleston Harbor, SC


def draw(elements, **extra):
    body = {"application_name": APP, "elements": elements, **extra}
    req = urllib.request.Request(BASE + "/api/display/draw",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5):
        pass


def text(txt, x=0, y=0, font="normal", color="0xFFFFFFFF", **kw):
    return {"type": "text", "text": str(txt), "x": x, "y": y, "font": font, "color": color, **kw}


# ---------------------------------------------------------------------------
# Tide data (NOAA CO-OPS, no API key required)
# ---------------------------------------------------------------------------

def fetch_next_tide():
    """Returns (kind, height_ft, when) for the next high/low tide, or None
    on any fetch/parse failure (caller just skips the tide frame that tick)."""
    today = datetime.date.today().strftime("%Y%m%d")
    url = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        f"?product=predictions&application=busybar_tideclock&begin_date={today}"
        f"&range=48&datum=MLLW&station={STATION}&time_zone=lst_ldt"
        "&units=english&interval=hilo&format=json"
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.load(r)
    except (urllib.error.URLError, ValueError):
        return None

    predictions = data.get("predictions", [])
    now = datetime.datetime.now()
    for p in predictions:
        when = datetime.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
        if when >= now:
            kind = "HIGH" if p["type"] == "H" else "LOW"
            return kind, float(p["v"]), when
    return None


_tide_cache = {"value": None, "fetched_at": 0.0}
TIDE_REFRESH_SECONDS = 15 * 60  # NOAA predictions don't change minute to minute


def get_next_tide():
    now = time.time()
    if now - _tide_cache["fetched_at"] > TIDE_REFRESH_SECONDS or _tide_cache["value"] is None:
        _tide_cache["value"] = fetch_next_tide()
        _tide_cache["fetched_at"] = now
    return _tide_cache["value"]


# ---------------------------------------------------------------------------
# App: alternate between clock (5s) and next-tide (5s)
# ---------------------------------------------------------------------------

CYCLE_SECONDS = 10
CLOCK_SECONDS = 5


def tick():
    phase = int(time.time()) % CYCLE_SECONDS
    if phase < CLOCK_SECONDS:
        hhmm = time.strftime("%H:%M:%S", time.localtime())
        elements = [text(hhmm, x=36, y=15, font="extra_large", align="bottom_mid")]
    else:
        tide = get_next_tide()
        if tide is None:
            elements = [text("TIDE N/A", x=36, y=8, font="normal", align="center")]
        else:
            kind, height, when = tide
            elements = [
                text(f"{kind} {height:.1f}ft", x=36, y=1, font="normal",
                     color="0x2B7FFFFF", align="top_mid"),
                text(when.strftime("%-I:%M %p"), x=36, y=9, font="normal",
                     color="0xFFFFFFFF", align="top_mid"),
            ]

    try:
        draw(elements)
    except urllib.error.HTTPError as e:
        # A higher-priority app owns the screen: keep ticking and retry next second.
        if e.code != 409:
            raise
        print("display busy (409), retrying...")


if __name__ == "__main__":
    print(f"tide clock (station {STATION}) → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            tick()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} — {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {BASE} — {e.reason}")
