#!/usr/bin/env python3
"""Live next-train departures for any NYC subway station on a BUSY Bar.

Pick a station, direction, and (optionally) lines; the 72x16 front display
shows the next train as an MTA route bullet (15x15 shaded disk, letter or
number baked in), minutes to departure in extra-large type, and position
dots down the right edge. When the shown train departs, a full-screen wipe
in the line's color sweeps through with the route letter, then the next
train slides in. Live GTFS-realtime data straight from the MTA — no API key.

Works for the whole system: 1-7, A-Z letters, shuttles, and the SIR. Route
bullets and departure animations are GENERATED at startup for exactly the
routes your station serves (official MTA line colors, glyphs from the BUSY
Bar's own fonts) and uploaded once — nothing is redrawn per frame; the
departure flash is a compiled .anim the device plays at 60fps.

Configuration (env vars, or the mirrored CLI flags which take precedence —
in busybar-manager, put these in a variation's "Environment variables"):

    STATION     station name, fuzzily matched ("Canal St", "bedford av",
                "times sq"). All platforms of same-named stations merge.
    DIRECTION   N | S | uptown | downtown | a label like "Brooklyn"
    ROUTES      optional comma filter ("N,Q") — otherwise every route the
                station serves
    STOPS       advanced: exact GTFS platform ids ("Q01N,R23N"), overrides
                STATION/DIRECTION

    BUSYBAR_TARGET       auto | usb | wifi | cloud   (default auto)
    BUSYBAR_CLOUD_TOKEN  API token for the cloud target (cloud.busy.app)
    BUSYBAR_WIFI_URL     default http://172.16.105.41
    BUSYBAR_WIFI_TOKEN   default 8888
    BUSYBAR_PRIORITY     draw priority, default 1 (visible when the Bar's
                         switch is on OFF; set 30+ to show over the clock)
    BUSYBAR_APP_NAME     application_name override, default "nyc-subway"
    BUSYBAR_WS           dial stream override: a ws:// URI for the Bar's
                         status socket. The Bar only serves it on USB, so
                         when the app runs elsewhere (busybar-manager, a
                         VPS, the cloud target) forward the USB port over
                         your tailnet/VPN — see tools/dial_forward.py —
                         and point this at it, e.g.
                         ws://100.x.y.z:8760/api/status/ws

With no configuration at all it shows uptown departures at Times Sq-42 St.

Usage:
    python app.py                        # run forever
    python app.py --station "Canal St" --direction uptown --routes N,Q
    python app.py --list-stations canal  # find names, platform ids, labels
    python app.py --demo                 # fake-data departure demo
    python app.py --clear                # clear the display and exit

Dial: over USB the Bar's dial scrolls through upcoming arrivals (needs the
optional `websockets` package); other transports show the next train unless
BUSYBAR_WS points them at a forwarded copy of the USB status socket.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import struct
import sys
import time
import zlib

requests = None  # imported lazily in main() — keeps `--help` stdlib-only for
#                  the busybar-manager options probe (it runs the system python)

USB_URL = "http://10.0.4.20"
WIFI_URL = os.environ.get("BUSYBAR_WIFI_URL", "http://172.16.105.41")
WIFI_TOKEN = os.environ.get("BUSYBAR_WIFI_TOKEN", "8888")
CLOUD_URL = "https://api.busy.app"
CLOUD_TOKEN = os.environ.get("BUSYBAR_CLOUD_TOKEN", "")
TARGET = os.environ.get("BUSYBAR_TARGET", "auto")
try:
    PRIORITY = int(os.environ.get("BUSYBAR_PRIORITY", "1"))
except ValueError:
    PRIORITY = 1
APP_NAME = os.environ.get("BUSYBAR_APP_NAME", "nyc-subway")
WS_OVERRIDE = os.environ.get("BUSYBAR_WS", "")

WHITE = "#FFFFFFFF"

FETCH_SECS = 30          # MTA poll interval
TICK_SECS = 2            # supervisor cadence for minute flips
BLOCKED_RETRY_SECS = 3   # draw-attempt cadence while another app owns screen
IDLE_RESET_SECS = 25     # snap back to the next train after dial inactivity
ELEMENT_TIMEOUT = 90     # stale elements self-erase if we stop pushing
MAX_ARRIVALS = 8         # 8 position dots x 2px = the 16px display height
FRAME_SECS = 0.04        # target animation frame interval (local transports)
SLIDE_OUT = (-1, -2, -4, -7, -11, -16)   # eased: accelerate off-screen
SLIDE_IN = (11, 7, 4, 2, 1, 0)           # eased: decelerate into place
# Departure flash: sweep-in, hold, fade-to-black — compiled to a .anim the
# device plays at 60fps, so it is smooth even over the cloud relay.
FLASH_FRAMES = (15, 84, 12)  # sweep, hold, fade — the single source of truth
FPS = 60
FLASH_ANIM_SECS = sum(FLASH_FRAMES) / FPS

# ------------------------------------------------------------------- routes

FEED_BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"
# GTFS-RT feed grouping. Routes not listed ride the main feed (1-7, GS, 42 St
# shuttle). The generic "S" config expands to all three shuttle feeds.
FEED_SUFFIX = {
    "A": "-ace", "C": "-ace", "E": "-ace", "H": "-ace",
    "B": "-bdfm", "D": "-bdfm", "F": "-bdfm", "FX": "-bdfm", "FS": "-bdfm",
    "G": "-g", "J": "-jz", "Z": "-jz",
    "N": "-nqrw", "Q": "-nqrw", "R": "-nqrw", "W": "-nqrw",
    "L": "-l", "SI": "-si", "SIR": "-si",
}

# designator -> (official MTA line color, letter is black, palette override)
# Letters are white on every line except the yellow N/Q/R/W (MTA convention);
# G keeps the hand-tuned black-letter look this app inherited.
DESIGNATOR_META = {
    "1": ("#EE352E", False, None), "2": ("#EE352E", False, None),
    "3": ("#EE352E", False, None),
    "4": ("#00933C", False, None), "5": ("#00933C", False, None),
    "6": ("#00933C", False, None),
    "7": ("#B933AD", False, None),
    "A": ("#0039A6", False, None), "C": ("#0039A6", False, None),
    "E": ("#0039A6", False, None),
    "B": ("#FF6319", False, None), "D": ("#FF6319", False, None),
    "F": ("#FF6319", False, None), "M": ("#FF6319", False, None),
    "G": ("#6CBE45", True, "green"),
    "J": ("#996633", False, None), "Z": ("#996633", False, None),
    "L": ("#A7A9AC", False, None),
    "N": ("#FCC30B", True, "yellow"), "Q": ("#FCC30B", True, "yellow"),
    "R": ("#FCC30B", True, "yellow"), "W": ("#FCC30B", True, "yellow"),
    "S": ("#808183", False, None),
    "SIR": ("#0039A6", False, None),
}

# hand-tuned ramps carried over from the original canal/g apps (the derived
# formula below approximates these; the originals stay exact)
PALETTES = {
    "yellow": {"spec": (255, 239, 142), "top": (255, 222, 30),
               "bot": (110, 95, 13), "lift": (148, 111, 18),
               "bullet_bot": (168, 136, 11)},
    "green": {"spec": (190, 237, 167), "top": (124, 219, 79),
              "bot": (53, 94, 34), "lift": (71, 123, 46),
              "bullet_bot": (69, 121, 44)},
}


def designator(route_id):
    """Collapse a GTFS route_id onto the bullet we draw for it."""
    r = route_id.upper()
    if r in ("GS", "FS", "H"):
        return "S"
    if r in ("SI", "SIR"):
        return "SIR"
    if r.endswith("X") and r[:-1] in DESIGNATOR_META:
        return r[:-1]  # 6X/7X express diamonds share the local's art
    return r


def letter_for(desig):
    return "S" if desig == "SIR" else desig


def feeds_for(route_ids):
    urls = []
    for r in route_ids:
        r = r.upper()
        if r == "S":  # generic shuttle: could live in any of three feeds
            cands = [FEED_BASE, FEED_BASE + "-ace", FEED_BASE + "-bdfm"]
        else:
            cands = [FEED_BASE + FEED_SUFFIX.get(r, "")]
        for u in cands:
            if u not in urls:
                urls.append(u)
    return urls


# --- BEGIN GENERATED: STATIONS ---
# generated by tools/build_stations.py — do not edit by hand
STATIONS_B64 = (
    "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"
    "1d/Af/8H48SCtg=="
)
# --- END GENERATED: STATIONS ---

# --- BEGIN GENERATED: GLYPHS ---
# generated by tools/build_glyphs.py — do not edit by hand
BULLET_GLYPHS = {
    "0": [".####.", "##..##", "##.###", "###.##", "##..##", "##..##", ".####."],
    "1": ["..##..", ".###..", "####..", "..##..", "..##..", "..##..", "######"],
    "2": [".####.", "##..##", "....##", "..###.", ".##...", "##....", "######"],
    "3": [".####.", "##..##", "....##", "..###.", "....##", "##..##", ".####."],
    "4": ["..###.", ".####.", ".#.##.", "##.##.", "##.##.", "######", "...##."],
    "5": ["######", "##....", "##....", "#####.", "....##", "....##", "#####."],
    "6": [".####.", "##..##", "##....", "#####.", "##..##", "##..##", ".####."],
    "7": ["######", "....##", "...##.", "..##..", "..##..", ".##...", ".##..."],
    "8": [".####.", "##..##", "##..##", ".####.", "##..##", "##..##", ".####."],
    "9": [".####.", "##..##", "##..##", ".#####", "....##", "##..##", ".####."],
    "A": ["..##..", "..##..", ".####.", ".#.##.", ".####.", "##..##", "##..##"],
    "B": ["#####.", "##..##", "##..##", "#####.", "##..##", "##..##", "#####."],
    "C": [".####.", "##..##", "##....", "##....", "##....", "##..##", ".####."],
    "D": ["####..", "##.##.", "##..##", "##..##", "##..##", "##.##.", "####.."],
    "E": ["#####", "##...", "##...", "####.", "##...", "##...", "#####"],
    "F": ["#####", "##...", "##...", "####.", "##...", "##...", "##..."],
    "G": [".####.", "##..##", "##....", "##.###", "##..##", "##..##", ".###.#"],
    "H": ["##..##", "##..##", "##..##", "######", "##..##", "##..##", "##..##"],
    "I": ["####", ".##.", ".##.", ".##.", ".##.", ".##.", "####"],
    "J": ["...##", "...##", "...##", "...##", "##.##", "##.##", ".###."],
    "K": ["##..##", "##.##.", "####..", "###...", "####..", "##.##.", "##..##"],
    "L": ["##...", "##...", "##...", "##...", "##...", "##...", "#####"],
    "M": ["##.....##", "###...###", "####.####", "#########", "##.###.##", "##..#..##", "##.....##"],
    "N": ["##...##", "###..##", "####.##", "#######", "##.####", "##..###", "##...##"],
    "O": [".####.", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
    "P": ["#####.", "##..##", "##..##", "#####.", "##....", "##....", "##...."],
    "Q": [".####.", "##..##", "##..##", "##..##", "##.#.#", "##..#.", ".###.#"],
    "R": ["#####.", "##..##", "##..##", "#####.", "##..##", "##..##", "##..##"],
    "S": [".####.", "##..##", "##....", ".####.", "....##", "##..##", ".####."],
    "T": ["######", "..##..", "..##..", "..##..", "..##..", "..##..", "..##.."],
    "U": ["##..##", "##..##", "##..##", "##..##", "##..##", "##..##", ".####."],
    "V": ["##...##", "##...##", "##...##", ".##.##.", ".##.##.", "..###..", "..###.."],
    "W": ["##......##", "##..##..##", "##..##..##", "##.####.##", "##########", ".###..###.", ".##....##."],
    "X": ["##..##", "##..##", ".####.", "..##..", ".####.", "##..##", "##..##"],
    "Y": ["##..##", "##..##", ".####.", ".####.", "..##..", "..##..", "..##.."],
    "Z": ["######", "....##", "...##.", "..##..", ".##...", "##....", "######"],
}
XL_GLYPHS = {
    "0": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "1": [".##.", "###.", "###.", ".##.", ".##.", ".##.", ".##.", ".##.", "####", "####"],
    "2": [".#####.", "#######", "##...##", "....###", "...###.", "..###..", ".###...", "###....", "#######", "#######"],
    "3": [".#####.", "#######", "##...##", ".....##", "...###.", "...####", ".....##", "##...##", "#######", ".#####."],
    "4": ["..####.", ".#####.", ".##.##.", "###.##.", "##..##.", "##..##.", "##..##.", "#######", "#######", "....##."],
    "5": ["#######", "#######", "##.....", "##.....", "######.", "#######", ".....##", "##...##", "#######", ".#####."],
    "6": [".#####.", "#######", "##...##", "##.....", "######.", "#######", "##...##", "##...##", "#######", ".#####."],
    "7": ["#######", "#######", ".....##", "....##.", "...###.", "...##..", "..##...", "..##...", "..##...", "..##..."],
    "8": [".#####.", "#######", "##...##", "##...##", ".#####.", "#######", "##...##", "##...##", "#######", ".#####."],
    "9": [".#####.", "#######", "##...##", "##...##", "#######", ".######", ".....##", "##...##", "#######", ".#####."],
    "A": [".#####.", "#######", "##...##", "##...##", "#######", "#######", "##...##", "##...##", "##...##", "##...##"],
    "B": ["######.", "#######", "##...##", "##...##", "######.", "#######", "##...##", "##...##", "#######", "######."],
    "C": [".#####.", "#######", "##...##", "##.....", "##.....", "##.....", "##.....", "##...##", "#######", ".#####."],
    "D": ["#####..", "######.", "##..###", "##...##", "##...##", "##...##", "##...##", "##..###", "######.", "#####.."],
    "E": ["######", "######", "##....", "##....", "#####.", "#####.", "##....", "##....", "######", "######"],
    "F": ["######", "######", "##....", "##....", "#####.", "#####.", "##....", "##....", "##....", "##...."],
    "G": [".#####.", "#######", "##...##", "##.....", "##..###", "##..###", "##...##", "##...##", "#######", ".#####."],
    "H": ["##...##", "##...##", "##...##", "##...##", "#######", "#######", "##...##", "##...##", "##...##", "##...##"],
    "I": ["####", "####", ".##.", ".##.", ".##.", ".##.", ".##.", ".##.", "####", "####"],
    "J": ["....##", "....##", "....##", "....##", "....##", "....##", "##..##", "##..##", "######", ".####."],
    "K": ["##...##", "##...##", "##..###", "##.###.", "#####..", "######.", "##..###", "##...##", "##...##", "##...##"],
    "L": ["##....", "##....", "##....", "##....", "##....", "##....", "##....", "##....", "######", "######"],
    "M": ["##....##", "##....##", "###..###", "########", "########", "##.##.##", "##....##", "##....##", "##....##", "##....##"],
    "N": ["##...##", "##...##", "###..##", "####.##", "#######", "##.####", "##..###", "##...##", "##...##", "##...##"],
    "O": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "P": ["######.", "#######", "##...##", "##...##", "#######", "######.", "##.....", "##.....", "##.....", "##....."],
    "Q": [".#####.", "#######", "##...##", "##...##", "##...##", "##...##", "##.####", "##.###.", "#######", ".###.##"],
    "R": ["######.", "#######", "##...##", "##...##", "#######", "######.", "##...##", "##...##", "##...##", "##...##"],
    "S": [".#####.", "#######", "##...##", "##.....", "######.", ".######", ".....##", "##...##", "#######", ".#####."],
    "T": ["######", "######", "..##..", "..##..", "..##..", "..##..", "..##..", "..##..", "..##..", "..##.."],
    "U": ["##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "##...##", "#######", ".#####."],
    "V": ["##....##", "##....##", "##....##", "###..##.", ".##..##.", ".##..##.", ".##..##.", ".##..##.", "..####..", "..####.."],
    "W": ["##....##", "##....##", "##....##", "##.##.##", "##.##.##", "##.##.##", "########", "########", ".##..##.", ".##..##."],
    "X": ["##...##", "##...##", "##...##", "###.###", ".#####.", ".#####.", "###.###", "##...##", "##...##", "##...##"],
    "Y": ["##...##", "##...##", "##...##", "##...##", "#######", ".######", ".....##", "##...##", "#######", ".#####."],
    "Z": ["######", "######", "....##", "...###", "..###.", ".###..", "###...", "##....", "######", "######"],
}
DISK_MASK = [".....#####.....", "...#########...", "..###########..", ".#############.", ".#############.", "###############", "###############", "###############", "###############", "###############", ".#############.", ".#############.", "..###########..", "...#########...", ".....#####....."]
BULLET_OVERRIDES = {
    "G": base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAA60lEQVR4nGNkwAL2vV3+"
        "H13MSTiSEV2MBZumXc+X4jTQCckQOKPkrBeGbbhAj/E2sD4wUXDMg2iNMDDBagcj2Nl/"
        "fv5lQAZTHHczoIOc/a4YYowZO5xQbJ3hsQ9MZ+xwwisGAix/fv5jwAaQxVM2OmCIEa0Z"
        "F2D5i+ZfGEAWXxx7FM6OXWwNZzOBAgsZI2xGiEXOscAqzrQ85QQjyIkwjOxsGF6ecgJD"
        "fHnKCUZwPAf2maCE+PqiMxjeCOwzQeGvLzoD0QwCvu2GRCeUzZXnESkMBjwb9AgasL3h"
        "EmbaRgaulToYhuxuv4KhFgDuTou5PYUPAAAAAABJRU5ErkJggg=="
    ),
    "N": base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAA6klEQVR4nGNkwAL+v+/7"
        "jy7GKFjEiC7Ggk3Tr5f9OA1kRDIEzvh8SRrDNlyAV+8pWB+YeHNGimiNMCBi8owR7Oxf"
        "PxF6payfg+lnRyXxioEA04P94v9//frHAMPIGtDFfiGpA+lj+vXrPwMyRgYKji9RxH6h"
        "qWVBNhkbUHN/hWIzMmD6+es/AzKGgbNrhRnQwU80tTidDWIfXy6EovkXmlomy8h3jNgC"
        "DMY/uFAAQ+zXr38MIH3geN49i5/keHZN+wjRDAJbpvARbYBPzidECoOBdX08BA0IKvqC"
        "mbaRwYoObgxDIiq+YqgFAHdZwW8uVTNcAAAAAElFTkSuQmCC"
    ),
    "Q": base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAA8AAAAPCAYAAAA71pVKAAAA2klEQVR4nGNkwAL+v+/7"
        "jy7GKFjEiC7Ggk3Tr5f9OA1kRDIEzvh8SRrDNlyAV+8pWB+YeHNGimiNMCBi8owR7Oxf"
        "P1H1Slk/Z0AHz45KYogxPtgvjqJTwfElmH6wXxyvGAiw/PqF3cXYxH+hibH8+vUPh+Z/"
        "BMVYfuKwGZv4T0yb8TvbMvIdTmczgoiDCwVQRO3jP2AYdnChAArfPv4DI1jz7ln8eOPZ"
        "Ne0jmN49ix9ZDKIZBLZM4SM6ofjkfEKkMBhY18dD0ICgoi+YaRsZrOjgxjAkouIrhloA"
        "z09jlGYHOTYAAAAASUVORK5CYII="
    ),
}
# --- END GENERATED: GLYPHS ---


# ------------------------------------------------------------ station lookup

class ConfigError(SystemExit):
    def __init__(self, message, display_hint):
        super().__init__(message)
        self.message = message
        self.display_hint = display_hint  # short string for the 72x16 screen


def load_stations():
    return json.loads(zlib.decompress(base64.b64decode(STATIONS_B64)))


def _norm(s):
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in s)
                    .split())


def match_station(rows, query):
    """All rows sharing the best-matching station name."""
    q = _norm(query)
    if not q:
        return []
    scored = {}
    for row in rows:
        name = _norm(row[1])
        if name == q:
            score = 3
        elif name.startswith(q):
            score = 2
        elif q in name:
            score = 1
        else:
            continue
        scored.setdefault((score, row[1]), []).append(row)
    if not scored:
        return []
    best_score = max(k[0] for k in scored)
    # among equally-scored names prefer the shortest (closest match)
    best_name = min((k[1] for k in scored if k[0] == best_score), key=len)
    return [r for r in rows if r[1] == best_name]


def parse_direction(value, rows):
    v = (value or "").strip().lower()
    if v in ("n", "north", "uptown", "up"):
        return "N"
    if v in ("s", "south", "downtown", "down"):
        return "S"
    if v:
        for row in rows:
            if v in row[3].lower():
                return "N"
            if v in row[4].lower():
                return "S"
    raise ConfigError(
        f"DIRECTION {value!r} not understood — use N/S/uptown/downtown or a "
        "destination that appears in the station's direction labels "
        f"({', '.join(sorted({x for r in rows for x in (r[3], r[4]) if x}))})",
        "check DIRECTION")


def resolve_config(station, direction, routes_csv, stops_csv):
    """-> dict(stops, route_ids, designators, feeds, dir_word, label)"""
    rows = load_stations()
    want_routes = [r.strip().upper() for r in (routes_csv or "").split(",")
                   if r.strip()]

    if stops_csv:
        stops = [s.strip().upper() for s in stops_csv.split(",") if s.strip()]
        bases = {s[:-1] if s[-1] in "NS" else s for s in stops}
        matched = [r for r in rows if r[0] in bases]
        suffix = stops[0][-1] if stops and stops[0][-1] in "NS" else "N"
    else:
        matched = match_station(rows, station or "")
        if not matched:
            near = sorted({r[1] for r in rows
                           if _norm(station or "")[:4] and
                           _norm(station or "")[:4] in _norm(r[1])})[:6]
            hint = f" — near matches: {', '.join(near)}" if near else ""
            raise ConfigError(
                f"STATION {station!r} not found{hint}. "
                "Try `--list-stations <query>`.", "check STATION")
        suffix = parse_direction(direction or "N", matched)
        stops = [r[0] + suffix for r in matched]

    served = {r for row in matched for r in row[2].split()}
    if want_routes:
        route_ids = want_routes
        unknown = [r for r in route_ids if designator(r) not in DESIGNATOR_META]
        if unknown:
            raise ConfigError(f"ROUTES {unknown} not recognized",
                              "check ROUTES")
    else:
        route_ids = sorted(served)
        if not route_ids:
            raise ConfigError(
                "no routes known for these stops — set ROUTES explicitly",
                "set ROUTES")

    desigs = []
    for r in route_ids:
        d = designator(r)
        if d not in desigs:
            desigs.append(d)

    # empty-state wording: a short direction label if the station has one
    # ("Church Av"), otherwise plain uptown/downtown
    label_rows = [r for r in matched
                  if set(r[2].split()) & {designator(x) for x in route_ids}] \
        or matched
    labels = [r[3 if suffix == "N" else 4] for r in label_rows]
    short = [l for l in labels if l and len(l) <= 10]
    dir_word = short[0] if short else ("uptown" if suffix == "N" else "downtown")

    return {
        "stops": stops,
        "route_ids": route_ids,
        "designators": desigs,
        "feeds": feeds_for(route_ids),
        "dir_word": dir_word,
        "label": matched[0][1] if matched else ",".join(stops),
    }


def list_stations(query):
    rows = load_stations()
    subset = [r for r in rows
              if not query or _norm(query) in _norm(r[1])]
    for r in sorted(subset, key=lambda r: (r[1], r[0])):
        print(f"{r[1]:<34} {r[0]:<5} routes: {r[2]:<14} "
              f"N: {r[3] or '-':<22} S: {r[4] or '-'}")
    print(f"{len(subset)} platform(s)")


# ------------------------------------------------------- art (pure stdlib)

def _hex_rgb(h):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _scale(c, k):
    return tuple(min(255, round(v * k)) for v in c)


def palette_for(desig):
    hexc, _black, key = DESIGNATOR_META[desig]
    if key:
        return PALETTES[key]
    base = _hex_rgb(hexc)
    return {
        "spec": tuple(round(v + (255 - v) * 0.60) for v in base),
        "top": _scale(base, 1.15),
        "bot": _scale(base, 0.47),
        "lift": _scale(base, 0.62),
        "bullet_bot": _scale(base, 0.65),
    }


def png_encode(w, h, rows):
    """Minimal RGBA PNG writer. rows: list of rows of (r, g, b, a)."""
    raw = b"".join(
        b"\x00" + bytes(v for px in row for v in px) for row in rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + \
            struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def make_bullet(desig):
    """15x15 shaded route disk with the letter/number baked in."""
    if desig in BULLET_OVERRIDES:  # the hand-tuned N/Q/G art, verbatim
        return BULLET_OVERRIDES[desig]
    pal = palette_for(desig)
    _hexc, black_letter, _key = DESIGNATOR_META[desig]
    size = len(DISK_MASK)
    disk_rows = [y for y in range(size) if "#" in DISK_MASK[y]]
    top, bot = min(disk_rows), max(disk_rows)
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            if DISK_MASK[y][x] != "#":
                continue
            if y == top or (y == top + 1 and DISK_MASK[top][x] != "#"):
                c = pal["spec"]  # 1px specular arc following the rim
            else:
                c = _lerp(pal["top"], pal["bullet_bot"],
                          (y - top) / max(bot - top, 1))
            px[y][x] = (*c, 255)
    glyph = BULLET_GLYPHS[letter_for(desig)]
    gw, gh = len(glyph[0]), len(glyph)
    x0, y0 = (size - gw) // 2, (size - gh) // 2
    ink = (0, 0, 0, 255) if black_letter else (255, 255, 255, 255)
    for gy, grow in enumerate(glyph):
        for gx, ch in enumerate(grow):
            if ch == "#" and DISK_MASK[y0 + gy][x0 + gx] == "#":
                px[y0 + gy][x0 + gx] = ink
    return png_encode(size, size, px)


def flash_card(desig):
    """72x16 shaded field in the line color with the XL letter centered.
    Returns rows of (r, g, b)."""
    pal = palette_for(desig)
    _hexc, black_letter, _key = DESIGNATOR_META[desig]
    w, h, r = 72, 16, 5
    rows = []
    for y in range(h):
        if y == 0:
            base = pal["spec"]
        elif y == h - 1:
            base = pal["lift"]
        else:
            base = _lerp(pal["top"], pal["bot"], (y - 1) / (h - 2))
        row = []
        for x in range(w):
            cx, cy = min(x, w - 1 - x), min(y, h - 1 - y)
            if cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                row.append((0, 0, 0))  # rounded-off corner
                continue
            edge = min(cx, cy)
            scale = (0.25, 0.5, 0.75)[edge] if edge < 3 else 1.0
            row.append(tuple(round(c * scale) for c in base))
        rows.append(row)
    glyph = XL_GLYPHS[letter_for(desig)]
    gw, gh = len(glyph[0]), len(glyph)
    x0, y0 = (w - gw) // 2, (h - gh) // 2
    ink = (0, 0, 0) if black_letter else (255, 255, 255)
    for gy, grow in enumerate(glyph):
        for gx, ch in enumerate(grow):
            if ch == "#":
                rows[y0 + gy][x0 + gx] = ink
    return rows


def _rgb_bytes(rows):
    return b"".join(bytes(v for px in row for v in px) for row in rows)


def flash_anim_frames(desig):
    """Sweep-in (eased), hold, fade-to-black — raw RGB frames."""
    card = flash_card(desig)
    w = len(card[0])
    black_row = [(0, 0, 0)] * w
    sweep, hold, fade = FLASH_FRAMES
    frames = []
    for i in range(sweep):  # ease-out cubic from x=-72 to 0
        t = (i + 1) / sweep
        x = round(-w * (1 - t) ** 3)
        frames.append(_rgb_bytes(
            [row[-x:] + black_row[: -x] if x else row for row in card]))
    frames += [_rgb_bytes(card)] * hold
    for i in range(fade):
        s = 1.0 - (i + 1) / fade
        frames.append(_rgb_bytes(
            [[tuple(round(v * s) for v in px) for px in row] for row in card]))
    return frames


# bicycle0 .anim container — byte-compatible port of the firmware's
# seq2anim.py (same as ~/busybar/tools/build_anim.py, minus PIL)

_MAX_BLOCKS = 127
_RLE_THRESHOLD = 3
_HEADER_FORMAT = "<8s BBBB BHB II III"
_SECTION_FORMAT = "<IIIB"
_FRAME_FORMAT = "<BBH"


def _rle_compress(source, blk_size):
    src_i, src_len = 0, len(source)
    dest = bytearray()
    while src_i < src_len:
        repeat_count = 0
        for i in range(src_i, src_len, blk_size):
            if source[i:i + blk_size] == source[src_i:src_i + blk_size]:
                repeat_count += 1
            else:
                break
        repeat_count = min(repeat_count, _MAX_BLOCKS)
        if repeat_count == 0:
            break
        if repeat_count < _RLE_THRESHOLD:
            repeat_count = 0
            verbatim_count = 0
            for i in range(src_i, src_len, blk_size):
                if source[i:i + blk_size] == source[i + blk_size:i + blk_size * 2]:
                    repeat_count += 1
                    if repeat_count > _RLE_THRESHOLD:
                        break
                else:
                    verbatim_count += 1 + repeat_count
                    repeat_count = 0
            verbatim_count += repeat_count
            verbatim_count = min(verbatim_count, _MAX_BLOCKS)
            dest.append(0x80 | verbatim_count)
            dest.extend(source[src_i:src_i + verbatim_count * blk_size])
            src_i += verbatim_count * blk_size
        else:
            dest.append(repeat_count)
            dest.extend(source[src_i:src_i + blk_size])
            src_i += repeat_count * blk_size
    return bytes(dest)


def anim_encode(frames, w, h, fps=FPS):
    """RGB frame bytes -> bicycle0 .anim blob (rgb888, stored BGR)."""
    encoded = []
    last = None
    for fb in frames:
        if fb == last:
            encoded[-1][1] += 1
            continue
        last = fb
        packed = bytearray()
        for i in range(0, len(fb), 3):
            packed.extend((fb[i + 2], fb[i + 1], fb[i]))
        packed = bytes(packed)
        rle = _rle_compress(packed, 3)
        if len(rle) < len(packed):
            encoded.append([1, 1, rle])
        else:
            encoded.append([0, 1, packed])

    frames_chunk_len = sum(struct.calcsize(_FRAME_FORMAT) + len(e[2])
                           for e in encoded)
    max_encoded_len = max(len(e[2]) for e in encoded)
    sections = [{"name": "default", "start": 0, "end": len(frames) - 1}]
    sections_chunk_len = sum(struct.calcsize(_SECTION_FORMAT)
                             + len(s["name"]) + 1 for s in sections)

    header_len = struct.calcsize(_HEADER_FORMAT)
    display_frame_start = []
    offs = header_len + sections_chunk_len
    for _enc, duration, data in encoded:
        for disp_offset in range(duration, 0, -1):
            display_frame_start.append((offs, disp_offset))
        offs += struct.calcsize(_FRAME_FORMAT) + len(data)

    out = bytearray()
    out += struct.pack(
        _HEADER_FORMAT, b"bicycle0", 0, w, h, 0, fps,
        max_encoded_len, 0, sections_chunk_len, frames_chunk_len,
        len(sections), len(encoded), len(frames))
    for s in sections:
        frame_offs, duration_override = display_frame_start[s["start"]]
        out += struct.pack(_SECTION_FORMAT, s["start"], s["end"],
                           frame_offs, duration_override)
        out += s["name"].encode() + b"\0"
    for enc, duration, data in encoded:
        out += struct.pack(_FRAME_FORMAT, enc, duration, len(data)) + data
    return bytes(out)


def build_assets(designators):
    """Generate per-route art; names carry a content hash so art-pipeline
    changes never collide with stale files cached on the device."""
    assets = {}
    for d in designators:
        bullet = make_bullet(d)
        anim = anim_encode(flash_anim_frames(d), 72, 16)
        assets[d] = {
            "bullet_name": f"bullet_{d}-{hashlib.sha256(bullet).hexdigest()[:8]}.png",
            "bullet": bullet,
            "flash_name": f"flash_{d}-{hashlib.sha256(anim).hexdigest()[:8]}.anim",
            "flash": anim,
        }
    return assets


# ---------------------------------------------------------------- device I/O

class Target:
    def __init__(self, name, base, api_prefix, headers, ws_uri):
        self.name = name
        self.base = base
        self.api_prefix = api_prefix
        self.headers = headers
        self.ws_uri = ws_uri

    def url(self, path):
        return f"{self.base}{self.api_prefix}{path}"


def make_targets():
    targets = {
        "usb": Target("usb", USB_URL, "/api", {},
                      f"ws://{USB_URL.removeprefix('http://')}/api/status/ws"),
        "wifi": Target("wifi", WIFI_URL, "/api",
                       {"X-API-Token": WIFI_TOKEN}, None),
    }
    if CLOUD_TOKEN:
        # cloud WS handshake rejects device API tokens (needs an account
        # session), so no dial stream on this target
        targets["cloud"] = Target(
            "cloud", CLOUD_URL, "/busybar",
            {"Authorization": f"Bearer {CLOUD_TOKEN}"}, None)
    return targets


class Bar:
    def __init__(self):
        self.t = None
        self.s = requests.Session()

    def connect(self, host_override=None):
        if host_override:
            # busybar-manager mode: plain HTTP to the manager's proxy, which
            # forwards to the bar (and injects the variation's priority)
            t = Target("manager", f"http://{host_override}", "/api", {},
                       WS_OVERRIDE or None)
            r = self.s.get(t.url("/version"), headers=t.headers, timeout=10)
            r.raise_for_status()
            self.t = t
            print(f"connected via manager proxy at {host_override}"
                  f" ({'dial' if t.ws_uri else 'static'} mode)")
            return
        targets = make_targets()
        order = ["usb", "wifi", "cloud"] if TARGET == "auto" else [TARGET]
        errors = []
        for name in order:
            t = targets.get(name)
            if t is None:
                errors.append(f"{name}: not configured "
                              "(cloud needs BUSYBAR_CLOUD_TOKEN)")
                continue
            try:
                r = self.s.get(t.url("/version"), headers=t.headers,
                               timeout=3 if name == "usb" else 10)
                r.raise_for_status()
                if WS_OVERRIDE:
                    t.ws_uri = WS_OVERRIDE
                self.t = t
                print(f"connected to BUSY Bar via {name}"
                      f" ({'dial' if t.ws_uri else 'static'} mode)")
                return
            except requests.RequestException as e:
                errors.append(f"{name}: {e}")
        raise SystemExit("no reachable BUSY Bar target:\n  " +
                         "\n  ".join(errors))

    def upload_assets(self, assets):
        files = {}
        for a in assets.values():
            files[a["bullet_name"]] = a["bullet"]
            files[a["flash_name"]] = a["flash"]
        # Upload only what's missing at the right size — over the cloud relay
        # re-pushing every ~16KB anim on each start is a visible stall.
        try:
            r = self.s.get(self.t.url("/storage/list"),
                           params={"path": f"/ext/user_assets/{APP_NAME}"},
                           headers=self.t.headers, timeout=10)
            if r.ok:
                present = {e["name"]: e.get("size")
                           for e in r.json().get("list", [])}
                files = {n: b for n, b in files.items()
                         if present.get(n) != len(b)}
        except (requests.RequestException, ValueError):
            pass  # unreadable listing just means we upload everything
        for filename, blob in files.items():
            r = self.s.post(
                self.t.url("/assets/upload"),
                params={"application_name": APP_NAME, "file": filename},
                headers={**self.t.headers,
                         "Content-Type": "application/octet-stream"},
                data=blob, timeout=20)
            r.raise_for_status()
        if files:
            print(f"uploaded {len(files)} asset(s)")

    def draw(self, elements):
        """Push elements. Returns True if drawn, False if the Bar is busy
        with a higher-priority app (409) — i.e. the user is elsewhere."""
        body = {"application_name": APP_NAME, "priority": PRIORITY,
                "elements": elements}
        r = self.s.post(self.t.url("/display/draw"),
                        headers=self.t.headers, json=body, timeout=15)
        if r.status_code == 409:
            return False
        if r.status_code == 400:
            # a stale element with a conflicting type wedges every draw
            # (the firmware 400s type changes on an existing id) — clear
            # our canvas and retry once to self-heal
            self.clear()
            r = self.s.post(self.t.url("/display/draw"),
                            headers=self.t.headers, json=body, timeout=15)
            if r.status_code == 409:
                return False
        r.raise_for_status()
        return True

    def clear(self):
        self.s.delete(
            self.t.url("/display/draw"), headers=self.t.headers,
            params={"application_name": APP_NAME}, timeout=15,
        ).raise_for_status()


# ------------------------------------------------------------------ MTA feed

def _walk_fields(buf):
    """Yield (field_number, wire_type, value) for one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        tag = 0
        shift = 0
        while True:
            b = buf[i]; i += 1
            tag |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        field, wire = tag >> 3, tag & 7
        if wire == 0:  # varint
            v = 0; shift = 0
            while True:
                b = buf[i]; i += 1
                v |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            yield field, wire, v
        elif wire == 1:  # fixed64
            yield field, wire, buf[i:i + 8]; i += 8
        elif wire == 2:  # length-delimited
            ln = 0; shift = 0
            while True:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            yield field, wire, buf[i:i + ln]; i += ln
        elif wire == 5:  # fixed32
            yield field, wire, buf[i:i + 4]; i += 4
        else:
            return


def decode_trip_updates(buf, stops, want_desigs):
    """Hand-rolled GTFS-realtime decode (same trick as the dial stream —
    no protobuf dependency). Yields (epoch, route_id, trip_id) for watched
    stops. Field numbers per gtfs-realtime.proto:
      FeedMessage.entity=2 -> FeedEntity.trip_update=3 ->
        TripUpdate.trip=1 {trip_id=1, route_id=5}
        TripUpdate.stop_time_update=2 {arrival=2{time=2}, departure=3{time=2},
                                       stop_id=4}
    """
    for f, w, entity in _walk_fields(buf):
        if f != 2 or w != 2:
            continue
        for f2, w2, tu in _walk_fields(entity):
            if f2 != 3 or w2 != 2:
                continue
            trip_id, route_id, hits = "", "", []
            for f3, w3, v3 in _walk_fields(tu):
                if f3 == 1 and w3 == 2:  # TripDescriptor
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 1 and w4 == 2:
                            trip_id = v4.decode("utf-8", "replace")
                        elif f4 == 5 and w4 == 2:
                            route_id = v4.decode("utf-8", "replace")
                elif f3 == 2 and w3 == 2:  # StopTimeUpdate
                    stop, dep, arr = "", 0, 0
                    for f4, w4, v4 in _walk_fields(v3):
                        if f4 == 4 and w4 == 2:
                            stop = v4.decode("utf-8", "replace")
                        elif f4 in (2, 3) and w4 == 2:  # arrival / departure
                            for f5, w5, v5 in _walk_fields(v4):
                                if f5 == 2 and w5 == 0:
                                    if f4 == 3:
                                        dep = v5
                                    else:
                                        arr = v5
                    if stop in stops:
                        hits.append(dep or arr)
            if route_id and designator(route_id) in want_desigs:
                for t in hits:
                    if t:
                        yield t, route_id, trip_id


def fetch_arrivals(cfg):
    """Return [(departure_epoch, route_id, trip_id), ...] sorted by time."""
    now = time.time()
    stops = set(cfg["stops"])
    want = set(cfg["designators"])
    per_trip = {}
    got_any = False
    errors = []
    for url in cfg["feeds"]:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            errors.append(f"{url.rsplit('%2F', 1)[-1]}: {e}")
            continue
        got_any = True
        for t, route_id, trip_id in decode_trip_updates(r.content, stops, want):
            if t <= now - 15:
                continue
            key = trip_id or f"{route_id}@{t}"
            # a trip that touches two watched platforms counts once, at its
            # earliest watched departure
            if key not in per_trip or t < per_trip[key][0]:
                per_trip[key] = (t, route_id, trip_id)
    if errors:
        print(f"[{time.strftime('%H:%M:%S')}] feed trouble: "
              + "; ".join(errors), file=sys.stderr)
    if not got_any:
        raise RuntimeError("all MTA feeds failed")
    out = sorted(per_trip.values())
    return out[:MAX_ARRIVALS]


# ----------------------------------------------------------------- rendering

def build_screen(cfg, assets, arrivals, index, offset=0):
    """One arrival card, optionally shifted vertically by `offset` px."""
    els = []
    if not arrivals:
        routes_label = "/".join(cfg["designators"])
        els.append({
            "id": "msg", "type": "text",
            "text": f"No {cfg['dir_word']} {routes_label} trains",
            "font": "small", "color": WHITE, "align": "center",
            "x": 36, "y": 8, "width": 72, "scroll_rate": 1500,
            "timeout": ELEMENT_TIMEOUT,
        })
        return els

    index = max(0, min(index, len(arrivals) - 1))
    dep_time, route, _trip = arrivals[index]
    mins = int(max(0, dep_time - time.time()) // 60)

    els.append({
        "id": "bullet", "type": "image",
        "path": assets[designator(route)]["bullet_name"],
        "x": 1, "y": 0 + offset, "timeout": ELEMENT_TIMEOUT,
    })
    if mins == 0:
        els.append({
            "id": "num", "type": "text", "text": "NOW",
            "font": "extra_large", "color": WHITE, "align": "center",
            "x": 42, "y": 8 + offset, "timeout": ELEMENT_TIMEOUT,
        })
        # park off-screen, same type: the firmware 400s a type change on an
        # existing id, so "unit" must always stay a text element
        els.append({
            "id": "unit", "type": "text", "text": "min",
            "font": "bold", "color": WHITE, "align": "bottom_left",
            "x": 37, "y": -30, "timeout": ELEMENT_TIMEOUT,
        })
    else:
        els.append({
            "id": "num", "type": "text", "text": str(mins),
            "font": "extra_large", "color": WHITE, "align": "mid_right",
            "x": 34, "y": 8 + offset, "timeout": ELEMENT_TIMEOUT,
        })
        els.append({
            "id": "unit", "type": "text", "text": "min",
            "font": "bold", "color": WHITE, "align": "bottom_left",
            "x": 37, "y": 15 + offset, "timeout": ELEMENT_TIMEOUT,
        })
    # position dots, two 1px columns down the right edge: the right column
    # always shows each upcoming train's line color; the single white pixel
    # in the left column marks the departure currently on screen (one
    # element that moves — merge-by-id keeps it from leaving trails)
    for i, (_t, r, _trip) in enumerate(arrivals):
        els.append({
            "id": f"dot{i}", "type": "rectangle",
            "x": 71, "y": i * 2, "width": 1, "height": 1,
            "fill": "solid",
            "fill_colors": [DESIGNATOR_META[designator(r)][0] + "FF"],
            "border_width": 0, "timeout": ELEMENT_TIMEOUT,
        })
    els.append({
        "id": "mark", "type": "rectangle",
        "x": 70, "y": index * 2, "width": 1, "height": 1,
        "fill": "solid", "fill_colors": [WHITE],
        "border_width": 0, "timeout": ELEMENT_TIMEOUT,
    })
    return els


def build_flash_anim(assets, desig):
    """Departure flash element: the compiled per-route .anim. One push; the
    device runs the sweep/hold/fade at 60fps and holds the final black frame,
    so the clear that follows is invisible. Never re-push this id while it
    plays — a re-push restarts the animation."""
    return [{
        "id": "flash_anim", "type": "animation",
        "path": assets[desig]["flash_name"],
        "x": 0, "y": 0, "loop": False, "timeout": ELEMENT_TIMEOUT,
    }]


# ---------------------------------------------------------------- main loops

class App:
    def __init__(self, bar, cfg, assets):
        self.bar = bar
        self.cfg = cfg
        self.assets = assets
        self.arrivals = []
        self.index = 0
        self.blocked = False        # a higher-priority app owns the screen
        self.last_dial = 0.0
        self.dot_count = 0
        self.canvas_mode = None     # "card" | "msg" | None (nothing pushed)
        self.shown_key = None       # (dep_time, route, mins) last rendered
        self.lock = asyncio.Lock()  # serializes renders/animations

    # -- rendering ---------------------------------------------------------

    def displayed(self):
        if not self.arrivals:
            return None
        self.index = max(0, min(self.index, len(self.arrivals) - 1))
        return self.arrivals[self.index]

    def _push(self, offset=0):
        """One draw attempt; tracks blocked state. Returns True if drawn."""
        # elements merge by id on the device, so start clean whenever the
        # shape of what we draw changes (fewer dots, card <-> message)
        mode = "card" if self.arrivals else "msg"
        if (len(self.arrivals) < self.dot_count
                or mode != self.canvas_mode) and self.canvas_mode:
            try:
                self.bar.clear()
            except requests.RequestException:
                pass
        drawn = self.bar.draw(
            build_screen(self.cfg, self.assets, self.arrivals, self.index,
                         offset))
        was_blocked, self.blocked = self.blocked, not drawn
        if drawn:
            self.dot_count = len(self.arrivals)
            self.canvas_mode = mode
            d = self.displayed()
            self.shown_key = d and (d[0], d[1],
                                    int(max(0, d[0] - time.time()) // 60))
            if was_blocked:
                print(f"[{time.strftime('%H:%M:%S')}] screen reclaimed")
        elif not was_blocked:
            print(f"[{time.strftime('%H:%M:%S')}] screen busy — "
                  "waiting politely")
        return drawn

    def _paced(self, fn, *args):
        """Run a draw call and absorb leftover time so animation frames land
        every ~FRAME_SECS regardless of transport latency."""
        t0 = time.time()
        result = fn(*args)
        rest = FRAME_SECS - (time.time() - t0)
        if rest > 0:
            time.sleep(rest)
        return result

    async def render(self):
        async with self.lock:
            try:
                await asyncio.to_thread(self._push)
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                      file=sys.stderr)

    async def slide_to(self, new_index, direction=1):
        """Eased slide to another arrival (up = next, down = previous)."""
        async with self.lock:
            if self.blocked or not self.arrivals:
                self.index = new_index
                try:
                    await asyncio.to_thread(self._push)
                except requests.RequestException:
                    pass
                return
            if self.bar.t.name != "usb":
                # dial events can arrive over a forwarded socket (BUSYBAR_WS)
                # while draws still go through the manager/cloud relay, where
                # per-frame eased pushes stretch into seconds — jump-cut
                self.index = new_index
                try:
                    await asyncio.to_thread(self._push)
                except requests.RequestException as e:
                    print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                          file=sys.stderr)
                return
            try:
                sign = 1 if direction >= 0 else -1
                for off in SLIDE_OUT:
                    if not await asyncio.to_thread(
                            self._paced, self._push, off * sign):
                        self.index = new_index
                        return
                self.index = new_index
                for off in SLIDE_IN:
                    if not await asyncio.to_thread(
                            self._paced, self._push, off * sign):
                        return
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                      file=sys.stderr)

    async def departure_flash(self, departed_route):
        """Full-screen wipe in the departed line's color, then the next
        train slides in."""
        async with self.lock:
            self.index = 0
            if self.blocked:
                try:
                    await asyncio.to_thread(self._push)
                except requests.RequestException:
                    pass
                return
            try:
                if not await asyncio.to_thread(
                        self.bar.draw,
                        build_flash_anim(self.assets,
                                         designator(departed_route))):
                    self.blocked = True
                    return
                await asyncio.sleep(FLASH_ANIM_SECS)  # device-side 60fps
                await asyncio.to_thread(self.bar.clear)
                self.canvas_mode = None
                self.dot_count = 0
                if self.bar.t.name == "usb":
                    for off in SLIDE_IN:
                        if not await asyncio.to_thread(
                                self._paced, self._push, off):
                            return
                else:
                    # per-frame pushes over the relay stretch the ease into
                    # mush; the screen is already black, so just appear
                    await asyncio.to_thread(self._push)
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] draw error: {e}",
                      file=sys.stderr)

    # -- tasks -------------------------------------------------------------

    @staticmethod
    def _same_train(shown, arrivals):
        """Match the shown train across polls by trip id (stable), falling
        back to route + closest time within 90s for feeds that omit it."""
        if shown[2]:
            for i, a in enumerate(arrivals):
                if a[2] == shown[2]:
                    return i
        best = None
        for i, (t, route, _trip) in enumerate(arrivals):
            if route == shown[1] and abs(t - shown[0]) < 90:
                if best is None or abs(t - shown[0]) < abs(
                        arrivals[best][0] - shown[0]):
                    best = i
        return best

    async def fetcher(self):
        while True:
            try:
                shown = self.displayed()
                self.arrivals = await asyncio.to_thread(fetch_arrivals,
                                                        self.cfg)
                match = shown and self._same_train(shown, self.arrivals)
                if shown and match is None and self.arrivals:
                    await self.departure_flash(shown[1])
                else:
                    if match is not None:
                        self.index = match
                    await self.render()
                nxt = ", ".join(
                    f"{r}:{int(max(0, t - time.time()) // 60)}m"
                    for t, r, _ in self.arrivals[:4])
                state = "blocked" if self.blocked else "showing"
                print(f"[{time.strftime('%H:%M:%S')}] {len(self.arrivals)} "
                      f"arrivals ({state})  {nxt}")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] fetch error: {e}",
                      file=sys.stderr)
            await asyncio.sleep(FETCH_SECS)

    async def supervisor(self):
        """Retry while blocked; flip the minutes exactly on the boundary."""
        while True:
            await asyncio.sleep(BLOCKED_RETRY_SECS if self.blocked
                                else TICK_SECS)
            d = self.displayed()
            if d is None:
                continue
            if self.blocked:
                await self.render()  # cheap 409 until we own the screen
                continue
            mins_now = int(max(0, d[0] - time.time()) // 60)
            if self.shown_key != (d[0], d[1], mins_now):
                await self.render()

    async def idle_reset(self):
        while True:
            await asyncio.sleep(5)
            if (self.index != 0 and not self.blocked
                    and time.time() - self.last_dial > IDLE_RESET_SECS):
                await self.slide_to(0, direction=-1)

    async def dial_listener(self):
        try:
            import websockets
        except ImportError:
            print("dial disabled: `pip install websockets` to scroll "
                  "arrivals with the dial over USB")
            return
        while True:
            try:
                async with websockets.connect(self.bar.t.ws_uri) as ws:
                    await ws.send(json.dumps({"enable": True}))
                    print("dial connected — spin to scroll arrivals")
                    async for msg in ws:
                        if isinstance(msg, str):
                            continue
                        moved = sum(encoder_deltas(msg))
                        if moved and self.arrivals:
                            self.last_dial = time.time()
                            await self.slide_to(
                                (self.index + moved) % len(self.arrivals),
                                direction=1 if moved > 0 else -1)
            except Exception as e:
                print(f"dial stream lost ({e}); retrying in 5s",
                      file=sys.stderr)
                await asyncio.sleep(5)

    async def run(self):
        # drop any elements a previous version left behind (draws merge by
        # id, so an element we no longer push would linger forever)
        try:
            await asyncio.to_thread(self.bar.clear)
        except requests.RequestException:
            pass
        tasks = [self.fetcher(), self.supervisor()]
        if self.bar.t.ws_uri:
            tasks += [self.dial_listener(), self.idle_reset()]
        await asyncio.gather(*tasks)

    async def demo(self):
        """Fake departure sequence to preview the art and animations."""
        now = time.time()
        routes = self.cfg["route_ids"]
        self.arrivals = [
            (now + 30, routes[0], "demo-1"),
            (now + 360, routes[1 % len(routes)], "demo-2"),
            (now + 780, routes[2 % len(routes)], "demo-3"),
        ]
        await self.render()
        await asyncio.sleep(2)
        departed = self.arrivals.pop(0)
        await self.departure_flash(departed[1])
        await asyncio.sleep(2)


def encoder_deltas(frame):
    """Extract dial rotation deltas from one status WS binary frame
    (BSB_State.State: updates=2 -> input=11 -> encoder_event=3 ->
    delta=1, zigzag sint32)."""
    deltas = []
    for f, w, update in _walk_fields(frame):
        if f != 2 or w != 2:
            continue
        for f2, w2, inp in _walk_fields(update):
            if f2 != 11 or w2 != 2:
                continue
            for f3, w3, enc in _walk_fields(inp):
                if f3 != 3 or w3 != 2:
                    continue
                delta = 0
                for f4, w4, v in _walk_fields(enc):
                    if f4 == 1 and w4 == 0:
                        delta = (v >> 1) ^ -(v & 1)  # zigzag decode
                deltas.append(delta)
    return deltas


# ----------------------------------------------------------------------- CLI

def config_error_loop(bar, err):
    """Config problems stay visible: log the details, show a short hint on
    the display, and keep the process alive (a crash-looping manager app is
    harder to diagnose from the dashboard than a message)."""
    print(err.message, file=sys.stderr)
    while True:
        try:
            bar.draw([{
                "id": "msg", "type": "text", "text": err.display_hint,
                "font": "small", "color": WHITE, "align": "center",
                "x": 36, "y": 8, "width": 72, "scroll_rate": 1500,
                "timeout": ELEMENT_TIMEOUT,
            }])
        except requests.RequestException:
            pass
        time.sleep(60)
        print(f"still misconfigured: {err.message}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Live next-train departures for any NYC subway station "
                    "on a BUSY Bar")
    parser.add_argument("--host", default=None,
                        help="bar or manager-proxy host[:port] "
                             "(busybar-manager passes this)")
    parser.add_argument("--station", default=None,
                        help="station name (fuzzy; env STATION)")
    parser.add_argument("--direction", default=None,
                        help="N/S/uptown/downtown or a destination label "
                             "(env DIRECTION)")
    parser.add_argument("--routes", default=None,
                        help="comma-separated route filter, e.g. N,Q "
                             "(env ROUTES)")
    parser.add_argument("--stops", default=None,
                        help="exact GTFS platform ids, e.g. Q01N,R23N "
                             "(env STOPS)")
    parser.add_argument("--list-stations", nargs="?", const="", default=None,
                        metavar="QUERY",
                        help="print matching stations and exit")
    parser.add_argument("--clear", action="store_true",
                        help="clear display and exit")
    parser.add_argument("--demo", action="store_true",
                        help="run the departure-flash demo and exit")
    args = parser.parse_args()

    if args.list_stations is not None:
        list_stations(args.list_stations)
        return

    global requests
    try:
        import requests as _requests
    except ImportError:
        raise SystemExit("this app needs the `requests` package "
                         "(pip install requests)")
    requests = _requests

    bar = Bar()
    bar.connect(args.host)

    if args.clear:
        bar.clear()
        print("cleared")
        return

    station = args.station or os.environ.get("STATION") or ""
    direction = args.direction or os.environ.get("DIRECTION") or ""
    routes_csv = args.routes or os.environ.get("ROUTES") or ""
    stops_csv = args.stops or os.environ.get("STOPS") or ""
    if not station and not stops_csv:
        station, direction = "Times Sq-42 St", direction or "N"
        print("no STATION configured — defaulting to uptown Times Sq-42 St")

    try:
        cfg = resolve_config(station, direction, routes_csv, stops_csv)
    except ConfigError as e:
        config_error_loop(bar, e)
        return

    print(f"{cfg['label']} ({cfg['dir_word']}) — routes "
          f"{','.join(cfg['route_ids'])} — stops {','.join(cfg['stops'])} — "
          f"{len(cfg['feeds'])} feed(s)")

    assets = build_assets(cfg["designators"])
    try:
        bar.upload_assets(assets)
    except requests.RequestException as e:
        print(f"asset upload failed: {e}", file=sys.stderr)

    app = App(bar, cfg, assets)
    try:
        asyncio.run(app.demo() if args.demo else app.run())
    except KeyboardInterrupt:
        try:
            bar.clear()
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
