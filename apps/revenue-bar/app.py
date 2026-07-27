#!/usr/bin/env python3
"""Live Stripe + Paddle net revenue on your BUSY Bar: day, week, month and year to date with deltas and sparklines.

    python3 app.py                       # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --host 127.0.0.1:8080 # emulator or a Wi-Fi bar
    python3 app.py --test                # fake data, no API keys needed
    python3 app.py --selftest            # offline checks of the math, exit 0/1

Environment variables (shell or a .env file next to app.py, shell wins):
    STRIPE_API_KEY   restricted read-only key (Balance transaction read access)
    PADDLE_API_KEY   Paddle API key
    PADDLE_ENV       live (default) or sandbox
    WEEK_START       mon (default) or sun
    CURRENCY         usd (default), gbp, eur, ... transactions in any other
                     currency are skipped. Non-usd hides the $ prefix, since
                     the device fonts cannot render pound or euro signs.

Either provider key alone is fine. Revenue definition: charges minus refunds,
excluding tax Paddle collects, before provider fees. One currency only.
"""
import argparse
import datetime as dt
import json
import os
import signal
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = "revenue-bar"
W, H = 72, 16
PRIORITY = 30

COLORS = {
    "label": "#6E7C93FF",
    "value": "#FFD24AFF",
    "up": "#3FD35AFF",
    "down": "#FF5A4EFF",
    "spark": "#2EA84CFF",
    "spark_hot": "#5DFF7EFF",
    "stale": "#8A8A8AFF",
    "spark_stale": "#103B1BFF",
}

SCREENS = ["day", "week", "month", "year"]
LABELS = {"day": "DAY", "week": "WK", "month": "MO", "year": "YR"}


def _load_dotenv():
    """Populate os.environ from a .env next to this script. Shell values win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


def env(name, default=None):
    return os.environ.get(name, default)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20", help="bar ip[:port], emulator: 127.0.0.1:8080")
    p.add_argument("--interval", type=int, default=300, help="seconds between revenue API polls")
    p.add_argument("--dwell", type=int, default=5, help="seconds per screen in the rotation")
    p.add_argument("--test", action="store_true", help="run with deterministic fake data, no keys")
    p.add_argument("--selftest", action="store_true", help="run offline math checks and exit")
    return p.parse_args(argv)


def cur_code():
    return env("CURRENCY", "usd").lower()


VALUE_MAX_CHARS = 8


def money_str(cents, max_chars=VALUE_MAX_CHARS):
    """Money string within max_chars. The full comma-formatted number is
    preferred, K/M contractions only when it will not fit. USD gets a $
    prefix, other currencies get no symbol (device fonts are ASCII, so
    pound and euro signs cannot render). Minus rides on top of the budget."""
    sym = "$" if cur_code() == "usd" else ""
    sign = "-" if cents < 0 else ""
    d = abs(cents) / 100
    forms = [f"{d:,.0f}"]
    if 1_000 <= d < 1_000_000:
        forms += [f"{d / 1e3:.1f}K", f"{d / 1e3:.0f}K"]
    elif d >= 1_000_000:
        forms += [f"{d / 1e6:.2f}M", f"{d / 1e6:.1f}M", f"{d / 1e6:.0f}M"]
    for f in forms:
        if len(sym + f) <= max_chars:
            return sign + sym + f
    return sign + sym + forms[-1]


def day_key(d):
    return d.strftime("%Y-%m-%d")


def sum_range(buckets, start, end, providers=None):
    """Inclusive sum of day buckets, optionally restricted to providers."""
    total = 0
    d = start
    while d <= end:
        day = buckets.get(day_key(d))
        if day:
            for prov, cents in day.items():
                if providers is None or prov in providers:
                    total += cents
        d += dt.timedelta(days=1)
    return total


def range_start(rk, ref, week_start):
    if rk == "day":
        return ref
    if rk == "week":
        wd = ref.weekday() if week_start == "mon" else (ref.weekday() + 1) % 7
        return ref - dt.timedelta(days=wd)
    if rk == "month":
        return ref.replace(day=1)
    return ref.replace(month=1, day=1)


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))


def prev_anchor(rk, now):
    if rk == "day":
        return now - dt.timedelta(days=1)
    if rk == "week":
        return now - dt.timedelta(days=7)
    if rk == "month":
        d = add_months(now.date(), -1)
    else:
        d = add_months(now.date(), -12)
    return dt.datetime.combine(d, now.time())


def to_date(buckets, rk, now, week_start, providers=None):
    return sum_range(buckets, range_start(rk, now.date(), week_start), now.date(), providers)


def prev_same_point(buckets, rk, now, week_start, providers=None):
    # ponytail: the previous period's trailing day is scaled by fraction of day
    # elapsed now, assuming revenue was uniform across that day.
    anchor = prev_anchor(rk, now)
    start = range_start(rk, anchor.date(), week_start)
    full = 0
    if anchor.date() > start:
        full = sum_range(buckets, start, anchor.date() - dt.timedelta(days=1), providers)
    frac = (now - dt.datetime.combine(now.date(), dt.time())).total_seconds() / 86400
    partial = sum_range(buckets, anchor.date(), anchor.date(), providers) * frac
    return full + int(partial)


def delta_pct(cur, prev):
    if prev <= 0:
        return None
    return max(-999, min(999, round((cur / prev - 1) * 100)))


def spark_series(buckets, rk, now, week_start):
    today = now.date()
    if rk == "day":
        days = [today - dt.timedelta(days=i) for i in range(12, -1, -1)]
        return [sum_range(buckets, d, d) for d in days]
    if rk == "week":
        w0 = range_start("week", today, week_start)
        out = []
        for i in range(12, -1, -1):
            ws = w0 - dt.timedelta(days=7 * i)
            out.append(sum_range(buckets, ws, min(ws + dt.timedelta(days=6), today)))
        return out
    if rk == "month":
        m0 = today.replace(day=1)
        out = []
        for i in range(12, -1, -1):
            ms = add_months(m0, -i)
            me = add_months(ms, 1) - dt.timedelta(days=1)
            out.append(sum_range(buckets, ms, min(me, today)))
        return out
    out = []
    for m in range(1, today.month + 1):
        ms = dt.date(today.year, m, 1)
        me = add_months(ms, 1) - dt.timedelta(days=1)
        out.append(sum_range(buckets, ms, min(me, today)))
    return out


LAYOUT = {
    "label_xy": (1, 1),
    "delta_x": 24,
    "value_x": 1,
    # ponytail: "normal" clears the label band on 24.3.0 firmware where
    # "large" is taller than the free height. Bump back if firmware fonts
    # change, the bottom anchor keeps whatever font from clipping offscreen.
    "value_font": "normal",
    "label_font": "tiny",
    "spark_right": 71,
    "spark_pitch": 2,
    # Firmware text bottom-anchors include ~2 rows of descender space that
    # digits never use, so the glyph image must be lifted to match visually.
    "glyph_lift": 2,
}


def build_model(buckets, now, week_start):
    model = {}
    for rk in SCREENS:
        cur = to_date(buckets, rk, now, week_start)
        model[rk] = {
            "value": cur,
            "delta": delta_pct(cur, prev_same_point(buckets, rk, now, week_start)),
            "spark": spark_series(buckets, rk, now, week_start),
        }
    return model


def _text(eid, text, font, color, x, y, dwell, align=None):
    el = {"id": eid, "type": "text", "text": text, "font": font, "color": color,
          "x": x, "y": y, "display": "front", "timeout": dwell * 3}
    if align:
        el["align"] = align
    return el


def _spark_rects(series, stale, dwell):
    """Sparkline as 1px-wide solid rectangles, right-aligned, bottom-anchored."""
    mx = max(series) if series and max(series) > 0 else 0
    # Short series (YEAR: up to 12 monthly bars) get wider bars to fill the region.
    bw, pitch = (2, 3) if len(series) <= 8 else (1, LAYOUT["spark_pitch"])
    x0 = LAYOUT["spark_right"] - (bw - 1) - pitch * (len(series) - 1)
    # 1 px baseline "axis" under the bars, shown only while data is sparse
    # (2 or fewer lit bars), so a lone bar reads as a chart, not a glitch.
    # Bars always reserve the bottom row, so geometry is stable either way.
    rects = []
    if sum(1 for v in series if v > 0) <= 2:
        rects.append({"id": "b", "type": "rectangle",
                      "x": x0, "y": H - 1,
                      "width": LAYOUT["spark_right"] - x0 + 1, "height": 1,
                      "fill": "solid",
                      "fill_colors": [COLORS["spark_stale"] if stale else BASELINE_COLOR],
                      "border_width": 0, "display": "front", "timeout": dwell * 3})
    for i, v in enumerate(series):
        if mx <= 0 or v <= 0:
            continue
        h = max(1, round(v / mx * SPARK_MAX))
        hot = i == len(series) - 1
        color = COLORS["spark_stale"] if stale else (
            COLORS["spark_hot"] if hot else COLORS["spark"])
        rects.append({"id": f"s{i}", "type": "rectangle",
                      "x": x0 + pitch * i, "y": H - 1 - h,
                      "width": bw, "height": h, "fill": "solid",
                      "fill_colors": [color], "border_width": 0,
                      "display": "front", "timeout": dwell * 3})
    return rects


TRANSPARENT = "#00000000"
GLYPH = False  # set at startup when a currency glyph asset was uploaded
GLYPH_W = 7    # 5 px glyph plus 2 px gap before the number
SPARK_MAX = 12  # bar height cap, keeps the graph clear of the delta row
BASELINE_COLOR = "#2A2F36FF"

POUND = [
    "..XX.",
    ".X..X",
    ".X...",
    "XXXX.",
    ".X...",
    ".X...",
    "XXXXX",
]


def _png(w, h, rgba_rows):
    """Minimal RGBA PNG encoder, stdlib only."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))
    raw = b"".join(b"\x00" + b"".join(bytes(px) for px in row) for row in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def pound_png():
    amber = (255, 210, 74, 255)
    clear = (0, 0, 0, 0)
    return _png(5, 7, [[amber if c == "X" else clear for c in row] for row in POUND])


def _park_text(eid, dwell):
    return _text(eid, "x", "tiny", TRANSPARENT, 0, 0, dwell)


def _park_rect(eid, dwell):
    return {"id": eid, "type": "rectangle", "x": 0, "y": 0, "width": 1,
            "height": 1, "fill": "solid", "fill_colors": [TRANSPARENT],
            "border_width": 0, "display": "front", "timeout": dwell * 3}


def screen_elements(rk, model, stale, dwell):
    """Every screen updates the same fixed id set (0-2 text, s0-s12 rects).
    The firmware persists elements per id, so disjoint id sets would ghost
    into each other, and delete-then-draw flashes the built-in UI through
    the gap. Ids a screen does not use are parked fully transparent."""
    els = {eid: _park_text(eid, dwell) for eid in "012"}
    for i in range(13):
        els[f"s{i}"] = _park_rect(f"s{i}", dwell)
    els["b"] = _park_rect("b", dwell)
    c_value = COLORS["stale"] if stale else COLORS["value"]
    c_label = COLORS["stale"] if stale else COLORS["label"]
    m = model[rk]
    els["0"] = _text("0", LABELS[rk], LAYOUT["label_font"], c_label,
                     *LAYOUT["label_xy"], dwell)
    if m["delta"] is not None:
        c = COLORS["stale"] if stale else (
            COLORS["up"] if m["delta"] >= 0 else COLORS["down"])
        els["1"] = _text("1", f"{m['delta']:+d}%", "tiny", c,
                         LAYOUT["delta_x"], LAYOUT["label_xy"][1], dwell)
    value_x = LAYOUT["value_x"] + (GLYPH_W if GLYPH else 0)
    if GLYPH:
        els["c"] = {"id": "c", "type": "image", "path": "pound.png",
                    "x": LAYOUT["value_x"], "y": H - LAYOUT["glyph_lift"],
                    "align": "bottom_left", "opacity": 35 if stale else 100,
                    "display": "front", "timeout": dwell * 3}
    # Bottom-anchored so the tallest firmware font can never clip below
    # the matrix, whatever its actual pixel height is.
    els["2"] = _text("2", money_str(m["value"]), LAYOUT["value_font"],
                     c_value, value_x, H, dwell, "bottom_left")
    for r in _spark_rects(m["spark"], stale, dwell):
        els[r["id"]] = r
    return list(els.values())


BASE = "http://10.0.4.20"


def set_host(host):
    global BASE
    BASE = "http://" + host.replace("http://", "").rstrip("/")


def api(method, path, body=None, raw=None, ctype="application/octet-stream"):
    data, headers = None, {}
    if raw is not None:
        data, headers["Content-Type"] = raw, ctype
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=5) as r:
        out = r.read()
    return json.loads(out) if out else None


def upload_asset(name, data):
    q = urllib.parse.urlencode({"application_name": APP, "file": name})
    api("POST", "/api/assets/upload?" + q, raw=data)


def draw(elements):
    try:
        api("POST", "/api/display/draw",
            {"application_name": APP, "priority": PRIORITY, "elements": elements})
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 means a focus session owns the display
            raise


def clear():
    try:
        api("DELETE", f"/api/display/draw?application_name={APP}")
    except OSError:
        pass


def fake_buckets(today):
    """500 days of plausible growing revenue. Deterministic via crc32."""
    out = {}
    for i in range(500):
        d = today - dt.timedelta(days=499 - i)
        k = day_key(d)
        base = 60000 + zlib.crc32(k.encode()) % 90000
        if d.weekday() >= 5:
            base //= 2
        base = int(base * (1 + i / 400))
        out[k] = {"stripe": int(base * 0.66), "paddle": base - int(base * 0.66)}
    return out


def run_loop(args, buckets, sync_fn):
    week_start = env("WEEK_START", "mon")
    stale_polls = 0
    next_fetch = 0.0
    idx = 0
    outage = False
    while True:
        if sync_fn and time.monotonic() >= next_fetch:
            stale_polls = sync_fn(buckets, stale_polls)
            next_fetch = time.monotonic() + args.interval
        model = build_model(buckets, dt.datetime.now(), week_start)
        try:
            draw(screen_elements(SCREENS[idx], model, stale_polls >= 3, args.dwell))
            outage = False
        except OSError as e:
            if not outage:
                print(f"[{dt.datetime.now():%H:%M:%S}] bar unreachable: {e}", file=sys.stderr)
            outage = True
        idx = (idx + 1) % len(SCREENS)
        time.sleep(args.dwell)


STRIPE_TYPES = {"charge", "payment", "refund", "payment_refund"}
_warned_currency = False


def stripe_apply(events, buckets):
    """Fold balance transactions into day buckets. Refund amounts arrive negative."""
    global _warned_currency
    for ev in events:
        if ev.get("type") not in STRIPE_TYPES:
            continue
        if ev.get("currency") != cur_code():
            if not _warned_currency:
                print(f"warning: skipping non-{cur_code()} transactions "
                      f"({ev.get('currency')})", file=sys.stderr)
                _warned_currency = True
            continue
        k = day_key(dt.datetime.fromtimestamp(ev["created"]).date())
        day = buckets.setdefault(k, {})
        day["stripe"] = day.get("stripe", 0) + ev["amount"]


def _stripe_get(key, params):
    url = "https://api.stripe.com/v1/balance_transactions?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def stripe_fetch(key, since_epoch):
    events, params = [], {"limit": 100, "created[gte]": since_epoch}
    while True:
        page = _stripe_get(key, params)
        events.extend(page["data"])
        if not page.get("has_more"):
            return events
        params["starting_after"] = page["data"][-1]["id"]


def stripe_validate(key):
    _stripe_get(key, {"limit": 1})


PADDLE_WINDOW_DAYS = 35


def paddle_base():
    return ("https://sandbox-api.paddle.com" if env("PADDLE_ENV", "live") == "sandbox"
            else "https://api.paddle.com")


def _paddle_date(iso):
    d = dt.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    d = d.replace(tzinfo=dt.timezone.utc).astimezone()
    return d.date()


def paddle_apply(txns, adjs, buckets, window_start):
    """Idempotently rebuild the paddle side of all buckets from window_start on."""
    for k, day in list(buckets.items()):
        if k >= day_key(window_start):
            day.pop("paddle", None)
            if not day:
                del buckets[k]
    global _warned_currency
    for t in txns:
        if t.get("currency_code") != cur_code().upper():
            if not _warned_currency:
                print(f"warning: skipping non-{cur_code()} transactions "
                      f"({t.get('currency_code')})", file=sys.stderr)
                _warned_currency = True
            continue
        k = day_key(_paddle_date(t["billed_at"]))
        day = buckets.setdefault(k, {})
        day["paddle"] = day.get("paddle", 0) + int(t["details"]["totals"]["subtotal"])
    for a in adjs:
        if a.get("action") not in ("refund", "partial_refund", "chargeback"):
            continue
        if a.get("status") != "approved":
            continue
        k = day_key(_paddle_date(a["created_at"]))
        day = buckets.setdefault(k, {})
        day["paddle"] = day.get("paddle", 0) - int(a["totals"]["subtotal"])


def _paddle_get(key, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _paddle_pages(key, url):
    items = []
    while url:
        page = _paddle_get(key, url)
        items.extend(page.get("data", []))
        pg = page.get("meta", {}).get("pagination", {})
        url = pg.get("next") if pg.get("has_more") else None
    return items


_warned_adjustments = False


def paddle_fetch(key, base_url, since_iso):
    q = urllib.parse.urlencode({"status": "completed", "billed_at[GTE]": since_iso,
                                "per_page": 200})
    txns = _paddle_pages(key, f"{base_url}/transactions?{q}")
    global _warned_adjustments
    try:
        adjs = _paddle_pages(key, f"{base_url}/adjustments?per_page=200")
    except urllib.error.HTTPError as e:
        # A key scoped to transactions only still works, minus refund tracking.
        if e.code != 403:
            raise
        if not _warned_adjustments:
            print("warning: paddle key lacks adjustment read permission, "
                  "refunds will not be subtracted", file=sys.stderr)
            _warned_adjustments = True
        adjs = []
    return txns, adjs


def paddle_validate(key):
    _paddle_get(key, f"{paddle_base()}/transactions?per_page=1")


CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")


def backfill_start(today):
    return dt.date(today.year - 1, 1, 1)


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
        return {"buckets": c.get("buckets", {}),
                "stripe_last_sync": c.get("stripe_last_sync"),
                "paddle_backfilled": c.get("paddle_backfilled", False)}
    except (OSError, ValueError):
        return {"buckets": {}, "stripe_last_sync": None, "paddle_backfilled": False}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_PATH)


def make_sync(cache, providers):
    stripe_key = env("STRIPE_API_KEY")
    paddle_key = env("PADDLE_API_KEY")

    def sync(buckets, stale_polls):
        ok = False
        now = dt.datetime.now()
        if "stripe" in providers:
            try:
                last = cache["stripe_last_sync"]
                if last is None:
                    since = int(dt.datetime.combine(
                        backfill_start(now.date()), dt.time()).timestamp())
                else:
                    yesterday = dt.datetime.combine(
                        now.date() - dt.timedelta(days=1), dt.time())
                    since = min(last, int(yesterday.timestamp()))
                # Idempotence: clear the stripe side of every bucket the refetch
                # covers, so overlap and multi-day gaps never double-count.
                cut = day_key(dt.datetime.fromtimestamp(since).date())
                for k in list(buckets):
                    if k >= cut:
                        buckets[k].pop("stripe", None)
                        if not buckets[k]:
                            del buckets[k]
                stripe_apply(stripe_fetch(stripe_key, since), buckets)
                cache["stripe_last_sync"] = int(now.timestamp()) - 300
                ok = True
            except OSError as e:
                print(f"[{now:%H:%M:%S}] stripe poll failed: {e}", file=sys.stderr)
        if "paddle" in providers:
            try:
                wstart = (now.date() - dt.timedelta(days=PADDLE_WINDOW_DAYS)
                          if cache.get("paddle_backfilled")
                          else backfill_start(now.date()))
                since_iso = wstart.strftime("%Y-%m-%dT00:00:00Z")
                txns, adjs = paddle_fetch(paddle_key, paddle_base(), since_iso)
                paddle_apply(txns, adjs, buckets, wstart)
                cache["paddle_backfilled"] = True
                ok = True
            except OSError as e:
                print(f"[{now:%H:%M:%S}] paddle poll failed: {e}", file=sys.stderr)
        cache["buckets"] = buckets
        save_cache(cache)
        return 0 if ok else stale_polls + 1

    return sync


def selftest():
    checks = 0
    # Task 1 sanity
    assert parse_args([]).host == "10.0.4.20"
    assert parse_args(["--host", "127.0.0.1:8080"]).host == "127.0.0.1:8080"
    checks += 2
    # Task 2: money_str. Full numbers preferred, contraction only past budget.
    assert money_str(128400) == "$1,284"
    assert money_str(999900) == "$9,999"
    assert money_str(1000000) == "$10,000"
    assert money_str(3197000) == "$31,970"
    assert money_str(21400000) == "$214,000"
    assert money_str(99995000) == "$999,950"
    assert money_str(128000000) == "$1.28M"
    assert money_str(0) == "$0"
    assert money_str(-42000) == "-$420"
    assert money_str(3197000, 6) == "$32.0K"  # split rows keep the tight budget
    assert money_str(1000000, 6) == "$10.0K"
    assert len(money_str(999999999999)) <= 9  # sign never pushes past budget
    # Task 2: sum_range
    bk = {
        "2026-07-25": {"stripe": 100, "paddle": 10},
        "2026-07-26": {"stripe": 200},
        "2026-07-27": {"paddle": 40},
    }
    d = dt.date
    assert sum_range(bk, d(2026, 7, 25), d(2026, 7, 27)) == 350
    assert sum_range(bk, d(2026, 7, 26), d(2026, 7, 26)) == 200
    assert sum_range(bk, d(2026, 7, 25), d(2026, 7, 27), providers=["paddle"]) == 50
    assert sum_range(bk, d(2026, 7, 1), d(2026, 7, 24)) == 0
    checks += 14
    # Currency config: non-usd drops the $ prefix (device fonts are ASCII only)
    os.environ["CURRENCY"] = "gbp"
    assert money_str(128400) == "1,284"
    assert money_str(3197000) == "31,970"
    gb = {}
    stripe_apply([{"type": "charge", "amount": 500, "currency": "usd", "created": 1784500000},
                  {"type": "charge", "amount": 700, "currency": "gbp", "created": 1784500000}], gb)
    assert list(gb.values()) == [{"stripe": 700}]
    del os.environ["CURRENCY"]
    assert money_str(128400) == "$1,284"
    checks += 4
    # Task 3: period math. Fixed clock: Wednesday 2026-07-15 15:00 local.
    now = dt.datetime(2026, 7, 15, 15, 0, 0)
    assert range_start("day", now.date(), "mon") == dt.date(2026, 7, 15)
    assert range_start("week", now.date(), "mon") == dt.date(2026, 7, 13)
    assert range_start("week", now.date(), "sun") == dt.date(2026, 7, 12)
    assert range_start("month", now.date(), "mon") == dt.date(2026, 7, 1)
    assert range_start("year", now.date(), "mon") == dt.date(2026, 1, 1)
    assert add_months(dt.date(2026, 7, 31), -1) == dt.date(2026, 6, 30)
    assert add_months(dt.date(2026, 1, 15), -1) == dt.date(2025, 12, 15)
    assert prev_anchor("day", now) == dt.datetime(2026, 7, 14, 15, 0, 0)
    assert prev_anchor("week", now) == dt.datetime(2026, 7, 8, 15, 0, 0)
    assert prev_anchor("month", dt.datetime(2026, 7, 31, 9, 0)) == dt.datetime(2026, 6, 30, 9, 0)
    assert prev_anchor("year", dt.datetime(2028, 2, 29, 9, 0)) == dt.datetime(2027, 2, 28, 9, 0)
    # Buckets: $100/day for stripe on every day of 2025 and 2026 up to now.
    flat = {}
    dd = dt.date(2025, 1, 1)
    while dd <= now.date():
        flat[day_key(dd)] = {"stripe": 10000}
        dd += dt.timedelta(days=1)
    # DAY: today-so-far 100 vs yesterday scaled 100 * 15/24 = 62.5, delta +60%
    assert to_date(flat, "day", now, "mon") == 10000
    assert prev_same_point(flat, "day", now, "mon") == 6250
    assert delta_pct(10000, 6250) == 60
    # WEEK: Mon+Tue full + Wed-so-far = 300 vs 200 + 62.5
    assert to_date(flat, "week", now, "mon") == 30000
    assert prev_same_point(flat, "week", now, "mon") == 26250
    # MONTH: 15 days vs 14 full + partial
    assert to_date(flat, "month", now, "mon") == 150000
    assert prev_same_point(flat, "month", now, "mon") == 146250
    # YEAR to date vs same point last year
    assert to_date(flat, "year", now, "mon") == 1960000
    assert prev_same_point(flat, "year", now, "mon") == 1956250
    # delta edge cases
    assert delta_pct(5000, 0) is None
    assert delta_pct(500000, 100) == 999
    assert delta_pct(0, 10000) == -100
    # sparklines
    s = spark_series(flat, "day", now, "mon")
    assert len(s) == 13 and s[-1] == 10000 and s[0] == 10000
    s = spark_series(flat, "week", now, "mon")
    assert len(s) == 13 and s[-1] == 30000 and s[-2] == 70000
    s = spark_series(flat, "month", now, "mon")
    assert len(s) == 13 and s[-1] == 150000 and s[-2] == 300000
    s = spark_series(flat, "year", now, "mon")
    assert len(s) == 7 and s[0] == 310000 and s[-1] == 150000
    checks += 26
    # Task 4: renderer, reusing `flat` buckets and `now` from above.
    model = build_model(flat, now, "mon")
    assert model["day"]["value"] == 10000
    assert model["week"]["delta"] == 14
    els = screen_elements("day", model, False, 5)
    texts = [e for e in els if e["type"] == "text"]
    rects = [e for e in els if e["type"] == "rectangle"]
    assert [t["text"] for t in texts[:2]] == ["DAY", "+60%"]
    assert any(t["text"] == "$100" for t in texts)
    bars = [r for r in rects if r["id"] != "b"]
    base = next(r for r in rects if r["id"] == "b")
    assert len(bars) == 13 and all(r["fill"] == "solid" for r in rects)
    assert base["fill_colors"] == [TRANSPARENT]  # dense data hides the axis
    assert all(0 <= e["x"] <= 71 and 0 <= e["y"] <= 16 for e in els)
    assert all(e.get("display") == "front" and e.get("timeout") == 15 for e in els)
    assert bars[-1]["fill_colors"] == [COLORS["spark_hot"]]
    assert all(r["y"] + r["height"] <= 16 for r in rects)
    # equal series scales to the capped height, sitting on the baseline
    assert bars[0]["height"] == SPARK_MAX and bars[0]["y"] == 15 - SPARK_MAX
    # stale variant dims everything visible (parked ids stay transparent)
    els = screen_elements("day", model, True, 5)
    assert all(e["color"] == COLORS["stale"] for e in els
               if e["type"] == "text" and e["color"] != TRANSPARENT)
    # zero previous period suppresses the delta element (parked transparent)
    zmodel = build_model({day_key(now.date()): {"stripe": 5000}}, now, "mon")
    zels = screen_elements("day", zmodel, False, 5)
    ztexts = [e for e in zels if e["type"] == "text"]
    assert all("%" not in t["text"] for t in ztexts)
    assert any(e["color"] == TRANSPARENT for e in zels)  # parking in effect
    zbase = next(r for r in zels if r["id"] == "b")
    assert zbase["fill_colors"] == [BASELINE_COLOR] and zbase["y"] == 15
    checks += 16
    # currency glyph: image element appears and the value shifts right
    global GLYPH
    GLYPH = True
    gels = screen_elements("day", model, False, 5)
    GLYPH = False
    glyph_el = next(e for e in gels if e.get("path") == "pound.png")
    assert glyph_el["y"] == H - LAYOUT["glyph_lift"]
    assert next(e for e in gels if e["id"] == "2")["x"] == LAYOUT["value_x"] + GLYPH_W
    p = pound_png()
    assert p[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", p[16:24]) == (5, 7)
    checks += 5
    # Task 5: deterministic fake data
    fb = fake_buckets(dt.date(2026, 7, 15))
    fb2 = fake_buckets(dt.date(2026, 7, 15))
    assert fb == fb2 and len(fb) == 500
    assert all(set(v) == {"stripe", "paddle"} for v in fb.values())
    assert all(v["stripe"] > 0 and v["paddle"] > 0 for v in fb.values())
    checks += 3
    # Task 6: stripe event parsing
    ev = [
        {"type": "charge", "amount": 5000, "currency": "usd", "created": 1784500000},
        {"type": "refund", "amount": -1000, "currency": "usd", "created": 1784500000},
        {"type": "payout", "amount": -99999, "currency": "usd", "created": 1784500000},
        {"type": "charge", "amount": 7777, "currency": "eur", "created": 1784500000},
    ]
    sb = {}
    stripe_apply(ev, sb)
    key = day_key(dt.datetime.fromtimestamp(1784500000).date())
    assert sb == {key: {"stripe": 4000}}
    checks += 1
    # Task 7: paddle window rebuild
    pb = {"2026-07-01": {"stripe": 111, "paddle": 99999},
          "2026-06-01": {"paddle": 7777}}
    txns = [
        {"billed_at": "2026-07-01T10:00:00.000Z", "currency_code": "USD",
         "details": {"totals": {"subtotal": "5000"}}},
        {"billed_at": "2026-07-02T12:00:00.000Z", "currency_code": "USD",
         "details": {"totals": {"subtotal": "2000"}}},
        {"billed_at": "2026-07-02T12:00:00.000Z", "currency_code": "EUR",
         "details": {"totals": {"subtotal": "4444"}}},
    ]
    adjs = [
        {"action": "refund", "status": "approved",
         "created_at": "2026-07-02T12:00:00.000Z", "totals": {"subtotal": "500"}},
        {"action": "refund", "status": "pending_approval",
         "created_at": "2026-07-02T12:00:00.000Z", "totals": {"subtotal": "9999"}},
    ]
    paddle_apply(txns, adjs, pb, dt.date(2026, 7, 1))
    assert pb["2026-07-01"] == {"stripe": 111, "paddle": 5000}
    assert pb["2026-07-02"] == {"paddle": 1500}
    assert pb["2026-06-01"] == {"paddle": 7777}  # outside window, untouched
    checks += 3
    # Task 8: cache round-trip and backfill start
    assert backfill_start(dt.date(2026, 7, 27)) == dt.date(2025, 1, 1)
    assert backfill_start(dt.date(2026, 1, 1)) == dt.date(2025, 1, 1)
    import tempfile
    global CACHE_PATH
    old_path = CACHE_PATH
    CACHE_PATH = os.path.join(tempfile.mkdtemp(), "cache.json")
    assert load_cache() == {"buckets": {}, "stripe_last_sync": None,
                            "paddle_backfilled": False}
    save_cache({"buckets": {"2026-07-27": {"stripe": 1}}, "stripe_last_sync": 123,
                "paddle_backfilled": True})
    assert load_cache() == {"buckets": {"2026-07-27": {"stripe": 1}},
                            "stripe_last_sync": 123, "paddle_backfilled": True}
    CACHE_PATH = old_path
    checks += 4
    print(f"selftest: {checks} checks ok")
    return 0


def main():
    global GLYPH
    args = parse_args()
    if args.selftest:
        sys.exit(selftest())  # hermetic: .env must not influence the checks
    _load_dotenv()
    set_host(args.host)
    signal.signal(signal.SIGINT, lambda *a: (clear(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda *a: (clear(), sys.exit(0)))
    if cur_code() == "gbp":
        try:
            upload_asset("pound.png", pound_png())
            GLYPH = True
        except OSError as e:
            print(f"warning: currency glyph upload failed ({e}), "
                  f"showing bare numbers", file=sys.stderr)
    if args.test:
        run_loop(args, fake_buckets(dt.date.today()), None)
        return
    providers = [p for p, k in (("stripe", env("STRIPE_API_KEY")),
                                ("paddle", env("PADDLE_API_KEY"))) if k]
    if not providers:
        print("No API keys found. Create a .env next to app.py containing\n"
              "STRIPE_API_KEY=rk_live_... and/or PADDLE_API_KEY=pdl_live_...\n"
              "(or run with --test for fake data)", file=sys.stderr)
        sys.exit(1)
    try:
        if "stripe" in providers:
            stripe_validate(env("STRIPE_API_KEY"))
        if "paddle" in providers:
            paddle_validate(env("PADDLE_API_KEY"))
    except urllib.error.HTTPError as e:
        print(f"API key rejected ({e.code}) during startup validation. "
              f"Check your .env values.", file=sys.stderr)
        sys.exit(1)
    cache = load_cache()
    print(f"revenue-bar: providers {providers}, backfilling/syncing...", file=sys.stderr)
    run_loop(args, cache["buckets"], make_sync(cache, providers))


if __name__ == "__main__":
    main()
