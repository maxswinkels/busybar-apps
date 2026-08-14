#!/usr/bin/env python3
"""Uptime Bar: your Uptime Kuma monitors as a wall of pixels, silent until something goes down.

    export KUMA_URL=https://uptime.example.com
    export KUMA_API_KEY=<your-api-key>        # Uptime Kuma: Settings -> API Keys
    python3 app.py                            # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --host 127.0.0.1:8080      # emulator or a Wi-Fi bar
    python3 app.py --status-page mystatus     # no API key: read a public status page
    python3 app.py --demo                     # scripted cycle, no network
"""
import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "uptime-bar"
W, H = 72, 16

# Monitor states, matching Uptime Kuma's own numbering.
DOWN, UP, PENDING, MAINTENANCE = 0, 1, 2, 3

# Cell colors. UP is deliberately dim: a healthy wall should not demand attention.
CELL_COLORS = {
    UP: (26, 160, 74),
    DOWN: (239, 68, 68),
    PENDING: (245, 158, 11),
    MAINTENANCE: (59, 130, 246),
}
EMPTY = (0, 0, 0)

STATE_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "busybar-uptime-bar.json",
)


def parse_args():
    p = argparse.ArgumentParser(description="Uptime Kuma monitor wall for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--kuma-url", default=os.environ.get("KUMA_URL", ""),
                   help="Uptime Kuma base URL (or set KUMA_URL)")
    p.add_argument("--api-key", default=os.environ.get("KUMA_API_KEY", ""),
                   help="API key for /metrics (or set KUMA_API_KEY)")
    p.add_argument("--status-page", default=os.environ.get("KUMA_STATUS_PAGE", ""),
                   help="read a public status page by slug instead of /metrics")
    p.add_argument("--interval", type=int, default=30, help="seconds between polls")
    p.add_argument("--recovery-hold", type=int, default=5,
                   help="seconds to show the recovery flash")
    p.add_argument("--rotate", type=float, default=3.0,
                   help="seconds per site when several are down")
    p.add_argument("--no-sound", action="store_true", help="stay silent on a new outage")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    p.add_argument("--demo", action="store_true", help="scripted cycle on fake data")
    p.add_argument("--tour", action="store_true",
                   help="walk every screen and grid size in turn, on fake data")
    p.add_argument("--dump", action="store_true", help="print parsed monitors and exit")
    return p.parse_args()


# ---------------------------------------------------------------------------
# BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs)
# ---------------------------------------------------------------------------

def _base(host):
    return "http://" + host.replace("http://", "").replace("https://", "").rstrip("/")


def _draw(host, elements, priority=30, led=None):
    """POST /api/display/draw. Returns the HTTP status; 409 means a busier app owns the screen."""
    body = {"application_name": APP, "priority": priority, "elements": elements}
    if led:
        body["led_notification_color"] = led
    req = urllib.request.Request(
        _base(host) + "/api/display/draw",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code


def _clear(host):
    qs = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + qs, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code


def _upload(host, filename, data):
    qs = urllib.parse.urlencode({"application_name": APP, "file": filename})
    req = urllib.request.Request(
        _base(host) + "/api/assets/upload?" + qs, data=data, method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code


def _play(host, stock_path):
    body = {"application_name": APP, "stock_path": stock_path}
    req = urllib.request.Request(
        _base(host) + "/api/audio/play", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code


def _png(pixels):
    """Encode a W*H list of (r, g, b) tuples as an RGBA PNG. No Pillow needed."""
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            r, g, b = pixels[y * W + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Uptime Kuma
# ---------------------------------------------------------------------------

_METRIC_RE = re.compile(r'^monitor_status\{(?P<labels>.*)\}\s+(?P<value>-?\d+(?:\.\d+)?)\s*$')
_NAME_RE = re.compile(r'monitor_name="((?:[^"\\]|\\.)*)"')


def _parse_metrics(text):
    """Pull (name, status) out of Uptime Kuma's Prometheus /metrics output."""
    monitors = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name = _NAME_RE.search(m.group("labels"))
        if not name:
            continue
        label = name.group(1).replace('\\"', '"').replace("\\\\", "\\").replace("\\n", " ")
        try:
            status = int(float(m.group("value")))
        except ValueError:
            continue
        monitors.append((label, status))
    return monitors


def _fetch_metrics(url, api_key):
    """GET {url}/metrics with Basic auth: empty username, API key as the password."""
    req = urllib.request.Request(url.rstrip("/") + "/metrics",
                                 headers={"User-Agent": "busybar-uptime-bar/1.0"})
    if api_key:
        token = base64.b64encode(("" + ":" + api_key).encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=15) as r:
        return _parse_metrics(r.read().decode("utf-8", "ignore"))


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "busybar-uptime-bar/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _fetch_status_page(url, slug):
    """Fallback for instances without an API key: a public status page needs no auth."""
    base = url.rstrip("/")
    page = _get_json(f"{base}/api/status-page/{urllib.parse.quote(slug)}")
    names = {}
    for group in page.get("publicGroupList") or []:
        for mon in group.get("monitorList") or []:
            names[str(mon.get("id"))] = mon.get("name") or str(mon.get("id"))

    beats = _get_json(f"{base}/api/status-page/heartbeat/{urllib.parse.quote(slug)}")
    monitors = []
    for mid, series in (beats.get("heartbeatList") or {}).items():
        if not series:
            continue
        monitors.append((names.get(str(mid), str(mid)), int(series[-1].get("status", UP))))
    return monitors


def _demo_monitors(elapsed):
    """A scripted cycle so the app can be previewed without a Kuma instance."""
    names = ["alpha.example.com", "api.example.com", "blog.example.net", "cdn.example.com",
             "checkout.example.com", "docs.example.io", "mail.example.com", "shop.example.com",
             "status.example.com", "store.example.net", "support.example.com", "www.example.com"]
    names += [f"node-{i:02d}.example.com" for i in range(1, 41)]
    names.sort()
    states = {n: UP for n in names}
    states[names[3]] = MAINTENANCE
    phase = elapsed % 16.0
    if 3.0 <= phase < 4.0:
        states["checkout.example.com"] = PENDING
    if 4.0 <= phase < 10.0:
        states["checkout.example.com"] = DOWN
    if 6.5 <= phase < 10.0:
        states["mail.example.com"] = DOWN
    return sorted(states.items())


TOUR_LONG = "very-long-hostname.example.com"


def _tour_names(count):
    fixed = ["alpha.example.com", "api.example.com", "blog.example.net", "cdn.example.com",
             "checkout.example.com", "docs.example.io", "mail.example.com", "shop.example.com"]
    names = fixed[:count]
    names += [f"node-{i:02d}.example.com" for i in range(1, count - len(names) + 1)]
    return sorted(names)


def _tour_monitors(elapsed):
    """Walk every screen in turn: each grid density, then every alert state.

    Only the monitor list is scripted; the outage timer, rotation and recovery
    flash all come from the normal code path, so the tour exercises the real logic.
    """
    t = elapsed % 34.0
    if t < 3.0:
        return [(n, UP) for n in _tour_names(8)]          # sparse wall
    if t < 6.0:
        return [(n, UP) for n in _tour_names(26)]         # two rows
    if t >= 29.0:
        return [(n, UP) for n in _tour_names(120)]        # dense 1px layout

    names = sorted(_tour_names(51) + [TOUR_LONG])
    states = {n: UP for n in names}
    states["cdn.example.com"] = MAINTENANCE               # blue cell, from 6s on
    if 9.0 <= t < 11.0:
        states["checkout.example.com"] = PENDING          # amber cell
    if 11.0 <= t < 23.0:
        states["checkout.example.com"] = DOWN             # single-outage takeover
    if 16.0 <= t < 23.0:
        states["mail.example.com"] = DOWN                 # rotation between sites
        states[TOUR_LONG] = DOWN                          # name long enough to scroll
    return sorted(states.items())


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def grid_layout(count):
    """Pick a cell size that fits `count` monitors on 72x16, densest-readable first."""
    for cell_w, pitch_x, cell_h, pitch_y in ((2, 3, 3, 4), (1, 2, 3, 4), (1, 2, 1, 2)):
        cols = (W + pitch_x - cell_w) // pitch_x
        rows = (H + pitch_y - cell_h) // pitch_y
        if cols * rows >= count:
            return cell_w, pitch_x, cell_h, pitch_y, cols, rows
    return 1, 2, 1, 2, (W + 1) // 2, (H + 1) // 2


def render_grid(monitors):
    """Monitors as a wall of cells, row-major in the caller's (alphabetical) order."""
    px = [EMPTY] * (W * H)
    cell_w, pitch_x, cell_h, pitch_y, cols, rows = grid_layout(len(monitors))

    # Center on the rows actually in use, so a half-empty wall still sits level.
    used_rows = max(1, min(rows, -(-len(monitors) // cols)))
    used_cols = min(cols, len(monitors))
    ox = max(0, (W - (used_cols * pitch_x - (pitch_x - cell_w))) // 2)
    oy = max(0, (H - (used_rows * pitch_y - (pitch_y - cell_h))) // 2)

    for i, (_, status) in enumerate(monitors[:cols * rows]):
        col, row = i % cols, i // cols
        color = CELL_COLORS.get(status, CELL_COLORS[PENDING])
        for dy in range(cell_h):
            y = oy + row * pitch_y + dy
            if not (0 <= y < H):
                continue
            for dx in range(cell_w):
                x = ox + col * pitch_x + dx
                if 0 <= x < W:
                    px[y * W + x] = color
    return px


def _text(eid, txt, y, font, color, scroll=False):
    el = {"id": eid, "type": "text", "text": txt, "y": y, "font": font, "color": color}
    if scroll:
        el.update({"x": 0, "align": "top_left", "width": W,
                   "scroll_rate": 600, "scroll_start_delay": 800, "scroll_repeat_delay": 1200})
    else:
        el.update({"x": W // 2, "align": "top_mid"})
    return el


# Elements are parked off-screen rather than dropped: the firmware keeps a
# persistent, id-keyed set per app and never releases what you stop sending.
def park(eid):
    return {"id": eid, "type": "text", "text": " ", "x": -400, "y": 0,
            "font": "tiny", "color": "#00000000"}


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%dS" % seconds
    if seconds < 3600:
        return "%dM" % (seconds // 60)
    if seconds < 86400:
        return "%dH%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dD%02dH" % (seconds // 86400, (seconds % 86400) // 3600)


# Per-glyph advance widths for ASCII 32..126, taken from the device font atlas.
# A flat estimate is too coarse here: "mail.example.com" measures 60 px, but
# 16 chars x 5 px would call it 80 and scroll a name that fits comfortably.
_ADVANCE = {
    "tiny": "32464552334424254444444444224444444444444445465545444566465353443444443342242644443444464444245",
    "small": "22464552334423234344444444234444555555555245465555554546444333440444443442342644443434464444245",
}


def text_width(txt, font="small"):
    table = _ADVANCE.get(font)
    if not table:
        return len(txt) * 6
    total = 0
    for ch in txt:
        i = ord(ch) - 32
        total += int(table[i]) if 0 <= i < len(table) else 6
    return total


def fits(txt, font="small"):
    return text_width(txt, font) <= W


# ---------------------------------------------------------------------------
# Outage bookkeeping
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: float(v) for k, v in (data.get("down_since") or {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(down_since):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"down_since": down_since}, fh)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass  # a missing cache only costs us the outage timer across restarts


class Screen:
    """Owns the three element ids and only pushes what actually changed."""

    def __init__(self, host):
        self.host = host
        self.frame_no = 0
        self.last_png = None
        self.last_push = 0.0
        self.grid_file = None  # last asset name we know the device accepted
        self.priority = 0
        self.sent = {}

    def _send(self, elements, priority, led=None):
        # The firmware refuses a lower-priority draw even from the current
        # owner, so stepping down from an alert means releasing the screen first.
        if priority < self.priority:
            _clear(self.host)
            self.sent.clear()
        self.priority = priority

        changed = [el for el in elements if self.sent.get(el["id"]) != el]
        if not changed and not led:
            return True
        status = _draw(self.host, changed or elements, priority=priority, led=led)
        if status == 409:
            return False  # someone louder owns the screen; try again next tick
        if status >= 400:
            print(f"draw failed: HTTP {status}", file=sys.stderr)
            return False
        for el in changed:
            self.sent[el["id"]] = el
        return True

    def show_grid(self, monitors):
        data = _png(render_grid(monitors))
        now = time.monotonic()
        # Re-upload only when the wall actually changed, but refresh once a
        # minute anyway so the element can never quietly expire on the device.
        if data != self.last_png or self.grid_file is None or now - self.last_push > 60:
            fn = "grid%d.png" % (self.frame_no % 4)
            self.frame_no += 1
            if _upload(self.host, fn, data) >= 400:
                return
            self.last_png, self.last_push, self.grid_file = data, now, fn
        bg = {"id": "bg", "type": "image", "path": self.grid_file, "x": 0, "y": 0}
        self._send([bg, park("t1"), park("t2")], priority=30)

    def show_message(self, name, label, label_color, priority, led=None):
        elements = []
        if "bg" in self.sent:  # nothing to hide until a grid has actually been drawn
            elements.append({"id": "bg", "type": "image", "path": self.grid_file,
                             "x": -400, "y": 0})
        elements += [
            _text("t1", name, 1, "small", "#FFFFFFFF", scroll=not fits(name, "small")),
            _text("t2", label, 10, "tiny", label_color),
        ]
        self._send(elements, priority=priority, led=led)


def main():
    args = parse_args()

    if args.dump:
        monitors = _fetch_metrics(args.kuma_url, args.api_key) if not args.status_page \
            else _fetch_status_page(args.kuma_url, args.status_page)
        for name, status in sorted(monitors):
            print(f"{status}  {name}")
        print(f"\n{len(monitors)} monitors")
        return

    # --test means "show me a frame", so fall back to demo data rather than
    # refusing: it keeps the app smoke-testable without a Kuma instance.
    if args.test and not args.demo and not args.tour and not args.kuma_url:
        print("no KUMA_URL set, drawing demo data", file=sys.stderr)
        args.demo = True
    offline = args.demo or args.tour
    configured = bool(offline or args.kuma_url)
    if not configured:
        print("no Kuma instance: set KUMA_URL (and KUMA_API_KEY), or pass --demo",
              file=sys.stderr)

    screen = Screen(args.host)
    down_since = {} if offline else load_state()
    monitors, last_poll = [], 0.0
    recovered_until, recovered_name, recovery_led = 0.0, "", False
    outage_seen = set()
    started = time.monotonic()

    print(f"{APP} -> {_base(args.host)}  (Ctrl-C to stop)")
    try:
        while True:
            now = time.monotonic()

            if not configured:
                # Say so on the bar itself, not just in a terminal nobody is watching.
                screen.show_message("UPTIME BAR", "SET KUMA_URL", "#F59E0BFF", priority=30)
                if args.test:
                    break
                time.sleep(2.0)
                continue

            if args.tour:
                monitors = _tour_monitors(now - started)
            elif args.demo:
                monitors = _demo_monitors(now - started)
            elif now - last_poll >= args.interval or not last_poll:
                try:
                    monitors = _fetch_status_page(args.kuma_url, args.status_page) \
                        if args.status_page else _fetch_metrics(args.kuma_url, args.api_key)
                    monitors.sort()
                    last_poll = now
                except Exception as e:
                    print(f"fetch error (reusing previous data): {e}", file=sys.stderr)
                    last_poll = now  # never hammer a failing instance

            if not monitors:
                time.sleep(1.0)
                continue

            down = [name for name, status in monitors if status == DOWN]
            for name in down:
                down_since.setdefault(name, time.time())
            for name in [n for n in down_since if n not in down]:
                del down_since[name]
                recovered_until, recovered_name = now + args.recovery_hold, name
                recovery_led = True
                outage_seen.discard(name)
            if not offline:
                save_state(down_since)

            if down:
                new_outage = [n for n in down if n not in outage_seen]
                if new_outage and not args.no_sound:
                    _play(args.host, "calendar_event_starts")
                outage_seen.update(down)

                idx = int(now / args.rotate) % len(down) if len(down) > 1 else 0
                name = sorted(down)[idx]
                elapsed = time.time() - down_since.get(name, time.time())
                label = "DOWN " + fmt_duration(elapsed)
                if len(down) > 1:
                    label += "  %d/%d" % (idx + 1, len(down))
                screen.show_message(name, label, "#EF4444FF", priority=60,
                                    led="#EF4444FF" if new_outage else None)
            elif now < recovered_until:
                screen.show_message(recovered_name, "BACK UP", "#22C55EFF", priority=60,
                                    led="#22C55EFF" if recovery_led else None)
                recovery_led = False
            else:
                screen.show_grid(monitors)

            if args.test:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        _clear(args.host)


if __name__ == "__main__":
    main()
