#!/usr/bin/env python3
"""Moneybird Invoice Paid: celebrate every paid invoice with coins and a cash-register sound.

    python app.py [--test] [--interval 60]      # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080 --test  # emulator or a Wi-Fi bar

    --test        Preview the celebration with a fake invoice and exit 0.
                  No Moneybird account or token is needed.
    --interval N  Poll interval in seconds (default 60).

Environment variables (set in your shell or in a .env file next to app.py):
    MONEYBIRD_TOKEN               API token (required, unless --test).
                                  Create one at https://moneybird.com/<administration-id>/oauth_tokens/new
    MONEYBIRD_ADMINISTRATION_ID   Administration id (optional; resolved automatically via the API).

The app watches your Moneybird administration for newly paid sales invoices and
plays a full three-phase celebration on the BUSY Bar display for each one:
    Phase A  Coin rain with physics bounce and a growing pile.
    Phase B  Odometer count-up to the invoice amount.
    Phase C  Amount hold with the contact name and sparkles.
A synthesized cash-register sound is uploaded to the device on startup and
played at the start of each celebration.
"""
import io
import json
import math
import os
import random
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

# ---------------------------------------------------------------------------
# .env loader (inline; no third-party deps)
# ---------------------------------------------------------------------------

def _load_dotenv():
    """Read a .env file next to this script and populate os.environ.

    Rules:
    - Lines starting with # (after optional whitespace) are comments.
    - Blank lines are ignored.
    - Format: KEY=VALUE  (leading/trailing whitespace stripped from both).
    - Shell (os.environ) values always win — an existing env var is never overwritten.
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass  # .env is optional


_load_dotenv()

APP = "moneybird-invoice-paid"
W, H = 72, 16

# ---------------------------------------------------------------------------
# BUSY Bar HTTP API — self-contained, stdlib only.
# Over USB the bar is always at 10.0.4.20; --host targets a Wi-Fi bar or the
# emulator. Full API docs are served by the device: http://10.0.4.20/docs
# ---------------------------------------------------------------------------

def _host(default="10.0.4.20"):
    if "--host" in sys.argv:
        i = sys.argv.index("--host")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

BASE = "http://" + _host().replace("http://", "").rstrip("/")

def api(method, path, body=None, raw=None, ctype="application/octet-stream"):
    data, headers = None, {}
    if raw is not None:
        data, headers["Content-Type"] = raw, ctype
    elif body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=5):
        pass

def draw(elements, **extra):
    api("POST", "/api/display/draw", {"application_name": APP, "elements": elements, **extra})

def clear():
    api("DELETE", "/api/display/draw?application_name=" + APP)

def play_audio(path=None, stock_path=None):
    body = {"application_name": APP}
    if path is not None:
        body["path"] = path
    if stock_path is not None:
        body["stock_path"] = stock_path
    api("POST", "/api/audio/play", body)

def upload_asset(file, data):
    q = urllib.parse.urlencode({"application_name": APP, "file": file})
    api("POST", "/api/assets/upload?" + q, raw=data)

def text(txt, x=0, y=0, font="normal", color="0xFFFFFFFF", **kw):
    return {"type": "text", "text": str(txt), "x": x, "y": y, "font": font, "color": color, **kw}

def rectangle(x, y, width, height, **kw):
    return {"type": "rectangle", "x": x, "y": y, "width": width, "height": height, **kw}

class RequestError(RuntimeError):
    """A Moneybird API request failed (non-401)."""

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def _flag(name):
    return name in sys.argv

def _opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default

TEST_MODE = _flag("--test")
INTERVAL  = int(_opt("--interval", "60"))

# ---------------------------------------------------------------------------
# Amount formatting
# ---------------------------------------------------------------------------

def format_amount(s):
    """Dutch-formatted amount without prefix: '1234.56' -> '1.234,56'."""
    s = str(s).strip()
    if "." in s:
        integer_part, decimal_part = s.split(".", 1)
    else:
        integer_part, decimal_part = s, "00"
    decimal_part = (decimal_part + "00")[:2]
    negative = integer_part.startswith("-")
    digits = integer_part.lstrip("-").lstrip("0") or "0"
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    formatted = ".".join(groups)
    if negative:
        formatted = "-" + formatted
    return formatted + "," + decimal_part

def format_euro(s):
    """Dutch-formatted euro amount: '1234.56' -> '€ 1.234,56'."""
    return "€ " + format_amount(s)

# ---------------------------------------------------------------------------
# Bold-font pixel advances (from baked atlas)
# digits -> 7 px, "." -> 4 px, "," -> 3 px
# ---------------------------------------------------------------------------

def bold_width(s):
    return sum(7 if c.isdigit() else 4 if c == "." else 3 for c in s)

# ---------------------------------------------------------------------------
# Tiny-font pixel advances and display-name helper
# ---------------------------------------------------------------------------

# Tiny-font advances for ASCII 32..126, baked from the atlas (all single digits).
TINY_ADV = "32464552334424254444444444224444444444444445465545444566465353443444443342242644443444464444245"

def tiny_width(s):
    """Pixel width of s in the tiny font (unknown chars ~4 px)."""
    return sum(int(TINY_ADV[ord(c) - 32]) if 32 <= ord(c) < 127 else 4 for c in s)

def display_name(contact):
    """Strip Dutch legal-form suffixes for display ('X B.V.' -> 'X')."""
    short = re.sub(r"\s+(b\.?v\.?|n\.?v\.?|v\.?o\.?f\.?|c\.?v\.?)\s*$", "", contact, flags=re.I)
    return short or contact

# ---------------------------------------------------------------------------
# Euro sprite (6 wide x 7 tall; 1 = pixel on)
# ---------------------------------------------------------------------------

EURO_SPRITE = [
    "011110",
    "100000",
    "111100",
    "100000",
    "111100",
    "100000",
    "011110",
]

def _euro_rects(ex, ey):
    """Render the euro sprite as gold rectangle elements at position (ex, ey)."""
    rects = []
    for row_idx, row in enumerate(EURO_SPRITE):
        y = ey + row_idx
        x = 0
        while x < len(row):
            if row[x] == "1":
                run_start = x
                x += 1
                while x < len(row) and row[x] == "1":
                    x += 1
                rects.append(rectangle(
                    x=ex + run_start, y=y,
                    width=x - run_start, height=1,
                    border_width=0,
                    fill="solid",
                    fill_colors=["0xFFD700FF"],
                ))
            else:
                x += 1
    return rects

# ---------------------------------------------------------------------------
# Pixel buffer -> rectangles
# ---------------------------------------------------------------------------

def _buf_to_rects(buf, palette):
    """72x16 index buffer -> rectangle elements. index 0 = transparent (skip)."""
    rects = []
    for y in range(H):
        x = 0
        while x < W:
            idx = buf[y][x]
            if idx == 0:
                x += 1
                continue
            run_start = x
            x += 1
            while x < W and buf[y][x] == idx:
                x += 1
            rects.append(rectangle(
                x=run_start, y=y,
                width=x - run_start, height=1,
                border_width=0,
                fill="solid",
                fill_colors=[palette[idx]],
            ))
    return rects


def _coarsen_coin_buf(buf):
    """Collapse palette: index 3->2 (highlight->gold) to reduce distinct runs."""
    return [[2 if v == 3 else v for v in row] for row in buf]


def _build_coin_frame(buf):
    """Convert coin buffer to rects, coarsening palette if needed to stay <=100."""
    rects = _buf_to_rects(buf, COIN_PALETTE)
    if len(rects) > 100:
        # Collapse highlight->gold: fewer distinct runs
        buf = _coarsen_coin_buf(buf)
        rects = _buf_to_rects(buf, COIN_PALETTE)
    if len(rects) > 100:
        # Collapse rim->gold too: one index per coin pixel so runs actually merge
        buf = [[2 if v else 0 for v in row] for row in buf]
        rects = _buf_to_rects(buf, COIN_PALETTE)
    # Hard cap: if pathologically many coins crowd the same rows, drop the tail
    # (the display stays valid; we just lose some coin pixels rather than crashing)
    return rects[:100]

# ---------------------------------------------------------------------------
# Coins & pile palette (pile buffer: 1=dark gold rim, 2=gold)
# ---------------------------------------------------------------------------

COIN_PALETTE = [
    None,          # 0 transparent
    "0xB8860BFF",  # 1 dark gold rim
    "0xFFD700FF",  # 2 gold
    "0xFFF8C8FF",  # 3 highlight
]

def _coin_rects(cx, cy):
    """One airborne coin as 2 layered elements: a rim-bordered gold rect plus a
    highlight pixel. 16 coins cost 32 elements, so the 100-element cap is never
    hit and the rim can't get collapsed away mid-rain. radius is ignored by the
    emulator (draws square) but rounds the corners on real hardware."""
    return [
        rectangle(x=cx, y=cy, width=5, height=5, radius=2,
                  border_width=1, border_color="0xB8860BFF",
                  fill="solid", fill_colors=["0xFFD700FF"]),
        rectangle(x=cx + 1, y=cy + 1, width=1, height=1, border_width=0,
                  fill="solid", fill_colors=["0xFFF8C8FF"]),
    ]

# ---------------------------------------------------------------------------
# Cash-register sound synthesis
# ---------------------------------------------------------------------------

def synth_chaching_wav():
    """Synthesize a cash-register sound in memory and return WAV bytes.

    Uses random.seed(42) for a reproducible timbre, then reseeds with
    random.seed() (no argument = OS entropy) so the celebration phase
    that follows uses truly random coin positions.
    """
    # Seed for reproducible sound synthesis — NOT for the celebration randomness.
    random.seed(42)

    SR = 22050
    DUR = 0.95
    n = int(SR * DUR)
    buf = [0.0] * n

    def add_tone(t0, freq, amp, tau, partials=((1.0, 1.0), (2.51, 0.35), (4.2, 0.12))):
        start = int(t0 * SR)
        for i in range(start, n):
            t = (i - start) / SR
            env = amp * math.exp(-t / tau)
            if env < 0.0005:
                break
            s = sum(a * math.sin(2 * math.pi * freq * m * t) for m, a in partials)
            buf[i] += env * s

    def add_noise(t0, dur, amp, tau):
        start = int(t0 * SR)
        for i in range(start, min(n, start + int(dur * SR))):
            t = (i - start) / SR
            buf[i] += amp * math.exp(-t / tau) * random.uniform(-1, 1)

    add_noise(0.00, 0.06, 0.55, 0.015)          # register drawer thunk
    add_tone(0.06, 1567.0, 0.50, 0.10)          # G6 strike
    add_tone(0.17, 2093.0, 0.65, 0.22)          # C7 strike, louder + longer ring
    for _ in range(7):                           # quiet coin jingle
        add_tone(random.uniform(0.24, 0.62), random.uniform(2500, 5200),
                 random.uniform(0.05, 0.12), 0.035, partials=((1.0, 1.0),))

    peak = max(abs(v) for v in buf)
    pcm = b"".join(struct.pack("<h", int(v / peak * 0.85 * 32767)) for v in buf)

    # Reseed with OS entropy so subsequent random calls (coin positions etc.)
    # are not deterministic — the synthesis seed must not leak into the celebration.
    random.seed()

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm)
    return out.getvalue()


# Synthesize once at import time so upload_sound() can use it immediately.
_CHACHING_WAV = synth_chaching_wav()

SOUND_UPLOADED = False


def upload_sound():
    """Upload the synthesized cash-register WAV as an app asset; returns True on success."""
    try:
        upload_asset("cha_ching.wav", _CHACHING_WAV)
        return True
    except OSError:
        return False

# ---------------------------------------------------------------------------
# Celebration
# ---------------------------------------------------------------------------

def _draw_frame(elements, first_draw_flag):
    """Send one frame; returns (ok, first_draw_flag).
    On 409: silent skip. On first frame 409 returns (False, False).
    Re-raises other errors."""
    try:
        draw(elements, priority=90, led_notification_color="0xFFD700FF")
        return True, False  # first_draw done, succeeded
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return False, first_draw_flag  # keep first_draw_flag unchanged
        raise


def _pile_rects(pile):
    """Render the coin pile as rectangle elements from the pile array."""
    buf = [[0] * W for _ in range(H)]
    for c in range(W):
        if pile[c] > 0:
            top_row = 15 - pile[c] + 1
            for row in range(top_row, 16):
                buf[row][c] = 2  # gold
            buf[top_row][c] = 1  # dark rim at the top edge
    return _build_coin_frame(buf)


def celebrate(amount_str, contact):
    """Play the full coin celebration on the LED display."""
    global SOUND_UPLOADED

    first_draw = True  # track whether the first draw 409'd

    # Phase A: coin rain with bounce and pile (max 40 frames)
    pile = [0] * W

    # Stratified spawn: one coin per ~4.5px band with jitter, so the rain always
    # covers the full width (pure uniform draws can cluster to one side).
    coins = []
    for i in range(16):
        coins.append({
            "x": min(67, int((i + random.random()) * 68 / 16)),
            "y": float(random.randint(-20, -5)),
            "vy": random.uniform(0.9, 1.5),
            "bounced": False,
        })

    # Sound plays once at the start of phase A
    try:
        if SOUND_UPLOADED:
            play_audio(path=APP + "/cha_ching.wav")
        else:
            play_audio(stock_path="calendar_event_starts")
    except Exception:
        pass

    for frame in range(40):
        still_airborne = []
        for coin in coins:
            x = coin["x"]
            coin["vy"] += 0.10  # gravity
            coin["y"] += coin["vy"]

            floor_y = 15 - max(pile[c] for c in range(x, min(x + 5, W)))

            if int(coin["y"]) + 4 >= floor_y:
                if not coin["bounced"] and coin["vy"] > 1.2:
                    coin["vy"] = -0.45 * coin["vy"]
                    coin["bounced"] = True
                    coin["y"] = float(floor_y - 4)
                    still_airborne.append(coin)
                else:
                    # settle into pile
                    for c in range(x, min(x + 5, W)):
                        pile[c] = min(6, pile[c] + 2)
            else:
                still_airborne.append(coin)

        coins = still_airborne

        # pile behind, airborne coins on top as layered rects
        rects = _pile_rects(pile)
        for coin in coins:
            cy = int(coin["y"])
            if cy + 4 >= 0 and cy < H:
                rects += _coin_rects(coin["x"], cy)

        if not rects:
            time.sleep(0.05)
            continue

        ok, first_draw = _draw_frame(rects, first_draw)
        if not ok and first_draw:
            # First draw 409'd: skip entire celebration
            return
        time.sleep(0.05)

        if not coins:
            break
    else:
        # timeout: force-settle remaining coins
        for coin in coins:
            x = coin["x"]
            for c in range(x, min(x + 5, W)):
                pile[c] = min(6, pile[c] + 2)

    # Phase B: odometer count-up (46 frames)
    target = float(amount_str)
    for k in range(46):
        t = k / 45.0
        value = target * (1 - (1 - t) ** 3)
        s = format_amount("%.2f" % value)
        amount_color = "0xFFFFFFFF" if k == 45 else "0xFFD700FF"

        total_w = 8 + bold_width(s)  # 6 sprite + 2 gap
        ex = (72 - total_w) // 2
        ey = 3

        euro_rects = _euro_rects(ex, ey)
        pile_rects = _pile_rects(pile)

        elements = pile_rects + euro_rects + [
            text(s, x=ex + 8, y=2, font="bold", align="top_left",
                 color=amount_color, id="amount"),
        ]

        # guard: drop excess if too many elements (phases B/C)
        if len(elements) > 100:
            elements = elements[:100]

        ok, first_draw = _draw_frame(elements, first_draw)
        if not ok and first_draw:
            return
        time.sleep(0.05)

    # Phase C: hold + contact
    final_s = format_amount("%.2f" % target)
    total_w = 8 + bold_width(final_s)
    ex = (72 - total_w) // 2
    ey = 3

    name = display_name(contact)
    name_w = tiny_width(name)
    if name_w <= W:
        c_frames = 60
        contact_el = text(name, x=36, y=16, font="tiny", align="bottom_mid",
                          color="0xAAAAAAFF", id="contact")
    else:
        # long name: stretch the phase so one full cycle (0.5s start delay +
        # (width + gap)/speed; renderer gap = 9) completes. Base speed 15 px/s,
        # sped up for extreme names so the cycle always fits the 10s cap.
        speed = max(15.0, (name_w + 9) / 9.0)
        cycle = 0.5 + (name_w + 9) / speed
        c_frames = min(200, int(cycle * 20) + 12)
        contact_el = text(name, x=36, y=16, font="tiny", align="bottom_mid",
                          color="0xAAAAAAFF", width=72,
                          scroll_rate=int(speed * 60), scroll_start_delay=500,
                          id="contact")

    for frame in range(c_frames):
        amount_color = "0xFFD700FF"

        # drain pile every 3rd frame
        if frame % 3 == 0:
            for c in range(W):
                if pile[c] > 0:
                    pile[c] -= 1

        euro_rects = _euro_rects(ex, ey)
        pile_rects = _pile_rects(pile)

        # 3 random sparkles
        sparkles = []
        for _ in range(3):
            sparkles.append(rectangle(
                x=random.randint(0, W - 1),
                y=random.randint(0, H - 1),
                width=1, height=1,
                border_width=0,
                fill="solid",
                fill_colors=["0xFFF8C8FF"],
            ))

        elements = pile_rects + euro_rects + [
            text(final_s, x=ex + 8, y=2, font="bold", align="top_left",
                 color=amount_color, id="amount"),
            # Stable id: the renderer keys scroll state on element id, so fresh
            # ids every frame would reset the contact scroll each redraw.
            contact_el,
        ] + sparkles

        # guard: drop sparkles first if too many (never the amount)
        if len(elements) > 100:
            elements = elements[:-len(sparkles)]

        ok, first_draw = _draw_frame(elements, first_draw)
        if not ok and first_draw:
            return
        time.sleep(0.05)

    # Release the display
    try:
        clear()
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Moneybird API helpers
# ---------------------------------------------------------------------------

MB_BASE = "https://moneybird.com/api/v2"


def token_url():
    """Token-creation page; fills in the admin id from the env when available."""
    aid = os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip()
    return f"https://moneybird.com/{aid or '<administration-id>'}/oauth_tokens/new"


def _mb_get(path, token, params=None, timeout=10):
    """GET a Moneybird JSON endpoint. Raises SystemExit on 401, RequestError on others."""
    url = MB_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Moneybird token is invalid or expired.")
            print(f"Create a new token at {token_url()}")
            print("Set it as MONEYBIRD_TOKEN in your shell or in a .env file next to app.py.")
            sys.exit(1)
        raise RequestError(f"Moneybird GET {path} -> {e.code}") from e
    except OSError as e:  # URLError, socket timeouts, DNS failures
        raise RequestError(f"Moneybird GET {path} failed: {e}") from e


def resolve_admin_id(token):
    """Return the administration id (string): from env or first admin via API."""
    env_id = os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip()
    if env_id:
        return env_id
    admins = _mb_get("/administrations.json", token)
    return str(admins[0]["id"])


def fetch_paid_invoices(admin_id, token):
    """Return ALL invoice dicts for state:paid in this_year (paginated: Moneybird
    caps responses at 100/page; a late-paid old invoice lands on a later page)."""
    invoices, page = [], 1
    while page <= 50:  # safety cap: 5000 invoices
        batch = _mb_get(
            f"/{admin_id}/sales_invoices.json",
            token,
            params={"filter": "state:paid,period:this_year",
                    "per_page": 100, "page": page},
        )
        invoices.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return invoices


def contact_name(invoice):
    contact = invoice.get("contact") or {}
    company = (contact.get("company_name") or "").strip()
    if company:
        return company
    first = (contact.get("firstname") or "").strip()
    last  = (contact.get("lastname")  or "").strip()
    full  = (first + " " + last).strip()
    return full or "Unknown"

# ---------------------------------------------------------------------------
# State file (stored next to app.py, gitignored by the gallery)
# ---------------------------------------------------------------------------

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".moneybird_state.json")


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(admin_id, seen_ids):
    with open(STATE_PATH, "w") as f:
        json.dump({"administration_id": admin_id, "seen": list(seen_ids)}, f)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global SOUND_UPLOADED
    SOUND_UPLOADED = upload_sound()

    # --test mode: single celebration, no Moneybird, no state file
    if TEST_MODE:
        print("cha-ching! [test] -- Test Client Ltd. -- " + format_euro("1234.56"))
        celebrate("1234.56", "Test Client Ltd.")
        sys.exit(0)

    # Resolve token
    token = os.environ.get("MONEYBIRD_TOKEN", "").strip()
    if not token:
        print("MONEYBIRD_TOKEN is not set.")
        print(f"Create a token at {token_url()}")
        print("Set it as MONEYBIRD_TOKEN in your shell or in a .env file next to app.py.")
        sys.exit(1)

    # Resolve administration id
    admin_id = resolve_admin_id(token)

    # Load / seed state
    state = load_state()
    needs_reseed = (
        state is None
        or state.get("administration_id") != admin_id
    )

    if needs_reseed:
        invoices = fetch_paid_invoices(admin_id, token)
        seen = set(str(inv["id"]) for inv in invoices)
        save_state(admin_id, seen)
        print(f"Seeded {len(seen)} already-paid invoices — watching for new ones.")
    else:
        seen = set(state.get("seen", []))

    print(f"moneybird-invoice-paid -> {BASE}  (polling Moneybird every {INTERVAL}s, Ctrl-C to stop)")

    try:
        while True:
            # Poll
            try:
                invoices = fetch_paid_invoices(admin_id, token)
            except RequestError as e:
                print(f"warning: poll failed -- {e}")
                time.sleep(INTERVAL)
                continue

            new_invoices = [inv for inv in invoices if str(inv["id"]) not in seen]

            for inv in new_invoices:
                inv_id     = str(inv["id"])
                invoice_no = str(inv.get("invoice_id", inv_id))
                amount     = str(inv.get("total_price_incl_tax", "0.00"))
                name       = contact_name(inv)
                print(f"cha-ching! {invoice_no} -- {name} -- {format_euro(amount)}")
                try:
                    celebrate(amount, name)
                except OSError as e:
                    print(f"warning: display error -- {e}")
                seen.add(inv_id)
                save_state(admin_id, seen)
                if len(new_invoices) > 1:
                    time.sleep(0.5)

            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("stopped.")
        try:
            clear()
        except Exception:
            pass
        sys.exit(0)


if __name__ == "__main__":
    main()
