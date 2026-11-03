#!/usr/bin/env python3
"""Claude Approve: answer Claude Code prompts (permissions, plan approval, questions) from the BUSY Bar.

    python app.py                               # BUSY Bar over USB (10.0.4.20)
    python app.py --host 127.0.0.1:8080         # emulator or a Wi-Fi bar
    python app.py --host 127.0.0.1:8080 --test  # draw one demo card and exit
    python app.py --print-hooks                 # settings.json snippet

Claude Code posts every hook event to this app (http hooks, see --print-hooks).
A PermissionRequest becomes a card on the bar: wheel picks an option, START
confirms, BACK hands the question back to the screen, OK shows the next
waiting session. Multiple-choice questions (AskUserQuestion) do not fit the
display: the bar only says "see terminal" for those. The front shows tool, chosen option and the scrolling detail;
the back OLED shows the project, the full question and the option list.

Two ways to answer, chosen per session:
- Claude runs in iTerm2 or tmux: the hook returns at once, so the dialog shows
  in the terminal immediately. The bar mirrors the dialog (it reads the real
  option labels off the iTerm2 screen) and a button press is typed into that
  terminal as arrow keys and Enter.
- Anywhere else (desktop app, VS Code): the hook is held for --window seconds
  and the bar answers it directly: allow, allow and remember, deny, or
  accept-edits for plans. After the window the
  normal dialog appears and the card stays as "on screen" until the session moves on.

Setup:
1. `python app.py --print-hooks` and merge the output into ~/.claude/settings.json
   (http hooks post straight to this app; when the app is down Claude Code
   simply shows its own dialog).
2. Run the app through busybar-manager with a priority above your idle apps,
   or by hand: `python app.py --host 10.0.4.20`.
3. First key press into iTerm2 triggers macOS's one-time "control iTerm2" consent
   for the process that runs the app (e.g. BusyBar Manager).

Standard library only. Needs macOS `osascript` for the iTerm2 path and `tmux` for tmux panes.
"""

import argparse
import json
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "claude-approve"
FRONT_W, FRONT_H = 72, 16
BACK_W, BACK_H = 160, 80

WHITE = "#FFFFFFFF"
GRAY = "#8A8A8AFF"
DIM = "#3A3A3AFF"
ORANGE = "#FF8A1EFF"
GREEN = "#38D45AFF"
RED = "#FF3C3CFF"
CLEAR = "#00000000"
BLACK = "#000000FF"
Y_GLYPH = "#FFC83CFF"
BLUE = "#3C8CFFFF"
PURPLE = "#B06CFFFF"
KIND_COLOR = {"permission": ORANGE, "plan": BLUE, "question": PURPLE, "notice": GRAY, "flash": GREEN}
TOOL_GLYPH = {"Bash": "$", "Edit": "E", "Write": "W", "MultiEdit": "E", "NotebookEdit": "E", "Read": "R",
              "WebFetch": "@", "WebSearch": "@", "ExitPlanMode": "P", "AskUserQuestion": "?"}
TOOL_WORD = {"Bash": "RUN", "Edit": "EDIT", "Write": "WRITE", "MultiEdit": "EDIT", "NotebookEdit": "EDIT",
             "Read": "READ", "WebFetch": "FETCH", "WebSearch": "SEARCH", "ExitPlanMode": "PLAN",
             "AskUserQuestion": "ASK"}

SCROLL_RATE = 1500          # pixel columns per minute
SCROLL_START_DELAY = 800    # ms
SCROLL_REPEAT_DELAY = 1500  # ms
ELEMENT_TIMEOUT = 20        # s; a dead app clears itself

BUTTONS = {0: "ok", 1: "back", 2: "start"}

HOOK_EVENTS_FAST = ["Notification", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionEnd"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20", help="BUSY Bar ip[:port]; emulator: 127.0.0.1:8080")
    p.add_argument("--listen", default="127.0.0.1:8324", help="where Claude Code's http hooks post to")
    p.add_argument("--window", type=int, default=30, help="seconds the bar may answer before the dialog shows (sessions without a terminal)")
    p.add_argument("--priority", type=int, default=60, help="draw priority 1-100 while a card is showing")
    p.add_argument("--idle", action="store_true", help="also show a card when a session has been idle for a minute")
    p.add_argument("--dry-run", action="store_true", help="log key presses instead of typing them into the terminal")
    p.add_argument("--test", action="store_true", help="draw one demo card and exit")
    p.add_argument("--print-hooks", action="store_true", help="print the ~/.claude/settings.json hooks snippet and exit")
    return p.parse_args()


ARGS = parse_args()
sys.stdout.reconfigure(line_buffering=True)
BASE = "http://" + ARGS.host.replace("http://", "").rstrip("/")


# --------------------------------------------------------------------------
# BUSY Bar HTTP API
# --------------------------------------------------------------------------

def _request(method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def draw(elements, led=None):
    body = {"application_name": APP, "elements": elements, "priority": ARGS.priority}
    if led:
        body["led_notification_color"] = led
    for attempt in (1, 2):
        try:
            _request("POST", "/api/display/draw", body)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409:  # a higher-priority app owns the display
                return False
            if exc.code in (502, 503, 504) and attempt == 1:
                time.sleep(0.5)
                continue
            print(f"{APP}: draw failed: HTTP {exc.code} {exc.read()[:120]!r}")
            return False
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            if attempt == 1:
                time.sleep(0.5)
                continue
            print(f"{APP}: bar unreachable: {exc}")
            return False
    return False


def clear_display():
    try:
        _request("DELETE", "/api/display/draw?" + urllib.parse.urlencode({"application_name": APP}))
    except (urllib.error.URLError, socket.timeout, OSError):
        pass


# --------------------------------------------------------------------------
# Button and wheel input over /api/status/ws (protobuf, decoded by hand)
#   State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1 | encoder_event=3
# --------------------------------------------------------------------------

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


def _iter_fields(buf):
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        fld, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield fld, wire, value
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            end = pos + length
            if end > len(buf):
                return
            yield fld, wire, buf[pos:end]
            pos = end
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > len(buf):
                return
            yield fld, wire, buf[pos:pos + width]
            pos += width
        else:
            return


def _zigzag32(v):
    return (v >> 1) ^ -(v & 1)


def input_events(frame):
    """Yield ("button", name) for presses and ("wheel", delta) for wheel steps."""
    for fld, wire, update in _iter_fields(frame):
        if fld != 2 or wire != 2:
            continue
        for ufld, uwire, payload in _iter_fields(update):
            if ufld != 11 or uwire != 2:
                continue
            for efld, ewire, event in _iter_fields(payload):
                if ewire != 2:
                    continue
                if efld == 1:
                    button, action = 0, 0
                    for bfld, bwire, value in _iter_fields(event):
                        if bwire == 0 and bfld == 1:
                            button = int(value)
                        elif bwire == 0 and bfld == 2:
                            action = int(value)
                    if action == 0 and button in BUTTONS:
                        yield ("button", BUTTONS[button])
                elif efld == 3:
                    raw = 0
                    for dfld, dwire, value in _iter_fields(event):
                        if dwire == 0 and dfld == 1:
                            raw = int(value)
                    delta = _zigzag32(raw)
                    if delta:
                        yield ("wheel", delta)


class ButtonListener(threading.Thread):
    """Minimal RFC 6455 client for /api/status/ws; reconnects with backoff."""

    def __init__(self, host):
        super().__init__(daemon=True)
        self.host = host.replace("http://", "")
        self.events = queue.Queue()
        self._complained = False

    def run(self):
        backoff = 1.0
        while True:
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 - keep the app alive without buttons
                if not self._complained:
                    print(f"{APP}: no button stream yet ({exc}); retrying")
                    self._complained = True
                backoff = min(30.0, backoff * 2)
            time.sleep(backoff)

    def _session(self):
        host, _, port = self.host.partition(":")
        sock = socket.create_connection((host, int(port or 80)), timeout=8)
        try:
            sock.settimeout(60)
            key = b64encode(os.urandom(16)).decode()
            sock.sendall((
                "GET /api/status/ws HTTP/1.1\r\n"
                f"Host: {self.host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            stream = sock.makefile("rb")
            status = stream.readline()
            if b"101" not in status:
                raise OSError("handshake refused: " + status.decode(errors="replace").strip())
            while stream.readline() not in (b"\r\n", b"\n", b""):
                pass
            self._send(sock, 0x1, json.dumps({"enable": True}).encode())
            print(f"{APP}: buttons connected")
            self._complained = False
            payload = b""
            while True:
                fin, opcode, chunk = self._read_frame(stream)
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    self._send(sock, 0xA, chunk)
                    continue
                if opcode == 0xA:
                    continue
                payload = chunk if opcode else payload + chunk
                if not fin:
                    continue
                if opcode == 0x2:
                    for ev in input_events(payload):
                        self.events.put(ev)
                payload = b""
        finally:
            sock.close()

    @staticmethod
    def _read_frame(stream):
        head = stream.read(2)
        if len(head) < 2:
            raise OSError("stream closed")
        fin, opcode = head[0] & 0x80, head[0] & 0x0F
        masked, length = head[1] & 0x80, head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", stream.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", stream.read(8))[0]
        mask = stream.read(4) if masked else b""
        body = stream.read(length) if length else b""
        if len(body) < length:
            raise OSError("stream closed mid-frame")
        if mask:
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        return fin, opcode, body

    @staticmethod
    def _send(sock, opcode, data):
        header = bytearray([0x80 | opcode])
        mask = os.urandom(4)
        size = len(data)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", size)
        header += mask
        header += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        sock.sendall(bytes(header))


# --------------------------------------------------------------------------
# Terminal access: iTerm2 (AppleScript) and tmux
# --------------------------------------------------------------------------

KEYS_APPLESCRIPT = {
    "down": '(character id 27) & "[B"',
    "up": '(character id 27) & "[A"',
    "enter": "(character id 13)",
    "esc": "(character id 27)",
}
KEYS_TMUX = {"down": "Down", "up": "Up", "enter": "Enter", "esc": "Escape"}


def _iterm_script(session_uuid, body):
    """AppleScript that finds the iTerm2 session by UUID and runs `body` with `s` bound to it."""
    return f'''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if unique ID of s is "{session_uuid}" then
          {body}
        end if
      end repeat
    end repeat
  end repeat
end tell
return "missing"'''


def _osascript(script, timeout=6):
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise OSError(out.stderr.strip() or "osascript failed")
    return out.stdout


def iterm_uuid(session_id):
    # ITERM_SESSION_ID looks like "w0t0p0:UUID"; iTerm2 wants the UUID.
    return session_id.rpartition(":")[2].strip()


def iterm_read_screen(session_uuid):
    try:
        return _osascript(_iterm_script(session_uuid, "return contents of s"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{APP}: cannot read iTerm2 screen: {exc}")
        return ""


def read_screen(card):
    if card.surface == "iterm":
        return iterm_read_screen(card.iterm)
    if card.surface == "tmux":
        try:
            return subprocess.run(["tmux", "capture-pane", "-p", "-t", card.tmux], capture_output=True,
                                  text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"{APP}: cannot read tmux pane: {exc}")
    return ""


def send_keys(card, keys):
    """Type keys ("down", "enter", ...) into the card's terminal, one per call."""
    if ARGS.dry_run:
        print(f"{APP}: dry-run keys {keys} -> {card.surface} {card.iterm or card.tmux}")
        return True
    try:
        for key in keys:
            if card.surface == "iterm":
                _osascript(_iterm_script(card.iterm, f"tell s to write text {KEYS_APPLESCRIPT[key]} newline NO"))
            elif card.surface == "tmux":
                subprocess.run(["tmux", "send-keys", "-t", card.tmux, KEYS_TMUX[key]], check=True, timeout=5)
            time.sleep(0.12)
        print(f"{APP}: typed {'+'.join(keys)} into {card.surface} for {card.title}")
        return True
    except (OSError, subprocess.SubprocessError, KeyError) as exc:
        print(f"{APP}: typing into the terminal failed: {exc}")
        return False


OPTION_LINE = re.compile(r"^\s*(❯\s*)?(\d+)\.\s+(.+?)\s*$")


def parse_dialog_options(screen):
    """Labels of the dialog's numbered options ("❯ 1. Yes", "2. No", ...).

    Only a block that carries the ❯ cursor counts, so numbered lists in the
    transcript above the dialog are ignored; the last such block wins."""
    blocks, current, cursor = [], [], False

    def close():
        nonlocal current, cursor
        if len(current) >= 2 and cursor:
            blocks.append(current)
        current, cursor = [], False

    for line in screen.splitlines():
        m = OPTION_LINE.match(line)
        if m and int(m.group(2)) == 1:
            close()
            current = [m.group(3)]
            cursor = bool(m.group(1))
        elif m and current and int(m.group(2)) == len(current) + 1:
            current.append(m.group(3))
            cursor = cursor or bool(m.group(1))
        elif current and line.strip():
            close()
    close()
    return blocks[-1] if blocks else []


# --------------------------------------------------------------------------
# Cards: one per open question
# --------------------------------------------------------------------------

@dataclass
class Option:
    label: str
    decision: dict = None       # hook path: the PermissionRequest decision
    index: int = 0              # inject path: position in the on-screen list
    esc: bool = False           # inject path: answer with Escape instead
    value: str = None           # AskUserQuestion: the answer label


@dataclass
class Card:
    session_id: str
    tool_use_id: str
    kind: str                   # permission | plan | question | notice | flash
    title: str
    detail: str
    project: str
    surface: str                # iterm | tmux | hold | none
    label: str = ""             # task title from the terminal, else Claude's own description
    glyph: str = "?"
    word: str = "RUN"           # action word on the front: RUN, EDIT, PLAN, ASK, MCP
    iterm: str = ""
    tmux: str = ""
    options: list = field(default_factory=list)
    selected: int = 0
    deadline: float = None
    state: str = "pending"      # pending | on_screen
    decision: dict = None
    event: threading.Event = field(default_factory=threading.Event)
    created: float = field(default_factory=time.monotonic)
    tool_input: dict = None

    verified: bool = True        # terminal sessions: dialog seen on screen

    @property
    def answerable(self):
        return self.state == "pending" and self.verified and bool(self.options)


class Store:
    def __init__(self):
        self.cards = []
        self.lock = threading.Lock()
        self.dirty = threading.Event()
        self.new_card = threading.Event()
        self.flash = None           # (Card, until): confirmation shown after an answer

    def add(self, card):
        with self.lock:
            self.cards = [c for c in self.cards if not (c.session_id == card.session_id and c.kind == "notice")]
            self.cards.append(card)
        self.dirty.set()
        self.new_card.set()

    def remove(self, card):
        with self.lock:
            if card in self.cards:
                self.cards.remove(card)
        self.dirty.set()

    def current(self):
        with self.lock:
            return self.cards[0] if self.cards else None

    def rotate(self):
        with self.lock:
            if len(self.cards) > 1:
                self.cards.append(self.cards.pop(0))
        self.dirty.set()

    def for_session_all(self):
        with self.lock:
            return list(self.cards)

    def for_session(self, session_id):
        with self.lock:
            return [c for c in self.cards if c.session_id == session_id]

    def resolve_session(self, session_id, keep_tool_use=None):
        """A session moved on: drop its cards (except a held one that is still pending)."""
        for c in self.for_session(session_id):
            if c.tool_use_id and c.tool_use_id == keep_tool_use:
                continue
            if c.surface == "hold" and c.state == "pending":
                continue
            self.remove(c)

    def show_flash(self, card, label):
        done = Card(session_id=card.session_id, tool_use_id="", kind="flash", title=card.title,
                    detail=label, label=card.label, glyph=card.glyph, word=card.word, project=card.project,
                    surface="none")
        self.flash = (done, time.monotonic() + 1.5)
        self.dirty.set()

    def snapshot(self):
        with self.lock:
            return [{"session": c.session_id[:8], "kind": c.kind, "title": c.title, "detail": c.detail[:80],
                     "surface": c.surface, "state": c.state, "options": [o.label for o in c.options],
                     "selected": c.selected} for c in self.cards]


STORE = Store()


# --------------------------------------------------------------------------
# Building cards from hook payloads
# --------------------------------------------------------------------------

def short_tool(name):
    if name.startswith("mcp__"):
        parts = name.split("__")
        return (parts[-1] if len(parts) < 3 else f"{parts[1]}:{parts[2]}")[:18]
    return name


def describe_input(tool, inp):
    if not isinstance(inp, dict):
        return str(inp)
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(key):
            value = str(inp[key])
            return os.path.basename(value) if key.endswith("_path") and tool in ("Edit", "Write", "Read") else value
    return json.dumps(inp, ensure_ascii=False)[:200]


TITLE_TRAILER = re.compile(r"[\s\-\u2013\u2014|\u00b7(\[]*claude[)\]]?\s*$", re.I)


def clean_title(name):
    """Claude Code titles the terminal "<glyph> <task> (claude)"; keep the task."""
    name = TITLE_TRAILER.sub("", (name or "").strip())
    while name and not name[0].isalnum():
        name = name[1:]
    return name.strip()[:60]


def terminal_title(card):
    try:
        if card.surface == "iterm":
            return clean_title(_osascript(_iterm_script(card.iterm, "return name of s")))
        if card.surface == "tmux":
            out = subprocess.run(["tmux", "display-message", "-p", "-t", card.tmux, "#{pane_title}"],
                                 capture_output=True, text=True, timeout=5).stdout
            return clean_title(out)
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def own_label(tool, inp):
    """Row 1 when no terminal title is available: Claude's description, else the file or tool."""
    if isinstance(inp, dict):
        if inp.get("description"):
            return str(inp["description"])
        for key in ("file_path", "notebook_path"):
            if inp.get(key):
                return f"{short_tool(tool)} {os.path.basename(str(inp[key]))}"
    return short_tool(tool)


def permission_options(payload, surface):
    suggestions = payload.get("permission_suggestions") or []
    if surface == "hold":
        opts = [Option("Allow", {"behavior": "allow"})]
        if suggestions:
            opts.append(Option("Allow always", {"behavior": "allow", "updatedPermissions": suggestions}))
        opts.append(Option("Deny", {"behavior": "deny", "message": "Denied from the BUSY Bar"}))
        return opts
    opts = [Option("Allow", index=0)]
    if suggestions:
        opts.append(Option("Allow always", index=1))
    opts.append(Option("Deny", index=len(opts), esc=(surface == "tmux")))
    return opts


def plan_options(surface):
    if surface == "hold":
        return [
            Option("Auto-accept edits", {"behavior": "allow", "updatedPermissions": [
                {"type": "setMode", "mode": "acceptEdits", "destination": "session"}]}),
            Option("Approve", {"behavior": "allow"}),
            Option("Keep planning", {"behavior": "deny", "message": "Keep planning (BUSY Bar)"}),
        ]
    return [Option("Auto-accept edits", index=0), Option("Approve", index=1), Option("Tell Claude", index=2)]


def card_from_permission(payload, headers):
    iterm = iterm_uuid(headers.get("X-Iterm-Session", ""))
    tmux = headers.get("X-Tmux-Pane", "").strip()
    remote = headers.get("X-Claude-Remote", "").strip().lower() == "true"
    if remote:
        surface = "none"
    elif iterm:
        surface = "iterm"
    elif tmux:
        surface = "tmux"
    else:
        surface = "hold"
    tool = payload.get("tool_name", "?")
    inp = payload.get("tool_input") or {}
    project = os.path.basename(payload.get("cwd") or "") or "claude"
    card = Card(session_id=payload.get("session_id", ""), tool_use_id=payload.get("tool_use_id", ""),
                kind="permission", title=short_tool(tool), detail=describe_input(tool, inp),
                project=project, surface=surface, iterm=iterm, tmux=tmux, tool_input=inp)
    card.glyph = TOOL_GLYPH.get(tool, "M" if tool.startswith("mcp__") else tool[:1].upper() or "?")
    card.word = TOOL_WORD.get(tool, "MCP" if tool.startswith("mcp__") else tool[:6].upper())
    card.label = own_label(tool, inp)
    if tool == "ExitPlanMode":
        card.kind = "plan"
        card.title = "Plan"
        card.label = "Plan ready"
        first = next((ln.strip("# ").strip() for ln in str(inp.get("plan", "")).splitlines() if ln.strip()), "")
        card.detail = first or "Plan ready"
        card.options = plan_options(surface)
    elif tool == "AskUserQuestion":
        # Multiple choice does not fit 72x16: point at the screen, never hold the dialog back
        card.kind = "question"
        q = (inp.get("questions") or [{}])[0]
        card.title = (q.get("header") or "Question")[:12]
        card.label = q.get("question", "Question")
        card.detail = "see terminal"
        card.state = "on_screen"
    else:
        card.options = permission_options(payload, surface)
    if card.kind == "question":
        return card
    if surface == "hold":
        card.deadline = time.monotonic() + ARGS.window
    else:
        card.verified = False
    return card


def verify_on_screen(card):
    """Terminal sessions: offer buttons only while the dialog really is on screen.

    Claude Code fires PermissionRequest before it knows whether it will ask at
    all (auto mode and allow rules resolve it silently), and the user may answer
    in the terminal. So the card appears once the numbered dialog is visible,
    takes the real option labels from it, and goes away when the dialog does."""
    title = terminal_title(card)
    if title:
        card.label = title
        STORE.dirty.set()
    misses = 0
    for attempt in range(600):
        time.sleep(0.7 if not card.verified else 1.0)
        if card not in STORE.cards:
            return
        labels = parse_dialog_options(read_screen(card))
        if len(labels) >= 2:
            misses = 0
            if not card.verified:
                card.options = [Option(lbl[:40], index=i) for i, lbl in enumerate(labels)]
                card.selected = min(card.selected, len(card.options) - 1)
                card.verified = True
                STORE.dirty.set()
            continue
        misses += 1
        if (not card.verified and attempt >= 5) or (card.verified and misses >= 2):
            print(f"{APP}: dialog gone for {card.title}, dropping the card")
            STORE.remove(card)
            return


# --------------------------------------------------------------------------
# Acting on a card
# --------------------------------------------------------------------------

def confirm(card):
    if not card.answerable:
        return
    option = card.options[card.selected]
    if card.surface == "hold":
        card.decision = option.decision
        card.event.set()
        STORE.remove(card)
        STORE.show_flash(card, option.label)
        return
    send_keys(card, ["esc"] if option.esc else ["down"] * option.index + ["enter"])
    STORE.remove(card)
    STORE.show_flash(card, option.label)


def dismiss(card):
    """BACK: hand the question back to the screen."""
    if card.surface == "hold" and card.state == "pending":
        card.state = "on_screen"
        card.options = []
        card.event.set()
        STORE.dirty.set()
        return
    STORE.remove(card)


# --------------------------------------------------------------------------
# Hook endpoint
# --------------------------------------------------------------------------

def hook_response(decision):
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


class HookHandler(BaseHTTPRequestHandler):
    server_version = "claude-approve/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/state"):
            return self._send(200, {"cards": STORE.snapshot()})
        return self._send(200, {"ok": True, "app": APP})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        event = payload.get("hook_event_name", "")
        session = payload.get("session_id", "")
        if event == "PermissionRequest":
            return self._permission(payload)
        if event == "Notification":
            self._notification(payload)
        elif event == "PreToolUse":
            STORE.resolve_session(session, keep_tool_use=payload.get("tool_use_id"))
        elif event in ("PostToolUse", "UserPromptSubmit", "Stop", "SessionEnd"):
            STORE.resolve_session(session)
        return self._send(200)

    def _permission(self, payload):
        card = card_from_permission(payload, self.headers)
        if card.surface == "none":
            return self._send(200)
        STORE.add(card)
        print(f"{APP}: {card.project} · {card.title} · {card.detail[:60]} ({card.surface})")
        if card.kind == "question":
            return self._send(200)
        if card.surface != "hold":
            threading.Thread(target=verify_on_screen, args=(card,), daemon=True).start()
            return self._send(200)
        card.event.wait(ARGS.window)
        if card.decision is not None:
            print(f"{APP}: answered {card.title}: {card.decision.get('behavior')}")
            return self._send(200, hook_response(card.decision))
        card.state = "on_screen"
        card.options = []
        STORE.dirty.set()
        return self._send(200)

    def _notification(self, payload):
        kind = payload.get("notification_type", "")
        wanted = {"permission_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"}
        if ARGS.idle:
            wanted.add("idle_prompt")
        if kind not in wanted:
            return
        session = payload.get("session_id", "")
        if STORE.for_session(session):
            return
        project = os.path.basename(payload.get("cwd") or "") or "claude"
        STORE.add(Card(session_id=session, tool_use_id="", kind="notice", title="Claude",
                       detail=payload.get("message") or kind.replace("_", " "), project=project,
                       surface="none", state="on_screen"))


def serve_hooks(listen):
    host, _, port = listen.partition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port or 8324)), HookHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _text(eid, text, x, y, font, color, display="front", align="top_left", **extra):
    el = {"id": eid, "type": "text", "text": text, "x": x, "y": y, "font": font, "color": color,
          "align": align, "display": display, "timeout": ELEMENT_TIMEOUT}
    el.update(extra)
    return el


def _rect(eid, x, y, w, h, color, display="front", border=False):
    """Filled block, or with border=True a 1 px outline in `color` and nothing inside.

    `fill` is the firmware's enum ("none" | "solid" | ...), not a boolean."""
    return {"id": eid, "type": "rectangle", "x": x, "y": y, "width": max(1, w), "height": max(1, h),
            "radius": 0, "fill": "none" if border else "solid", "fill_colors": [color],
            "border_width": 1 if border else 0, "border_color": color,
            "display": display, "timeout": ELEMENT_TIMEOUT}


def _scrolling(eid, text, x, y, font, color, width, display="front"):
    return _text(eid, text, x, y, font, color, display, width=width, scroll_rate=SCROLL_RATE,
                 scroll_start_delay=SCROLL_START_DELAY, scroll_repeat_delay=SCROLL_REPEAT_DELAY)


def _small_width(txt):
    # rough advance of the bar's small font: ~4 px per glyph, narrow ones less
    return sum(2 if ch in " .,:;'il!|" else 4 for ch in txt)


def front_elements(card, status, choosing, on_screen, accent, remaining, label, badge):
    """Front 72x16, after the Flipper demo: options side by side with the chosen one boxed
    and an arrow, below it the action word and the command, a countdown line at the bottom."""
    word = "DONE" if card.kind == "flash" else card.word
    word_w = _small_width(word) + 6
    detail = label if card.kind == "flash" else (card.detail or label)
    options = card.options if choosing else []
    nxt = options[(card.selected + 1) % len(options)].label if len(options) > 1 else ""
    if card.kind == "question":
        status = label
    right_edge = 72 - (7 if badge else 0)
    if choosing or card.kind == "flash":
        box_w = min(10 + _small_width(status) + 2, 46 if nxt else right_edge)
        box_color = GREEN if card.kind == "flash" else accent
    else:
        box_w, box_color = 1, CLEAR
    text_color = WHITE if choosing or card.kind == "flash" else GRAY
    arrow = ">" if choosing else ("+" if card.kind == "flash" else "")
    return [
        _rect("f_box", 0, 0, box_w, 9, CLEAR if box_w == 1 else box_color, border=True),
        _text("f_arrow", arrow, 2, 0, "small", box_color if arrow else CLEAR),
        _scrolling("f_opt0", status, 8 if arrow else 0, 0, "small", text_color, max(4, box_w - 10) if arrow else right_edge),
        _scrolling("f_opt1", nxt, box_w + 5, 0, "small", WHITE, max(4, right_edge - box_w - 5)),
        _text("f_badge", badge, 71, 0, "tiny", GRAY, align="top_right"),
        _text("f_word", word, 0, 7, "small", Y_GLYPH),
        _scrolling("f_detail", detail, word_w, 7, "small", WHITE, 72 - word_w),
        _rect("f_timer", 0, 15, int(72 * remaining) if remaining is not None else 1, 1,
              accent if remaining is not None else CLEAR),
    ]


def build_frame(card, count, now):
    """Every element id is sent every frame (hidden ones transparent): the firmware never auto-releases ids.

    Front 72x16: the options side by side with the chosen one boxed, the action word and
    the command below, a thin countdown line along the bottom edge.
    Back 160x80: accent header, title, the actual command, the option list."""
    accent = KIND_COLOR.get(card.kind, ORANGE)
    on_screen = card.state == "on_screen" or (not card.options and card.kind != "flash")
    choosing = card.answerable
    if card.kind == "flash":
        status = card.detail
    elif on_screen:
        status = "on screen"
    elif not card.verified:
        status = "..."
    else:
        status = card.options[card.selected].label
    remaining = None
    if card.deadline and card.state == "pending":
        remaining = max(0.0, card.deadline - now) / max(1, ARGS.window)

    label = card.label or card.detail
    badge = str(count) if count > 1 else ""
    front = front_elements(card, status, choosing, on_screen, accent, remaining, label, badge)

    back = [
        _rect("b_head", 0, 0, 160, 12, accent, "back"),
        _text("b_tool", f"{card.glyph} {card.title.upper()}", 3, 1, "small", BLACK, "back"),
        _text("b_proj", card.project[:22], 157, 1, "small", BLACK, "back", align="top_right"),
        _scrolling("b_label", label, 2, 15, "normal", WHITE, 156, "back"),
        _scrolling("b_detail", card.detail if card.kind != "flash" else "", 2, 28, "small", GRAY, 156, "back"),
    ]
    rows = card.options[:4] if choosing else []
    for i in range(4):
        y = 40 + i * 9
        if i < len(rows):
            sel = i == card.selected
            back.append(_rect(f"b_opt{i}_bg", 2, y, 110, 8, accent if sel else CLEAR, "back"))
            back.append(_text(f"b_opt{i}", rows[i].label[:30], 5, y - 1, "small", BLACK if sel else WHITE, "back"))
        else:
            back.append(_rect(f"b_opt{i}_bg", 2, y, 1, 1, CLEAR, "back"))
            back.append(_text(f"b_opt{i}", "", 5, y, "small", CLEAR, "back"))
    if card.kind == "flash":
        hint = "sent to Claude"
    elif on_screen:
        hint = "waiting for your answer on screen"
    else:
        hint = "wheel picks - START confirms - BACK to screen" + (" - OK next" if count > 1 else "")
    back.append(_text("b_hint", hint, 2, 71, "tiny", GRAY, "back"))
    back.append(_rect("b_bar", 0, 78, int(BACK_W * remaining) if remaining is not None else 1, 2,
                      accent if remaining is not None else CLEAR, "back"))
    return front + back


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def hooks_snippet(listen):
    http = {"type": "http", "url": f"http://{listen}/hook"}
    env = ["ITERM_SESSION_ID", "TMUX_PANE", "TERM_PROGRAM", "CLAUDE_CODE_REMOTE"]
    perm = dict(http, timeout=ARGS.window + 15,
                headers={"X-Iterm-Session": "$ITERM_SESSION_ID", "X-Tmux-Pane": "$TMUX_PANE",
                         "X-Term-Program": "$TERM_PROGRAM", "X-Claude-Remote": "$CLAUDE_CODE_REMOTE"},
                allowedEnvVars=env)
    fast = dict(http, timeout=3)
    hooks = {"PermissionRequest": [{"hooks": [perm]}]}
    for ev in HOOK_EVENTS_FAST:
        hooks[ev] = [{"hooks": [fast]}]
    return {"hooks": hooks}


def demo_card():
    card = Card(session_id="demo", tool_use_id="demo", kind="permission", title="Bash",
                detail="git push origin main", label="Write release notes", glyph="$",
                project="busybar-apps", surface="hold")
    card.options = permission_options({"permission_suggestions": [{"type": "addRules"}]}, "hold")
    card.deadline = time.monotonic() + ARGS.window * 0.7
    return card


def main():
    if ARGS.print_hooks:
        print(json.dumps(hooks_snippet(ARGS.listen), indent=2))
        return 0

    if ARGS.test:
        draw(build_frame(demo_card(), 2, time.monotonic()), led=ORANGE)
        time.sleep(1.0)
        clear_display()
        return 0

    clear_display()
    serve_hooks(ARGS.listen)
    buttons = ButtonListener(ARGS.host)
    buttons.start()
    print(f"{APP} → {BASE}  hooks on http://{ARGS.listen}/hook  (Ctrl-C to stop)")

    showing = False
    last_draw = 0.0
    try:
        while True:
            try:
                kind, value = buttons.events.get(timeout=0.25)
            except queue.Empty:
                kind = None
            card = STORE.current()
            if kind and card:
                if kind == "wheel" and card.answerable:
                    card.selected = (card.selected + (1 if value > 0 else -1)) % len(card.options)
                    STORE.dirty.set()
                elif kind == "button" and value == "start":
                    confirm(card)
                elif kind == "button" and value == "back":
                    dismiss(card)
                elif kind == "button" and value == "ok":
                    STORE.rotate()
                card = STORE.current()

            now = time.monotonic()
            for stale in [c for c in STORE.for_session_all() if c.state == "on_screen" and now - c.created > 900]:
                STORE.remove(stale)
                card = STORE.current()
            if card is None and STORE.flash and now < STORE.flash[1]:
                if STORE.dirty.is_set():
                    STORE.dirty.clear()
                    if draw(build_frame(STORE.flash[0], 1, now)):
                        showing = True
                continue
            if card is None:
                STORE.flash = None
                if showing:
                    clear_display()
                    showing = False
                continue
            led = ORANGE if STORE.new_card.is_set() else None
            countdown_tick = card.deadline is not None and now - last_draw >= 1.0
            if STORE.dirty.is_set() or countdown_tick or now - last_draw >= 5.0:
                STORE.dirty.clear()
                STORE.new_card.clear()
                with STORE.lock:
                    count = len(STORE.cards)
                if draw(build_frame(card, count, now), led=led):
                    showing = True
                else:
                    time.sleep(2.0)  # bar busy or unreachable: do not hammer it
                last_draw = now
    except KeyboardInterrupt:
        pass
    finally:
        clear_display()
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
