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
import math
import os
import re
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

# Service status (Mercury alerts, held trains, track changes) rendered in
# the firmware's busy-mode plate grammar. BUSYBAR_ALERTS=off disables it.
ALERTS_ON = os.environ.get("BUSYBAR_ALERTS", "on").lower() not in (
    "off", "0", "no", "false")
ALERTS_URL = ("https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"
              "camsys%2Fsubway-alerts.json")
ALERTS_POLL_SECS = 120
ALERT_PAGE_EVERY = 75     # seconds between alert-page interruptions
HELD_AFTER_SECS = 180     # STOPPED_AT with no movement this long = held
WASH_SECS = 0.6           # the compiled amber wash that covers page swaps
MARQUEE_RATE = 1400       # px/min for the in-plate headline marquee
AMBER = "#FFB000FF"

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

# designator -> (official MTA line color, palette override). Every letter is
# white with the firmware's baked drop shadow (_stamp_letter) — including the
# yellow N/Q/R/W the MTA prints black: black-on-yellow reads as a hole at LED
# scale, white-with-shadow doesn't.
DESIGNATOR_META = {
    "1": ("#EE352E", None), "2": ("#EE352E", None), "3": ("#EE352E", None),
    "4": ("#00933C", None), "5": ("#00933C", None), "6": ("#00933C", None),
    "7": ("#B933AD", None),
    "A": ("#0039A6", None), "C": ("#0039A6", None), "E": ("#0039A6", None),
    "B": ("#FF6319", None), "D": ("#FF6319", None), "F": ("#FF6319", None),
    "M": ("#FF6319", None),
    "G": ("#6CBE45", "green"),
    "J": ("#996633", None), "Z": ("#996633", None),
    "L": ("#A7A9AC", None),
    "N": ("#FCC30B", "yellow"), "Q": ("#FCC30B", "yellow"),
    "R": ("#FCC30B", "yellow"), "W": ("#FCC30B", "yellow"),
    "S": ("#808183", None),
    "SIR": ("#0039A6", None),
}

# rush-hour express services the MTA marks with a diamond bullet
EXPRESS_OF = {"6": "6X", "7": "7X", "F": "FX"}

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
    return r  # 6X/7X/FX keep their identity and draw as diamonds


def base_desig(desig):
    """6X -> 6: the local a rush-hour express borrows color and letter from."""
    if desig.endswith("X") and desig[:-1] in DESIGNATOR_META:
        return desig[:-1]
    return desig


def is_express(desig):
    return desig != base_desig(desig)


def line_color(desig):
    return DESIGNATOR_META[base_desig(desig)][0]


def letter_for(desig):
    d = base_desig(desig)
    return "S" if d == "SIR" else d


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
TINY_GLYPHS = {
    "0": [".#.", "#.#", "#.#", ".#."],
    "1": [".#.", "##.", ".#.", "###"],
    "2": ["##.", "..#", "#..", "###"],
    "3": ["##.", ".##", "..#", "##."],
    "4": ["..#", "#.#", "###", "..#"],
    "5": ["###", "#..", "..#", "##."],
    "6": [".##", "#..", "###", "###"],
    "7": ["###", "..#", ".#.", "#.."],
    "8": [".#.", "###", "#.#", ".#."],
    "9": [".##", "#.#", "###", "..#"],
    "A": [".#.", "#.#", "###", "#.#"],
    "B": ["##.", "###", "#.#", "##."],
    "C": [".##", "#..", "#..", ".##"],
    "D": ["##.", "#.#", "#.#", "##."],
    "E": ["###", "##.", "#..", "###"],
    "F": ["###", "#..", "##.", "#.."],
    "G": [".##", "#..", "#.#", ".##"],
    "H": ["#.#", "#.#", "###", "#.#"],
    "I": ["###", ".#.", ".#.", "###"],
    "J": ["..#", "..#", "#.#", ".#."],
    "K": ["#..#", "#.#.", "###.", "#..#"],
    "L": ["#..", "#..", "#..", "###"],
    "M": ["#...#", "##.##", "#.#.#", "#...#"],
    "N": ["#..#", "##.#", "#.##", "#..#"],
    "O": [".##.", "#..#", "#..#", ".##."],
    "P": ["##.", "#.#", "##.", "#.."],
    "Q": [".##.", "#..#", "#..#", ".##.", "..#."],
    "R": ["##.", "#.#", "##.", "#.#"],
    "S": [".##", "#..", "..#", "##."],
    "T": ["###", ".#.", ".#.", ".#."],
    "U": ["#..#", "#..#", "#..#", ".##."],
    "V": ["#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#.#.#", "#.#.#", ".#.#."],
    "X": ["#.#", ".#.", "#.#", "#.#"],
    "Y": ["#...#", ".#.#.", "..#..", "..#.."],
    "Z": ["####", "..#.", ".#..", "####"],
}
CONDENSED_GLYPHS = {
    "0": [".##.", "#..#", "#.##", "##.#", "#..#", "#..#", ".##."],
    "1": ["..#.", ".##.", "#.#.", "..#.", "..#.", "..#.", "####"],
    "2": [".##.", "#..#", "...#", "..#.", ".#..", "#...", "####"],
    "3": [".##.", "#..#", "...#", ".##.", "...#", "#..#", ".##."],
    "4": ["..##", ".#.#", ".#.#", "#..#", "#..#", "####", "...#"],
    "5": ["####", "#...", "#...", "###.", "...#", "...#", "###."],
    "6": [".##.", "#..#", "#...", "###.", "#..#", "#..#", ".##."],
    "7": ["####", "...#", "..#.", "..#.", ".#..", ".#..", ".#.."],
    "8": [".##.", "#..#", "#..#", ".##.", "#..#", "#..#", ".##."],
    "9": [".##.", "#..#", "#..#", ".###", "...#", "#..#", ".##."],
    "A": ["..#..", "..#..", ".#.#.", ".#.#.", ".###.", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["###..", "#..#.", "#...#", "#...#", "#...#", "#..#.", "###.."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": ["###", ".#.", ".#.", ".#.", ".#.", ".#.", "###"],
    "J": ["...#", "...#", "...#", "...#", "#..#", "#..#", ".##."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#...", "#...", "#...", "#...", "#...", "#...", "####"],
    "M": ["#.....#", "##...##", "##...##", "#.#.#.#", "#.#.#.#", "#..#..#", "#..#..#"],
    "N": ["#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "#...#"],
    "S": [".###.", "#...#", "#....", ".###.", "....#", "#...#", ".###."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", ".#.#.", ".#.#.", "..#..", "..#.."],
    "W": ["#.....#", "#..#..#", "#..#..#", "#.#.#.#", "#.#.#.#", ".#...#.", ".#...#."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", ".#.#.", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
}
SMALL_GLYPHS = {
    "0": [".#.", "#.#", "#.#", "#.#", ".#."],
    "1": [".#", "##", ".#", ".#", ".#"],
    "2": ["##.", "..#", ".#.", "#..", "###"],
    "3": ["##.", "..#", ".#.", "..#", "##."],
    "4": ["..#", ".##", "#.#", "###", "..#"],
    "5": ["###", "#..", "##.", "..#", "##."],
    "6": [".##", "#..", "##.", "#.#", ".#."],
    "7": ["###", "..#", ".#.", ".#.", ".#."],
    "8": [".#.", "#.#", ".#.", "#.#", ".#."],
    "9": [".#.", "#.#", ".##", "..#", "##."],
    "A": [".##.", "#..#", "####", "#..#", "#..#"],
    "B": ["###.", "#..#", "###.", "#..#", "###."],
    "C": [".##.", "#..#", "#...", "#..#", ".##."],
    "D": ["###.", "#..#", "#..#", "#..#", "###."],
    "E": ["####", "#...", "###.", "#...", "####"],
    "F": ["####", "#...", "###.", "#...", "#..."],
    "G": [".###", "#...", "#.##", "#..#", ".###"],
    "H": ["#..#", "#..#", "####", "#..#", "#..#"],
    "I": ["#", "#", "#", "#", "#"],
    "J": ["..#", "..#", "..#", "#.#", ".#."],
    "K": ["#..#", "#.#.", "##..", "#.#.", "#..#"],
    "L": ["#..", "#..", "#..", "#..", "###"],
    "M": ["#...#", "##.##", "#.#.#", "#...#", "#...#"],
    "N": ["#..#", "##.#", "#.##", "#..#", "#..#"],
    "O": [".##.", "#..#", "#..#", "#..#", ".##."],
    "P": ["###.", "#..#", "###.", "#...", "#..."],
    "Q": [".##.", "#..#", "#..#", "#.#.", ".#.#"],
    "R": ["###.", "#..#", "###.", "#..#", "#..#"],
    "S": [".###", "#...", ".##.", "...#", "###."],
    "T": ["###", ".#.", ".#.", ".#.", ".#."],
    "U": ["#..#", "#..#", "#..#", "#..#", ".##."],
    "V": ["#.#", "#.#", "#.#", ".#.", ".#."],
    "W": ["#.#.#", "#.#.#", "#.#.#", ".#.#.", ".#.#."],
    "X": ["#.#", "#.#", ".#.", "#.#", "#.#"],
    "Y": ["#.#", "#.#", ".#.", ".#.", ".#."],
    "Z": ["###", "..#", ".#.", "#..", "###"],
}
DISK_MASK = [".....#####.....", "...#########...", "..###########..", ".#############.", ".#############.", "###############", "###############", "###############", "###############", "###############", ".#############.", ".#############.", "..###########..", "...#########...", ".....#####....."]
BULLET_GLYPH_OVERRIDES = {
    "G": [".#####..", "##...##.", "##......", "##......", "##...###", "##....##", "##...###", ".#####.."],
    "Q": [".#####.", "##...##", "##...##", "##...##", "##...##", "##..###", ".#####.", ".....##"],
}
# --- END GENERATED: GLYPHS ---

# --- BEGIN GENERATED: OFFSETS ---
# per-icon letter tuning, edited with tools/bullet_editor.py.
# LETTER_OFFSETS: (dx, dy) nudges from dead center — the Q/G seeds carry
# the legacy hand alignment. LETTER_SIZES: glyph size override per icon
# ("tiny" ~3x4 / "bold" 7px / "xl" 10px); defaults are bullet=bold,
# flash=xl for locals and bold inside the express diamond mark.
LETTER_OFFSETS = {
    "bullet": {
        "7X": (1, 1),
        "L": (1, 0),
    },
    "flash": {},
}
LETTER_SIZES = {
    "bullet": {
        "1": "xl",
        "2": "xl",
        "3": "xl",
        "4": "xl",
        "5": "xl",
        "6": "xl",
        "7": "xl",
        "A": "xl",
        "B": "xl",
        "C": "xl",
        "D": "xl",
        "E": "xl",
        "F": "xl",
        "G": "xl",
        "J": "xl",
        "L": "xl",
        "N": "xl",
        "Q": "xl",
        "R": "xl",
        "S": "xl",
        "SIR": "xl",
        "W": "xl",
        "Z": "xl",
    },
    "flash": {},
}
# --- END GENERATED: OFFSETS ---


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
        unknown = [r for r in route_ids
                   if base_desig(designator(r)) not in DESIGNATOR_META]
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
    # a station serving the local also sees its rush-hour diamond twin —
    # build that art up front so 6X/7X/FX arrivals draw as diamonds
    for d in list(desigs):
        x = EXPRESS_OF.get(d)
        if x and x not in desigs:
            desigs.append(x)

    # empty-state wording: a short direction label if the station has one
    # ("Church Av"), otherwise plain uptown/downtown
    label_rows = [r for r in matched
                  if set(r[2].split())
                  & {base_desig(designator(x)) for x in route_ids}] \
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
    hexc, key = DESIGNATOR_META[base_desig(desig)]
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


# The firmware's glyph drop-shadow ratios, strongest first where they
# overlap — the same treatment the stock assets give the BUSY wordmark
# (hardware-verified on the truvo cards): each shadow pixel multiplies
# whatever it lands on.
GLYPH_SHADOW = ((0, 1, 0.43), (-1, 0, 0.78), (1, 0, 0.78), (0, 2, 0.76))

# 15x15 rotated square for the rush-hour express diamonds
DIAMOND_MASK = ["".join("#" if abs(x - 7) + abs(y - 7) <= 7 else "."
                        for x in range(15)) for y in range(15)]


def bullet_glyph(desig):
    ch = letter_for(desig)
    return BULLET_GLYPH_OVERRIDES.get(ch, BULLET_GLYPHS[ch])


def letter_offset(kind, desig):
    return LETTER_OFFSETS.get(kind, {}).get(desig, (0, 0))


def default_size(kind, desig):
    if kind == "flash" and not is_express(desig):
        return "xl"
    return "bold"


def glyph_for(kind, desig):
    """The glyph at this icon's tuned size; hand-tuned letterforms apply at
    the default bold size, and a size the font can't do falls back."""
    size = LETTER_SIZES.get(kind, {}).get(desig, default_size(kind, desig))
    ch = letter_for(desig)
    if size == "tiny":
        table = TINY_GLYPHS
    elif size == "xl":
        table = XL_GLYPHS
    else:
        return bullet_glyph(desig)
    return table.get(ch) or bullet_glyph(desig)


def _stamp_letter(grid, glyph, x0, y0, inside, extra_ink=()):
    """Bake a glyph in white with the firmware shadow ratios onto a mutable
    pixel grid (rows of RGB or RGBA tuples). `inside(x, y)` bounds both ink
    and shadow, so nothing bleeds off a disk or card."""
    ink = {(x0 + gx, y0 + gy)
           for gy, grow in enumerate(glyph)
           for gx, ch in enumerate(grow) if ch == "#"}
    ink |= set(extra_ink)
    ink = {p for p in ink if inside(*p)}
    shaded = set()
    for dx, dy, factor in GLYPH_SHADOW:
        for x, y in ink:
            tx, ty = x + dx, y + dy
            if (tx, ty) in ink or (tx, ty) in shaded or not inside(tx, ty):
                continue
            c = grid[ty][tx]
            grid[ty][tx] = tuple(round(v * factor) for v in c[:3]) + c[3:]
            shaded.add((tx, ty))
    for x, y in ink:
        grid[y][x] = (255, 255, 255) + grid[y][x][3:]


def make_bullet(desig):
    """15x15 shaded route disk (diamond for expresses) with the letter baked
    in white over the firmware drop shadow."""
    pal = palette_for(desig)
    express = is_express(desig)
    mask = DIAMOND_MASK if express else DISK_MASK
    size = len(mask)
    filled = [y for y in range(size) if "#" in mask[y]]
    top, bot = min(filled), max(filled)
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            if mask[y][x] != "#":
                continue
            if express:
                # rim light down both upper edges (the diamond's "arc")
                spec = y <= size // 2 and (y == top or mask[y - 1][x] != "#")
            else:
                spec = y == top or (y == top + 1 and mask[top][x] != "#")
            if spec:
                c = pal["spec"]  # 1px specular arc following the rim
            else:
                c = _lerp(pal["top"], pal["bullet_bot"],
                          (y - top) / max(bot - top, 1))
            px[y][x] = (*c, 255)
    glyph = glyph_for("bullet", desig)
    gw, gh = len(glyph[0]), len(glyph)
    dx, dy = letter_offset("bullet", desig)
    _stamp_letter(px, glyph, (size - gw) // 2 + dx, (size - gh) // 2 + dy,
                  lambda x, y: 0 <= x < size and 0 <= y < size
                  and mask[y][x] == "#")
    return png_encode(size, size, px)


_LEGACY_BLACK = {"G", "N", "Q", "R", "W"}  # parity_check only: the old look


def flash_card(desig, legacy=False):
    """72x16 shaded field in the line color with the letter riding it — the
    XL letter for locals, the diamond-outline mark for expresses. Returns
    rows of (r, g, b). `legacy` reproduces the retired black-letter/no-shadow
    rendering so parity_check can still prove the pipeline byte-exact."""
    pal = palette_for(desig)
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

    in_card = lambda x, y: 0 <= x < w and 0 <= y < h  # noqa: E731

    if is_express(desig):
        # the countdown-clock express mark: the bullet's diamond outline
        # with the small letter inside, centered on the field
        ox, oy = (w - 15) // 2, 0
        outline = {
            (ox + x, oy + y)
            for y in range(15) for x in range(15)
            if DIAMOND_MASK[y][x] == "#"
            and any(not (0 <= y + ey < 15 and 0 <= x + ex < 15
                         and DIAMOND_MASK[y + ey][x + ex] == "#")
                    for ex, ey in ((1, 0), (-1, 0), (0, 1), (0, -1)))}
        glyph = glyph_for("flash", desig)
        gw, gh = len(glyph[0]), len(glyph)
        dx, dy = letter_offset("flash", desig)
        _stamp_letter(rows, glyph, ox + (15 - gw) // 2 + dx,
                      oy + (15 - gh) // 2 + dy, in_card, extra_ink=outline)
        return rows

    glyph = (XL_GLYPHS[letter_for(desig)] if legacy
             else glyph_for("flash", desig))
    gw, gh = len(glyph[0]), len(glyph)
    dx, dy = letter_offset("flash", desig)
    x0, y0 = (w - gw) // 2 + dx, (h - gh) // 2 + dy
    if legacy:
        ink = (0, 0, 0) if desig in _LEGACY_BLACK else (255, 255, 255)
        for gy, grow in enumerate(glyph):
            for gx, ch in enumerate(grow):
                if ch == "#":
                    rows[y0 + gy][x0 + gx] = ink
        return rows
    _stamp_letter(rows, glyph, x0, y0, in_card)
    return rows


def _rgb_bytes(rows):
    return b"".join(bytes(v for px in row for v in px) for row in rows)


def flash_anim_frames(desig, legacy=False):
    """Sweep-in (eased), hold, fade-to-black — raw RGB frames."""
    card = flash_card(desig, legacy=legacy)
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


# ------------------------------------------------ service-status art

def _plate_ramp(hexc):
    base = _hex_rgb(hexc)
    return {
        "spec": tuple(round(v + (255 - v) * 0.60) for v in base),
        "top": _scale(base, 1.15),
        "bot": _scale(base, 0.47),
        "lift": _scale(base, 0.62),
    }


WORD_INK_TOP = 2    # status word ink rows 2-8; marquee inks 10-13 below it
WORD_X = 19         # left-aligned after the bullet slot, firmware style


def _plate_pixels(hexc, hazard=False):
    """The busy-mode box as an RGBA grid: 1px side inset (the stock plates
    span cols 1-70), 1px specular top, vertical ramp, lifted bottom edge,
    3px corner vignette, radius-5 corners. `hazard` lays dashed stripes on
    the top and bottom rows (keep_out grammar)."""
    pal = _plate_ramp(hexc)
    w, h, r = 70, 16, 5
    dark = (24, 20, 2, 255)
    rows = []
    for y in range(h):
        if y == 0:
            base = pal["spec"]
        elif y == h - 1:
            base = pal["lift"]
        else:
            base = _lerp(pal["top"], pal["bot"], (y - 1) / (h - 2))
        row = [(0, 0, 0, 0)]  # left inset column
        for x in range(w):
            cx, cy = min(x, w - 1 - x), min(y, h - 1 - y)
            if cx < r and cy < r and (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                row.append((0, 0, 0, 0))
                continue
            edge = min(cx, cy)
            scale = (0.25, 0.5, 0.75)[edge] if edge < 3 else 1.0
            if hazard and y in (0, h - 1) and ((x + y) // 4) % 2:
                row.append(dark)
            else:
                row.append(tuple(round(c * scale) for c in base) + (255,))
        row.append((0, 0, 0, 0))  # right inset column
        rows.append(row)
    return rows


def make_status_screen(hexc, word, font="bold", motion="breathe",
                       hazard=False):
    """A status page authored the way the stock busy-mode screens are: the
    WORD baked at ink rows 2-8 (left x=19, firmware shadow) onto a shaded
    plate — compiled as a short LOOPING .anim so each screen keeps exactly
    one living element, like the firmware's own modes:
      breathe — calm brightness sine (dnd grammar; red plates)
      crawl   — hazard dashes marching along rows 0/15 (keep_out grammar)
      sweep   — a soft gradient drifting through the fill (the REROUTED
                look)
    Returns {"anim": bytes, "png": frame-0 PNG} — the PNG feeds previews.
    Baking sidesteps the text elements' 2-row font leading (measured on
    hardware), which once pushed a line clean off the panel."""
    pal = _plate_ramp(hexc)
    table = {"bold": BULLET_GLYPHS, "condensed": CONDENSED_GLYPHS}[font]
    ink = set()
    x = WORD_X
    for ch in word.upper():
        if ch == " ":
            x += 4
            continue
        g = table[ch]
        for gy, grow in enumerate(g):
            for gx, c in enumerate(grow):
                if c == "#":
                    ink.add((x + gx, WORD_INK_TOP + gy))
        x += len(g[0]) + 1
    if x - 1 > 70:
        raise SystemExit(f"status word {word!r} is {x - 1}px — over the "
                         "plate (52px text area)")
    shadow = {(px, py + 1) for px, py in ink
              if (px, py + 1) not in ink and py + 1 < 16}

    n, fps = {"breathe": (32, 16), "crawl": (8, 8),
              "sweep": (24, 15)}[motion]
    dark = (24, 20, 2)
    w, h, r = 70, 16, 5
    frames_rgb = []
    frame0_rgba = None
    for i in range(n):
        t = i / n
        k = 0.93 + 0.07 * math.sin(t * 2 * math.pi) if motion == "breathe" \
            else 1.0
        rgba = []
        for y in range(h):
            if y == 0:
                base = pal["spec"]
            elif y == h - 1:
                base = pal["lift"]
            else:
                base = _lerp(pal["top"], pal["bot"], (y - 1) / (h - 2))
            row = [(0, 0, 0, 0)]
            for x in range(w):
                cx, cy = min(x, w - 1 - x), min(y, h - 1 - y)
                if cx < r and cy < r and \
                        (r - cx) ** 2 + (r - cy) ** 2 > r * r:
                    row.append((0, 0, 0, 0))
                    continue
                edge = min(cx, cy)
                scale = (0.25, 0.5, 0.75)[edge] if edge < 3 else 1.0
                c = base
                if motion == "sweep" and 0 < y < h - 1:
                    v = 0.75 + 0.25 * math.sin(
                        (x + y * 2 - i * 3) * math.pi / 12)
                    c = tuple(min(255, round(cc * v)) for cc in c)
                if hazard and y in (0, h - 1):
                    off = i if motion == "crawl" else 0
                    if ((x + off + y) // 4) % 2:
                        row.append((*dark, 255))
                        continue
                row.append(tuple(min(255, round(cc * scale * k))
                                 for cc in c) + (255,))
            row.append((0, 0, 0, 0))
            rgba.append(row)
        for px, py in shadow:
            if rgba[py][px][3]:
                pp = rgba[py][px]
                rgba[py][px] = tuple(round(v * 0.4) for v in pp[:3]) + (255,)
        for px, py in ink:
            if rgba[py][px][3]:
                rgba[py][px] = (255, 255, 255, 255)
        if frame0_rgba is None:
            frame0_rgba = rgba
        frames_rgb.append(b"".join(
            bytes(v for pp in row for v in pp[:3]) for row in rgba))
    anim = anim_encode(frames_rgb, 72, 16, fps=fps)
    if len(anim) > 135_000:
        raise SystemExit(f"status anim {word!r} is {len(anim)}B — over the "
                         "cloud relay's ~150KiB request cap")
    return {"anim": anim, "png": png_encode(72, 16, frame0_rgba)}


def wash_anim_frames():
    """The page-swap wash, transition_select grammar: a soft amber ring
    blooms from the top edge while a wash rises fast and decays, ending
    black — the swap underneath lands invisibly (departure-flash trick)."""
    amber = (255, 176, 0)
    frames = []
    n = int(WASH_SECS * FPS)
    for i in range(n):
        t = i / FPS
        ring_r = 4 + (t / WASH_SECS) * 82
        ring_gain = max(0.0, 1.0 - t / (WASH_SECS * 0.75))
        wash = 0.85 * min(1.0, t / 0.10) * math.exp(-t / 0.22)
        fade = 1.0 if t < WASH_SECS - 0.15 else \
            max(0.0, (WASH_SECS - t) / 0.15)
        row_out = []
        for y in range(16):
            row = []
            for x in range(72):
                d = math.hypot(x - 36, y * 2.6)
                g = math.exp(-((d - ring_r) ** 2) / 50.0) * ring_gain + wash
                g = min(1.0, g) * fade
                row.append(tuple(min(255, round(c * g)) for c in amber))
            row_out.append(row)
        frames.append(b"".join(bytes(v for px in row for v in px)
                               for row in row_out))
    frames.append(b"\x00" * (72 * 16 * 3))  # end black, hold
    return frames


def text_width(text, font):
    """Pixel width of a status string, from the same glyph tables the
    device fonts were parsed into (status screens are all-caps)."""
    table = {"bold": BULLET_GLYPHS, "tiny": TINY_GLYPHS,
             "condensed": CONDENSED_GLYPHS, "small": SMALL_GLYPHS,
             "extra_large": XL_GLYPHS}[font]
    w = 0
    for ch in text.upper():
        if ch == " ":
            w += 3 if font == "tiny" else 4
        else:
            g = table.get(ch)
            # unknown chars (punctuation) get a safe overestimate so a
            # marquee pass never gets cut short
            w += (len(g[0]) + 1) if g else 4
    return max(w - 1, 1)


def build_status_assets():
    """The five baked status screens (looping anims + frame-0 PNGs) and
    the wash anim, content-hash named like every other asset."""
    out = {}
    for key, screen in (
            ("susp", make_status_screen("#7E1416", "NO TRAINS",
                                        font="condensed")),
            ("planned", make_status_screen("#FCC30B", "PLANNED",
                                           motion="crawl", hazard=True)),
            ("delayed", make_status_screen("#7E1416", "DELAYED")),
            ("alertpg", make_status_screen("#7E1416", "ALERT")),
            ("track", make_status_screen("#123A7A", "REROUTED",
                                         font="condensed",
                                         motion="sweep"))):
        digest = hashlib.sha256(screen["anim"]).hexdigest()[:8]
        out[key] = {"name": f"st_{key}-{digest}.anim",
                    "bytes": screen["anim"], "png": screen["png"]}
    wash = anim_encode(wash_anim_frames(), 72, 16)
    out["wash"] = {"name": f"wash-{hashlib.sha256(wash).hexdigest()[:8]}"
                           ".anim", "bytes": wash}
    return out


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

    def upload_assets(self, assets, extra=None):
        files = {}
        for a in assets.values():
            files[a["bullet_name"]] = a["bullet"]
            files[a["flash_name"]] = a["flash"]
        for a in (extra or {}).values():
            files[a["name"]] = a["bytes"]
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
            d = designator(route_id) if route_id else ""
            # base fallback: an express variant we didn't pre-build (say a
            # one-off 5X) still counts as its local
            if route_id and (d in want_desigs
                             or base_desig(d) in want_desigs):
                for t in hits:
                    if t:
                        yield t, route_id, trip_id


def decode_status(buf, stops):
    """Second pass over a feed for the status layer: vehicle positions
    (held-train detection) and the NYCT scheduled/actual track extension at
    the watched stops. Returns (held {trip: (secs, stop)}, track {trip:
    (scheduled, actual)}).

    Held = STOPPED_AT whose position timestamp lags the FEED's own header
    timestamp — NYCT stamps vehicles at their last movement. Comparing two
    feed clocks (not wall clock) means a stale snapshot can't mark the
    whole railroad as held; if the feed itself is stale we skip held
    detection entirely."""
    now = time.time()
    feed_ts = 0
    held, track = {}, {}
    for f, w, ent in _walk_fields(buf):
        if f == 1 and w == 2:  # FeedHeader{gtfs_version=1, ..., timestamp=3}
            for f2, w2, v2 in _walk_fields(ent):
                if f2 == 3 and w2 == 0:
                    feed_ts = v2
            continue
        if f != 2 or w != 2:
            continue
        for f2, w2, v2 in _walk_fields(ent):
            if f2 == 3 and w2 == 2:  # TripUpdate: track ext at our stops
                trip_id = ""
                diffs = []
                for f3, w3, v3 in _walk_fields(v2):
                    if f3 == 1 and w3 == 2:
                        for f4, w4, v4 in _walk_fields(v3):
                            if f4 == 1 and w4 == 2:
                                trip_id = v4.decode("utf-8", "replace")
                    elif f3 == 2 and w3 == 2:
                        stop, sched, act = "", "", ""
                        for f4, w4, v4 in _walk_fields(v3):
                            if f4 == 4 and w4 == 2:
                                stop = v4.decode("utf-8", "replace")
                            elif f4 == 1001 and w4 == 2:  # NyctStopTimeUpdate
                                for f5, w5, v5 in _walk_fields(v4):
                                    if f5 == 1 and w5 == 2:
                                        sched = v5.decode("utf-8", "replace")
                                    elif f5 == 2 and w5 == 2:
                                        act = v5.decode("utf-8", "replace")
                        if stop in stops and sched and act and sched != act:
                            diffs.append((sched, act))
                if trip_id and diffs:
                    track[trip_id] = diffs[0]
            elif f2 == 4 and w2 == 2:  # VehiclePosition
                trip_id, stop = "", ""
                status = ts = None
                for f3, w3, v3 in _walk_fields(v2):
                    if f3 == 1 and w3 == 2:
                        for f4, w4, v4 in _walk_fields(v3):
                            if f4 == 1 and w4 == 2:
                                trip_id = v4.decode("utf-8", "replace")
                    elif f3 == 4 and w3 == 0:
                        status = v3
                    elif f3 == 5 and w3 == 0:
                        ts = v3
                    elif f3 == 7 and w3 == 2:
                        stop = v3.decode("utf-8", "replace")
                fresh = feed_ts and now - feed_ts < 120
                if fresh and trip_id and status == 1 and ts \
                        and feed_ts - ts > HELD_AFTER_SECS:
                    held[trip_id] = (feed_ts - ts,
                                     stop[:-1] if stop[-1:] in "NS" else stop)
    return held, track


def fetch_arrivals(cfg):
    """Return ([(departure_epoch, route_id, trip_id), ...] sorted by time,
    {"held": ..., "track": ...} for the status layer)."""
    now = time.time()
    stops = set(cfg["stops"])
    want = set(cfg["designators"])
    per_trip = {}
    held, track = {}, {}
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
        if ALERTS_ON:
            h, tr = decode_status(r.content, stops)
            held.update(h)
            track.update(tr)
    if errors:
        print(f"[{time.strftime('%H:%M:%S')}] feed trouble: "
              + "; ".join(errors), file=sys.stderr)
    if not got_any:
        raise RuntimeError("all MTA feeds failed")
    out = sorted(per_trip.values())
    return out[:MAX_ARRIVALS], {"held": held, "track": track}


def plain_text(text):
    """Alert copy -> device-safe ASCII: strip the [G]-style bullet tokens
    and icon markers, fold typographic punctuation."""
    text = re.sub(r"\[([0-9A-Z]+)\]", r"\1", text)
    text = re.sub(r"\[[^\]]+ icon\]\s*", "", text)
    for a, b in (("—", "-"), ("–", "-"), ("•", "-"),
                 ("’", "'"), ("‘", "'"), ("“", '"'),
                 ("”", '"'), (" ", " ")):
        text = text.replace(a, b)
    return text.encode("ascii", "ignore").decode()


def fetch_alerts(cfg):
    """Currently-active Mercury alerts scoped to this station's routes and
    platforms. Returns [{kind, type, head, period, routes}] sorted most
    severe first (suspension > delays > planned > other)."""
    now = time.time()
    want_routes = {base_desig(d) for d in cfg["designators"]}
    want_stops = {s[:-1] if s[-1:] in "NS" else s for s in cfg["stops"]}
    r = requests.get(ALERTS_URL, timeout=15)
    r.raise_for_status()
    out = []
    for e in r.json().get("entity", []):
        a = e.get("alert", {})
        merc = a.get("transit_realtime.mercury_alert", {})
        windows = a.get("active_period", [])
        if windows and not any(
                p.get("start", 0) <= now <= p.get("end", 2 ** 62)
                for p in windows):
            continue
        ents = a.get("informed_entity", [])
        # the feed carries raw GTFS route_ids (GS, FS, H, SI) — collapse
        # them exactly like every other feed path before matching
        routes = {base_desig(designator(i["route_id"]))
                  for i in ents if i.get("route_id")}
        stops_hit = {s[:-1] if s[-1:] in "NS" else s
                     for s in (i.get("stop_id") for i in ents) if s}
        stop_scoped = bool(stops_hit)
        hit_routes = routes & want_routes
        if not hit_routes:
            continue
        if stop_scoped and not (stops_hit & want_stops):
            continue
        head = next((t["text"] for t in
                     a.get("header_text", {}).get("translation", [])
                     if t.get("language") == "en"), "")
        if not head:
            continue
        period = next((t["text"] for t in
                       merc.get("human_readable_active_period", {})
                       .get("translation", [])
                       if t.get("language") == "en"), "")
        atype = merc.get("alert_type", "")
        if "Suspended" in atype and not atype.startswith("Planned"):
            kind = "suspension"
        elif atype == "Delays":
            kind = "delays"
        elif atype.startswith("Planned"):
            kind = ("suspension" if "Suspended" in atype else "planned")
        else:
            kind = "other"
        out.append({"kind": kind, "type": atype,
                    "head": plain_text(head), "period": plain_text(period),
                    "routes": sorted(hit_routes)})
    rank = {"suspension": 0, "delays": 1, "planned": 2, "other": 3}
    out.sort(key=lambda a: rank[a["kind"]])
    return out


# ----------------------------------------------------------------- rendering

def asset_desig(assets, route_id):
    """Art key for an arrival: its own designator when we built art for it,
    else its local's (a feed can surface an express we didn't pre-build)."""
    d = designator(route_id)
    return d if d in assets else base_desig(d)


def build_plate_screen(status_assets, screen_key, bullet_name,
                       marquee=None, marquee_color=WHITE):
    """A status page: the baked looping screen (plate + word + its one
    living element), the route bullet in the icon slot, and an optional
    marquee in the small face — top_left y=8 inks rows 10-14 with the
    measured 2-row leading. Element ids stay type-stable."""
    els = [{"id": "plate", "type": "animation",
            "path": status_assets[screen_key]["name"],
            "x": 0, "y": 0, "loop": True, "timeout": ELEMENT_TIMEOUT}]
    if bullet_name:
        els.append({"id": "bullet", "type": "image", "path": bullet_name,
                    "x": 1, "y": 0, "timeout": ELEMENT_TIMEOUT})
    if marquee:
        el = {"id": "mq", "type": "text", "text": marquee,
              "font": "small", "color": marquee_color,
              "x": WORD_X, "y": 8, "align": "top_left",
              "timeout": ELEMENT_TIMEOUT}
        win = 69 - WORD_X + 1
        if text_width(marquee, "small") > win:
            # scroll props only when the line actually overflows — a label
            # that fits must not carry them (firmware scrolls it anyway)
            el.update({"width": win, "scroll_rate": MARQUEE_RATE})
        els.append(el)
    return els


def marquee_pass_secs(text):
    """One full circular-scroll cycle for the small in-plate marquee — the
    LVGL formula: (text_px + 15px wait gap) * 60000 / rate."""
    return (text_width(text, "small") + 15) * 60.0 / MARQUEE_RATE


def build_screen(cfg, assets, arrivals, index, offset=0, alert_dot=False):
    """One arrival card, optionally shifted vertically by `offset` px."""
    els = []
    if not arrivals:
        routes_label = "/".join(
            d for d in cfg["designators"] if not is_express(d)) \
            or "/".join(cfg["designators"])
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
        "path": assets[asset_desig(assets, route)]["bullet_name"],
        "x": 1, "y": 0 + offset, "timeout": ELEMENT_TIMEOUT,
    })
    if alert_dot:
        # a live service alert exists: quiet amber corner dot on the
        # bullet; the full story plays as the periodic alert page
        els.append({
            "id": "adot", "type": "rectangle", "x": 12, "y": 0 + offset,
            "width": 3, "height": 3, "fill": "solid",
            "fill_colors": [AMBER], "border_width": 0,
            "timeout": ELEMENT_TIMEOUT,
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
            "fill_colors": [line_color(designator(r)) + "FF"],
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
    def __init__(self, bar, cfg, assets, status_assets=None):
        self.bar = bar
        self.cfg = cfg
        self.assets = assets
        self.status_assets = status_assets or {}
        self.arrivals = []
        self.alerts = []            # active Mercury alerts for this station
        self.held = {}              # trip_id -> (secs stuck, stop base)
        self.track = {}             # trip_id -> (scheduled, actual) track
        self.last_alert_page = time.time()  # settle before first interrupt
        self.page_hold_until = 0.0  # alert page on screen until then
        self.stop_names = ({r[0]: r[1] for r in load_stations()}
                           if self.status_assets else {})
        self.index = 0
        self.blocked = False        # a higher-priority app owns the screen
        self.last_dial = 0.0
        self.dot_count = 0
        self.canvas_mode = None     # "card" | "msg" | "plate_*" | None
        self.shown_key = None       # (dep_time, route, mins) last rendered
        self.lock = asyncio.Lock()  # serializes renders/animations

    # -- rendering ---------------------------------------------------------

    def displayed(self):
        if not self.arrivals:
            return None
        self.index = max(0, min(self.index, len(self.arrivals) - 1))
        return self.arrivals[self.index]

    def _alert(self, *kinds):
        for a in self.alerts:
            if a["kind"] in kinds:
                return a
        return None

    def _status_bullet(self, alert=None):
        """The bullet for a status plate: the alert's own line when we have
        art for it, else the station's first — a C-only suspension at an
        A/C/E station must not fly an A bullet."""
        for r in (alert or {}).get("routes", []):
            if r in self.assets:
                return self.assets[r]["bullet_name"]
        return self.assets[self.cfg["designators"][0]]["bullet_name"]

    def _screen_elements(self, offset=0):
        """What belongs on screen right now: a status takeover plate, or
        the ordinary card (with the amber dot during live alerts)."""
        if self.status_assets:
            if not self.arrivals:
                a = self._alert("suspension")
                if a:
                    mq = a["head"] + ("   " + a["period"]
                                      if a["period"] else "")
                    return build_plate_screen(
                        self.status_assets, "susp",
                        self._status_bullet(a),
                        marquee=mq.upper()), "plate_susp"
                a = self._alert("planned")
                if a:
                    mq = a["head"] + ("   " + a["period"]
                                      if a["period"] else "")
                    return build_plate_screen(
                        self.status_assets, "planned",
                        self._status_bullet(a),
                        marquee=mq.upper(),
                        marquee_color="#201A02FF"), "plate_plan"
            shown = self.displayed()
            if shown and shown[2] in self.held:
                secs, stop = self.held[shown[2]]
                name = self.stop_names.get(stop, stop)
                bullet = self.assets[
                    asset_desig(self.assets, shown[1])]["bullet_name"]
                return build_plate_screen(
                    self.status_assets, "delayed", bullet,
                    marquee=f"HELD {int(secs // 60)} MIN AT {name}".upper()
                ), "plate_delay"
        dot = bool(self.status_assets
                   and self._alert("delays", "suspension", "planned"))
        els = build_screen(self.cfg, self.assets, self.arrivals, self.index,
                           offset, alert_dot=dot)
        return els, ("card" if self.arrivals else "msg") + \
            ("+a" if dot else "")

    def _push(self, offset=0):
        """One draw attempt; tracks blocked state. Returns True if drawn."""
        els, mode = self._screen_elements(offset)
        # elements merge by id on the device, so start clean whenever the
        # shape of what we draw changes (fewer dots, card <-> plate <-> msg)
        if (len(self.arrivals) < self.dot_count
                or mode != self.canvas_mode) and self.canvas_mode:
            try:
                self.bar.clear()
            except requests.RequestException:
                pass
        drawn = self.bar.draw(els)
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
        self.page_hold_until = 0.0  # the dial always wins over alert pages
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
                                         asset_desig(self.assets,
                                                     departed_route))):
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

    def _pick_page(self):
        """The alert page worth interrupting the card for, most urgent
        first: live delays, a track change on the shown train, planned
        work coming up."""
        a = self._alert("delays")
        if a:
            return ("alertpg", a["head"].upper(), WHITE)
        a = self._alert("suspension")
        if a:  # partial suspension while trains still run here
            mq = a["head"] + ("   " + a["period"] if a["period"] else "")
            return ("susp", mq.upper(), "#FFD2CCFF")
        shown = self.displayed()
        if shown and shown[2] in self.track:
            sched, act = self.track[shown[2]]
            return ("track", f"THIS TRAIN RUNS ON TRACK {act} INSTEAD OF "
                             f"{sched}", WHITE)
        a = self._alert("planned")
        if a:
            mq = a["head"] + ("   " + a["period"] if a["period"] else "")
            return ("planned", mq.upper(), "#201A02FF")
        return None

    async def _page_recover(self):
        """Land back on the card with a clean canvas, whatever happened.
        Caller holds self.lock."""
        try:
            await asyncio.to_thread(self.bar.clear)
        except requests.RequestException:
            pass
        self.canvas_mode = None
        self.dot_count = 0
        try:
            await asyncio.to_thread(self._push)
        except requests.RequestException:
            pass

    async def alert_page(self):
        """The card ⇄ alert page cycle: an amber wash covers the swap to a
        full-screen plate, the headline makes one marquee pass, the wash
        brings the card back — transition_select grammar throughout. The
        lock is held only for the swap legs; during the hold the page is
        protected by page_hold_until, which user input (the dial) may
        override."""
        self.last_alert_page = time.time()
        page = self._pick_page()
        if not page or not self.status_assets:
            return
        screen_key, marquee, mcolor = page
        shown = self.displayed()
        bullet = (self.assets[asset_desig(self.assets, shown[1])]
                  ["bullet_name"] if shown else self._status_bullet())
        els = build_plate_screen(self.status_assets, screen_key, bullet,
                                 marquee=marquee,
                                 marquee_color=mcolor or "#FFD2CCFF")
        hold = (min(12.0, max(4.0, marquee_pass_secs(marquee) + 0.5))
                if marquee else 5.0)
        wash = [{"id": "wash", "type": "animation",
                 "path": self.status_assets["wash"]["name"],
                 "x": 0, "y": 0, "loop": False,
                 "timeout": ELEMENT_TIMEOUT}]
        async with self.lock:
            if self.blocked:
                return
            try:
                if not await asyncio.to_thread(self.bar.draw, wash):
                    self.blocked = True
                    return
                await asyncio.sleep(WASH_SECS)  # ends black and holds
                await asyncio.to_thread(self.bar.clear)
                self.canvas_mode = None
                self.dot_count = 0
                if not await asyncio.to_thread(self.bar.draw, els):
                    self.blocked = True
                    return
                self.canvas_mode = "alert_page"
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] alert page error: "
                      f"{e}", file=sys.stderr)
                await self._page_recover()
                return
        self.page_hold_until = time.time() + hold
        await asyncio.sleep(hold)
        self.page_hold_until = 0.0
        async with self.lock:
            if self.canvas_mode != "alert_page":
                return  # the dial already took the screen back
            try:
                await asyncio.to_thread(self.bar.draw, wash)
                await asyncio.sleep(WASH_SECS)
            except requests.RequestException as e:
                print(f"[{time.strftime('%H:%M:%S')}] alert page error: "
                      f"{e}", file=sys.stderr)
            await self._page_recover()
        self.last_alert_page = time.time()

    async def alerts_poller(self):
        while True:
            try:
                alerts = await asyncio.to_thread(fetch_alerts, self.cfg)
                if ([a["type"] for a in alerts]
                        != [a["type"] for a in self.alerts]):
                    kinds = ", ".join(a["type"] for a in alerts) or "clear"
                    print(f"[{time.strftime('%H:%M:%S')}] service status: "
                          f"{kinds}")
                self.alerts = alerts
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] alerts fetch error: "
                      f"{e}", file=sys.stderr)
            await asyncio.sleep(ALERTS_POLL_SECS)

    async def fetcher(self):
        while True:
            try:
                shown = self.displayed()
                self.arrivals, status = await asyncio.to_thread(
                    fetch_arrivals, self.cfg)
                self.held = status["held"]
                self.track = status["track"]
                match = shown and self._same_train(shown, self.arrivals)
                if time.time() < self.page_hold_until:
                    if match is not None:
                        self.index = match  # data stays fresh; page stays up
                elif shown and match is None and self.arrivals:
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
        """Retry while blocked; flip the minutes exactly on the boundary;
        interrupt with the alert page on its cadence."""
        while True:
            await asyncio.sleep(BLOCKED_RETRY_SECS if self.blocked
                                else TICK_SECS)
            d = self.displayed()
            if d is None:
                continue
            if self.blocked:
                await self.render()  # cheap 409 until we own the screen
                continue
            if time.time() < self.page_hold_until:
                continue  # an alert page owns the screen right now
            if (self.status_assets and self.arrivals
                    and (self.canvas_mode or "").startswith("card")
                    and time.time() - self.last_alert_page > ALERT_PAGE_EVERY
                    and self._pick_page()):
                await self.alert_page()
                continue
            mins_now = int(max(0, d[0] - time.time()) // 60)
            if self.shown_key != (d[0], d[1], mins_now):
                await self.render()

    async def idle_reset(self):
        while True:
            await asyncio.sleep(5)
            if (self.index != 0 and not self.blocked
                    and time.time() >= self.page_hold_until
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
        if self.status_assets:
            tasks.append(self.alerts_poller())
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

    async def demo_alerts(self):
        """Stage every service-status screen with fake data, in sequence:
        card with alert dot, the alert-page cycle, DELAYED plate, the
        EXPRESS/TRACK page, NO TRAINS plate, PLANNED WORK plate. The
        [demo] markers let capture tooling slice states deterministically."""
        def mark(name):
            print(f"[demo] {name}", flush=True)

        now = time.time()
        routes = self.cfg["route_ids"]
        self.arrivals = [
            (now + 420, routes[0], "demo-1"),
            (now + 900, routes[1 % len(routes)], "demo-2"),
            (now + 1260, routes[2 % len(routes)], "demo-3"),
        ]
        self.alerts = [{
            "kind": "delays", "type": "Delays",
            "head": f"{designator(routes[0])} trains are running with "
                    "delays while we investigate a signal problem",
            "period": "", "routes": [designator(routes[0])]}]
        mark("card_dot")
        await self.render()
        await asyncio.sleep(4)
        mark("alert_cycle")
        await self.alert_page()
        await asyncio.sleep(2)
        d = self.displayed()
        self.alerts = []
        self.held = {d[2]: (7 * 60, self.cfg["stops"][0][:-1])}
        mark("delayed")
        await self.render()
        await asyncio.sleep(6)
        self.held = {}
        self.track = {d[2]: ("D4", "D3")}
        self.last_alert_page = 0
        mark("track_cycle")
        await self.alert_page()
        self.track = {}
        await asyncio.sleep(1)
        self.arrivals = []
        self.alerts = [{
            "kind": "suspension", "type": "Planned - Part Suspended",
            "head": f"No {designator(routes[0])} trains between Court Sq "
                    "and Bedford-Nostrand Avs",
            "period": "Fri 9:45 PM to Mon 5:00 AM",
            "routes": [designator(routes[0])]}]
        mark("notrains")
        await self.render()
        await asyncio.sleep(7)
        self.alerts = [{
            "kind": "planned", "type": "Planned - Stops Skipped",
            "head": "Trains skip 4 Av-9 St, 15 St-Prospect Park and Fort "
                    "Hamilton Pkwy",
            "period": "Aug 21 - 24", "routes": [designator(routes[0])]}]
        mark("planned")
        await self.render()
        await asyncio.sleep(7)
        mark("end")
        await asyncio.to_thread(self.bar.clear)


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
    parser.add_argument("--demo-alerts", action="store_true",
                        help="stage every service-status screen with fake "
                             "data and exit")
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
    status_assets = build_status_assets() if ALERTS_ON else {}
    try:
        bar.upload_assets(assets, status_assets)
    except requests.RequestException as e:
        print(f"asset upload failed: {e}", file=sys.stderr)

    app = App(bar, cfg, assets, status_assets)
    try:
        asyncio.run(app.demo_alerts() if args.demo_alerts
                    else app.demo() if args.demo else app.run())
    except KeyboardInterrupt:
        try:
            bar.clear()
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
