#!/usr/bin/env python3
"""
BUSY Bar Dashboard
===================
Cycles through: clock, weather (with moon phase), Charleston Harbor tides,
and season-aware sports scores (Pirates, Steelers, WVU, South Carolina, Clemson).

CONNECTION
----------
Connect BUSY Bar to this PC via USB. Its IP is fixed at 10.0.4.20.
Confirm the exact HTTP API shape (param names, field names) against the
live docs at http://10.0.4.20/docs before your first run -- this script
was written against the BUSY Bar HTTP API documentation and examples
published in BUSY's blog post ("How to Make BUSY Bar Widgets Without
Coding"), but has NOT been tested against physical hardware. Expect to
tweak font sizes / x,y offsets / colors once you see it on the real
72x16 screen.

DEPENDENCIES
------------
pip install requests pillow websocket-client --break-system-packages

RUN
---
python3 2026-07-19-busy-bar-dashboard.py
"""

import io
import json
import math
import os
import socket
import sys
import struct
import time
import datetime
import threading

import requests
from PIL import Image

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
# City, tide station, and teams live in config.json (same folder as this
# script) so they can be edited without touching code. If config.json is
# missing or malformed, the defaults below (Charleston, SC) are used and
# a fresh config.json is written so there's something to edit next time.

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

DEFAULT_CONFIG = {
	# BUSY Bar's IP -- 10.0.4.20 is the fixed USB virtual-LAN address. Point
	# this at 127.0.0.1:8080 (with a port) to run against the BUSY Bar
	# Emulator (https://github.com/maxswinkels/busybar-emulator) instead of
	# real hardware -- handy for trying this out or hacking on it before
	# your own bar arrives. Can also be overridden per-run without editing
	# this file: BUSY_BAR_HOST=127.0.0.1:8080 python3 dashboard.py
	"busy_ip": "10.0.4.20",
	"city_name": "Charleston",
	"lat": 32.7765,
	"lon": -79.9311,
	"tide_station": "8665530",  # NOAA Tides & Currents station ID (free, no key)
	# Dashboard stops drawing during this daily window (24h "HH:MM", wraps
	# past midnight). BUSY Bar's own physical power switch/state isn't
	# visible over the HTTP API (confirmed via REST polling and a WebSocket
	# capture -- see 2026-07-20-test-busybar-ws.py), so this schedule is a
	# practical stand-in rather than true switch detection.
	"quiet_hours_start": "22:00",
	"quiet_hours_end": "08:00",
	"teams": [
		# [display name, ESPN sport path, ESPN league path, team slug]
		["Pirates", "baseball", "mlb", "pit"],
		["Steelers", "football", "nfl", "pit"],
		["WVU", "football", "college-football", "wvu"],
		["S Carolina", "football", "college-football", "sc"],
		["Clemson", "football", "college-football", "clemson"],
	],
}


def load_config():
	if not os.path.exists(CONFIG_PATH):
		with open(CONFIG_PATH, "w") as f:
			json.dump(DEFAULT_CONFIG, f, indent=2)
		print(f"[config] no config.json found -- wrote defaults to {CONFIG_PATH}")
		return dict(DEFAULT_CONFIG)
	try:
		with open(CONFIG_PATH) as f:
			cfg = json.load(f)
		merged = dict(DEFAULT_CONFIG)
		merged.update(cfg)
		return merged
	except (json.JSONDecodeError, OSError) as e:
		print(f"[config] failed to read {CONFIG_PATH} ({e}) -- using defaults")
		return dict(DEFAULT_CONFIG)


CONFIG = load_config()

# Precedence: --host argv (the emulator's Apps runner passes this) >
# BUSY_BAR_HOST env var > "busy_ip" in config.json > hardcoded default.
def _host_from_argv():
	if "--host" in sys.argv:
		i = sys.argv.index("--host")
		if i + 1 < len(sys.argv):
			return sys.argv[i + 1]
	return None

BUSY_IP = _host_from_argv() or os.environ.get("BUSY_BAR_HOST") or CONFIG.get("busy_ip", "10.0.4.20")
# Wi-Fi (192.168.55.235) returned {"error":"Forbidden"} without further
# authorization -- see notes below main(). USB virtual LAN (10.0.4.20) works.
BASE_URL = f"http://{BUSY_IP}"
APP_ID = "dashboard"

ICON_DIR = os.path.join(SCRIPT_DIR, "icons")
os.makedirs(ICON_DIR, exist_ok=True)

CITY_NAME = CONFIG["city_name"]
LAT, LON = CONFIG["lat"], CONFIG["lon"]

# NOAA Tides & Currents station (free, no API key)
TIDE_STATION = CONFIG["tide_station"]

# How long each screen stays up, in seconds
CLOCK_DWELL = 6
WEATHER_DWELL = 6
MOON_DWELL = 5
TIDE_DWELL = 6
SPORTS_DWELL = 6

# How often background data refreshes
WEATHER_REFRESH_SEC = 15 * 60      # 15 min
TIDE_REFRESH_SEC = 24 * 60 * 60    # once a day
SPORTS_REFRESH_SEC = 60 * 60       # hourly
MOON_REFRESH_SEC = 24 * 60 * 60    # once a day

# A team counts as "in season" if it has a game within this many days
# before or after today. Self-adjusting across years -- no hardcoded
# season calendar to maintain.
SEASON_WINDOW_DAYS = 10

# Sports teams: (display name, ESPN sport path, ESPN league path, team slug)
TEAMS = [tuple(t) for t in CONFIG["teams"]]

# Daily window during which the dashboard stops drawing (see note above
# DEFAULT_CONFIG). "HH:MM" 24h, wraps past midnight if start > end.
QUIET_HOURS_START = CONFIG["quiet_hours_start"]
QUIET_HOURS_END = CONFIG["quiet_hours_end"]

# ----------------------------------------------------------------------------
# BUSY BAR HTTP API HELPERS
# ----------------------------------------------------------------------------

def busy_upload(filename, image_bytes):
	"""Upload an image asset to BUSY Bar. Skips re-upload if already cached
	locally (BUSY Bar itself doesn't dedupe -- this just avoids re-uploading
	every loop iteration for icons that don't change)."""
	url = f"{BASE_URL}/api/assets/upload"
	try:
		resp = requests.post(
			url,
			params={"application_name": APP_ID, "file": filename},
			data=image_bytes,
			headers={"Content-Type": "application/octet-stream"},
			timeout=5,
		)
		resp.raise_for_status()
	except requests.RequestException as e:
		body = getattr(e, "response", None)
		detail = body.text if body is not None else ""
		print(f"[busy_upload] failed for {filename}: {e} {detail}")


# True while our last draw attempt was rejected because a higher-priority
# app -- e.g. an active BUSY work-session interval timer (priority 90) or
# BUSY's own ON CALL indicator (priority 50) -- currently owns the display.
# We draw at 30, below both, so we never collide with ON CALL or a session
# timer; BUSY Bar returns 409 "Not drawn due to low priority" if we ever do
# lose out, and we detect it and back off instead of fighting it every cycle.
_priority_blocked = False


def _is_low_priority_rejection(exc):
	resp = getattr(exc, "response", None)
	if resp is None or resp.status_code != 409:
		return False
	return "low priority" in resp.text.lower()


def busy_clear():
	"""Clear all currently-displayed elements for this app. BUSY Bar only
	replaces an element when a new one arrives with the SAME id -- elements
	with different ids just stack up on screen. Segments here use different
	ids from each other (icon/info vs icon/title/content vs logo/info), so
	each segment clears the display first rather than relying on id reuse."""
	url = f"{BASE_URL}/api/display/draw"
	try:
		resp = requests.delete(url, params={"application_name": APP_ID}, timeout=5)
		resp.raise_for_status()
	except requests.RequestException as e:
		if _is_low_priority_rejection(e):
			return  # busy_draw() below reports/tracks this; avoid double-logging
		body = getattr(e, "response", None)
		detail = body.text if body is not None else ""
		print(f"[busy_clear] failed: {e} {detail}")


def busy_draw(elements):
	"""Send a draw request with a list of text/image elements. Returns True
	on success, False if rejected -- most notably a 409 because a
	higher-priority app (e.g. an active work-session timer) currently owns
	the display."""
	global _priority_blocked
	url = f"{BASE_URL}/api/display/draw"
	payload = {"application_name": APP_ID, "priority": 30, "elements": elements}
	try:
		resp = requests.post(url, json=payload, timeout=5)
		resp.raise_for_status()
		_priority_blocked = False
		return True
	except requests.RequestException as e:
		if _is_low_priority_rejection(e):
			if not _priority_blocked:
				print("[busy_draw] a higher-priority app (e.g. an active "
					  "work-session timer) owns the display -- pausing "
					  "until it's free")
			_priority_blocked = True
		else:
			body = getattr(e, "response", None)
			detail = body.text if body is not None else ""
			print(f"[busy_draw] failed: {e} {detail}")
		return False


# Valid font enum per BUSY Bar's OpenAPI spec: tiny, small, normal, condensed,
# bold, large, extra_large, global. "medium"/"big" (used in BUSY's own blog
# examples) do NOT exist -- mapped to nearest real fonts below.
#
# scroll_rate is in PIXELS PER MINUTE (not per second). A value of 60 -- what
# BUSY's own blog example uses -- is about 1px/sec, i.e. visually static.
# SCROLL_RATE_PPM below is a comfortable reading speed; tune this one constant
# to speed up or slow down every scrolling element at once.
SCROLL_RATE_PPM = 360          # ~6 px/sec -- slower, readable pace
SCROLL_START_DELAY_MS = 800    # pause before a scroll cycle begins
SCROLL_REPEAT_DELAY_MS = 2000  # pause between scroll loops

# Rough average pixel width per character for the "normal" bitmap font at
# this resolution -- BUSY Bar doesn't publish exact font metrics, so this is
# an estimate used only to time how long a full scroll loop takes. Adjust if
# the ticker refreshes noticeably before or after 3 real loops on the device.
AVG_CHAR_PX = 4


def text_el(el_id, text, x, y, font="normal", color="#FFFFFFFF",
			width=72, scroll_rate=SCROLL_RATE_PPM, timeout=6):
	return {
		"id": str(el_id),
		"timeout": timeout,
		"type": "text",
		"text": text,
		"x": x,
		"y": y,
		"font": font,
		"color": color,
		"width": width,
		"scroll_rate": scroll_rate,
		"scroll_start_delay": SCROLL_START_DELAY_MS,
		"scroll_repeat_delay": SCROLL_REPEAT_DELAY_MS,
	}


def image_el(el_id, path, x, y, timeout=6):
	return {
		"id": str(el_id),
		"timeout": timeout,
		"type": "image",
		"path": path,
		"x": x,
		"y": y,
	}


# ----------------------------------------------------------------------------
# ICON PREP  (downloaded once, resized to 16x16, uploaded to BUSY Bar)
# ----------------------------------------------------------------------------

NOTO_BASE = "https://raw.githubusercontent.com/googlefonts/noto-emoji/main/png/32"

WEATHER_ICONS = {
	"sun": "u2600",
	"cloud": "u2601",
	"fog": "u1f32b",
	"partly": "u1f324",
	"rain": "u1f327",
	"snow": "u1f328",
}

MOON_ICONS = {
	"new": "u1f311",
	"waxing_crescent": "u1f312",
	"first_quarter": "u1f313",
	"waxing_gibbous": "u1f314",
	"full": "u1f315",
	"waning_gibbous": "u1f316",
	"last_quarter": "u1f317",
	"waning_crescent": "u1f318",
}

OTHER_ICONS = {
	"tide": "u1f30a",   # wave emoji
}


def generate_calendar_icon(day):
	"""Draw a 16x16 calendar-page icon with today's day-of-month number on
	it (e.g. a small red header band + the number below), instead of a
	generic calendar graphic. Regenerated once a day."""
	from PIL import ImageDraw, ImageFont

	img = Image.new("RGBA", (16, 16), (255, 255, 255, 255))
	draw = ImageDraw.Draw(img)

	# Red header band (top of the "calendar page")
	draw.rectangle([0, 0, 15, 3], fill=(217, 51, 63, 255))
	# Body outline
	draw.rectangle([0, 4, 15, 15], outline=(120, 120, 120, 255))

	text = str(day)
	try:
		font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 9)
	except Exception:
		font = ImageFont.load_default()

	bbox = draw.textbbox((0, 0), text, font=font)
	text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
	x = (16 - text_w) // 2 - bbox[0]
	y = 4 + (12 - text_h) // 2 - bbox[1]
	draw.text((x, y), text, fill=(30, 30, 30, 255), font=font)

	local_path = os.path.join(ICON_DIR, "calendar.png")
	img.save(local_path)
	with open(local_path, "rb") as f:
		busy_upload("calendar.png", f.read())
	return "calendar.png"


def prepare_icon(local_name, source_url):
	"""Download an icon, resize to 16x16, cache locally, upload to BUSY Bar."""
	local_path = os.path.join(ICON_DIR, f"{local_name}.png")
	if not os.path.exists(local_path):
		try:
			resp = requests.get(source_url, timeout=10)
			resp.raise_for_status()
			img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
			img = img.resize((16, 16), Image.LANCZOS)
			img.save(local_path)
		except Exception as e:
			print(f"[prepare_icon] failed for {local_name}: {e}")
			return None
	with open(local_path, "rb") as f:
		busy_upload(f"{local_name}.png", f.read())
	return f"{local_name}.png"


def prepare_team_logo(team_name, sport, league, slug):
	"""Fetch team badge from ESPN and upload to BUSY Bar."""
	local_path = os.path.join(ICON_DIR, f"{slug}_{league}.png")
	if not os.path.exists(local_path):
		try:
			resp = requests.get(
				f"http://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{slug}",
				timeout=10,
			)
			resp.raise_for_status()
			data = resp.json()
			logo_url = data["team"]["logos"][0]["href"]
			img_resp = requests.get(logo_url, timeout=10)
			img = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")
			img = img.resize((16, 16), Image.LANCZOS)
			img.save(local_path)
		except Exception as e:
			print(f"[prepare_team_logo] failed for {team_name}: {e}")
			return None
	with open(local_path, "rb") as f:
		busy_upload(f"{slug}_{league}.png", f.read())
	return f"{slug}_{league}.png"


def setup_icons():
	weather_icon_files = {}
	for key, code in WEATHER_ICONS.items():
		weather_icon_files[key] = prepare_icon(key, f"{NOTO_BASE}/emoji_{code}.png")

	moon_icon_files = {}
	for key, code in MOON_ICONS.items():
		moon_icon_files[key] = prepare_icon(key, f"{NOTO_BASE}/emoji_{code}.png")

	other_icon_files = {}
	for key, code in OTHER_ICONS.items():
		other_icon_files[key] = prepare_icon(key, f"{NOTO_BASE}/emoji_{code}.png")
	other_icon_files["clock"] = generate_calendar_icon(datetime.datetime.now().day)

	team_logo_files = {}
	for name, sport, league, slug in TEAMS:
		team_logo_files[slug + league] = prepare_team_logo(name, sport, league, slug)

	return weather_icon_files, moon_icon_files, other_icon_files, team_logo_files


# ----------------------------------------------------------------------------
# DATA FETCHERS
# ----------------------------------------------------------------------------

def wmo_to_icon_key(code):
	"""Map Open-Meteo's WMO weather code to one of our icon keys."""
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


def fetch_weather():
	"""Open-Meteo -- free, no API key required. Includes today's forecast
	high/low alongside current conditions."""
	try:
		resp = requests.get(
			"https://api.open-meteo.com/v1/forecast",
			params={
				"latitude": LAT,
				"longitude": LON,
				"current": "temperature_2m,weather_code",
				"daily": "temperature_2m_max,temperature_2m_min",
				"temperature_unit": "fahrenheit",
				"timezone": "auto",  # so "daily" aligns with the local calendar day
			},
			timeout=10,
		)
		resp.raise_for_status()
		payload = resp.json()
		current = payload["current"]
		daily = payload["daily"]
		return {
			"temp_f": round(current["temperature_2m"]),
			"high_f": round(daily["temperature_2m_max"][0]),
			"low_f": round(daily["temperature_2m_min"][0]),
			"icon_key": wmo_to_icon_key(current["weather_code"]),
		}
	except Exception as e:
		print(f"[fetch_weather] failed: {e}")
		return None


def compute_moon_phase():
	"""Local synodic-month calculation. No API call needed.
	Accurate to within a few hours -- fine for a glance-at-your-desk
	widget, not for precise illumination percentage."""
	known_new_moon = datetime.date(2000, 1, 6)
	days_since = (datetime.date.today() - known_new_moon).days
	synodic_month = 29.53058867
	phase = (days_since % synodic_month) / synodic_month  # 0.0 - 1.0

	phases = [
		(0.0, "new"), (0.125, "waxing_crescent"), (0.25, "first_quarter"),
		(0.375, "waxing_gibbous"), (0.5, "full"), (0.625, "waning_gibbous"),
		(0.75, "last_quarter"), (0.875, "waning_crescent"), (1.0, "new"),
	]
	labels = {
		"new": "New Moon", "waxing_crescent": "Waxing Crescent",
		"first_quarter": "First Quarter", "waxing_gibbous": "Waxing Gibbous",
		"full": "Full Moon", "waning_gibbous": "Waning Gibbous",
		"last_quarter": "Last Quarter", "waning_crescent": "Waning Crescent",
	}
	closest_key = min(phases, key=lambda p: abs(p[0] - phase))[1]
	return {"icon_key": closest_key, "label": labels[closest_key]}


def fetch_tides():
	"""NOAA Tides & Currents, station 8665530 (Charleston Harbor). Free, no key."""
	try:
		resp = requests.get(
			"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter",
			params={
				"product": "predictions",
				"application": "busy-bar-dashboard",
				"station": TIDE_STATION,
				"datum": "MLLW",
				"time_zone": "lst_ldt",
				"units": "english",
				"interval": "hilo",
				"format": "json",
				"date": "today",
			},
			timeout=10,
		)
		resp.raise_for_status()
		preds = resp.json().get("predictions", [])
		events = []
		for p in preds:
			dt = datetime.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")
			kind = "HIGH" if p["type"] == "H" else "LOW"
			events.append(f"{kind} {float(p['v']):.1f}ft {dt.strftime('%I:%M%p').lstrip('0')}")
		return events
	except Exception as e:
		print(f"[fetch_tides] failed: {e}")
		return []


def fetch_team_status(sport, league, slug):
	"""Return dict with last game, next game, live status, and whether the
	team is currently 'in season' (game within SEASON_WINDOW_DAYS)."""
	try:
		resp = requests.get(
			f"http://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams/{slug}/schedule",
			timeout=10,
		)
		resp.raise_for_status()
		events = resp.json().get("events", [])
	except Exception as e:
		print(f"[fetch_team_status] failed for {slug}/{league}: {e}")
		return None

	today = datetime.datetime.now(datetime.timezone.utc)
	last_game, next_game, live_game = None, None, None

	for ev in events:
		try:
			ev_date = datetime.datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
		except Exception:
			continue
		comp = ev.get("competitions", [{}])[0]
		status = comp.get("status", {}).get("type", {})
		state = status.get("state")  # "pre", "in", "post"

		competitors = comp.get("competitors", [])
		opponent, is_home = None, True
		for c in competitors:
			team_info = c.get("team", {})
			if team_info.get("abbreviation", "").lower() != slug.lower():
				opponent = team_info.get("shortDisplayName") or team_info.get("displayName")
			else:
				is_home = c.get("homeAway") == "home"

		entry = {
			"date": ev_date,
			"opponent": opponent or "TBD",
			"is_home": is_home,
			"state": state,
			"competitors": competitors,
		}

		if state == "in":
			live_game = entry
		elif state == "post" and (last_game is None or ev_date > last_game["date"]):
			last_game = entry
		elif state == "pre" and ev_date > today and (next_game is None or ev_date < next_game["date"]):
			next_game = entry

	ref_dates = [g["date"] for g in (last_game, next_game, live_game) if g]
	in_season = any(abs((d - today).days) <= SEASON_WINDOW_DAYS for d in ref_dates)

	def score_str(entry):
		if not entry:
			return None
		home = next((c for c in entry["competitors"] if c.get("homeAway") == "home"), {})
		away = next((c for c in entry["competitors"] if c.get("homeAway") == "away"), {})
		return f"{away.get('team',{}).get('abbreviation','?')} {away.get('score','-')} @ {home.get('team',{}).get('abbreviation','?')} {home.get('score','-')}"

	return {
		"in_season": in_season,
		"live": live_game,
		"last": last_game,
		"next": next_game,
		"last_score_str": score_str(last_game),
		"live_score_str": score_str(live_game),
	}


# ----------------------------------------------------------------------------
# BACKGROUND REFRESH (keeps HTTP calls off the render loop)
# ----------------------------------------------------------------------------

class DataStore:
	def __init__(self):
		self.weather = None
		self.moon = compute_moon_phase()
		self.tides = []
		self.teams = {}  # slug+league -> status dict
		self.ntp_offset = 0.0  # seconds to add to local time.time() to get network time
		self.switch_off = False  # switch is in a position with its own native UI (see PAUSE_SWITCH_POSITIONS)
		self.lock = threading.Lock()


store = DataStore()

NTP_SERVER = "time.apple.com"  # Mac-friendly default; pool.ntp.org also works
NTP_PORT = 123
NTP_DELTA = 2208988800  # seconds between NTP epoch (1900) and Unix epoch (1970)
NTP_REFRESH_SEC = 60 * 60  # resync hourly


def fetch_ntp_offset():
	"""Query a public NTP server and return (network_time - local_time) in
	seconds, so the clock displays real network time instead of trusting
	whatever the system clock happens to say."""
	try:
		packet = b"\x1b" + 47 * b"\0"
		with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
			s.settimeout(5)
			local_send_time = time.time()
			s.sendto(packet, (NTP_SERVER, NTP_PORT))
			data, _ = s.recvfrom(48)
		unpacked = struct.unpack("!12I", data)
		ntp_timestamp = unpacked[10] + float(unpacked[11]) / 2**32
		network_time = ntp_timestamp - NTP_DELTA
		local_now = time.time()
		# rough round-trip correction: assume half the request time has passed
		round_trip = local_now - local_send_time
		return (network_time + round_trip / 2) - local_now
	except Exception as e:
		print(f"[fetch_ntp_offset] failed: {e}")
		return None


def network_now():
	"""Current time adjusted by the last known NTP offset."""
	with store.lock:
		offset = store.ntp_offset
	return datetime.datetime.fromtimestamp(time.time() + offset)


def is_quiet_hours(now=None):
	"""True if we're currently inside the configured quiet-hours window.
	Handles the overnight case (e.g. 22:00-08:00, where start > end)."""
	now = now or network_now()
	start_h, start_m = (int(x) for x in QUIET_HOURS_START.split(":"))
	end_h, end_m = (int(x) for x in QUIET_HOURS_END.split(":"))
	start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
	end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
	if start <= end:
		return start <= now < end
	return now >= start or now < end


def refresh_loop():
	last_weather = 0
	last_tide = 0
	last_moon = 0
	last_sports = 0
	last_ntp = 0
	last_calendar_day = datetime.datetime.now().day  # set in setup_icons() at startup

	while True:
		now = time.time()

		if now - last_ntp > NTP_REFRESH_SEC:
			offset = fetch_ntp_offset()
			if offset is not None:
				with store.lock:
					store.ntp_offset = offset
				print(f"[ntp] synced, offset {offset:+.3f}s from system clock")
			last_ntp = now

		today = network_now().day
		if today != last_calendar_day:
			generate_calendar_icon(today)  # same filename ("calendar.png"),
			last_calendar_day = today      # so no need to update any dict

		if now - last_weather > WEATHER_REFRESH_SEC:
			w = fetch_weather()
			with store.lock:
				if w:
					store.weather = w
			last_weather = now

		if now - last_tide > TIDE_REFRESH_SEC:
			t = fetch_tides()
			with store.lock:
				store.tides = t
			last_tide = now

		if now - last_moon > MOON_REFRESH_SEC:
			with store.lock:
				store.moon = compute_moon_phase()
			last_moon = now

		if now - last_sports > SPORTS_REFRESH_SEC:
			for name, sport, league, slug in TEAMS:
				status = fetch_team_status(sport, league, slug)
				with store.lock:
					store.teams[slug + league] = status
			last_sports = now

		time.sleep(5)


# ----------------------------------------------------------------------------
# PHYSICAL SWITCH POSITION MONITOR (WebSocket)
# ----------------------------------------------------------------------------
# BUSY Bar's on-device switch is a real 5-position selector -- BUSY, CUSTOM,
# OFF, APPS, SETTINGS -- not a simple on/off toggle. Confirmed via the
# official protobuf schemas at https://github.com/busy-app/busybar-protobuf
# (input.proto: BSB_Input.SwitchPosition, OFF = 2). Position changes stream
# in real time over /api/status/ws as protobuf State messages.
#
# Rather than pull in the full generated protobuf/nanopb toolchain (which
# needs protoc/grpc_tools and the whole schema set), this hand-rolls just
# enough of a varint/tag walker to find one path in the message:
# State.updates[].input.switch_event.position. See state.proto and
# input.proto in that repo for the full message shapes.

try:
	import websocket  # websocket-client package
	_HAVE_WEBSOCKET = True
except ImportError:
	_HAVE_WEBSOCKET = False

WS_URL = f"ws://{BUSY_IP}/api/status/ws"
SWITCH_BUSY = 0  # BSB_Input.SwitchPosition.BUSY
SWITCH_OFF = 2   # BSB_Input.SwitchPosition.OFF
# Positions where BUSY Bar shows its own native UI (focus/busy status, or
# off) that our dashboard would otherwise silently draw over, since our
# priority-30 draws aren't rejected the way an active work session's are.
# CUSTOM/APPS/SETTINGS are left alone -- CUSTOM in particular is the
# position meant for exactly this kind of custom HTTP-API content.
PAUSE_SWITCH_POSITIONS = {SWITCH_BUSY, SWITCH_OFF}


def _read_varint(buf, pos):
	result = 0
	shift = 0
	while True:
		b = buf[pos]
		pos += 1
		result |= (b & 0x7F) << shift
		if not (b & 0x80):
			break
		shift += 7
	return result, pos


def _iter_protobuf_fields(buf):
	"""Yield (field_number, wire_type, value) for a flat protobuf buffer.
	value is an int for wire_type 0 (varint) or raw bytes for wire_type 2
	(length-delimited); fixed32/64 fields are skipped since nothing we care
	about here uses them."""
	pos = 0
	n = len(buf)
	while pos < n:
		tag, pos = _read_varint(buf, pos)
		field_no, wire_type = tag >> 3, tag & 0x7
		if wire_type == 0:
			value, pos = _read_varint(buf, pos)
			yield field_no, wire_type, value
		elif wire_type == 1:
			pos += 8
		elif wire_type == 2:
			length, pos = _read_varint(buf, pos)
			value = buf[pos:pos + length]
			pos += length
			yield field_no, wire_type, value
		elif wire_type == 5:
			pos += 4
		else:
			return  # unknown wire type -- bail rather than misparse the rest


def _extract_switch_position(state_message_bytes):
	"""Walk a top-level BSB_State.State message looking for
	updates[].input.switch_event.position (see input.proto/state.proto).
	Returns a SwitchPosition int, or None if this message doesn't carry a
	switch event at all (most messages are frames/timers/etc, not this)."""
	for field_no, wire_type, value in _iter_protobuf_fields(state_message_bytes):
		if field_no != 2 or wire_type != 2:       # State.updates (StateUpdate)
			continue
		for su_field, su_wt, su_val in _iter_protobuf_fields(value):
			if su_field != 11 or su_wt != 2:      # StateUpdate.input (InputEvent)
				continue
			for ie_field, ie_wt, ie_val in _iter_protobuf_fields(su_val):
				if ie_field != 2 or ie_wt != 2:    # InputEvent.switch_event
					continue
				position = 0  # SwitchPosition default (BUSY=0) if field omitted
				for se_field, se_wt, se_val in _iter_protobuf_fields(ie_val):
					if se_field == 1 and se_wt == 0:
						position = se_val
				return position
	return None


def switch_monitor_loop():
	"""Background thread: keep a WebSocket connection to BUSY Bar's status
	stream open and update store.switch_off whenever a switch_event arrives.
	Reconnects on any drop. Degrades gracefully -- switch_off just stays
	False, i.e. the dashboard behaves as if this feature doesn't exist -- if
	websocket-client isn't installed or the device is unreachable."""
	if not _HAVE_WEBSOCKET:
		print("[switch_monitor] websocket-client not installed -- switch "
			  "position detection disabled (pip install websocket-client)")
		return

	def on_message(ws, message):
		if not isinstance(message, (bytes, bytearray)):
			return  # text frames (if any) aren't the protobuf state stream
		try:
			position = _extract_switch_position(message)
		except Exception as e:
			print(f"[switch_monitor] failed to parse a state message: {e!r}")
			return
		if position is not None:
			with store.lock:
				store.switch_off = (position in PAUSE_SWITCH_POSITIONS)

	def on_open(ws):
		ws.send(json.dumps({"enable": True}))

	def on_error(ws, error):
		print(f"[switch_monitor] connection error: {error!r}")

	while True:
		try:
			ws = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=on_message,
										on_error=on_error)
			ws.run_forever(ping_interval=30, ping_timeout=10)
		except Exception as e:
			print(f"[switch_monitor] connection error: {e}")
		time.sleep(5)  # backoff before reconnecting


# ----------------------------------------------------------------------------
# TICKER (per-topic segments)
# ----------------------------------------------------------------------------
# A single text element can't swap icons mid-scroll, so each topic is its own
# draw call: icon fixed on the left, text scrolling on the right. Instead of
# a fixed dwell time per topic (the old "flipping" behavior), each segment
# stays up for LOOPS_BEFORE_REFRESH full scroll cycles of its own text, so
# motion never stops -- it just changes topic when the current one has
# scrolled past a few times.

LOOPS_BEFORE_REFRESH = 3
TEXT_WIDTH = 54       # width of the text area to the right of a 16px icon
FULL_WIDTH = 72        # width when there's no icon (e.g. the clock)
ICON_X = 0
TEXT_X = 18


# Only show a completed game's score if it happened this recently -- an old
# score from well outside this window isn't worth a segment (matches the
# in-season window's spirit, but tighter, since a score is only interesting
# while it's fresh).
RECENT_LAST_GAME_DAYS = 7


def team_status_kind(status):
	"""Return what should currently be shown for a team:
	("live", text) or ("next", text) -- single-line, as before -- or
	("last", opponent, score_text) for a completed game within
	RECENT_LAST_GAME_DAYS, which show_team_segment renders as two lines.
	None if nothing applies (e.g. last completed game is older than that)."""
	if status["live"]:
		return ("live", f"LIVE {status['live_score_str'] or 'In progress'}")
	if status["next"] and (not status["last"] or status["next"]["date"] < datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=SEASON_WINDOW_DAYS)):
		nxt = status["next"]
		vs = "vs" if nxt["is_home"] else "@"
		when = nxt["date"].astimezone().strftime("%a %m/%d %I:%M%p").lstrip("0")
		return ("next", f"NEXT {vs} {nxt['opponent']} {when}")
	if status["last"]:
		last = status["last"]
		days_ago = (datetime.datetime.now(datetime.timezone.utc) - last["date"]).days
		if days_ago <= RECENT_LAST_GAME_DAYS:
			when = last["date"].astimezone().strftime("%a %m/%d").lstrip("0")
			score_text = f"{status['last_score_str']} ({when})" if status["last_score_str"] \
				else f"vs {last['opponent']} ({when})"
			return ("last", last["opponent"], score_text)
	return None


def estimate_loop_seconds(text, width=TEXT_WIDTH):
	"""Estimate how long one full scroll cycle of `text` takes, so a segment
	can advance after LOOPS_BEFORE_REFRESH repeats instead of a fixed timer."""
	text_px = len(text) * AVG_CHAR_PX
	overflow_px = max(text_px - width, 0)
	scroll_px_per_sec = SCROLL_RATE_PPM / 60
	scroll_seconds = overflow_px / scroll_px_per_sec if scroll_px_per_sec else 0
	pause_seconds = (SCROLL_START_DELAY_MS + SCROLL_REPEAT_DELAY_MS) / 1000
	return scroll_seconds + pause_seconds


def hold_seconds(text, width=TEXT_WIDTH):
	return max(estimate_loop_seconds(text, width) * LOOPS_BEFORE_REFRESH, 5)


def show_clock_segment(other_icon_files):
	"""Same layout as tides: icon, small title (date) on top, small content
	(time) below -- both rows use 'small' font so the bottom row isn't cut
	off, matching what actually fits in the 16px height."""
	now = network_now()  # NTP-corrected, not just the system clock
	date_text = now.strftime("%m-%d-%Y")
	time_text = now.strftime("%I:%M %p").lstrip("0")
	icon_path = other_icon_files.get("clock")
	hold = max(hold_seconds(date_text), hold_seconds(time_text))
	busy_clear()
	elements = []
	if icon_path:
		elements.append(image_el("icon", icon_path, ICON_X, 0, timeout=math.ceil(hold) + 2))
	elements.append(text_el("title", date_text, TEXT_X, 0, font="small",
							 color="#AAAAAAFF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	elements.append(text_el("content", time_text, TEXT_X, 8, font="small",
							 color="#90EE90FF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	busy_draw(elements)
	return hold


def show_weather_segment(weather_icon_files):
	with store.lock:
		w = store.weather
	if not w:
		return 5
	icon_path = weather_icon_files.get(w["icon_key"])
	text = f"{CITY_NAME} {w['temp_f']}°F (H:{w['high_f']}° L:{w['low_f']}°)"
	hold = hold_seconds(text)
	busy_clear()
	elements = []
	if icon_path:
		elements.append(image_el("icon", icon_path, ICON_X, 0, timeout=math.ceil(hold) + 2))
	elements.append(text_el("info", text, TEXT_X, 5, font="normal",
							 color="#FFD700FF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	busy_draw(elements)
	return hold


def show_moon_segment(moon_icon_files):
	with store.lock:
		m = store.moon
	if not m:
		return 5
	icon_path = moon_icon_files.get(m["icon_key"])
	text = m["label"]
	hold = hold_seconds(text)
	busy_clear()
	elements = []
	if icon_path:
		elements.append(image_el("icon", icon_path, ICON_X, 0, timeout=math.ceil(hold) + 2))
	elements.append(text_el("info", text, TEXT_X, 5, font="normal",
							 color="#CCCCFFFF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	busy_draw(elements)
	return hold


def show_tide_segment(other_icon_files):
	"""Icon, small 'TIDE' title on top, scrolling content line below."""
	with store.lock:
		tides = list(store.tides)
	if not tides:
		return 5
	icon_path = other_icon_files.get("tide")
	content = "   |   ".join(tides)
	hold = hold_seconds(content)
	busy_clear()
	elements = []
	if icon_path:
		elements.append(image_el("icon", icon_path, ICON_X, 0, timeout=math.ceil(hold) + 2))
	elements.append(text_el("title", "TIDE", TEXT_X, 0, font="small",
							 color="#66CCFFFF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	elements.append(text_el("content", content, TEXT_X, 8, font="small",
							 color="#66CCFFFF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	busy_draw(elements)
	return hold


def show_team_segment(name, slug, league, team_logo_files):
	with store.lock:
		status = store.teams.get(slug + league)
	if not status or not status["in_season"]:
		return None  # signal: skip, not in season

	kind = team_status_kind(status)
	if kind is None:
		return None  # no live/upcoming game, and no completed game recently

	logo_path = team_logo_files.get(slug + league)

	if kind[0] == "last":
		# Two-line layout, same pattern as tides/clock: team name on top,
		# the score on the bottom line.
		_, _opponent, score_text = kind
		hold = max(hold_seconds(name), hold_seconds(score_text))
		busy_clear()
		elements = []
		if logo_path:
			elements.append(image_el("logo", logo_path, ICON_X, 0, timeout=math.ceil(hold) + 2))
		elements.append(text_el("title", name, TEXT_X, 0, font="small",
								 color="#FFCC66FF", width=TEXT_WIDTH,
								 timeout=math.ceil(hold) + 2))
		elements.append(text_el("content", score_text, TEXT_X, 8, font="small",
								 color="#FFCC66FF", width=TEXT_WIDTH,
								 timeout=math.ceil(hold) + 2))
		busy_draw(elements)
		return hold

	# LIVE / NEXT: unchanged single-line layout.
	_, status_text = kind
	text = f"{name}: {status_text}"
	hold = hold_seconds(text)
	busy_clear()
	elements = []
	if logo_path:
		elements.append(image_el("logo", logo_path, ICON_X, 0, timeout=math.ceil(hold) + 2))
	elements.append(text_el("info", text, TEXT_X, 5, font="normal",
							 color="#FFCC66FF", width=TEXT_WIDTH,
							 timeout=math.ceil(hold) + 2))
	busy_draw(elements)
	return hold


# ----------------------------------------------------------------------------
# MAIN LOOP
# ----------------------------------------------------------------------------

def run_and_wait(hold):
	"""Sleep for `hold` seconds -- or a short 15s retry interval instead, if
	the segment's draw was just rejected for low priority, so we check back
	in soon rather than waiting out a (possibly long) scroll-based hold time
	for content that isn't actually showing."""
	time.sleep(15 if _priority_blocked else hold)


def main():
	print("Preparing icons (first run may take a minute)...")
	weather_icon_files, moon_icon_files, other_icon_files, team_logo_files = setup_icons()

	print("Doing initial data fetch...")
	ntp_offset = fetch_ntp_offset()
	with store.lock:
		if ntp_offset is not None:
			store.ntp_offset = ntp_offset
			print(f"[ntp] initial sync, offset {ntp_offset:+.3f}s from system clock")
		store.weather = fetch_weather()
		store.tides = fetch_tides()
		store.moon = compute_moon_phase()
	for name, sport, league, slug in TEAMS:
		status = fetch_team_status(sport, league, slug)
		with store.lock:
			store.teams[slug + league] = status

	threading.Thread(target=refresh_loop, daemon=True).start()
	threading.Thread(target=switch_monitor_loop, daemon=True).start()

	print("Starting segment loop. Ctrl+C to stop.")
	was_quiet = False
	was_switch_off = False
	try:
		while True:
			with store.lock:
				switch_off = store.switch_off
			if switch_off:
				if not was_switch_off:
					print("[switch] BUSY/OFF position detected -- pausing display")
					busy_clear()
					was_switch_off = True
				time.sleep(5)
				continue
			if was_switch_off:
				print("[switch] moved to CUSTOM/APPS/SETTINGS -- resuming display")
				was_switch_off = False

			if is_quiet_hours():
				if not was_quiet:
					print(f"[quiet hours] {QUIET_HOURS_START}-{QUIET_HOURS_END}: "
						  f"pausing display")
					busy_clear()
					was_quiet = True
				time.sleep(60)
				continue
			if was_quiet:
				print("[quiet hours] window ended, resuming display")
				was_quiet = False

			def _switch_flipped_off():
				with store.lock:
					return store.switch_off

			run_and_wait(show_clock_segment(other_icon_files))
			if _switch_flipped_off():
				continue
			run_and_wait(show_weather_segment(weather_icon_files))
			if _switch_flipped_off():
				continue
			run_and_wait(show_moon_segment(moon_icon_files))
			if _switch_flipped_off():
				continue
			run_and_wait(show_tide_segment(other_icon_files))
			if _switch_flipped_off():
				continue
			for name, sport, league, slug in TEAMS:
				wait_seconds = show_team_segment(name, slug, league, team_logo_files)
				if wait_seconds is not None:
					run_and_wait(wait_seconds)
				if _priority_blocked or _switch_flipped_off():
					break
	except KeyboardInterrupt:
		print("Stopped.")


if __name__ == "__main__":
	main()
