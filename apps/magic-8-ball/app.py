#!/usr/bin/env python3
"""Magic 8-Ball for BUSY Bar.

A black 8-ball waits at one edge of the 72x16 display. Press the BUSY Bar's
START/top button and the ball rolls to the opposite side while one of the 20
classic Magic 8-Ball answers appears. The answer remains visible for 5 seconds,
then fades out over 1 second. Each press reverses the direction.

Input is read directly from BUSY Bar's /api/status/ws WebSocket. No busylib.
The listener reconnects automatically after connection loss.

Examples:
    python3 app.py
    python3 app.py --host 127.0.0.1:8080
    python3 app.py --language it
    python3 app.py --language de --fps 15
    python3 app.py --auto-roll 30

Dependency:
    pip install websockets
"""

import argparse
import asyncio
import json
import math
import queue
import random
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib


APP = "magic-8-ball"
W, H = 72, 16
BALL_SIZE = 15
LEFT_X = 1
RIGHT_X = W - BALL_SIZE - 1
BALL_Y = 0
ROLL_SECONDS = 1.15
ANSWER_SECONDS = 5.0
FADE_SECONDS = 1.0
DEFAULT_FPS = 12
IDLE_HINT_DELAY = 2.0
IDLE_HINT_PERIOD = 2.4

BLACK = (0, 0, 0)
BALL_DARK = (3, 5, 9)
BALL_MID = (12, 16, 24)
BALL_EDGE = (72, 82, 108)
BALL_EDGE_BRIGHT = (125, 138, 172)
WHITE = (245, 245, 250)
DIM_WHITE = (180, 185, 200)
BLUE = (40, 125, 255)
PURPLE = (166, 72, 255)
HINT_COLOR = (70, 100, 145)

# 3x5 pixel font. Glyph rows are strings for readability; 1 = lit pixel.
FONT = {
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "011", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "110", "101", "010"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("010", "101", "010", "101", "010"),
    "9": ("010", "101", "011", "001", "110"),
    "-": ("000", "000", "111", "000", "000"),
    "?": ("110", "001", "010", "000", "010"),
    "!": ("010", "010", "010", "000", "010"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    "'": ("010", "010", "000", "000", "000"),
    ":": ("000", "010", "000", "010", "000"),
    "/": ("001", "001", "010", "100", "100"),
    " ": ("000", "000", "000", "000", "000"),
}

# The classic 20-answer set, translated for this app.
IDLE_HINTS = {
    "en": "CLICK TO ROLL",
    "fr": "CLIC POUR JOUER",
    "de": "KLICK ZUM WURF",
    "es": "PULSA Y GIRA",
    "it": "PREMI E GIRA",
    "nl": "KLIK EN ROL",
}

ANSWERS = {
    "en": [
        "As I see it, yes", "It is certain", "It is decidedly so", "Most likely",
        "Outlook good", "Signs point to yes", "Without a doubt", "Yes",
        "Yes - definitely", "You may rely on it", "Reply hazy, try again",
        "Ask again later", "Better not tell you now", "Cannot predict now",
        "Concentrate and ask again", "Don't count on it", "My reply is no",
        "My sources say no", "Outlook not so good", "Very doubtful",
    ],
    "it": [
        "Per quanto posso vedere, si", "E certo", "E decisamente cosi",
        "Molto probabilmente", "Le prospettive sono buone", "I segni indicano di si",
        "Senza alcun dubbio", "Si", "Si, senza dubbio", "Ci puoi contare",
        "E difficile rispondere, prova di nuovo", "Rifai la domanda piu tardi",
        "Meglio non risponderti adesso", "Non posso predirlo ora",
        "Concentrati e rifai la domanda", "Non ci contare", "La mia risposta e no",
        "Le mie fonti dicono di no", "Le prospettive non sono buone", "Molto incerto",
    ],
    "fr": [
        "A mon avis, oui", "C'est certain", "C'est decidement le cas",
        "Tres probablement", "Les perspectives sont bonnes", "Les signes disent oui",
        "Sans aucun doute", "Oui", "Oui - definitivement", "Vous pouvez compter dessus",
        "Reponse floue, reessayez", "Redemandez plus tard", "Mieux vaut ne pas vous le dire",
        "Impossible de predire maintenant", "Concentrez-vous et redemandez",
        "N'y comptez pas", "Ma reponse est non", "Mes sources disent non",
        "Les perspectives sont mauvaises", "Tres douteux",
    ],
    "de": [
        "So wie ich es sehe, ja", "Es ist sicher", "Ganz entschieden ja",
        "Sehr wahrscheinlich", "Die Aussichten sind gut", "Die Zeichen stehen auf ja",
        "Ohne jeden Zweifel", "Ja", "Ja - definitiv", "Darauf kannst du dich verlassen",
        "Antwort unklar, frag erneut", "Frag spaeter noch einmal", "Besser jetzt nicht sagen",
        "Jetzt nicht vorhersagbar", "Konzentriere dich und frag erneut", "Verlass dich nicht darauf",
        "Meine Antwort ist nein", "Meine Quellen sagen nein", "Die Aussichten sind nicht gut",
        "Sehr zweifelhaft",
    ],
    "es": [
        "Tal como lo veo, si", "Es seguro", "Definitivamente es asi",
        "Muy probablemente", "Buenas perspectivas", "Las senales dicen que si",
        "Sin ninguna duda", "Si", "Si - definitivamente", "Puedes confiar en ello",
        "Respuesta confusa, intenta otra vez", "Pregunta de nuevo mas tarde",
        "Mejor no decirtelo ahora", "No puedo predecirlo ahora",
        "Concentrate y pregunta de nuevo", "No cuentes con ello", "Mi respuesta es no",
        "Mis fuentes dicen que no", "Las perspectivas no son buenas", "Muy dudoso",
    ],
    "nl": [
        "Zoals ik het zie, ja", "Het is zeker", "Zonder twijfel ja",
        "Waarschijnlijk wel", "De vooruitzichten zijn goed", "Alles wijst op ja",
        "Zonder enige twijfel", "Ja", "Ja - absoluut", "Je kunt erop vertrouwen",
        "Antwoord onduidelijk, probeer opnieuw", "Vraag het later opnieuw",
        "Dat kan ik nu beter niet zeggen", "Kan het nu niet voorspellen",
        "Concentreer je en vraag opnieuw", "Reken er niet op", "Mijn antwoord is nee",
        "Mijn bronnen zeggen nee", "De vooruitzichten zijn niet zo goed", "Zeer twijfelachtig",
    ],
}


def parse_args():
    p = argparse.ArgumentParser(description="Magic 8-Ball for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20", help="BUSY Bar host ip[:port]")
    p.add_argument("--language", "--lang", choices=["en", "fr", "de", "es", "it", "nl"],
                   default="en", help="answer language (default: en)")
    p.add_argument("--fps", type=int, default=DEFAULT_FPS,
                   help=f"animation frame rate (default: {DEFAULT_FPS})")
    p.add_argument("--auto-roll", type=float, default=0.0, metavar="SECONDS",
                   help="automatically roll every N seconds; 0 disables it (default: 0)")
    p.add_argument("--demo", action="store_true",
                   help="shortcut for --auto-roll 7; useful with the emulator")
    return p.parse_args()


def _base(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "http://" + host


def _ws_url(host):
    host = host.replace("http://", "").replace("https://", "").rstrip("/")
    return "ws://" + host + "/api/status/ws"


def _png(pixels):
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            r, g, b = pixels[y * W + x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 5))
            + chunk(b"IEND", b""))


_RING = 5
_frame_no = 0


def _post(host, path, data, content_type):
    req = urllib.request.Request(_base(host) + path, data=data, method="POST",
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.getcode()


def show(host, pixels):
    global _frame_no
    fn = "frame%d.png" % (_frame_no % _RING)
    _frame_no += 1
    try:
        q = urllib.parse.urlencode({"application_name": APP, "file": fn})
        _post(host, "/api/assets/upload?" + q, _png(pixels), "application/octet-stream")
        body = {
            "application_name": APP,
            "elements": [{"id": "frame", "type": "image", "path": fn, "x": 0, "y": 0}],
        }
        return _post(host, "/api/display/draw", json.dumps(body).encode(), "application/json")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return 409
        raise


def clear(host):
    q = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/display/draw?" + q, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


def delete_assets(host):
    q = urllib.parse.urlencode({"application_name": APP})
    req = urllib.request.Request(_base(host) + "/api/assets/upload?" + q, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


# ---- Minimal protobuf walker for /api/status/ws ---------------------------

def _read_varint(buf, pos):
    value = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7f) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


def _fields(buf):
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_no, wt = tag >> 3, tag & 7
        if wt == 0:
            value, pos = _read_varint(buf, pos)
            yield field_no, wt, value
        elif wt == 1:
            if pos + 8 > len(buf):
                return
            yield field_no, wt, buf[pos:pos + 8]
            pos += 8
        elif wt == 2:
            n, pos = _read_varint(buf, pos)
            end = pos + n
            if end > len(buf):
                return
            yield field_no, wt, buf[pos:end]
            pos = end
        elif wt == 5:
            if pos + 4 > len(buf):
                return
            yield field_no, wt, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")


def _decode_state_inputs(frame):
    """Decode only BUSY Bar input events needed by this app.

    Firmware path, matching media-player:
    State.updates=2 -> StateUpdate.input=11 -> InputEvent.button_event=1.
    START=2, PRESS=0, RELEASE=1.
    """
    events = []
    for field_no, wire, update in _fields(frame):
        if field_no != 2 or wire != 2:
            continue
        for uf, uw, uv in _fields(update):
            if uf != 11 or uw != 2:
                continue
            for inf, inw, inv in _fields(uv):
                if inf != 1 or inw != 2:
                    continue
                button = 0
                action = 0
                for bf, bw, bv in _fields(inv):
                    if bw == 0 and bf == 1:
                        button = int(bv)
                    elif bw == 0 and bf == 2:
                        action = int(bv)
                events.append({"button": button, "action": action})
    return events


def _ws_url(host):
    raw = host.rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/api/status/ws"
    return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))


class ButtonListener:
    """START-button listener using the same `websockets` pattern as media-player."""

    def __init__(self, host):
        self.host = host
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.available = True
        self.error = None

    def start(self):
        try:
            import websockets  # noqa: F401
        except ImportError as exc:
            self.available = False
            self.error = f"websockets not installed: {exc}"
            raise RuntimeError("websockets is required: pip install websockets") from exc
        self.thread = threading.Thread(target=self._thread_main, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def poll_press(self):
        got = False
        try:
            while True:
                self.events.get_nowait()
                got = True
        except queue.Empty:
            return got

    def _thread_main(self):
        try:
            asyncio.run(self._listen_forever())
        except Exception as exc:
            self.available = False
            self.error = str(exc)
            if not self.stop_event.is_set():
                print(f"controls: listener stopped: {exc}")

    async def _listen_forever(self):
        import websockets

        url = _ws_url(self.host)
        backoff = 0.5
        max_backoff = 5.0
        connected_once = False

        while not self.stop_event.is_set():
            try:
                async with websockets.connect(
                    url,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    open_timeout=8,
                ) as ws:
                    await ws.send(json.dumps({"enable": True}))
                    print("controls: BUSY Bar input reconnected" if connected_once
                          else "controls: START=roll (direct WebSocket)")
                    connected_once = True
                    self.available = True
                    self.error = None
                    backoff = 0.5

                    async for message in ws:
                        if self.stop_event.is_set():
                            return
                        if isinstance(message, str):
                            continue
                        try:
                            for event in _decode_state_inputs(bytes(message)):
                                if event.get("button") == 2 and event.get("action") == 0:
                                    self.events.put(time.monotonic())
                        except Exception as exc:
                            print(f"controls: ignored malformed status frame: {exc}")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop_event.is_set():
                    return
                self.available = False
                self.error = str(exc)
                print(f"controls: BUSY Bar input disconnected: {exc}; reconnecting in {backoff:g}s")
                deadline = time.monotonic() + backoff
                while not self.stop_event.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(min(0.25, deadline - time.monotonic()))
                backoff = min(max_backoff, backoff * 2.0)


# ---- Raster drawing --------------------------------------------------------

def blank():
    return [BLACK] * (W * H)


def px(buf, x, y, color):
    if 0 <= x < W and 0 <= y < H:
        buf[y * W + x] = color


def blend(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t + 0.5) for i in range(3))


def scale_color(c, alpha):
    alpha = max(0.0, min(1.0, alpha))
    return tuple(int(v * alpha + 0.5) for v in c)


def circle(buf, cx, cy, radius, color):
    rr = radius * radius
    for y in range(int(cy - radius), int(cy + radius) + 1):
        for x in range(int(cx - radius), int(cx + radius) + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= rr:
                px(buf, x, y, color)


def draw_ball(buf, x, roll_angle=0.0, y_offset=0):
    cx = x + 7
    cy = BALL_Y + 7 + y_offset
    # Two-stage rim: the brighter outer ring stays visible even on dim matrices.
    circle(buf, cx, cy, 7, BALL_EDGE_BRIGHT)
    circle(buf, cx, cy, 6, BALL_EDGE)
    circle(buf, cx, cy, 5, BALL_DARK)
    circle(buf, cx - 1, cy - 1, 4, BALL_MID)
    circle(buf, cx, cy, 4, BALL_DARK)

    # A pair of surface highlights rotates with the sphere. Their angular
    # position is tied to distance travelled, so reversing direction reverses
    # the apparent spin as a real rolling ball would.
    gx = cx + int(round(math.cos(roll_angle - 1.05) * 4))
    gy = cy + int(round(math.sin(roll_angle - 1.05) * 4))
    px(buf, gx, gy, (125, 138, 172))
    gx2 = cx + int(round(math.cos(roll_angle + 2.10) * 5))
    gy2 = cy + int(round(math.sin(roll_angle + 2.10) * 5))
    px(buf, gx2, gy2, (52, 60, 78))

    # White circular number label. The disk stays centred but the pixel-art
    # numeral itself rotates, making the roll unmistakable even at 15x15 px.
    circle(buf, cx, cy, 3, WHITE)
    eight_pixels = ((0, -2), (-1, -1), (1, -1), (0, 0),
                    (-1, 1), (1, 1), (0, 2))
    ca = math.cos(roll_angle)
    sa = math.sin(roll_angle)
    drawn = set()
    for ex, ey in eight_pixels:
        rx = int(round(ex * ca - ey * sa))
        ry = int(round(ex * sa + ey * ca))
        drawn.add((rx, ry))
    for rx, ry in drawn:
        px(buf, cx + rx, cy + ry, BALL_DARK)


def normalize_text(s):
    repl = {
        "à": "a", "á": "a", "â": "a", "ä": "a", "ã": "a",
        "è": "e", "é": "e", "ê": "e", "ë": "e",
        "ì": "i", "í": "i", "î": "i", "ï": "i",
        "ò": "o", "ó": "o", "ô": "o", "ö": "o",
        "ù": "u", "ú": "u", "û": "u", "ü": "u",
        "ç": "c", "ñ": "n", "ß": "ss", "’": "'", "–": "-", "—": "-",
    }
    out = "".join(repl.get(ch, ch) for ch in s.lower()).upper()
    return "".join(ch if ch in FONT else " " for ch in out)


def text_width(s):
    return max(0, len(s) * 4 - 1)


def wrap_two_lines(text, max_chars=13):
    text = normalize_text(text)
    words = text.split()
    if not words:
        return [""]
    lines = [""]
    for word in words:
        candidate = word if not lines[-1] else lines[-1] + " " + word
        if len(candidate) <= max_chars:
            lines[-1] = candidate
        elif len(lines) == 1:
            lines.append(word)
        else:
            # If a translation still exceeds two lines, compact the second line.
            lines[-1] = (lines[-1] + " " + word).strip()
    if len(lines) == 1:
        return lines
    # Hard-cap unusually long second lines; the marquee logic below handles them.
    return lines[:2]


def draw_text_line(buf, text, x, y, color, clip_x0, clip_x1):
    pen = x
    for ch in text:
        glyph = FONT.get(ch, FONT[" "])
        for yy, row in enumerate(glyph):
            for xx, bit in enumerate(row):
                if bit == "1" and clip_x0 <= pen + xx < clip_x1:
                    px(buf, pen + xx, y + yy, color)
        pen += 4


def draw_answer(buf, answer, settled_ball_x, alpha, elapsed, color, reveal_x0=None, reveal_x1=None):
    # Text occupies the side opposite the final ball position.
    if settled_ball_x < W // 2:
        x0, x1 = 17, W
    else:
        x0, x1 = 0, 55

    # Optional moving reveal mask. During a roll this is bounded by the moving
    # ball, so the letters literally become visible only after the ball passes.
    clip_x0 = x0 if reveal_x0 is None else max(x0, reveal_x0)
    clip_x1 = x1 if reveal_x1 is None else min(x1, reveal_x1)
    if clip_x1 <= clip_x0:
        return

    width = x1 - x0
    lines = wrap_two_lines(answer, max_chars=max(8, width // 4))
    col = scale_color(color, alpha)

    ys = [5] if len(lines) == 1 else [2, 9]
    for line, y in zip(lines, ys):
        tw = text_width(line)
        if tw <= width:
            x = x0 + (width - tw) // 2
        else:
            overflow = tw - width
            phase = (max(0.0, elapsed - ROLL_SECONDS) * 11.0) % max(1.0, 2 * (overflow + 8))
            offset = int(phase if phase <= overflow + 8 else 2 * (overflow + 8) - phase)
            x = x0 + 4 - offset
        draw_text_line(buf, line, x, y, col, clip_x0, clip_x1)


def ease_in_out(t):
    t = max(0.0, min(1.0, t))
    return 0.5 - 0.5 * math.cos(math.pi * t)


def build_frame(side_from, side_to, started, answer, color, now):
    elapsed = now - started
    rolling = elapsed < ROLL_SECONDS
    if rolling:
        p = ease_in_out(elapsed / ROLL_SECONDS)
        x = int(round(side_from + (side_to - side_from) * p))
        y_bounce = -1 if 0.28 < p < 0.72 and int(elapsed * 18) % 2 == 0 else 0
    else:
        p = 1.0
        x = side_to
        y_bounce = 0

    # The answer is revealed by motion during the roll, then remains fully
    # visible for five complete seconds. Only after that does the 1 s fade begin.
    fade_start = ROLL_SECONDS + ANSWER_SECONDS
    if elapsed <= fade_start:
        alpha = 1.0
    elif elapsed <= fade_start + FADE_SECONDS:
        alpha = 1.0 - (elapsed - fade_start) / FADE_SECONDS
    else:
        alpha = 0.0

    buf = blank()

    # Draw answer first, masked to the trail already uncovered by the ball.
    if alpha > 0.0:
        if rolling and side_to > side_from:
            draw_answer(buf, answer, side_to, alpha, elapsed, color, reveal_x1=x)
        elif rolling and side_to < side_from:
            draw_answer(buf, answer, side_to, alpha, elapsed, color, reveal_x0=x + BALL_SIZE)
        else:
            draw_answer(buf, answer, side_to, alpha, elapsed, color)

    # Ground shadow and ball are rendered on top, preserving physical occlusion.
    shadow_center = x + 7
    for dx in range(-5, 6):
        if 0 <= shadow_center + dx < W:
            shade = max(3, 12 - abs(dx) * 2)
            px(buf, shadow_center + dx, 15, (shade, shade, shade + 2))

    # Rolling angle follows travelled distance (theta = distance / radius).
    # Near the end of the trip, progressively correct the angle toward the
    # nearest complete revolution. This preserves the rolling motion but makes
    # the numeral finish perfectly upright at either edge.
    raw_angle = -(x - side_from) / 7.0
    final_raw_angle = -(side_to - side_from) / 7.0
    final_upright_angle = round(final_raw_angle / (2.0 * math.pi)) * (2.0 * math.pi)
    settle = max(0.0, min(1.0, (p - 0.72) / 0.28))
    settle = settle * settle * (3.0 - 2.0 * settle)
    roll_angle = raw_angle + (final_upright_angle - final_raw_angle) * settle
    draw_ball(buf, x, roll_angle, y_offset=y_bounce)
    return buf, elapsed


def draw_idle_hint(buf, side, text, alpha):
    """Draw a subtle localized usage hint in the free space beside the ball."""
    text = normalize_text(text)
    if side == LEFT_X:
        x0, x1 = 17, W
    else:
        x0, x1 = 0, 55
    tw = text_width(text)
    x = x0 + max(0, ((x1 - x0) - tw) // 2)
    col = scale_color(HINT_COLOR, alpha)
    draw_text_line(buf, text, x, 5, col, x0, x1)


def idle_frame(side, idle_elapsed=0.0, language="en"):
    buf = blank()
    if idle_elapsed >= IDLE_HINT_DELAY:
        t = idle_elapsed - IDLE_HINT_DELAY
        # Smooth 0->1->0 breathing pulse. Keep it deliberately understated.
        pulse = 0.5 - 0.5 * math.cos(2.0 * math.pi * (t % IDLE_HINT_PERIOD) / IDLE_HINT_PERIOD)
        alpha = 0.14 + 0.40 * pulse
        draw_idle_hint(buf, side, IDLE_HINTS[language], alpha)
    # Ball is always rendered last so the hint appears behind it rather than on top.
    draw_ball(buf, side, 0.0)
    return buf


def main():
    args = parse_args()
    args.fps = max(6, min(20, args.fps))

    listener = ButtonListener(args.host)
    listener.start()

    side = LEFT_X
    from_x = side
    to_x = side
    active = False
    started = 0.0
    answer = ""
    answer_color = BLUE
    last_frame_at = 0.0
    if args.demo and args.auto_roll <= 0:
        args.auto_roll = 7.0
    args.auto_roll = max(0.0, args.auto_roll)
    last_roll_at = time.monotonic()
    idle_since = time.monotonic()

    print(f"magic-8-ball -> {_base(args.host)}  language={args.language}  (Ctrl-C to stop)")
    print("Press the top/START button to ask the Magic 8-Ball.")
    if args.auto_roll > 0:
        print(f"Auto-roll enabled every {args.auto_roll:g} seconds.")

    try:
        show(args.host, idle_frame(side, 0.0, args.language))
        while True:
            now = time.monotonic()
            pressed = listener.poll_press()
            auto_trigger = args.auto_roll > 0 and now - last_roll_at >= args.auto_roll
            if auto_trigger:
                pressed = True

            if pressed:
                # Any manual/automatic roll immediately suppresses the idle hint.
                idle_since = now
                # Direction always alternates between left and right.
                # If pressed mid-animation, start from the current visual side's target;
                # this keeps interaction deterministic and prevents double-press drift.
                if active and (now - started) < ROLL_SECONDS:
                    progress = ease_in_out((now - started) / ROLL_SECONDS)
                    current = int(round(from_x + (to_x - from_x) * progress))
                else:
                    current = to_x if active else side
                from_x = current
                to_x = RIGHT_X if side == LEFT_X else LEFT_X
                side = to_x
                answer = random.choice(ANSWERS[args.language])
                answer_color = random.choice((BLUE, PURPLE))
                started = now
                active = True
                last_roll_at = now
                print(f"8-ball: {answer}")

            frame_interval = 1.0 / args.fps
            if now - last_frame_at >= frame_interval:
                if active:
                    frame, elapsed = build_frame(from_x, to_x, started, answer, answer_color, now)
                    show(args.host, frame)
                    if elapsed > ROLL_SECONDS + ANSWER_SECONDS + FADE_SECONDS:
                        active = False
                        idle_since = now
                        show(args.host, idle_frame(side, 0.0, args.language))
                else:
                    show(args.host, idle_frame(side, now - idle_since, args.language))
                last_frame_at = now

            time.sleep(0.008)

    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {_base(args.host)} - {e.reason}")
    finally:
        listener.stop()
        clear(args.host)
        delete_assets(args.host)


if __name__ == "__main__":
    main()
