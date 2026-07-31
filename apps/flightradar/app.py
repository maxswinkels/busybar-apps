#!/usr/bin/env python3
"""Flightradar: when a plane flies over, shows its callsign, route, altitude and speed.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
"""
import argparse
import json
import math
import signal
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

APP = "flightradar"
ADSB_URL_TEMPLATE = "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{dist}"
ADSBDB_URL_TEMPLATE = "https://api.adsbdb.com/v0/callsign/{callsign}"

# State machine constants
MIN_DWELL_S = 10
MISS_GRACE = 3
STATE_IDLE = "IDLE"
STATE_SHOWING = "SHOWING"
STATE_HOLDING = "HOLDING"

# Colors: warm cream palette
BRIGHT = "#F0F0DCFF"
DIM = "#6C6C63FF"
LED_COLOR = "#F0F0DCFF"

# Layout for the 5px-tall small font, two lines, no separator. The small-font ink
# sits ~2px lower than its nominal y on the bar, so line 1 at y=0 lands on ink rows
# 2-6 and line 2 at y=9 on rows 11-15: the lines sit at top and bottom with a roomy
# ~4px gap in the middle.
Y_LINE1 = 0
Y_LINE2 = 9
# The device font carries a 1px right-side bearing (advance = ink + 1). Anchoring
# right-aligned text one column past the 72px width lands the ink flush to the
# right edge, matching the flush left column.
X_RIGHT = 73

# Route cache: {callsign: (expires_time, route_dict|None)}
ROUTE_CACHE = {}
ROUTE_CACHE_FOUND = float('inf')  # routes found: forever
ROUTE_CACHE_NOT_FOUND = 6 * 3600  # 404: 6h TTL
ROUTE_CACHE_ERROR = 120  # network error: 120s TTL
ROUTE_CACHE_MAX_ENTRIES = 512
ROUTE_NET_SPACING = 1.0  # min seconds between adsbdb network calls
_last_route_net = 0.0    # timestamp of the last adsbdb network call


def parse_args():
    p = argparse.ArgumentParser(description="Flightradar for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--lat", type=float, default=52.37, help="(default: 52.37)")
    p.add_argument("--lon", type=float, default=4.89, help="(default: 4.89)")
    p.add_argument("--radius", type=float, default=15, help="search/trigger radius in NM (default: 15)")
    p.add_argument("--max-alt", type=int, default=40000, help="flyover altitude ceiling in ft (default: 40000)")
    p.add_argument("--interval", type=float, default=5, help="seconds between polls (default: 5)")
    p.add_argument("--mode", choices=["flyover", "ambient", "arrival"], default="flyover", help="screen behaviour (default: flyover)")
    p.add_argument("--show-secs", type=int, default=20, help="arrival: seconds per plane (default: 20)")
    p.add_argument("--hold", type=int, default=30, help="hold seconds after data lost (default: 30)")
    p.add_argument("--cycle-secs", type=int, default=30, help="ambient: rotate planes after N seconds (default: 30)")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()
    # Clamp radius to 1-250 NM
    args.radius = max(1.0, min(250.0, args.radius))
    # Floor interval at 1.0s
    args.interval = max(1.0, args.interval)
    return args


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _draw(host, elements, priority=60, led_notification_color=None):
    """POST /api/display/draw. Returns (status_code, body_text)."""
    body = {
        "application_name": APP,
        "priority": priority,
        "elements": elements,
    }
    if led_notification_color is not None:
        body["led_notification_color"] = led_notification_color
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


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_aircraft(lat, lon, radius_nm):
    """Fetch aircraft from adsb.fi. Returns list of ac dicts or raises on error."""
    url = ADSB_URL_TEMPLATE.format(lat=lat, lon=lon, dist=radius_nm)
    req = urllib.request.Request(url, headers={"User-Agent": "busybar-flightradar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
            return data.get("ac") or []
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("adsb.fi rate limit (429)") from e
        raise RuntimeError(f"adsb.fi HTTP {e.code}") from e
    except Exception as e:
        raise RuntimeError(f"adsb.fi fetch: {e}") from e


def _fetch_route(callsign):
    """Fetch route from adsbdb. Returns {iata, icao, city} or raises on error."""
    url = ADSBDB_URL_TEMPLATE.format(callsign=urllib.parse.quote(callsign))
    req = urllib.request.Request(url, headers={"User-Agent": "busybar-flightradar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
            response = data.get("response", {})
            route = response.get("flightroute", {})
            origin = route.get("origin", {})
            dest = route.get("destination", {})
            return {
                "origin_iata": origin.get("iata_code", ""),
                "origin_icao": origin.get("icao_code", ""),
                "origin_city": _ascii(origin.get("municipality", "")),
                "dest_iata": dest.get("iata_code", ""),
                "dest_icao": dest.get("icao_code", ""),
                "dest_city": _ascii(dest.get("municipality", "")),
            }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("NOT_FOUND") from e
        raise RuntimeError(f"adsbdb HTTP {e.code}") from e
    except Exception as e:
        raise RuntimeError(f"adsbdb fetch: {e}") from e


def _ascii(s):
    """Convert string to ASCII, dropping accents and unknown chars."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two (lat, lon) points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Normalization and selection
# ---------------------------------------------------------------------------

def _normalize(ac, home_lat, home_lon):
    """Filter and enrich aircraft. Returns dict or None if invalid."""
    if "lat" not in ac or "lon" not in ac:
        return None
    alt_baro = ac.get("alt_baro")
    if alt_baro == "ground":
        return None

    # Compute slant range in km
    horiz_km = _haversine(home_lat, home_lon, ac["lat"], ac["lon"])
    alt_ft = ac.get("alt_baro")
    if alt_ft is not None:
        slant_km = math.hypot(horiz_km, alt_ft * 0.0003048)
    else:
        slant_km = horiz_km

    return {
        "hex": ac.get("hex", ""),
        "callsign": (ac.get("flight") or "").strip().upper(),
        "lat": ac["lat"],
        "lon": ac["lon"],
        "alt_ft": alt_ft,
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "type": (ac.get("t") or "").strip(),
        "reg": (ac.get("r") or "").strip(),
        "horiz_km": horiz_km,
        "slant_km": slant_km,
    }


def _select(candidates, sel_state, now):
    """Pick the plane with the smallest slant range, with stickiness.

    Refreshes the currently shown plane from fresh data each poll (so its
    altitude/speed/position stay live), keeps it through a short miss grace when
    it briefly drops out of the feed, and only switches to a different aircraft
    when that one is clearly closer and the current one has been shown long
    enough. sel_state = {plane, shown_at, last_seen_at, misses} or None. Returns
    updated sel_state or None."""
    # Refresh / age the current selection against the fresh candidate set.
    if sel_state is not None:
        cur_hex = sel_state["plane"]["hex"]
        fresh = next((c for c in candidates if c["hex"] == cur_hex), None)
        if fresh is not None:
            sel_state = dict(sel_state, plane=fresh, last_seen_at=now, misses=0)
        else:
            misses = sel_state.get("misses", 0) + 1
            if misses <= MISS_GRACE:
                # Momentary drop from the feed: keep showing the last-known sample.
                return dict(sel_state, misses=misses)
            sel_state = None  # gone for good, concede the screen

    if not candidates:
        return sel_state  # None (idle) or conceded above

    challenger = min(candidates, key=lambda p: p["slant_km"])

    if sel_state is None:
        # First plane to show.
        return {"plane": challenger, "shown_at": now, "last_seen_at": now, "misses": 0}

    current = sel_state["plane"]
    # Switch only to a different aircraft that is clearly closer, after min dwell.
    if (challenger["hex"] != current["hex"]
            and challenger["slant_km"] < 0.8 * current["slant_km"]
            and (now - sel_state["shown_at"]) >= MIN_DWELL_S):
        return {"plane": challenger, "shown_at": now, "last_seen_at": now, "misses": 0}

    return sel_state


def _get_route(callsign, now, fetch_fn):
    """Get a cached route or fetch it, with TTL and a network-call throttle.

    Returns (route_dict or None, is_not_found). A cache hit never hits the
    network. On a miss, the adsbdb call is throttled to ROUTE_NET_SPACING apart;
    while throttled the route is reported as "pending" (None) without caching."""
    global _last_route_net
    if not callsign:
        return None, False

    # Prune the oldest (soonest-expiring) entry if the cache is full.
    if len(ROUTE_CACHE) >= ROUTE_CACHE_MAX_ENTRIES:
        oldest_key = min(ROUTE_CACHE, key=lambda k: ROUTE_CACHE[k][0], default=None)
        if oldest_key is not None:
            del ROUTE_CACHE[oldest_key]

    # Cache hit (found or negative) still within TTL.
    if callsign in ROUTE_CACHE:
        expires, route_dict = ROUTE_CACHE[callsign]
        if now < expires:
            return route_dict, route_dict is None
        del ROUTE_CACHE[callsign]

    # Throttle network calls; report pending until the spacing has elapsed.
    if now - _last_route_net < ROUTE_NET_SPACING:
        return None, False
    _last_route_net = now

    try:
        route_dict = fetch_fn(callsign)
        ROUTE_CACHE[callsign] = (now + ROUTE_CACHE_FOUND, route_dict)
        return route_dict, False
    except RuntimeError as e:
        if "NOT_FOUND" in str(e):
            ROUTE_CACHE[callsign] = (now + ROUTE_CACHE_NOT_FOUND, None)
            return None, True
        # Network / transient error: short TTL so it recovers.
        ROUTE_CACHE[callsign] = (now + ROUTE_CACHE_ERROR, None)
        return None, False


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_alt(alt_ft):
    """Format altitude for display."""
    if alt_ft is None:
        return "---"
    if alt_ft >= 10000:
        return "FL" + str(round(alt_ft / 100))
    return f"{round(alt_ft)}ft"


def _fmt_speed(gs):
    """Format ground speed for display."""
    if gs is None:
        return "---"
    return f"{round(gs)}kt"


def _fmt_ident(plane):
    """Format plane identity: callsign > reg > hex."""
    if plane["callsign"]:
        return plane["callsign"]
    if plane["reg"]:
        return plane["reg"]
    return plane["hex"].upper()


def _fmt_route(route):
    """Format route as 'AMS>LHR' or None if unknown."""
    if route is None:
        return None
    origin = route.get("origin_iata") or route.get("origin_icao", "")
    dest = route.get("dest_iata") or route.get("dest_icao", "")
    if not origin or not dest:
        return None
    return f"{origin}>{dest}"


# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------

def _build_frame(plane, route, tick, mode):
    """Build display frame from plane data.
    Returns list of element dicts."""
    ident = _fmt_ident(plane)
    alt_str = _fmt_alt(plane["alt_ft"])
    speed_str = _fmt_speed(plane["gs"])
    route_str = _fmt_route(route)

    elements = []

    # Line 1: callsign/ident on left, aircraft type on right (if available)
    elements.append({
        "id": "callsign", "type": "text", "text": ident, "x": 0, "y": Y_LINE1,
        "font": "small", "color": BRIGHT, "align": "top_left",
    })
    if plane["type"]:
        elements.append({
            "id": "type", "type": "text", "text": plane["type"], "x": X_RIGHT, "y": Y_LINE1,
            "font": "small", "color": DIM, "align": "top_right",
        })

    # Line 2: with a route, show it on the left and flap altitude<->speed on the
    # right; without a route, show altitude and speed side by side.
    if route_str:
        left_text = route_str
        right_text = alt_str if tick % 2 == 0 else speed_str
    else:
        left_text = alt_str
        right_text = speed_str
    elements.append({
        "id": "l2left", "type": "text", "text": left_text, "x": 0, "y": Y_LINE2,
        "font": "small", "color": BRIGHT, "align": "top_left",
    })
    elements.append({
        "id": "l2right", "type": "text", "text": right_text, "x": X_RIGHT, "y": Y_LINE2,
        "font": "small", "color": BRIGHT, "align": "top_right",
    })

    return elements


# ---------------------------------------------------------------------------
# State machine tick
# ---------------------------------------------------------------------------

def _tick(loop_state, aircraft, args, now, fetch_route_fn):
    """Process one poll cycle. Returns updated loop_state."""
    state = loop_state["state"]
    sel_state = loop_state["sel_state"]
    last_draw_time = loop_state.get("last_draw_time", now)
    shown_hexes = loop_state.get("shown_hexes", set())  # for arrival mode
    suppressed_hexes = loop_state.get("suppressed_hexes", {})  # hex -> expiry time
    last_error = loop_state.get("last_error")
    failure_count = loop_state.get("failure_count", 0)
    tick = loop_state.get("tick", 0)

    # Prune suppressed hexes
    suppressed_hexes = {h: t for h, t in suppressed_hexes.items() if t > now}

    if aircraft is None:
        # No fresh data
        if state == STATE_SHOWING:
            # Transition to HOLDING to keep showing last aircraft
            print(f"step {tick}: data loss -> state=HOLDING")
            loop_state["state"] = STATE_HOLDING
            loop_state["holding_started_at"] = now
            loop_state["failure_count"] = 0
            loop_state["tick"] = tick + 1
            return loop_state

        if state == STATE_HOLDING:
            # Check if hold period has expired
            holding_duration = now - loop_state.get("holding_started_at", now)
            if holding_duration > args.hold:
                # Hold expired, go IDLE
                if sel_state:
                    print(f"step {tick}: holding expired ({holding_duration:.1f}s > {args.hold}s) -> state=IDLE")
                try:
                    _clear(args.host)
                except Exception as e:
                    print(f"clear error: {e}")
                loop_state["state"] = STATE_IDLE
                loop_state["sel_state"] = None
            # else stay HOLDING with last-known plane
            loop_state["failure_count"] = 0
            loop_state["tick"] = tick + 1
            return loop_state

        # IDLE: stay idle
        loop_state["failure_count"] = 0
        loop_state["tick"] = tick + 1
        return loop_state

    # Fresh data received
    failure_count = 0

    # Normalize aircraft
    normalized = [_normalize(ac, args.lat, args.lon) for ac in aircraft]
    normalized = [ac for ac in normalized if ac is not None]

    # Apply mode-specific gates
    if args.mode == "flyover":
        gate_radius_km = args.radius * 1.852
        normalized = [ac for ac in normalized if ac["horiz_km"] <= gate_radius_km and (ac["alt_ft"] is None or ac["alt_ft"] <= args.max_alt)]
    elif args.mode == "arrival":
        gate_radius_km = args.radius * 1.852
        normalized = [ac for ac in normalized if ac["horiz_km"] <= gate_radius_km]
    # ambient: no gate

    # Select plane
    new_sel_state = _select(normalized, sel_state, now)

    cycle_ticks = max(1, round(args.cycle_secs / args.interval))
    if args.mode == "ambient" and tick % cycle_ticks == 0:
        # Ambient mode: cycle through planes periodically
        if len(normalized) > 1 and sel_state:
            idx = next((i for i, ac in enumerate(normalized) if ac["hex"] == sel_state["plane"]["hex"]), 0)
            next_idx = (idx + 1) % len(normalized)
            new_sel_state = {
                "plane": normalized[next_idx],
                "shown_at": now,
                "last_seen_at": now,
            }

    if new_sel_state is None:
        # No plane to show
        if state != STATE_IDLE:
            print(f"step {tick}: no candidate plane -> state=IDLE")
            try:
                _clear(args.host)
            except Exception as e:
                print(f"clear error: {e}")
        loop_state["state"] = STATE_IDLE
        loop_state["sel_state"] = None
        loop_state["failure_count"] = failure_count
        loop_state["tick"] = tick + 1
        return loop_state

    plane = new_sel_state["plane"]

    # Enrich the selected plane's route (cached; network throttled inside).
    route = None
    if plane["callsign"]:
        try:
            route, _ = _get_route(plane["callsign"], now, fetch_route_fn)
        except Exception as e:
            msg = str(e)
            if msg != last_error:
                print(f"route fetch error: {e}")
                last_error = msg

    # Check arrival mode suppression
    plane_hex = plane["hex"]
    if args.mode == "arrival":
        if plane_hex in suppressed_hexes:
            # Still suppressed, don't draw
            loop_state["state"] = STATE_HOLDING
            loop_state["sel_state"] = new_sel_state
            loop_state["holding_started_at"] = now
            loop_state["failure_count"] = failure_count
            loop_state["suppressed_hexes"] = suppressed_hexes
            loop_state["tick"] = tick + 1
            return loop_state

        if plane_hex not in shown_hexes:
            # First time seeing this hex, show it
            shown_hexes.add(plane_hex)
            suppressed_hexes[plane_hex] = now + 15 * 60  # 15min suppression

    # Draw the frame
    frame = _build_frame(plane, route, tick, args.mode)
    led_color = None

    if state != STATE_SHOWING or sel_state["plane"]["hex"] != plane_hex:
        # Newly showing or switched: fire LED
        if args.mode != "ambient":
            led_color = LED_COLOR

        if state != STATE_SHOWING:
            print(f"step {tick}: showing plane {_fmt_ident(plane)} -> state=SHOWING")
        else:
            print(f"step {tick}: switched to {_fmt_ident(plane)} -> state=SHOWING")

    priority = 50 if args.mode == "ambient" else 60
    try:
        status, _ = _draw(args.host, frame, priority=priority, led_notification_color=led_color)
        if status == 409:
            msg = "display busy (409)"
            if msg != last_error:
                print(f"{msg}, continuing")
                last_error = msg
        else:
            state = STATE_SHOWING
            last_draw_time = now
    except Exception as e:
        msg = str(e)
        if msg != last_error:
            print(f"draw error: {e}")
            last_error = msg

    loop_state["state"] = state
    loop_state["sel_state"] = new_sel_state
    loop_state["last_draw_time"] = last_draw_time
    loop_state["failure_count"] = failure_count
    loop_state["last_error"] = last_error
    loop_state["shown_hexes"] = shown_hexes
    loop_state["suppressed_hexes"] = suppressed_hexes
    loop_state["tick"] = tick + 1
    return loop_state


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_loop(args, fetch_aircraft_fn, fetch_route_fn, sleep_fn):
    """Main loop: fetch aircraft, select, draw, repeat."""
    loop_state = {
        "state": STATE_IDLE,
        "sel_state": None,
        "last_draw_time": time.time(),
        "shown_hexes": set(),
        "suppressed_hexes": {},
        "last_error": None,
        "failure_count": 0,
        "tick": 0,
    }

    last_aircraft = []
    last_aircraft_count = 0

    while True:
        now = time.time()

        # Fetch aircraft
        aircraft = None
        try:
            aircraft = fetch_aircraft_fn(args.lat, args.lon, args.radius)
            last_aircraft = aircraft
            last_aircraft_count = 0
        except RuntimeError as e:
            msg = str(e)
            if "rate limit" in msg.lower():
                # Back off on rate limit
                print(f"rate limit, backing off")
                sleep_fn(min(60, args.interval * (2 ** last_aircraft_count)))
                last_aircraft_count += 1
                aircraft = last_aircraft if last_aircraft_count <= 5 else None
            else:
                # Other error: keep last aircraft once
                last_aircraft_count += 1
                aircraft = last_aircraft if last_aircraft_count <= 5 else None

        # Process one tick
        loop_state = _tick(loop_state, aircraft, args, now, fetch_route_fn)

        sleep_fn(args.interval)


def main():
    args = parse_args()

    # Install SIGTERM handler for Apps-tab Stop button
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if args.test:
        _run_test(args)
        return

    print(f"flightradar -> {_base(args.host)}  lat={args.lat} lon={args.lon}  mode={args.mode}  (Ctrl-C to stop)")

    try:
        run_loop(args, _fetch_aircraft, _fetch_route, time.sleep)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            _clear(args.host)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------

class _TestState:
    """Container for test state."""
    def __init__(self):
        self.step = 0

def _run_test(args):
    """Run test with scripted aircraft data."""
    test_state = _TestState()

    def fake_fetch_aircraft_fn(lat, lon, radius_nm):
        """Simulate aircraft observations over multiple polls."""
        step = test_state.step
        test_state.step += 1

        if step == 0:
            # Empty: transition to IDLE
            return []
        elif step == 1:
            # KLM1234 low and close, TRA5150 similar distance, a ground plane, blank-callsign
            # Should show KLM1234 and fire LED
            return [
                {
                    "hex": "ABCD0001",
                    "flight": "KLM1234",
                    "lat": lat + 0.01,
                    "lon": lon + 0.01,
                    "alt_baro": 2400,
                    "gs": 180,
                    "track": 45,
                    "t": "B738",
                    "r": "PH-BGC",
                },
                {
                    "hex": "ABCD0002",
                    "flight": "TRA5150",
                    "lat": lat + 0.011,
                    "lon": lon + 0.011,
                    "alt_baro": 2500,
                    "gs": 185,
                    "track": 46,
                    "t": "A320",
                    "r": "N123AB",
                },
                {
                    "hex": "ABCD0003",
                    "flight": "GND001",
                    "lat": lat + 0.02,
                    "lon": lon + 0.02,
                    "alt_baro": "ground",
                    "gs": 10,
                    "track": 0,
                    "t": "C172",
                    "r": "PH-XYZ",
                },
                {
                    "hex": "ABCD0004",
                    "flight": "",
                    "lat": lat + 0.005,
                    "lon": lon + 0.005,
                    "alt_baro": 5000,
                    "gs": 200,
                    "track": 90,
                    "t": "B787",
                    "r": "N999ZZ",
                },
            ]
        elif step == 2:
            # Same KLM1234, no TRA5150 (test near-tie hysteresis keeps KLM)
            return [
                {
                    "hex": "ABCD0001",
                    "flight": "KLM1234",
                    "lat": lat + 0.0101,
                    "lon": lon + 0.0101,
                    "alt_baro": 2410,
                    "gs": 180,
                    "track": 45,
                    "t": "B738",
                    "r": "PH-BGC",
                },
            ]
        elif step == 3:
            # Missing from poll (test HOLDING with miss grace)
            return []
        elif step == 4:
            # Back: KLM1234 reappears (test miss grace recovery)
            return [
                {
                    "hex": "ABCD0001",
                    "flight": "KLM1234",
                    "lat": lat + 0.0102,
                    "lon": lon + 0.0102,
                    "alt_baro": 2420,
                    "gs": 180,
                    "track": 45,
                    "t": "B738",
                    "r": "PH-BGC",
                },
            ]
        elif step == 5:
            # Go far: triggers IDLE (alt becomes too high or horiz distance too far)
            return [
                {
                    "hex": "ABCD0001",
                    "flight": "KLM1234",
                    "lat": lat + 0.5,
                    "lon": lon + 0.5,
                    "alt_baro": 35000,
                    "gs": 450,
                    "track": 90,
                    "t": "B738",
                    "r": "PH-BGC",
                },
            ]
        elif step <= 8:
            # A few empty polls: miss grace expires and the screen clears -> IDLE
            return []
        else:
            # Script exhausted: end the test cleanly
            raise SystemExit

    def fake_fetch_route_fn(callsign):
        """Fake route lookups with caching."""
        if callsign == "KLM1234":
            return {
                "origin_iata": "AMS",
                "origin_icao": "EHAM",
                "origin_city": "Amsterdam",
                "dest_iata": "LHR",
                "dest_icao": "EGLL",
                "dest_city": "London",
            }
        elif callsign == "TRA5150":
            raise RuntimeError("NOT_FOUND")
        else:
            raise RuntimeError("unknown route")

    print(f"flightradar test -> {_base(args.host)}  lat={args.lat} lon={args.lon}  mode={args.mode}")

    # Demonstrate route lookup + negative caching with spaced fake timestamps.
    global _last_route_net
    _last_route_net = 0.0
    ROUTE_CACHE.clear()
    print("route cache:")
    r, nf = _get_route("KLM1234", 1000.0, fake_fetch_route_fn)
    print(f"  KLM1234 -> {_fmt_route(r)}")
    r, nf = _get_route("KLM1234", 1000.2, fake_fetch_route_fn)
    print(f"  KLM1234 (cache hit) -> {_fmt_route(r)}")
    r, nf = _get_route("TRA5150", 1002.0, fake_fetch_route_fn)
    print(f"  TRA5150 -> {_fmt_route(r)}  not_found={nf}")
    r, nf = _get_route("TRA5150", 1002.2, fake_fetch_route_fn)
    print(f"  TRA5150 (neg cache) -> {_fmt_route(r)}  not_found={nf}")
    _last_route_net = 0.0
    ROUTE_CACHE.clear()

    try:
        run_loop(args, fake_fetch_aircraft_fn, fake_fetch_route_fn, lambda s: time.sleep(0.6))
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            _clear(args.host)
        except Exception:
            pass


if __name__ == "__main__":
    main()
