#!/usr/bin/env python3
"""Workout Timer: Tabata, fixed-rest and fixed-work intervals on the BUSY Bar.

    python3 app.py                         # BUSY Bar over USB
    python3 app.py --host 192.168.1.50     # Wi-Fi: --token or BUSY_HTTP_PASSWORD
    python3 app.py --host 127.0.0.1:8080   # emulator

The wheel changes a value, OK confirms, BACK goes back, START starts. In a
workout OK skips the phase, START pauses (SKIP WITH in SETTINGS swaps them)
and BACK twice stops; on the recap, START repeats the workout. Settings are
saved in ~/.config/workout-timer/config.json (under %APPDATA% on Windows), or
in the file $WORKOUT_TIMER_CONFIG names.
"""
import argparse
import base64
import copy
import dataclasses
import http.client
import json
import math
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP = "workout-timer"
W, H = 72, 16

PRIORITY = 50              # above built-in apps (10), below a BUSY/CUSTOM session (90)
ELEMENT_TIMEOUT = 15       # s; a killed app doesn't leave a frozen timer behind
REFRESH = 5.0              # s between redraws of an unchanged frame, renewing its timeout
RETRY = 1.0                # s before retrying a failed draw; after a 409, it retakes the screen


# --- timer logic: a pure state machine, no I/O ------------------------------
# A fixed phase ends at exactly start + duration, however late update() runs.

TABATA, FIX_REST, FIX_WORK = "tabata", "fix_rest", "fix_work"
GET_READY, WORK, REST, DONE = "get_ready", "work", "rest", "done"

OPEN_CAP = 99 * 60 + 59    # an open phase's display stops here (99:59)
SKIP_GRACE = 0.5           # s a phase that began by itself ignores skips meant for the last one


@dataclasses.dataclass
class Stats:
    work_seconds: float = 0.0
    rest_seconds: float = 0.0
    rounds_completed: int = 0

    @property
    def total_seconds(self):
        # Phases follow each other without gaps; get-ready and pauses don't count.
        return self.work_seconds + self.rest_seconds


class Workout:
    """GET_READY -> WORK <-> REST -> DONE; with rounds=None, until stop()."""

    def __init__(self, kind, work=20, rest=10, rounds=8, get_ready=5):
        if kind not in (TABATA, FIX_REST, FIX_WORK):
            raise ValueError("unknown timer type %r" % kind)
        if work <= 0 or rest <= 0 or get_ready < 0:
            raise ValueError("work and rest must be > 0, get-ready >= 0")
        self.kind = kind
        self.work = work
        self.rest = rest
        self.rounds = rounds
        self.get_ready = get_ready
        self._reset()

    def _reset(self):
        self.phase = None
        self.round = 0
        self.phase_start = 0.0        # shifted forward by pauses
        self.began_by_itself = False
        self.paused_at = None
        self.stopped = False
        self.stats = Stats()

    def is_fixed(self):
        phase = self.phase
        if phase == GET_READY:
            return True
        if phase == WORK:
            return self.kind != FIX_REST
        if phase == REST:
            return self.kind != FIX_WORK
        return False

    def duration(self):
        return {GET_READY: self.get_ready, WORK: self.work, REST: self.rest}.get(self.phase)

    @property
    def paused(self):
        return self.paused_at is not None

    @property
    def running(self):
        return self.phase in (GET_READY, WORK, REST)

    def elapsed(self, now):
        if not self.running:
            return 0.0
        clock = self.paused_at if self.paused else now
        return max(0.0, clock - self.phase_start)

    def display_seconds(self, now):
        elapsed = self.elapsed(now)
        if self.is_fixed():
            # Rounded up: a 20 s phase shows 20 first and 1 last.
            return max(0, math.ceil(self.duration() - elapsed - 1e-9))
        return min(OPEN_CAP, int(elapsed))

    def is_last_round(self):
        return self.rounds is not None and self.round >= self.rounds

    # Commands return the phases they entered, in order.

    def start(self, now):
        self._reset()
        if self.get_ready > 0:
            self._enter(GET_READY, now)
            return [GET_READY]
        return self._begin_work(now)

    def update(self, now):
        entered = []
        while self.running and not self.paused and self.is_fixed():
            end = self.phase_start + self.duration()
            if now < end:
                break
            entered += self._finish_phase(end, by_itself=True)
        return entered

    def advance(self, now):
        """Skip the current phase. Ignored while paused, and within SKIP_GRACE
        of a phase that began by itself, even one this call's update() began."""
        entered = self.update(now)
        if self.running and not self.paused and not self._in_skip_grace(now):
            entered += self._finish_phase(now, by_itself=False)
        return entered

    def toggle_pause(self, now):
        entered = self.update(now)
        if not self.running:
            return entered
        if self.paused:
            self.phase_start += now - self.paused_at
            self.paused_at = None
        else:
            self.paused_at = now
        return entered

    def stop(self, now):
        entered = self.update(now)
        if not self.running:
            return entered
        self._bank(self.elapsed(now))
        self.stopped = True
        self.paused_at = None
        self._enter(DONE, now)
        return entered + [DONE]

    def _enter(self, phase, at):
        self.phase = phase
        self.phase_start = at

    def _begin_work(self, at):
        self.round += 1
        self._enter(WORK, at)
        return [WORK]

    def _bank(self, seconds):
        if self.phase == WORK:
            self.stats.work_seconds += seconds
        elif self.phase == REST:
            self.stats.rest_seconds += seconds

    def _in_skip_grace(self, now):
        return self.began_by_itself and self.elapsed(now) < SKIP_GRACE

    def _finish_phase(self, end, by_itself):
        self._bank(end - self.phase_start)
        self.began_by_itself = by_itself
        if self.phase == GET_READY:
            return self._begin_work(end)
        if self.phase == WORK:
            self.stats.rounds_completed += 1
            if self.is_last_round():
                self._enter(DONE, end)       # no trailing rest
                return [DONE]
            self._enter(REST, end)
            return [REST]
        return self._begin_work(end)


# --- settings: defaults, validation and wheel steps (pure) -------------------
# Invalid values fall back one at a time, so a typo costs one setting, not all.

TYPES = (TABATA, FIX_REST, FIX_WORK)
TIME_MIN, TIME_MAX = 1, 59 * 60 + 59       # saved; the wheel's lowest is the time step
ROUNDS_MAX = 99                             # plus None = unlimited
GET_READY_MAX = 15
STEPS = (1, 5, 10, 15)
SKIP_BUTTONS = ("ok", "start")              # SKIP WITH's choices


def default_config():
    return {
        "last_type": TABATA,
        "types": {TABATA: {"work": 20, "rest": 10, "rounds": 8},
                  FIX_REST: {"work": 20, "rest": 90, "rounds": 5},
                  FIX_WORK: {"work": 45, "rest": 10, "rounds": 5}},
        "get_ready": 5,
        "sound": True,
        "step": 5,
        "skip_with": "ok",
    }


def clean_config(raw):
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg

    def is_int(v):
        return isinstance(v, int) and not isinstance(v, bool)

    if raw.get("last_type") in TYPES:
        cfg["last_type"] = raw["last_type"]
    types = raw.get("types") if isinstance(raw.get("types"), dict) else {}
    for kind in TYPES:
        saved = types.get(kind) if isinstance(types.get(kind), dict) else {}
        for key in ("work", "rest"):
            v = saved.get(key)
            if is_int(v) and TIME_MIN <= v <= TIME_MAX:
                cfg["types"][kind][key] = v
        v = saved.get("rounds", cfg["types"][kind]["rounds"])
        if v is None or (is_int(v) and 1 <= v <= ROUNDS_MAX):
            cfg["types"][kind]["rounds"] = v
    if is_int(raw.get("get_ready")) and 0 <= raw["get_ready"] <= GET_READY_MAX:
        cfg["get_ready"] = raw["get_ready"]
    if isinstance(raw.get("sound"), bool):
        cfg["sound"] = raw["sound"]
    if raw.get("step") in STEPS and is_int(raw.get("step")):
        cfg["step"] = raw["step"]
    if raw.get("skip_with") in SKIP_BUTTONS:
        cfg["skip_with"] = raw["skip_with"]
    return cfg


def step_minutes(value, delta, step):
    m, s = divmod(value, 60)
    for _ in range(abs(delta)):
        m = (m + (1 if delta > 0 else -1)) % 60
        if m == 0:
            s = max(s, step)    # the lowest time, at every detent that lands on 0
    return m * 60 + s


def step_seconds(value, delta, step):
    m, s = divmod(value, 60)
    top, low = 60 - step, (step if m == 0 else 0)
    for _ in range(abs(delta)):
        if delta > 0:
            s = (s // step + 1) * step
            if s > top:
                s = low
        else:
            down = ((s - 1) // step) * step
            if down >= low:
                s = down
            else:
                s = top if s == low else low    # below the step (0:03): up to it
    return m * 60 + s


def step_rounds(value, delta):
    cycle = list(range(1, ROUNDS_MAX + 1)) + [None]
    return cycle[(cycle.index(value) + delta) % len(cycle)]


# --- controller: menu flow and buttons (pure) ---------------------------------

SETTINGS = "settings"
MENU_TYPES = TYPES + (SETTINGS,)
TYPE_FIELDS = {
    TABATA: ("work", "rest", "rounds"),
    FIX_REST: ("rest", "rounds"),
    FIX_WORK: ("work", "rounds"),
}
SETTINGS_FIELDS = ("get_ready", "sound", "step", "skip_with")
TIME_FIELDS = ("work", "rest")
MINUTES, SECONDS = "minutes", "seconds"   # the part of a time being set
MENU, SUMMARY, RUN, RECAP = "menu", "summary", "run", "recap"
STOP_CONFIRM = 3.0                   # s a STOP? prompt waits for the second BACK
RECAP_PAGES = ("work", "rest", "rounds", "total")
RECAP_CYCLE = 3.0                    # s per recap page while it pages by itself
RECAP_GRACE = 1.0                    # s the recap ignores OK and START: a late skip press
BOUNCE_MS = 80                       # one detent turned back sooner is a contact bounce
HELD_MAX = 2                         # a larger held burst doesn't page the recap (a flick)
SOUND_TICK, SOUND_FINISH, SOUND_COMPLETED = "tick", "finish", "completed"
SOUNDS = (SOUND_TICK, SOUND_FINISH, SOUND_COMPLETED)   # least to most important
FINAL_SECONDS = 3                    # the last seconds of a fixed phase tick and blink


@dataclasses.dataclass(frozen=True)
class Wheel:
    delta: int
    at: float       # s on the bar's clock; the arrival time if the bar sends none
    held: bool      # its message carried several detents: the bar held input back


def ms_between(earlier, later):
    # The bar stamps whole ms, but 0.08 s apart in float seconds can be 79.9999 ms.
    return round((later - earlier) * 1000)


class Controller:
    """Wheel turns change `cfg` at once; `save_due` waits for the next button
    press, so a spin doesn't save once per detent."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.screen = MENU
        self.field = "type"
        self.part = MINUTES
        self.choice = cfg["last_type"]   # highlighted on the TYPE wheel
        self.workout = None
        self.stop_until = None
        self.page = None                 # recap page; None while it auto-cycles
        self._last_detent = (None, 0)    # (bar time, direction)
        self.input_at = 0.0              # the blink of a time being set counts from here
        self.after_detent = False        # the last input was a detent: lit for DETENT_LIT
        self.sounds = []
        self._heard = None               # (phase, round, seconds shown) last update
        self.save_due = False
        self._saved = copy.deepcopy(cfg)

    @property
    def kind(self):
        return self.cfg["last_type"]

    def unsaved(self):
        return self.cfg != self._saved

    def saved(self):
        self._saved = copy.deepcopy(self.cfg)
        self.save_due = False

    def update(self, now):
        if self.stop_until is not None and now >= self.stop_until:
            self.stop_until = None
        if self.screen == RUN:
            self.workout.update(now)
            self._listen(now)
            if not self.workout.running:
                # A pending STOP? stays, so its second BACK doesn't skip past the recap.
                self.screen, self.page = RECAP, None

    def recap_page(self, now):
        if self.page is not None:
            return self.page
        shown = self._since_the_end(now) // RECAP_CYCLE
        return RECAP_PAGES[int(shown) % len(RECAP_PAGES)]

    def _since_the_end(self, now):
        # DONE began at exactly the end time, however late update() noticed.
        return now - self.workout.phase_start

    def handle(self, event, now):
        self.update(now)
        if isinstance(event, tuple):
            return                        # ("release", button)
        if not (self.screen == MENU and event == "start"):    # START does nothing in the menu
            self.input_at, self.after_detent = now, isinstance(event, Wheel)
        if isinstance(event, Wheel):
            if self.screen == MENU:
                self._wheel(event.delta, event.at, event.held)
            elif self.screen == RECAP and not (event.held and abs(event.delta) > HELD_MAX):
                self._turn_page(event.delta, now)
            return
        self._last_detent = (None, 0)     # a button ends a turn
        {MENU: self._menu, SUMMARY: self._summary, RUN: self._run,
         RECAP: self._recap}[self.screen](event, now)
        self.update(now)
        if self.unsaved():
            self.save_due = True

    def _menu(self, button, now):
        if button == "start":
            return
        if self.field == "type":
            if button == "back":
                return                    # an exit would only make the manager restart us
            if self.choice == SETTINGS:
                self.field = SETTINGS_FIELDS[0]
            else:
                self.cfg["last_type"] = self.choice
                self.field, self.part = TYPE_FIELDS[self.choice][0], MINUTES
            return
        if self.field in SETTINGS_FIELDS:
            i = SETTINGS_FIELDS.index(self.field) + 1
            if button == "ok" and i < len(SETTINGS_FIELDS):
                self.field = SETTINGS_FIELDS[i]
            else:   # BACK, or OK on the last field
                self.field = "type"
                self.choice = self.kind if button == "ok" else SETTINGS
            return
        if self.field in TIME_FIELDS:
            if button == "ok" and self.part == MINUTES:
                self.part = SECONDS
                return
            if button == "back" and self.part == SECONDS:
                self.part = MINUTES
                return
        fields = ("type",) + TYPE_FIELDS[self.kind]
        i = fields.index(self.field) + (1 if button == "ok" else -1)
        if i < len(fields) and fields[i] != "type":
            self.field, self.part = fields[i], MINUTES if button == "ok" else SECONDS
        elif button == "ok":
            self.screen = SUMMARY
        else:
            self.field, self.choice = "type", self.kind

    def _summary(self, button, now):
        if button == "start":
            self.start(now)
        elif button == "back":
            self.screen = MENU
            self.field = TYPE_FIELDS[self.kind][-1]

    def _run(self, button, now):
        w = self.workout
        if button == "back":
            if self.stop_until is not None:
                self.stop_until = None    # answered: nothing left pending on the recap
                w.stop(now)
            else:
                self.stop_until = now + STOP_CONFIRM
            return
        self.stop_until = None            # any other button answers "no"
        if button == self.cfg["skip_with"]:
            w.advance(now)
        elif button in SKIP_BUTTONS:
            w.toggle_pause(now)

    def _recap(self, button, now):
        pending, self.stop_until = self.stop_until, None
        if button == "back" and pending is not None:
            return                        # STOP? answered after the end anyway
        if button in SKIP_BUTTONS and self._since_the_end(now) < RECAP_GRACE:
            return                        # a late skip or pause meant for the workout
        if button == "start":
            self.start(now)
        elif button == "back":
            self.screen, self.field, self.choice = MENU, "type", self.kind
        elif button == "ok":
            self._turn_page(1, now)

    def _turn_page(self, delta, now):
        i = RECAP_PAGES.index(self.recap_page(now)) + delta
        self.page = RECAP_PAGES[i % len(RECAP_PAGES)]

    def start(self, now):
        s = self.cfg["types"][self.kind]
        self.workout = Workout(self.kind, work=s["work"], rest=s["rest"],
                               rounds=s["rounds"], get_ready=self.cfg["get_ready"])
        self.workout.start(now)
        self.screen, self.stop_until = RUN, None
        self._heard = None

    def _listen(self, now):
        """Queue the sound for what the workout just did, as the BUSY timer
        does. A start is silent unless its first phase is short enough to tick."""
        w = self.workout
        seen = (w.phase, w.round, w.display_seconds(now) if w.running else None)
        last, self._heard = self._heard, seen
        if last is None:
            last = (w.phase, w.round, None)
        if seen == last:
            return
        if not w.running:
            # Stopping is the only way an unlimited workout ends: it completes.
            if not w.stopped or (w.rounds is None and w.round > 0):
                self._cue(SOUND_COMPLETED)
        elif seen[:2] != last[:2]:
            self._cue(SOUND_FINISH)
        elif w.is_fixed() and not w.paused and 1 <= seen[2] <= FINAL_SECONDS:
            self._cue(SOUND_TICK)

    def _cue(self, sound):
        if self.cfg["sound"]:
            self.sounds.append(sound)

    def _wheel(self, delta, at, held):
        way = 1 if delta > 0 else -1
        last_at, last_way = self._last_detent
        if (not held and abs(delta) == 1 and way != last_way and last_at is not None
                and 0 <= ms_between(last_at, at) < BOUNCE_MS):
            return
        self._last_detent = (at, way)
        field, cfg, s = self.field, self.cfg, self.cfg["types"][self.kind]
        if field in TIME_FIELDS:
            move = step_minutes if self.part == MINUTES else step_seconds
            s[field] = move(s[field], delta, cfg["step"])
        elif field == "type":
            i = MENU_TYPES.index(self.choice) + delta
            self.choice = MENU_TYPES[i % len(MENU_TYPES)]
        elif field == "rounds":
            s["rounds"] = step_rounds(s["rounds"], delta)
        elif field == "get_ready":
            cfg["get_ready"] = max(0, min(GET_READY_MAX, cfg["get_ready"] + delta))
        elif field == "sound":
            if delta % 2:
                cfg["sound"] = not cfg["sound"]
                self._cue(SOUND_TICK)     # turned on: a tick to hear it works
        elif field == "step":
            cfg["step"] = STEPS[(STEPS.index(cfg["step"]) + delta) % len(STEPS)]
        elif field == "skip_with":
            i = SKIP_BUTTONS.index(cfg["skip_with"]) + delta
            cfg["skip_with"] = SKIP_BUTTONS[i % len(SKIP_BUTTONS)]


# --- device I/O: BUSY Bar HTTP API (stdlib only) ---------------------------

# A dropped or garbled response raises http.client errors, which aren't OSErrors.
NET_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)
DRAW_TIMEOUT = 2.0         # s; a full frame takes ~60 ms on the bar
STORAGE_TIMEOUT = 2.0      # s; listing or reading a sound file, from the main loop
UPLOAD_TIMEOUT = 15        # s; uploading a sound file holds up the main loop, but only once per bar
PLAY_TIMEOUT = 1           # s; a play holds up the main loop, and a late sound is no use
CLEAR_TIMEOUT = 5          # s; removing our screen on exit: once, so worth a wait

INPUT_CONNECT_TIMEOUT = 8  # s; opening the input stream, off the main loop so no hurry
INPUT_READ_TIMEOUT = 60    # s of silence before reopening the stream; idle is normal
INPUT_HANGUP_TIMEOUT = 1   # s to read out a refused handshake's answer before hanging up
INPUT_RETRY_QUICK = 0.2    # s before reconnecting after a stream that worked: straight back
INPUT_RETRY_FIRST = 1.0    # s before retrying a failed connection, doubling each time
INPUT_RETRY_MAX = 5.0      # s; the doubling stops here: USB is cheap to retry
INPUT_STOP_POLL = 0.2      # s between checks for stop() while waiting to retry

SOUND_SOURCE = "/ext/apps_assets/busy/sounds"    # the BUSY timer's own sounds
SOUND_ASSETS = "/ext/user_assets/" + APP          # where uploads land (fw 1.2.4)
SOUND_FILES = {SOUND_TICK: "countdown_tick.snd", SOUND_FINISH: "countdown_finish.snd",
               SOUND_COMPLETED: "session_completed.snd"}


class Bar:
    """Drawing and sounds never raise; each problem is logged once."""

    def __init__(self, host, token=None, log=print):
        self.base = "http://" + host.replace("http://", "").rstrip("/")
        self.token = token
        self.log = log
        self.on_screen = True                 # did the last draw succeed?
        self._draw_problem = None
        self._sound_problem = None

    def _request(self, method, path, body=None, *, timeout):
        headers = {}
        data = None
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-API-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _report(self, state, problem):
        # One line per change of problem, not one per retry.
        if problem != getattr(self, state):
            self.log("%s: %s" % (APP, problem))
            setattr(self, state, problem)

    def draw(self, elements):
        body = {"application_name": APP, "priority": PRIORITY, "elements": elements}
        self.on_screen = False
        try:
            self._request("POST", "/api/display/draw", body, timeout=DRAW_TIMEOUT)
        except urllib.error.HTTPError as exc:
            with exc:                 # the error carries an open response
                self._refused(exc)
            return False
        except NET_ERRORS as exc:
            reason = getattr(exc, "reason", exc)
            self._report("_draw_problem",
                         "bar unreachable at %s (%s); retrying" % (self.base, reason))
            return False
        if self._draw_problem is not None:
            self.log("%s: display back" % APP)
            self._draw_problem = None
        self.on_screen = True
        return True

    def _refused(self, exc):
        if exc.code == 409:
            self._report("_draw_problem", "another app owns the display; waiting")
        elif exc.code in (401, 403):
            self._report("_draw_problem",
                         "token rejected; pass --token or set BUSY_HTTP_PASSWORD")
        else:
            try:
                detail = exc.read().decode(errors="replace").strip()
            except NET_ERRORS:
                detail = ""
            self._report("_draw_problem",
                         "draw refused (HTTP %d): %s" % (exc.code, detail))

    def install_sounds(self):
        """Copy the BUSY timer's sounds into our assets: firmware 1.2.4 plays
        stock sounds only from shared/. False if the bar is unreachable (retry)."""
        def listing(folder):
            query = "/api/storage/list?path=" + urllib.parse.quote(folder)
            answer = self._request("GET", query, timeout=STORAGE_TIMEOUT)
            return {e["name"]: e.get("size") for e in json.loads(answer)["list"]}

        try:
            try:
                source = listing(SOUND_SOURCE)
            except urllib.error.HTTPError as exc:
                exc.close()
                self.log("%s: no sounds (the bar has no %s)" % (APP, SOUND_SOURCE))
                return True
            try:
                have = listing(SOUND_ASSETS)
            except urllib.error.HTTPError as exc:
                exc.close()
                have = {}                # nothing uploaded yet
            for name in SOUND_FILES.values():
                if name not in source or have.get(name) == source[name]:
                    continue
                data = self._request("GET", "/api/storage/read?path="
                                     + urllib.parse.quote(SOUND_SOURCE + "/" + name),
                                     timeout=STORAGE_TIMEOUT)
                self._request("POST", "/api/assets/upload?application_name=%s&file=%s"
                              % (urllib.parse.quote(APP), urllib.parse.quote(name)),
                              data, timeout=UPLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            exc.close()
            self.log("%s: no sounds (copy failed: HTTP %d)" % (APP, exc.code))
            return True
        except NET_ERRORS:
            return False
        except (ValueError, KeyError, TypeError, AttributeError):
            self.log("%s: no sounds (unexpected storage listing)" % APP)
            return True
        return True

    def play(self, sound):
        body = {"application_name": APP, "path": SOUND_FILES[sound]}
        try:
            self._request("POST", "/api/audio/play", body, timeout=PLAY_TIMEOUT)
        except urllib.error.HTTPError as exc:
            with exc:
                problem = "sound refused (HTTP %d)" % exc.code
        except NET_ERRORS as exc:
            problem = "sound failed (%s)" % getattr(exc, "reason", exc)
        else:
            self._sound_problem = None
            return
        self._report("_sound_problem", problem)

    def clear(self):
        query = "?application_name=" + urllib.parse.quote(APP)
        try:
            self._request("DELETE", "/api/display/draw" + query, timeout=CLEAR_TIMEOUT)
        except NET_ERRORS:
            pass


# --- device I/O: config.json ---------------------------------------------------

# Earlier versions saved here, in the app folder, which busybar-manager replaces on update.
LEGACY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def config_path(env=os.environ, windows=os.name == "nt", home=None):
    if env.get("WORKOUT_TIMER_CONFIG"):
        # busybar-manager passes env vars without a shell: no ~ expansion.
        return os.path.abspath(os.path.expanduser(env["WORKOUT_TIMER_CONFIG"]))
    home = home or os.path.expanduser("~")
    if windows:
        folder = env.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    else:
        xdg = env.get("XDG_CONFIG_HOME", "")
        # The XDG spec says to ignore a relative path.
        folder = xdg if os.path.isabs(xdg) else os.path.join(home, ".config")
    return os.path.join(folder, APP, "config.json")


def legacy_config_path(env=os.environ):
    return None if env.get("WORKOUT_TIMER_CONFIG") else LEGACY_CONFIG_PATH


def legacy_to_read(path, legacy_path):
    if legacy_path and not os.path.exists(path) and os.path.exists(legacy_path):
        return legacy_path
    return None


def load_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return clean_config(json.load(fh))
    except (OSError, ValueError):
        return default_config()


def save_config(cfg, path, log=print):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())  # on disk before the rename: a power cut can't leave an empty file
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        log("%s: cannot save settings to %s (%s)" % (APP, path, exc))
        return False
    return True


def open_settings(path, legacy_path=None, log=print):
    """The settings and a save(cfg). The first save removes the legacy file,
    or deleting the new file to reset the settings would bring it back."""
    moving_from = legacy_to_read(path, legacy_path)

    def save(cfg):
        nonlocal moving_from
        if not save_config(cfg, path, log=log):
            return False
        if moving_from:
            try:
                os.remove(moving_from)
            except OSError:
                pass    # harmless: the new file now exists, so the old one is never read
            log("%s: settings moved to %s" % (APP, path))
            moving_from = None
        return True

    return load_config(moving_from or path), save


# --- device I/O: buttons and wheel over the status WebSocket ------------------
# proto3 leaves zero values off the wire, so an OK press is an empty ButtonEvent.

BUTTONS = {0: "ok", 1: "back", 2: "start"}


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


def _proto_fields(buf):
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            if pos + length > len(buf):
                raise ValueError("truncated protobuf field")
            value, pos = buf[pos:pos + length], pos + length
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > len(buf):
                raise ValueError("truncated protobuf field")
            value, pos = buf[pos:pos + width], pos + width
        else:
            raise ValueError("unsupported protobuf wire type %d" % wire)
        yield field, wire, value


def decode_input(frame):
    """Presses ("ok"), releases (("release", "ok")) and Wheels, in order: the
    detents between two buttons add up to one Wheel, which cancels a bounce."""
    events, stamp, detents = [], None, 0
    for field, wire, value in _proto_fields(frame):
        if field == 1 and wire == 1:                       # State.timestamp
            stamp = struct.unpack("<Q", value)[0] / 1000.0
    for field, wire, update in _proto_fields(frame):
        if field != 2 or wire != 2:                        # State.updates
            continue
        for ufield, uwire, payload in _proto_fields(update):
            if ufield != 11 or uwire != 2:                 # StateUpdate.input
                continue
            for efield, ewire, event in _proto_fields(payload):
                if ewire != 2:
                    continue
                values = {f: v for f, w, v in _proto_fields(event) if w == 0}
                if efield == 1:                            # button_event
                    button = BUTTONS.get(values.get(1, 0))
                    if button and values.get(2, 0) == 0:   # PRESS
                        events.append(button)
                    elif button and values.get(2, 0) == 1:  # RELEASE
                        events.append(("release", button))
                elif efield == 3:                          # encoder_event
                    raw = values.get(1, 0)
                    delta = (raw >> 1) ^ -(raw & 1)        # zigzag
                    if not delta:
                        continue
                    detents += 1
                    if events and isinstance(events[-1], int):
                        events[-1] += delta
                    else:
                        events.append(delta)
    return [Wheel(e, stamp, detents > 1) if isinstance(e, int) else e
            for e in events if e != 0]                     # a run can cancel out


class InputListener(threading.Thread):
    """A minimal RFC 6455 client for /api/status/ws; failures are logged once and retried."""

    def __init__(self, host, token=None, log=print):
        super().__init__(daemon=True, name="input")
        self.host = host.replace("http://", "").rstrip("/")
        self.token = token
        self.log = log
        self.events = queue.Queue()
        self._woken_by = []                # the event wait() woke on, until poll() returns it
        self._stopping = threading.Event()
        self._complained = False
        self._connected = False            # this session got past the handshake

    def stop(self):
        self._stopping.set()

    def wait(self, timeout):
        """Sleep up to `timeout` s, waking at an event so a press is handled at once."""
        if self._woken_by:
            return
        try:
            self._woken_by.append(self.events.get(timeout=timeout))
        except queue.Empty:
            pass

    def poll(self):
        out, self._woken_by = self._woken_by, []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def run(self):
        backoff = INPUT_RETRY_FIRST
        while not self._stopping.is_set():
            self._connected = False
            try:
                self._session()
            except Exception as exc:  # noqa: BLE001 - a thread must not die
                # A quiet stream timing out is routine: the emulator sends nothing when idle.
                quiet = self._connected and isinstance(exc, socket.timeout)
                if not quiet and not self._complained:
                    self.log("%s: no button input (%s); retrying" % (APP, exc))
                    self._complained = True
            if self._connected:
                pause, backoff = INPUT_RETRY_QUICK, INPUT_RETRY_FIRST
            else:
                pause, backoff = backoff, min(INPUT_RETRY_MAX, backoff * 2)
            deadline = time.monotonic() + pause
            while not self._stopping.is_set() and time.monotonic() < deadline:
                time.sleep(INPUT_STOP_POLL)

    def _session(self):
        host, _, port = self.host.partition(":")
        path = "/api/status/ws"
        if self.token:   # the WebSocket takes the password as a query parameter
            path += "?" + urllib.parse.urlencode({"x-api-token": self.token})
        sock = socket.create_connection((host, int(port or 80)),
                                        timeout=INPUT_CONNECT_TIMEOUT)
        try:
            sock.settimeout(INPUT_READ_TIMEOUT)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((
                "GET %s HTTP/1.1\r\n"
                "Host: %s\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n" % (path, self.host, key)).encode())
            stream = sock.makefile("rb")
            status = stream.readline()
            if b" 101 " not in status:
                # Read out the refusal first: closing with it unread resets the connection.
                try:
                    sock.shutdown(socket.SHUT_WR)
                    sock.settimeout(INPUT_HANGUP_TIMEOUT)
                    while stream.read(4096):
                        pass
                except OSError:
                    pass
                raise OSError("handshake refused: %s"
                              % status.decode(errors="replace").strip())
            while stream.readline() not in (b"\r\n", b"\n", b""):
                pass
            # The stream stays silent until this is sent, on every connection.
            self._send(sock, 0x1, json.dumps({"enable": True}).encode())
            self._connected = True
            if self._complained:
                self.log("%s: button input connected" % APP)
                self._complained = False
            payload = b""
            while not self._stopping.is_set():
                fin, opcode, chunk = self._read_frame(stream)
                if opcode == 0x8:                          # close
                    raise OSError("bar closed the input stream")
                if opcode == 0x9:                          # ping
                    self._send(sock, 0xA, chunk)
                    continue
                if opcode == 0xA:                          # pong
                    continue
                payload = chunk if opcode else payload + chunk
                if not fin:
                    continue
                if opcode == 0x2 or (opcode == 0 and payload):
                    try:
                        for event in decode_input(payload):
                            if isinstance(event, Wheel) and event.at is None:
                                # No bar clock (the emulator): when it came.
                                event = dataclasses.replace(event, at=time.time())
                            self.events.put(event)
                    except ValueError:
                        pass                               # one bad frame, one lost event
                payload = b""
        finally:
            sock.close()

    @staticmethod
    def _read_frame(stream):
        head = stream.read(2)
        if len(head) < 2:
            raise OSError("input stream closed")
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, length = head[1] & 0x80, head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", stream.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", stream.read(8))[0]
        mask = stream.read(4) if masked else b""
        body = stream.read(length) if length else b""
        if len(body) < length:
            raise OSError("input stream closed mid-frame")
        if mask:
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        return fin, opcode, body

    @staticmethod
    def _send(sock, opcode, data):
        # Client frames must be masked (RFC 6455).
        header = bytearray([0x80 | opcode])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", len(data))
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", len(data))
        mask = os.urandom(4)
        header += mask
        sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


# --- rendering ---------------------------------------------------------------

# Text is printable ASCII only, so ∞ is drawn from rectangles, one per font.
INF_ART = {
    "extra_large": (
        "..###......###..",
        ".#####....#####.",
        "##...##..##...##",
        "##....####....##",
        "##.....##.....##",
        "##.....##.....##",
        "##....####....##",
        "##...##..##...##",
        ".#####....#####.",
        "..###......###..",
    ),
    "condensed": (
        ".##...##.",
        "#..#.#..#",
        "#...#...#",
        "#..#.#..#",
        ".##...##.",
    ),
}


def _cover(art):
    """Lit cells as (x, y, w, h) rectangles: row runs, merged down while they repeat."""
    rects, open_runs = [], {}
    for y, row in enumerate(art + ("." * len(art[0]),)):
        runs, x = set(), 0
        while x < len(row):
            if row[x] == "#":
                end = row.index(".", x) if "." in row[x:] else len(row)
                runs.add((x, end - x))
                x = end
            else:
                x += 1
        for run in list(open_runs):
            if run not in runs:
                top = open_runs.pop(run)
                rects.append((run[0], top, run[1], y - top))
        for run in runs:
            open_runs.setdefault(run, y)
    return sorted(rects, key=lambda r: (r[1], r[0]))


INF_RECTS = {font: _cover(art) for font, art in INF_ART.items()}
INF_SIZE = {font: (len(art[0]), len(art)) for font, art in INF_ART.items()}
INF_SLOTS = tuple("inf%d" % i for i in range(max(map(len, INF_RECTS.values()))))

# Every frame fills the same ids, back to front: the emulator stacks ids in the
# order they first arrived. Each id keeps one type: the bar answers 400 to a
# text id turned into a rectangle or back.
CLOCK_SLOTS = tuple("c%d" % i for i in range(6))   # from the right: c0 is the last digit
PART_SLOTS = {SECONDS: CLOCK_SLOTS[:2], MINUTES: CLOCK_SLOTS[3:]}   # of m:ss; c2 is the colon
TEXT_SLOTS = ("label", "round", "note", "value") + CLOCK_SLOTS
RECT_SLOTS = INF_SLOTS + ("bar_bg", "bar")
SLOTS = TEXT_SLOTS + RECT_SLOTS

COLOR_WORK = "#00FF40FF"
COLOR_REST = "#FFA000FF"
COLOR_READY = "#3090FFFF"
COLOR_PAUSED = "#808080FF"   # dimmed by colour: the emulator ignores alpha on text
COLOR_STOP = "#FF3030FF"
COLOR_ROUND = "#C0C0C0FF"
COLOR_LABEL = COLOR_ROUND
COLOR_VALUE = "#FFFFFFFF"
COLOR_GO = COLOR_WORK
COLOR_RECAP = "#00E0FFFF"

PHASE_COLOR = {GET_READY: COLOR_READY, WORK: COLOR_WORK, REST: COLOR_REST}
PHASE_LABEL = {GET_READY: "READY", WORK: "WORK", REST: "REST"}
TYPE_NAMES = {TABATA: "TABATA", FIX_REST: "FIXED REST", FIX_WORK: "FIXED WORK",
              SETTINGS: "SETTINGS"}
FIELD_LABELS = {"type": "TYPE", "work": "WORK", "rest": "REST", "rounds": "ROUNDS",
                "get_ready": "GET READY", "sound": "SOUND", "step": "STEP",
                "skip_with": "SKIP WITH"}
FIELD_COLORS = {"work": COLOR_WORK, "rest": COLOR_REST}
RECAP_LABELS = dict(FIELD_LABELS, total="TOTAL")

BLINK = 0.5                  # s on, then s off
DETENT_LIT = 1.0             # s lit after a detent: the bar holds input back for up to ~0.75 s

# A text element's y is its font box, which has blank rows above the ink.
INK_TOP = {"tiny": 1, "small": 2, "condensed": 2, "bold": 2, "extra_large": 2}
INK_ROWS = {"condensed": 7, "extra_large": 10}   # digit height
TIME_MID_X = 49              # centre of the time area, columns 27-71
ROW_LABEL, ROW_ROUND, ROW_TIME = 0, 6, 2
ROW_VALUE = 6                # menu value: extra_large ink on rows 6-15
BAR_ROW = H - 1

# Glyph advances for " " to "~" as chr(32 + px). No glyph draws past its
# advance, so a string's width is their sum.
ADVANCE = {
    "tiny":        '#"$&$%%"##$$"$"%$$$$$$$$$$""$$$$$$$$$$$$$$$%$&%%$%$$$%&&$&%#%#$$#$$$$$##$""$"&$$$$#$$$$&$$$$"$%',
    "small":       '""$&$%%"##$$"#"#$#$$$$$$$$"#$$$$%%%%%%%%%"$%$&%%%%%%$%$&$$$###$$$$$$$$#$$"#$"&$$$$#$#$$&$$$$"$%',
    "condensed":   '%#$&&(\'"##$&#$$%%%%%%%%%%%"#$&$&(&&&&&&&&$%&%(&&&&&&&&&(&&&#%#&%&%%%%%%%%$$%$&%%%%$%%%&&&%%$#$&',
    "bold":        '%$$())("$$$\'#$$&\'\'\'\'\'\'\'\'\'\'"#%\'%\'*\'\'\'\'&&\'\'%&\'&*(\'\'\'\'\'\'\'(+\'\'\'$&$\'%\'&&&&&&&&%%&%(&&&&%&&&\'(\'&&%$%&',
    "extra_large": "$#&*)+*#%%''#$#&(%((((((((##%'%',((((''((%'(')(((((('())(('$&$(%'((((''((%'(')(((((('())(('&#&(",
}


def text_width(txt, font):
    return sum(ord(ADVANCE[font][ord(c) - 32]) - 32 for c in txt)


def text(eid, txt, x=0, y=0, font="normal", color="#FFFFFFFF"):
    return {"id": eid, "type": "text", "text": str(txt), "x": x, "y": y,
            "font": font, "color": color, "timeout": ELEMENT_TIMEOUT}


def rect(eid, x, y, width, height, color):
    return {"id": eid, "type": "rectangle", "x": x, "y": y, "width": width,
            "height": height, "fill": "solid", "fill_colors": [color],
            "border_width": 0, "timeout": ELEMENT_TIMEOUT}


def blank(eid):
    if eid in RECT_SLOTS:
        return rect(eid, W, H, 1, 1, "#00000000")    # the bar refuses a zero-size one
    return text(eid, " ", font="tiny", color="#00000000")


def frame(parts):
    unknown = set(parts) - set(SLOTS)
    if unknown:
        raise ValueError("no slot for %s" % ", ".join(sorted(unknown)))
    out = []
    for z, eid in enumerate(SLOTS):
        el = dict(parts.get(eid) or blank(eid))
        if (el["type"] == "rectangle") != (eid in RECT_SLOTS):
            raise ValueError("slot %s can't hold a %s" % (eid, el["type"]))
        el["id"], el["z_index"] = eid, z
        out.append(el)
    return out


def is_blank(el):
    if el["type"] == "text":
        return not el["text"].strip()
    return el["x"] >= W or el["y"] >= H


class Painter:
    """The bar keeps each element until it is replaced, so a draw sends only
    the changes, and a blank only to wipe what may still be shown."""

    def __init__(self):
        self.forget()

    def forget(self):
        # The bar's state is unknown: start, a failed draw, BACK.
        self.shown = {}
        self.unsure = set(SLOTS)

    def plan(self, elements, refresh=False):
        """(visible, wipes): visible goes first, as the small, fast draw that
        takes the screen back after BACK. A refresh resends it all."""
        visible, wipes = [], []
        for el in elements:
            eid = el["id"]
            if is_blank(el):
                if eid in self.unsure or self.shown.get(eid, el) != el:
                    wipes.append(el)
            elif refresh or self.shown.get(eid) != el:
                visible.append(el)
        return visible, wipes

    def sent(self, elements):
        for el in elements:
            self.shown[el["id"]] = el
            self.unsure.discard(el["id"])


def dim(color, factor):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02X%02X%02XFF" % tuple(round(c * factor) for c in (r, g, b))


def fmt_time(seconds, plus=False, hours=False):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    if hours and m >= 60:
        body = "%d:%02d:%02d" % (m // 60, m % 60, s)
    else:
        body = "%d:%02d" % (m, s)
    return ("+" if plus else "") + body


def left_text(eid, txt, row, color, font="small"):
    return text(eid, txt, y=row - INK_TOP[font], font=font, color=color)


def clock_cells(txt):
    """([(char, x), ...], width) in extra_large, every digit in a cell as wide
    as a 0 (a 1 is 3 px narrower), so a time doesn't shift as it changes."""
    cell = text_width("0", "extra_large")
    out, x = [], 0
    for ch in txt:
        if ch.isdigit():
            out.append((ch, x + (cell - text_width(ch, "extra_large") + 1) // 2))
            x += cell
        else:
            out.append((ch, x))
            x += text_width(ch, "extra_large")
    return out, x


def clock(txt, x, top, color, hide=()):
    cells, _ = clock_cells(txt)
    parts = {}
    for slot, (ch, dx) in zip(CLOCK_SLOTS, reversed(cells)):
        if slot not in hide:
            parts[slot] = text(slot, ch, x=x + dx, y=top - INK_TOP["extra_large"],
                               font="extra_large", color=color)
    return parts


def blinked_off(since):
    return since % (2 * BLINK) >= BLINK


def render_workout(w, now, confirm_stop=False):
    fixed = w.is_fixed()
    color = COLOR_PAUSED if w.paused else PHASE_COLOR[w.phase]
    parts = {}

    if confirm_stop:
        parts["label"] = left_text("label", "STOP?", ROW_LABEL, COLOR_STOP)
    else:
        parts["label"] = left_text("label", "PAUSE" if w.paused else PHASE_LABEL[w.phase],
                                   ROW_LABEL, color)
    if w.phase != GET_READY:
        # Bold while no wider than the label, else condensed, which clears the widest time.
        counter = "R%d" % w.round if w.rounds is None else "%d/%d" % (w.round, w.rounds)
        font = "bold" if text_width(counter, "bold") <= text_width("WORK", "small") \
            else "condensed"
        parts["round"] = left_text("round", counter, ROW_ROUND,
                                   COLOR_PAUSED if w.paused else COLOR_ROUND, font=font)

    seconds = w.display_seconds(now)

    def as_shown(v):
        return str(v) if w.phase == GET_READY else fmt_time(v, plus=not fixed)
    shown = as_shown(seconds)
    if w.paused:
        hide_all = hide_digit = blinked_off(now - w.paused_at)
    else:
        into_second = seconds - (w.duration() - w.elapsed(now)) if fixed else 0
        hide_all = False
        hide_digit = fixed and seconds <= FINAL_SECONDS and into_second >= BLINK
    # Right-aligned in a box as wide as the phase's start, so 10:00 -> 9:59
    # doesn't move; centred by hand, as the bar's `align` rounds differently.
    width = clock_cells(shown)[1]
    box = max(width, clock_cells(as_shown(w.duration()))[1]) if fixed else width
    x = TIME_MID_X - box // 2 + box - width
    if not hide_all:
        parts.update(clock(shown, x, ROW_TIME, color, hide=CLOCK_SLOTS[:1] if hide_digit else ()))

    if fixed:
        # From the shown seconds, so the bar never needs a draw of its own.
        parts["bar_bg"] = rect("bar_bg", 0, BAR_ROW, W, 1, dim(color, 0.2))
        width = math.ceil(W * seconds / w.duration())
        if width > 0:
            parts["bar"] = rect("bar", 0, BAR_ROW, width, 1, color)
    return frame(parts)


def value_line(txt, color, inf=False, steady=False, hide=()):
    """`txt` (then ∞ if `inf`) centred, in condensed if extra_large doesn't
    fit, so nothing scrolls. `steady` draws it as a clock."""
    if steady and not inf:
        width = clock_cells(txt)[1]
        top = ROW_VALUE + (H - ROW_VALUE - INK_ROWS["extra_large"]) // 2
        return clock(txt, (W - width) // 2, top, color, hide=hide)
    for font in ("extra_large", "condensed"):
        inf_w = (INF_SIZE[font][0] + (1 if txt else 0)) if inf else 0
        width = text_width(txt, font) + inf_w
        if width <= W:
            break
    x0 = (W - width) // 2
    top = ROW_VALUE + (H - ROW_VALUE - INK_ROWS[font]) // 2
    parts = {}
    if txt:
        parts["value"] = text("value", txt, x=x0, y=top - INK_TOP[font], font=font,
                              color=color)
    if inf:
        ix = x0 + width - INF_SIZE[font][0]
        iy = top + (INK_ROWS[font] - INF_SIZE[font][1]) // 2
        for slot, (x, y, w, h) in zip(INF_SLOTS, INF_RECTS[font]):
            parts[slot] = rect(slot, ix + x, iy + y, w, h, color)
    return parts


def corner(txt, color):
    return text("note", txt, x=W - text_width(txt, "tiny"), y=ROW_LABEL - INK_TOP["tiny"],
                font="tiny", color=color)


def field_value(cfg, field, choice):
    """(text, unlimited)"""
    s = cfg["types"][cfg["last_type"]]
    if field == "type":
        return TYPE_NAMES[choice], False
    if field in TIME_FIELDS:
        return fmt_time(s[field]), False
    if field == "rounds":
        return ("", True) if s["rounds"] is None else (str(s["rounds"]), False)
    if field == "get_ready":
        return ("%d SEC" % cfg["get_ready"] if cfg["get_ready"] else "OFF"), False
    if field == "sound":
        return ("ON" if cfg["sound"] else "OFF"), False
    if field == "skip_with":
        return cfg["skip_with"].upper(), False
    return "%d SEC" % cfg["step"], False


def render_menu(cfg, field, choice, hidden_part=None):
    parts = {"label": left_text("label", FIELD_LABELS[field], ROW_LABEL,
                                FIELD_COLORS.get(field, COLOR_LABEL))}
    txt, inf = field_value(cfg, field, choice)
    hide = PART_SLOTS[hidden_part] if hidden_part and field in TIME_FIELDS else ()
    parts.update(value_line(txt, COLOR_VALUE, inf,
                            steady=field in TIME_FIELDS + ("rounds",), hide=hide))
    return frame(parts)


def summary_text(kind, s):
    if kind == FIX_REST:
        return "%s x" % fmt_time(s["rest"])
    if kind == FIX_WORK:
        return "%s x" % fmt_time(s["work"])
    short = s["work"] < 60 and s["rest"] < 60

    def t(v):
        return str(v) if short else fmt_time(v)
    return "%s/%s x" % (t(s["work"]), t(s["rest"]))


def render_summary(cfg):
    kind = cfg["last_type"]
    s = cfg["types"][kind]
    line = summary_text(kind, s)
    unlimited = s["rounds"] is None
    parts = {"label": left_text("label", TYPE_NAMES[kind], ROW_LABEL, COLOR_LABEL),
             "note": corner("START", COLOR_GO)}
    parts.update(value_line(line if unlimited else line + str(s["rounds"]),
                            COLOR_VALUE, unlimited))
    return frame(parts)


def recap_value(w, page):
    st = w.stats
    if page == "rounds":
        if w.rounds is None:
            return str(st.rounds_completed)
        return "%d/%d" % (st.rounds_completed, w.rounds)
    seconds = {"work": st.work_seconds, "rest": st.rest_seconds,
               "total": st.total_seconds}[page]
    return fmt_time(seconds, hours=True)


def render_recap(w, page):
    parts = {"label": left_text("label", RECAP_LABELS[page], ROW_LABEL,
                                FIELD_COLORS.get(page, COLOR_LABEL)),
             "note": corner("STOPPED", COLOR_STOP) if w.stopped
             else corner("DONE", COLOR_RECAP)}
    parts.update(value_line(recap_value(w, page), COLOR_RECAP))
    return frame(parts)


def render(ctrl, now):
    if ctrl.screen == MENU:
        # After a detent, the blink's first lit half stretches to DETENT_LIT.
        since = now - ctrl.input_at - (DETENT_LIT - BLINK if ctrl.after_detent else 0.0)
        hidden = ctrl.part if blinked_off(max(0.0, since)) else None
        return render_menu(ctrl.cfg, ctrl.field, ctrl.choice, hidden)
    if ctrl.screen == SUMMARY:
        return render_summary(ctrl.cfg)
    if ctrl.screen == RECAP:
        return render_recap(ctrl.workout, ctrl.recap_page(now))
    return render_workout(ctrl.workout, now, confirm_stop=ctrl.stop_until is not None)


def changes_by_itself(ctrl):
    """Whether render(ctrl, now) can change with `now` alone."""
    return (ctrl.screen == RUN or (ctrl.screen == RECAP and ctrl.page is None)
            or (ctrl.screen == MENU and ctrl.field in TIME_FIELDS))


# --- app ---------------------------------------------------------------------

TICK = 0.1                 # s between loop passes: a new second or blink shows this late at most
RECLAIM = 1.0              # s of full draws after BACK: the firmware may clear us just after one
RECLAIM_EVERY = 0.15       # s between those draws, so a home screen they miss is soon covered


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20",
                   help="bar address; 10.0.4.20 is USB, 127.0.0.1:8080 the emulator")
    p.add_argument("--token", default=os.environ.get("BUSY_HTTP_PASSWORD"),
                   help="Wi-Fi API password (default: $BUSY_HTTP_PASSWORD)")
    p.add_argument("--test", action="store_true", help="draw a single frame and exit")
    return p


def _terminate(signum, _frame):
    # busybar-manager stops apps with SIGTERM; leave through the same cleanup
    # path as Ctrl-C.
    raise KeyboardInterrupt


class DrawSchedule:
    """When the loop draws, and what it sends.

    Only what changed is sent (a one-element draw takes ~10 ms on the bar, a
    full frame ~60). All visible elements go out every REFRESH s, renewing
    their timeouts and taking the screen back after a 409, and every
    RECLAIM_EVERY s just after BACK. After a failed draw nothing is sent for
    RETRY s, so a lost bar isn't hammered at 10 Hz. Once the bar answers,
    the sounds are copied onto it.

    No I/O of its own: `send` (Bar.draw), `install_sounds` and `clock` come
    in from main(), so the tests drive it with fakes."""

    def __init__(self, send, install_sounds, clock):
        self.send = send
        self.install_sounds = install_sounds
        self.clock = clock
        self.painter = Painter()
        self.hold_until = self.next_full = self.reclaim_until = 0.0
        self.up_to_date = False           # the bar shows the last frame rendered
        self.sounds_ready, self.next_install = False, 0.0

    def reclaim(self, now):
        """BACK: the firmware also takes a short BACK press (on release): it
        clears our elements and shows its home screen. Drawing takes the
        screen back, so send the visible elements at once and again for a
        moment, in case the firmware clears them after our draw."""
        self._forget()
        self.next_full, self.reclaim_until = now, now + RECLAIM

    def draw(self, now, build_frame, unchanged=False):
        """One loop pass: send what changed in the frame `build_frame()`
        builds. `unchanged` says the frame is the same as the last frame
        built (no input, and a screen that doesn't change by itself); then,
        unless a full draw is due, it isn't even built."""
        if now < self.hold_until:
            return
        full = now >= self.next_full
        if full or not unchanged or not self.up_to_date:
            if not self._send(self.painter.plan(build_frame(), refresh=full)):
                self._forget()
                # From now, not from the pass's start: a draw that timed
                # out has already taken a while.
                self.hold_until = self.next_full = self.clock() + RETRY
                return
            self.up_to_date = True
            if full:
                self.next_full = now + (RECLAIM_EVERY if now < self.reclaim_until
                                        else REFRESH)
        if not self.sounds_ready and now >= self.next_install:
            self.sounds_ready = self.install_sounds()   # once, while the bar answers
            self.next_install = now + RETRY

    def _send(self, payloads):
        for payload in payloads:
            if payload:
                if not self.send(payload):
                    return False
                self.painter.sent(payload)
        return True

    def _forget(self):
        self.painter.forget()
        self.up_to_date = False


def main(argv=None):
    args = build_parser().parse_args(argv)
    bar = Bar(args.host, args.token)

    if args.test:
        workout = Workout(TABATA, work=20, rest=10, rounds=8, get_ready=5)
        now = time.monotonic()
        workout.start(now - 7.5)       # a WORK screen, 2.5 s into round 1
        workout.update(now)
        return 0 if bar.draw(render_workout(workout, now)) else 1

    signal.signal(signal.SIGTERM, _terminate)
    print("%s -> %s  (Ctrl-C to stop)" % (APP, bar.base))
    cfg, save = open_settings(config_path(), legacy_config_path())
    ctrl = Controller(cfg)
    listener = InputListener(args.host, args.token)
    listener.start()
    schedule = DrawSchedule(bar.draw, bar.install_sounds, time.monotonic)
    try:
        while True:
            now = time.monotonic()
            events = listener.poll()
            for event in events:
                ctrl.handle(event, now)
                if event in ("back", ("release", "back")):
                    schedule.reclaim(now)
            if ctrl.save_due:
                if save(ctrl.cfg):
                    ctrl.saved()
                else:
                    ctrl.save_due = False   # tried; again on the next press and on exit
            ctrl.update(now)
            schedule.draw(now, lambda: render(ctrl, now),
                          unchanged=not events and not changes_by_itself(ctrl))
            if ctrl.sounds:
                # One sound at a time: the most important one that is due.
                # After this pass's draw, so it doesn't run ahead of the
                # screen, and dropped unless the last draw (this pass's, if
                # it sent anything) succeeded: while the screen isn't
                # ours a sound is unwanted, and while the bar is down it would
                # be late, and a play to a dead connection can block the loop
                # for its whole timeout.
                if bar.on_screen:
                    bar.play(max(ctrl.sounds, key=SOUNDS.index))
                ctrl.sounds.clear()
            listener.wait(TICK)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        listener.stop()
        if ctrl.unsaved():
            save(ctrl.cfg)
        bar.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
