#!/usr/bin/env python3
"""busyrec: record a gallery app's display output, with no emulator required.

An app's entire visual output is the stream of `POST /api/display/draw` bodies
plus whatever it uploads to `/api/assets/upload`. This tool stands in for the
BUSY Bar, runs the app against itself, and writes that stream to a portable
`.busyrec` file. `tools/render/render.mjs` replays it into preview.gif.

Recording the stream instead of screen-scraping a live canvas is what makes the
previews smooth: replay happens off the clock, so frame timing is exact by
construction instead of whatever a browser timer managed to hit.

    python3 tools/busyrec.py waves                    # 6 s, standalone
    python3 tools/busyrec.py waves --seconds 10
    python3 tools/busyrec.py waves -- --lat 52.4      # args after -- go to the app
    python3 tools/busyrec.py waves --steal-at 3       # take the screen away at t=3
    python3 tools/busyrec.py waves --upstream 127.0.0.1:8080   # proxy a real emulator

Stdlib only, on purpose: anyone who can write an app can run this.

The standalone stub deliberately reproduces three behaviours of the real
firmware (mirrored from busybar-emulator/server.js, which stays the source of
truth): draw-body validation, the 100-element accumulating cap, and priority
arbitration. Those three are where submissions actually break, so reproducing
them here is what turns a recording into a review. `--conformance` replays a
recording against a real emulator and diffs the status codes, to catch drift.
"""

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISPLAY_W, DISPLAY_H = 72, 16
MAX_ELEMENTS = 100
MAX_BODY = 8 * 1024 * 1024
API_SEMVER = "25.0.0"
STEAL_APP = "_scenario.steal"

DRAW_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
DRAW_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{8}$")
FONTS = ("tiny", "small", "normal", "condensed", "bold", "large", "extra_large")

ASSET_MIME = {
    "png": "image/png", "gif": "image/gif", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
}


# --------------------------------------------------------------------------
# device state (mirrors busybar-emulator/server.js)
# --------------------------------------------------------------------------

class Device:
    """The subset of BUSY Bar state that affects what ends up on screen."""

    def __init__(self):
        self.frame = {"application_name": None, "elements": [], "ts": 0, "priority": 0}
        self.app_elements = {}     # per-app persistent, id-keyed sets
        self.assets = {}           # "app/file" -> bytes
        self.storage = {}
        self.brightness = 80
        self.volume = 0
        self.name = "BUSY-BUSYREC"
        self.busy_snapshot = {
            "snapshot": {
                "type": "NOT_STARTED",
                "busy_bar_settings": {"theme": "busy", "show_work_phase_only": False,
                                      "trigger_smart_home": True},
            },
            "snapshot_timestamp_ms": int(time.time() * 1000),
        }
        self.seq = 1
        self.lock = threading.Lock()

    # Firmware (canvas_draw_rejected): the current owner may redraw at equal
    # priority; a different app needs strictly higher priority to take over.
    def draw_frame(self, app, elements, priority):
        if self.frame["elements"]:
            same = app == self.frame["application_name"]
            cur = self.frame["priority"]
            if (priority < cur) if same else (priority <= cur):
                return False
        self.seq += 1
        self.frame = {"application_name": app, "elements": elements,
                      "ts": self.seq, "priority": priority}
        return True

    # Firmware keeps a persistent id-keyed element set PER application_name: a
    # draw upserts into it and never releases what you stop sending. Capped at
    # 100; the draw that would exceed it 400s and leaves the set untouched. This
    # is why an app that mints fresh ids every frame dies after ~100 frames on
    # real hardware while every individual draw looks tiny.
    def merge_elements(self, app, incoming):
        merged = list(self.app_elements.get(app, []))
        index = {el.get("id"): i for i, el in enumerate(merged)}
        for el in incoming:
            at = index.get(el.get("id"))
            if at is None:
                index[el.get("id")] = len(merged)
                merged.append(el)
            else:
                merged[at] = el
        if len(merged) > MAX_ELEMENTS:
            return None
        self.app_elements[app] = merged
        return merged

    def clear(self, app):
        if app:
            self.app_elements.pop(app, None)
        else:
            self.app_elements = {}
        if not app or self.frame["application_name"] == app or not self.frame["elements"]:
            self.seq += 1
            self.frame = {"application_name": None, "elements": [],
                          "ts": self.seq, "priority": 0}


# Firmware schema (api_semver 25.0.0): every element carries an `id`, every
# colour is #RRGGBBAA. The real bar 400s on a missing id or an old 0xRRGGBBAA
# colour, so we do too. Returns an error string, or None when valid.
def validate_draw_body(body):
    lnc = body.get("led_notification_color")
    if lnc is not None and not DRAW_COLOR_RE.match(str(lnc)):
        return "led_notification_color must be #RRGGBBAA"
    for i, el in enumerate(body.get("elements") or []):
        if not isinstance(el, dict):
            return "element %d: must be an object" % i
        eid = el.get("id")
        if not isinstance(eid, str) or not DRAW_ID_RE.match(eid):
            return "element %d: 'id' required (^[a-zA-Z0-9._-]+$)" % i
        for key in ("color", "border_color"):
            val = el.get(key)
            if val is not None and not DRAW_COLOR_RE.match(str(val)):
                return "element '%s': %s must be #RRGGBBAA" % (eid, key)
        fills = el.get("fill_colors")
        if fills is not None:
            if not isinstance(fills, list):
                return "element '%s': fill_colors must be an array" % eid
            for c in fills:
                if not DRAW_COLOR_RE.match(str(c)):
                    return "element '%s': fill_colors must be #RRGGBBAA" % eid
    return None


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

class Recording:
    def __init__(self, slug, mode):
        self.slug = slug
        self.mode = mode
        self.events = []
        self.assets = {}
        self.notes = []
        self.t0 = time.monotonic()
        self.lock = threading.Lock()

    def now(self):
        return round(time.monotonic() - self.t0, 4)

    def add(self, **event):
        with self.lock:
            self.events.append(event)

    def note(self, text):
        with self.lock:
            if text not in self.notes:
                self.notes.append(text)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "busyrec"

    # silence the default per-request stderr logging
    def log_message(self, fmt, *args):
        pass

    # -- plumbing ----------------------------------------------------------
    @property
    def rec(self):
        return self.server.rec

    @property
    def dev(self):
        return self.server.dev

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return b""
        return self.rfile.read(length) if length else b""

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Token, X-API-Sem-Ver")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(body)
        return code

    def _ok(self, extra=None):
        payload = {"result": "OK"}
        if extra:
            payload.update(extra)
        return self._send(200, payload)

    def _fail(self, code, msg):
        return self._send(code, {"error": msg, "code": code})

    # -- dispatch ----------------------------------------------------------
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def _handle(self, method):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body = self._read_body()

        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            self.rec.note(
                "app tried to open a WebSocket (%s): input events are not stubbed, "
                "re-record with --upstream against a running emulator" % path)
            self._fail(501, "WebSocket not supported by busyrec stub")
            return

        t = self.rec.now()
        if self.server.upstream:
            status, extra = self._proxy(method, self.path, body)
            # Keep a shadow of the device state so replay has the merged element
            # set even in proxy mode. Only apply what upstream accepted; the
            # --conformance pass is what verifies the two stay in agreement.
            if status == 200:
                shadow = self._shadow(method, path, query, body)
                if shadow:
                    extra = dict(extra or {}, **shadow)
        else:
            status, extra = self._stub(method, path, query, body)
        event = {"t": t, "method": method, "path": path, "status": status}
        if query:
            event["query"] = query
        parsed_body = _maybe_json(body)
        if parsed_body is not None:
            event["body"] = parsed_body
        if extra:
            event.update(extra)
        self.rec.add(**event)

    # -- proxy mode --------------------------------------------------------
    def _proxy(self, method, path, body):
        host, port = self.server.upstream
        conn = http.client.HTTPConnection(host, port, timeout=10)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection")}
        try:
            conn.request(method, path, body=body or None, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            ctype = resp.getheader("Content-Type") or "application/json"
            self._send(resp.status, data, ctype)
            return resp.status, None
        except Exception as exc:                      # upstream down or slow
            self._fail(502, "upstream unreachable: %s" % exc)
            return 502, None
        finally:
            conn.close()

    def _shadow(self, method, path, query, body):
        """Mirror an upstream-accepted call into local state, for replay."""
        dev = self.dev
        if path == "/api/display/draw" and method == "POST":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                return None
            app = payload.get("application_name") or payload.get("app_id")
            elements = payload.get("elements") or []
            priority = 50 if payload.get("priority") is None else payload["priority"]
            with dev.lock:
                for el in elements:
                    if el.get("type") == "image" and el.get("path"):
                        key = "%s/%s" % (app, el["path"])
                        if key in dev.assets:
                            el["path"] = key
                merged = dev.merge_elements(app, elements)
                if merged is None:
                    return None
                dev.draw_frame(app, merged, priority)
                return {"frame": _frame_copy(dev.frame)}
        if path == "/api/display/draw" and method == "DELETE":
            with dev.lock:
                dev.clear(query.get("application_name"))
                return {"frame": _frame_copy(dev.frame)}
        if path == "/api/assets/upload" and method == "POST":
            app, name = query.get("application_name"), query.get("file")
            if app and name:
                return self._store_asset(app, name, body)
        return None

    # -- standalone stub ---------------------------------------------------
    def _stub(self, method, path, query, body):
        dev = self.dev

        if path == "/api/display/draw" and method == "POST":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                return self._fail(400, "Bad request: invalid JSON"), None
            return self._stub_draw(payload)

        if path == "/api/display/draw" and method == "DELETE":
            with dev.lock:
                dev.clear(query.get("application_name"))
                frame = _frame_copy(dev.frame)
            return self._ok(), {"frame": frame}

        if path == "/api/display/brightness":
            if method == "GET":
                val = dev.brightness
                return self._send(200, {"value": "auto" if val == "auto" else str(val)}), None
            if method == "POST":
                raw = query.get("value")
                if raw == "auto":
                    dev.brightness = "auto"
                else:
                    try:
                        num = float(raw)
                    except (TypeError, ValueError):
                        return self._fail(400, "Bad request: value 0-100 or auto"), None
                    if not 0 <= num <= 100:
                        return self._fail(400, "Bad request: value 0-100 or auto"), None
                    dev.brightness = num
                return self._ok(), None

        if path == "/api/assets/upload" and method == "POST":
            app, name = query.get("application_name"), query.get("file")
            if not app or not name:
                return self._fail(400, "application_name and file required"), None
            return self._ok(), self._store_asset(app, name, body)

        if path == "/api/assets/upload" and method == "DELETE":
            app = query.get("application_name")
            if not app:
                return self._fail(400, "application_name required"), None
            with dev.lock:
                hits = [k for k in dev.assets if k.startswith(app + "/")]
                for k in hits:
                    del dev.assets[k]
            if not hits:
                return self._fail(404, "Assets not found"), None
            return self._ok(), None

        if path == "/api/audio/play" and method == "POST":
            try:
                payload = json.loads(body.decode()) if body else {}
            except ValueError:
                payload = {}
            if not payload.get("application_name"):
                return self._fail(400, "Missing application_name"), None
            if payload.get("path") and payload.get("stock_path"):
                return self._fail(400, "Both path and stock_path are defined"), None
            if not payload.get("path") and not payload.get("stock_path"):
                return self._fail(400, "Missing path or stock_path"), None
            return self._ok(), None

        if path == "/api/audio/play" and method == "DELETE":
            return self._ok(), None

        if path == "/api/audio/volume":
            if method == "GET":
                return self._send(200, {"volume": dev.volume}), None
            try:
                num = float(query.get("volume"))
            except (TypeError, ValueError):
                return self._fail(400, "Bad request: volume 0-100"), None
            if not 0 <= num <= 100:
                return self._fail(400, "Bad request: volume 0-100"), None
            dev.volume = num
            return self._ok(), None

        if path.startswith("/api/storage/"):
            return self._stub_storage(method, path, query, body), None

        if path == "/api/busy/snapshot":
            if method == "GET":
                return self._send(200, dev.busy_snapshot), None
            if method == "PUT":
                try:
                    payload = json.loads(body.decode()) if body else {}
                except ValueError:
                    return self._fail(400, "Bad request: invalid JSON"), None
                snap = payload.get("snapshot") or {}
                types = ("NOT_STARTED", "INFINITE", "SIMPLE", "INTERVAL")
                if snap.get("type") not in types:
                    return self._fail(400, "Bad request: snapshot.type"), None
                dev.busy_snapshot = {
                    "snapshot": snap,
                    "snapshot_timestamp_ms": payload.get("snapshot_timestamp_ms")
                    or int(time.time() * 1000),
                }
                self.rec.note(
                    "app drives BUSY modes (PUT /api/busy/snapshot). Neither this stub "
                    "nor the emulator renders theme animations, so the preview will not "
                    "show them: capture from hardware for this one.")
                return self._ok(), None

        if path.startswith("/api/busy/profiles/"):
            slot = path.rsplit("/", 1)[-1]
            if method == "GET":
                if slot not in ("busy", "custom"):
                    return self._fail(404, "no such profile"), None
                return self._send(200, {
                    "sort_order": 0 if slot == "busy" else 1,
                    "title": slot.capitalize(), "id": "profile-" + slot,
                    "timer_settings": {"type": "INFINITE"},
                    "busy_bar_settings": dev.busy_snapshot["snapshot"]["busy_bar_settings"],
                    "profile_timestamp_ms": int(time.time() * 1000),
                }), None
            return self._ok(), None

        if path == "/api/name":
            if method == "GET":
                return self._send(200, {"name": dev.name}), None
            return self._ok(), None

        if path == "/api/time" and method == "GET":
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            return self._send(200, {"timestamp": now + "Z"}), None
        if path == "/api/time/timezone" and method == "GET":
            return self._send(200, {"name": "Europe/Amsterdam", "offset": 3600, "abbr": "CET"}), None
        if path == "/api/time/tzlist" and method == "GET":
            return self._send(200, {"list": [
                {"name": "Europe/Amsterdam", "offset": 3600, "abbr": "CET"},
                {"name": "UTC", "offset": 0, "abbr": "UTC"}]}), None
        if path.startswith("/api/time"):
            return self._ok(), None

        if path == "/api/status" or path.startswith("/api/status/"):
            groups = {
                "device": {"serial_number": "BUSYREC0000", "usb_mac": "02:00:00:00:00:01",
                           "otp_valid": True, "firmware_security": "none"},
                "firmware": {"version": "busyrec-1.0.0", "target": "emu", "branch": "dev",
                             "build_date": "2026-08-11", "commit_hash": "busyrec",
                             "api_semver": API_SEMVER},
                "system": {"api_semver": API_SEMVER, "uptime": "00d 00h 05m 00s",
                           "boot_time": int(time.time()) - 300, "auto_update_enabled": False},
                "power": {"state": "discharging", "battery_charge": 100,
                          "battery_voltage": 4.2, "battery_current": -0.12, "usb_voltage": 0},
            }
            if path == "/api/status":
                return self._send(200, groups), None
            sub = path[len("/api/status/"):]
            if sub in groups:
                return self._send(200, groups[sub]), None
            return self._fail(404, "no such status group"), None

        if path == "/api/version":
            return self._send(200, {"api_semver": API_SEMVER}), None
        if path == "/api/transport":
            return self._send(200, {"type": "usb"}), None
        if path == "/api/access":
            if method == "GET":
                return self._send(200, {"mode": "disabled", "key_valid": True}), None
            return self._ok(), None
        if path == "/api/log_dump":
            return self._ok({"path": "/ext/logs/dump.txt"}), None

        self.rec.note("app called %s %s, which the stub does not implement "
                      "(re-record with --upstream if the app depends on it)" % (method, path))
        return self._fail(404, "not found"), None

    def _store_asset(self, app, name, body):
        """Store an upload content-addressed.

        Apps rotate a small ring of filenames (waves cycles frame0..frame3 for
        every frame it draws), so keying by filename would keep only the last
        version of each and replay would show four frames instead of sixty.
        Hashing the bytes keeps every distinct image while still deduplicating
        the ones that genuinely repeat.
        """
        ctype = (self.headers.get("Content-Type") or "")
        if "application/json" in ctype:
            try:
                raw = base64.b64decode(json.loads(body.decode()).get("data") or "")
            except Exception:
                raw = b""
        else:
            raw = body
        key = "%s/%s" % (app, name)
        sha = hashlib.sha256(raw).hexdigest()[:16]
        with self.dev.lock:
            self.dev.assets[key] = raw
        with self.rec.lock:
            self.rec.assets.setdefault(sha, base64.b64encode(raw).decode("ascii"))
        return {"asset": key, "sha": sha, "bytes": len(raw)}

    def _stub_draw(self, payload):
        dev = self.dev
        app = payload.get("application_name") or payload.get("app_id")
        if not app:
            return self._fail(400, "Bad request: application_name required"), None
        elements = payload.get("elements")
        if not isinstance(elements, list) or not elements:
            return self._fail(400, "Nothing to display"), None
        priority = 50 if payload.get("priority") is None else payload.get("priority")
        if not isinstance(priority, (int, float)) or isinstance(priority, bool) \
                or not 1 <= priority <= 100:
            return self._fail(400, "Bad request: priority 1-100"), None
        err = validate_draw_body(payload)
        if err:
            return self._fail(400, "Bad request: " + err), None

        with dev.lock:
            # Firmware resolves bare image paths inside the drawing app's asset
            # namespace: upload file="logo.png", then draw path="logo.png".
            for el in elements:
                if el.get("type") == "image" and el.get("path"):
                    key = "%s/%s" % (app, el["path"])
                    if key in dev.assets:
                        el["path"] = key
            merged = dev.merge_elements(app, elements)
            if merged is None:
                return self._fail(400, "Elements number limit exceeded"), None
            if not dev.draw_frame(app, merged, priority):
                return self._fail(409, "Not drawn due to low priority"), None
            frame = _frame_copy(dev.frame)
        return self._ok(), {"frame": frame}

    def _stub_storage(self, method, path, query, body):
        dev, key = self.dev, query.get("path")
        if path == "/api/storage/write" and method == "POST":
            if not key:
                return self._fail(400, "path required")
            dev.storage[key] = body
            return self._ok()
        if path == "/api/storage/read" and method == "GET":
            if key not in dev.storage:
                return self._fail(400, "not found")
            return self._send(200, dev.storage[key], "application/octet-stream")
        if path == "/api/storage/list" and method == "GET":
            pre = key or ""
            items = [{"type": "file", "name": k, "size": len(v or b"")}
                     for k, v in dev.storage.items() if k.startswith(pre)]
            return self._send(200, {"list": items})
        if path == "/api/storage/remove" and method == "DELETE":
            dev.storage.pop(key, None)
            return self._ok()
        if path == "/api/storage/status" and method == "GET":
            return self._send(200, {"used_bytes": 1048576, "free_bytes": 15728640,
                                    "total_bytes": 16777216})
        return self._ok()


def _frame_copy(frame):
    return {"application_name": frame["application_name"],
            "priority": frame["priority"],
            "elements": json.loads(json.dumps(frame["elements"]))}


def _maybe_json(body):
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"_binary_bytes": len(body)}


# --------------------------------------------------------------------------
# running the app
# --------------------------------------------------------------------------

def resolve_app(arg):
    """Accept `waves`, `apps/waves`, or a path to the folder."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (arg, os.path.join(root, arg), os.path.join(root, "apps", arg)):
        if os.path.isfile(os.path.join(candidate, "app.py")):
            return os.path.abspath(candidate)
    sys.exit("busyrec: no app.py found for %r (looked in apps/%s)" % (arg, arg))


def python_for(app_dir, allow_venv=True):
    """Return the interpreter to use, creating a venv when requirements.txt exists."""
    req = os.path.join(app_dir, "requirements.txt")
    if not allow_venv or not os.path.isfile(req):
        return sys.executable, None
    venv = os.path.join(app_dir, ".venv")
    binary = os.path.join(venv, "bin", "python")
    stamp_file = os.path.join(venv, ".busyrec-stamp")
    with open(req, "rb") as fh:
        stamp = hashlib.sha256(fh.read()).hexdigest()
    current = ""
    if os.path.isfile(stamp_file):
        with open(stamp_file) as fh:
            current = fh.read().strip()
    if os.path.isfile(binary) and current == stamp:
        return binary, None
    print("busyrec: preparing %s/.venv from requirements.txt" % os.path.basename(app_dir),
          file=sys.stderr)
    subprocess.run([sys.executable, "-m", "venv", venv], check=True)
    proc = subprocess.run([binary, "-m", "pip", "install", "-q", "-r", req],
                          capture_output=True, text=True)
    if proc.returncode:
        return binary, "pip install failed: %s" % (proc.stderr.strip()[:400] or "unknown error")
    with open(stamp_file, "w") as fh:
        fh.write(stamp)
    return binary, None


def drain(stream, sink):
    for line in iter(stream.readline, ""):
        sink.append(line.rstrip("\n"))
    stream.close()


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def analyse(rec, exit_info, slug):
    """Turn a recording into the findings a reviewer actually needs."""
    findings, stats = [], {}
    events = rec.events
    draws = [e for e in events if e["path"] == "/api/display/draw" and e["method"] == "POST"]
    ok_draws = [e for e in draws if e["status"] == 200]

    calls = {}
    for e in events:
        calls["%s %s" % (e["method"], e["path"])] = calls.get("%s %s" % (e["method"], e["path"]), 0) + 1
    stats["calls"] = dict(sorted(calls.items(), key=lambda kv: -kv[1]))
    stats["draws"] = len(draws)
    stats["draws_ok"] = len(ok_draws)
    stats["duration_s"] = round(events[-1]["t"], 2) if events else 0.0

    if not draws:
        stderr = exit_info.get("stderr") or ""
        required = re.search(r"error: the following arguments are required: (.+)", stderr)
        if required:
            findings.append(("error", "app needs arguments (%s) and exited before drawing. "
                                      "Re-record with: busyrec %s -- %s <value>"
                             % (required.group(1), slug, required.group(1).split(",")[0].strip())))
        elif "usage:" in stderr and exit_info.get("returncode") == 2:
            findings.append(("error", "app rejected its arguments and exited: %s"
                             % stderr.strip().splitlines()[-1][:160]))
        elif exit_info.get("has_test"):
            # Alert-style apps (iss-alert, pollen-alarm, buienradar-alarm) only draw
            # when there is something to report, so a quiet day looks like a broken
            # app. That is what the --test convention is for.
            findings.append(("warn", "app drew nothing, which is expected if it only draws when "
                                     "it has something to report. It accepts --test, so record "
                                     "it with: busyrec %s -- --test" % slug))
        else:
            findings.append(("error", "app never called /api/display/draw: nothing to preview"))
    else:
        stats["first_draw_s"] = round(draws[0]["t"], 2)
        if stats["duration_s"] > 0:
            stats["draw_rate_hz"] = round(len(ok_draws) / max(stats["duration_s"], 0.001), 2)
        if draws[0]["t"] > 5:
            findings.append(("warn", "first draw only at t=%.1fs: a preview run needs a longer "
                                     "--seconds, and users stare at a blank bar that long too"
                             % draws[0]["t"]))

    uploads = [e for e in events if e["path"] == "/api/assets/upload" and e["method"] == "POST"]
    if uploads:
        stats["asset_uploads"] = len(uploads)
        stats["asset_distinct"] = len(rec.assets)
        stats["asset_bytes"] = sum(e.get("bytes", 0) for e in uploads)
        if stats["duration_s"] > 1 and stats["asset_bytes"] / stats["duration_s"] > 2_000_000:
            findings.append(("warn", "uploading %.1f MB/s of assets: fine on USB, but this is a "
                                     "lot to push over Wi-Fi continuously"
                             % (stats["asset_bytes"] / stats["duration_s"] / 1e6)))

    # element id churn: the 100-element cap killer
    stored = [e["frame"]["elements"] for e in ok_draws if e.get("frame")]
    if stored:
        counts = [len(x) for x in stored]
        peak = max(counts)
        stats["elements_stored_max"] = peak
        # Judge on the peak and how often the set grew, not on first-vs-last: an
        # app that clears at the end would otherwise hide a run of growth, and
        # this has to fire long before the cap is actually reached (the bug takes
        # about a minute to bite on hardware, far longer than a preview run).
        rises = sum(1 for i in range(1, len(counts)) if counts[i] > counts[i - 1])
        if len(counts) > 4 and peak >= max(8, counts[0] * 2) and rises >= (len(counts) - 1) * 0.5:
            findings.append((
                "error",
                "accumulating element ids: the stored set grew %d -> %d over %d draws. "
                "The firmware caps it at %d and then 400s. Reuse a fixed set of ids "
                "instead of minting new ones per frame."
                % (counts[0], peak, len(counts), MAX_ELEMENTS)))
        elif peak > MAX_ELEMENTS * 0.6:
            findings.append(("warn", "stored element set peaked at %d of the %d cap"
                             % (peak, MAX_ELEMENTS)))

    # rejections
    for status, label in ((400, "rejected"), (409, "refused")):
        hits = [e for e in draws if e["status"] == status]
        if hits:
            stats["draws_%d" % status] = len(hits)
    bad = [e for e in draws if e["status"] == 400]
    if bad:
        findings.append(("error", "%d draw(s) rejected with 400: the same call fails on real "
                                  "hardware. First failing body at t=%.2fs." % (len(bad), bad[0]["t"])))

    refused = [e for e in draws if e["status"] == 409]
    if refused:
        after = [e for e in draws if e["t"] > refused[-1]["t"]]
        if not after and exit_info.get("returncode") not in (0, None):
            findings.append(("error", "app stopped drawing after a 409 and exited non-zero: "
                                      "409 means a higher-priority app owns the screen and must "
                                      "be tolerated, not treated as fatal"))
        else:
            findings.append(("info", "handled %d x 409 and kept running" % len(refused)))

    # geometry, fonts
    # Parking an element far off to the left is an established way to hide it
    # (the firmware keeps the last version of an id until it times out, so you
    # cannot un-draw by omission). Only flag elements that sit horizontally ON
    # the display but vertically off it, which is a real layout mistake rather
    # than that idiom.
    seen_fonts, misplaced = set(), []
    for e in ok_draws:
        for el in (e.get("body") or {}).get("elements") or []:
            if el.get("font"):
                seen_fonts.add(el["font"])
            x, y = el.get("x"), el.get("y")
            if el.get("display") == "back":
                continue           # the back OLED is 160x80, not this display
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue
            if 0 <= x <= DISPLAY_W and not -4 <= y <= DISPLAY_H + 4:
                misplaced.append((el.get("id"), x, y))
    unknown = sorted(f for f in seen_fonts if f not in FONTS)
    if unknown:
        findings.append(("error", "unknown font(s) %s: allowed are %s"
                         % (", ".join(unknown), ", ".join(FONTS))))
    if misplaced:
        findings.append(("warn", "%d element(s) sit above or below the 16 px display, e.g. id=%r "
                                 "at x=%s y=%s" % (len(misplaced), misplaced[0][0],
                                                   misplaced[0][1], misplaced[0][2])))

    # shutdown behaviour
    cleared = any(e["method"] == "DELETE" and e["path"] == "/api/display/draw" for e in events)
    stats["cleared_on_exit"] = cleared
    if not cleared and ok_draws:
        findings.append(("warn", "app never sent DELETE /api/display/draw: it leaves its frame "
                                 "on the bar after quitting"))
    argv_problem = any("needs arguments" in m or "rejected its arguments" in m
                       for _, m in findings)
    if exit_info.get("timed_out"):
        findings.append(("error", "app did not exit within %ss of SIGINT: needs a KeyboardInterrupt "
                                  "handler" % exit_info.get("grace")))
    elif not argv_problem and exit_info.get("returncode") not in (0, None, -signal.SIGINT):
        findings.append(("warn", "app exited with code %s after SIGINT" % exit_info["returncode"]))
    if exit_info.get("traceback"):
        last = exit_info["traceback"].strip().splitlines()[-1][:200]
        if last.strip() == "KeyboardInterrupt":
            findings.append(("warn", "Ctrl-C prints a traceback: wrap the main loop in "
                                     "try/except KeyboardInterrupt, as the other gallery apps do"))
        else:
            findings.append(("error", "app raised: %s" % last))

    for note in rec.notes:
        findings.append(("warn", note))

    return findings, stats


def print_report(slug, findings, stats, out_path):
    icon = {"error": "FAIL", "warn": "WARN", "info": "note"}
    print("\nbusyrec report: %s" % slug)
    print("-" * (16 + len(slug)))
    print("  duration      %.2fs" % stats.get("duration_s", 0))
    print("  draws         %d ok / %d total%s" % (
        stats.get("draws_ok", 0), stats.get("draws", 0),
        "".join("  (%d x %s)" % (stats[k], k.split("_")[1])
                for k in ("draws_400", "draws_409") if k in stats)))
    if "draw_rate_hz" in stats:
        print("  rate          %.2f draws/s" % stats["draw_rate_hz"])
    if "first_draw_s" in stats:
        print("  first draw    %.2fs" % stats["first_draw_s"])
    if "elements_stored_max" in stats:
        print("  elements      %d stored at peak (cap %d)"
              % (stats["elements_stored_max"], MAX_ELEMENTS))
    if "asset_uploads" in stats:
        print("  assets        %d upload(s), %d distinct, %.0f kB"
              % (stats["asset_uploads"], stats["asset_distinct"], stats["asset_bytes"] / 1000))
    print("  clean exit    %s" % ("yes" if stats.get("cleared_on_exit") else "no DELETE sent"))
    print("  endpoints     " + ", ".join("%s x%d" % (k, v) for k, v in
                                         list(stats.get("calls", {}).items())[:6]))
    if findings:
        print()
        for level, msg in findings:
            first = True
            for line in _wrap(msg, 74):
                print("  %-5s %s" % (icon[level] if first else "", line))
                first = False
    print("\n  recording     %s" % out_path)
    errors = sum(1 for lvl, _ in findings if lvl == "error")
    return errors


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = word if not line else line + " " + word
    if line:
        out.append(line)
    return out


# --------------------------------------------------------------------------
# conformance
# --------------------------------------------------------------------------

def conformance(rec_path, upstream):
    """Replay a recording against a real emulator and diff the status codes.

    The stub reimplements firmware rules that live in busybar-emulator/server.js.
    This is how we notice when the two drift apart.
    """
    with open(rec_path) as fh:
        rec = json.load(fh)
    host, port = parse_host(upstream)
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("DELETE", "/api/display/draw")
    conn.getresponse().read()

    diffs, checked = [], 0
    for asset_key, b64 in (rec.get("assets") or {}).items():
        app, _, name = asset_key.partition("/")
        conn.request("POST", "/api/assets/upload?application_name=%s&file=%s"
                     % (urllib.parse.quote(app), urllib.parse.quote(name)),
                     body=base64.b64decode(b64),
                     headers={"Content-Type": "application/octet-stream"})
        conn.getresponse().read()

    for event in rec["events"]:
        if not event["path"].startswith("/api/"):
            continue
        query = urllib.parse.urlencode(event.get("query") or {})
        path = event["path"] + ("?" + query if query else "")
        body = json.dumps(event["body"]).encode() if isinstance(event.get("body"), dict) \
            and "_binary_bytes" not in event["body"] else None
        try:
            conn.request(event["method"], path, body=body,
                         headers={"Content-Type": "application/json"} if body else {})
            resp = conn.getresponse()
            resp.read()
        except Exception as exc:
            diffs.append((event, "transport error: %s" % exc))
            conn = http.client.HTTPConnection(host, port, timeout=10)
            continue
        checked += 1
        if resp.status != event["status"]:
            diffs.append((event, "stub said %d, emulator said %d" % (event["status"], resp.status)))
    conn.request("DELETE", "/api/display/draw")
    conn.getresponse().read()
    conn.close()

    print("conformance: replayed %d call(s) against %s" % (checked, upstream))
    for event, msg in diffs:
        print("  MISMATCH  t=%.2f %s %s: %s" % (event["t"], event["method"], event["path"], msg))
    if not diffs:
        print("  no differences: the stub matches the emulator for this recording")
    return 1 if diffs else 0


def parse_host(value):
    value = value.replace("http://", "").rstrip("/")
    if ":" in value:
        host, _, port = value.partition(":")
        return host, int(port)
    return value, 80


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="busyrec",
        description="Record a BUSY Bar app's display output to a portable .busyrec file.",
        epilog="Anything after -- is passed straight through to the app.")
    parser.add_argument("app", help="app slug or folder, e.g. waves or apps/waves")
    parser.add_argument("--seconds", type=float, default=6.0,
                        help="how long to record (default 6)")
    parser.add_argument("--out", help="output path (default apps/<slug>/<slug>.busyrec)")
    parser.add_argument("--upstream", metavar="HOST[:PORT]",
                        help="proxy to a real emulator or bar instead of using the built-in stub")
    parser.add_argument("--steal-at", type=float, metavar="SECONDS",
                        help="have a higher-priority app take the screen at this point, to check "
                             "the app tolerates 409")
    parser.add_argument("--grace", type=float, default=5.0,
                        help="seconds to wait for a clean exit after SIGINT (default 5)")
    parser.add_argument("--no-venv", action="store_true",
                        help="do not build a venv from requirements.txt")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra environment variable for the app (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--conformance", metavar="HOST[:PORT]",
                        help="replay an existing .busyrec against a real emulator and diff "
                             "status codes (pass the recording as the positional argument)")
    args, app_args = parser.parse_known_args(argv)
    if app_args and app_args[0] == "--":
        app_args = app_args[1:]

    if args.conformance:
        return conformance(args.app, args.conformance)

    app_dir = resolve_app(args.app)
    slug = os.path.basename(app_dir)
    out_path = args.out or os.path.join(app_dir, slug + ".busyrec")

    interpreter, venv_error = python_for(app_dir, allow_venv=not args.no_venv)
    if venv_error:
        sys.exit("busyrec: %s" % venv_error)

    rec = Recording(slug, "proxy" if args.upstream else "standalone")
    dev = Device()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.rec, server.dev = rec, dev
    server.upstream = parse_host(args.upstream) if args.upstream else None
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if args.upstream:                      # start from a clean screen, as the skill does
        host, up_port = server.upstream
        try:
            conn = http.client.HTTPConnection(host, up_port, timeout=5)
            conn.request("DELETE", "/api/display/draw")
            conn.getresponse().read()
            conn.close()
        except Exception as exc:
            sys.exit("busyrec: upstream %s unreachable: %s" % (args.upstream, exc))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    for pair in args.env:
        key, _, value = pair.partition("=")
        env[key] = value

    cmd = [interpreter, "app.py", "--host", "127.0.0.1:%d" % port] + app_args
    print("busyrec: %s -> 127.0.0.1:%d (%s, %.0fs)"
          % (slug, port, rec.mode, args.seconds), file=sys.stderr)

    rec.t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=app_dir, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    out_lines, err_lines = [], []
    threading.Thread(target=drain, args=(proc.stdout, out_lines), daemon=True).start()
    threading.Thread(target=drain, args=(proc.stderr, err_lines), daemon=True).start()

    steal_done = False
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        if args.steal_at is not None and not steal_done and rec.now() >= args.steal_at:
            steal_done = True
            with dev.lock:
                dev.draw_frame(STEAL_APP, [{"id": "steal", "type": "rectangle", "x": 0, "y": 0,
                                            "width": 72, "height": 16, "color": "#FF0000FF"}], 100)
            rec.note("screen stolen at t=%.1fs by a priority-100 app (--steal-at)" % args.steal_at)
        time.sleep(0.05)

    exit_info = {"grace": args.grace}
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=args.grace)
        except subprocess.TimeoutExpired:
            exit_info["timed_out"] = True
            proc.kill()
            proc.wait(timeout=5)
    exit_info["returncode"] = proc.returncode
    with open(os.path.join(app_dir, "app.py"), encoding="utf-8", errors="ignore") as fh:
        exit_info["has_test"] = '"--test"' in fh.read() and "--test" not in app_args
    time.sleep(0.15)                       # let in-flight requests land
    server.shutdown()

    stderr_text = "\n".join(err_lines)
    exit_info["stderr"] = stderr_text[-4000:]
    if "Traceback (most recent call last)" in stderr_text:
        exit_info["traceback"] = stderr_text[stderr_text.index("Traceback"):]

    payload = {
        "version": 1,
        "app": slug,
        "mode": rec.mode,
        "recorded_with": "busyrec/1.0",
        "duration_ms": int(rec.now() * 1000),
        "display": {"width": DISPLAY_W, "height": DISPLAY_H},
        "assets": rec.assets,
        "events": rec.events,
        "notes": rec.notes,
        "exit": exit_info,
        "stdout": out_lines[-40:],
        "stderr": err_lines[-40:],
    }
    with open(out_path, "w") as fh:
        json.dump(payload, fh)

    findings, stats = analyse(rec, exit_info, slug)
    if args.json:
        print(json.dumps({"app": slug, "recording": out_path, "stats": stats,
                          "findings": [{"level": l, "message": m} for l, m in findings]}, indent=2))
        return 1 if any(l == "error" for l, _ in findings) else 0
    errors = print_report(slug, findings, stats, out_path)
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
