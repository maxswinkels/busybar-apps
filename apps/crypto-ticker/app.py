"""Crypto Ticker for the BUSY Bar -- live prices on the 72x16 LED matrix.

Cycles through a list of coins, showing each one's logo, symbol, price and
24-hour change: green with a + when it's up over the day, red with a - when
down. A small play/pause glyph sits in the bottom-right corner. On the bar, the
top (START) button pauses/resumes the auto-cycle -- while paused the two LEDs
on the top surface under the button glow amber -- and the wheel steps left/right
through the coins. Prices and logos come from the free CoinGecko API (no key).

    python app.py                        # BUSY Bar over USB (10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
    python app.py --test                 # print prices and exit (no device)

Edit the COINS list below to add your own tokens (CoinGecko id + label).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from PIL import Image

from busylib import AsyncBusyBar, BusyBar, types
from busylib.exceptions import BusyBarAPIError, BusyBarRequestError

APP = "crypto-ticker"

# --- settings ----------------------------------------------------------------
DEFAULT_HOST = "10.0.4.20"   # USB address; use --host for Wi-Fi or the emulator
CURRENCY = "usd"             # any CoinGecko fiat code (usd, eur, gbp, ...)
CARD_SECONDS = 4.0           # seconds each coin is shown when auto-cycling
REFRESH_SECONDS = 60.0       # seconds between CoinGecko price refreshes

# Coins to show: (CoinGecko id, short label). Add your own tokens here -- the
# id is the slug in the coin's coingecko.com URL.
COINS = [
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("chainlink", "LINK"),
    ("milady-cult-coin", "CULT"),
]

# --- constants ---------------------------------------------------------------
FRONT = types.DisplayName.FRONT
# The markets endpoint returns price, 24h change and a logo URL in one call.
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

FRAME_INTERVAL = 0.5        # seconds between redraws
LOGO_SIZE = 16              # coin logo is drawn 16x16 on the left of the front
TEXT_X = 18                 # where text starts when a logo is shown

GREEN = "#33DD55FF"
RED = "#FF3030FF"
WHITE = "#FFFFFFFF"
AMBER = "#FFAA00FF"
TRANSPARENT = "#00000000"
BLANK_LOGO = "blank.png"      # transparent tile parked under the "logo" id


@dataclass
class Coin:
    id: str
    symbol: str
    price: float | None = None
    change: float | None = None  # 24h percent change
    image_url: str | None = None
    logo_file: str | None = None  # uploaded asset filename once ready
    logo_tried: bool = False


def make_coins() -> list[Coin]:
    return [Coin(id=cid, symbol=sym.upper()) for cid, sym in COINS]


def connect(host: str, token: str | None = None) -> BusyBar:
    """Open a BusyBar client. Over USB no token is needed; Wi-Fi uses the PIN."""
    return BusyBar(host, token=token) if token else BusyBar(host)


def fetch_prices(coins: list[Coin], currency: str, timeout: float = 10.0) -> None:
    """Update each coin's price, 24h change and logo URL in place from CoinGecko."""
    by_id = {c.id: c for c in coins}
    query = urllib.parse.urlencode({
        "vs_currency": currency,
        "ids": ",".join(coins_id for coins_id in by_id),
        "price_change_percentage": "24h",
    })
    url = f"{COINGECKO_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "busybar-crypto-ticker"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rows = json.load(resp)
    for row in rows:
        coin = by_id.get(row.get("id"))
        if not coin:
            continue
        coin.price = row.get("current_price")
        coin.change = row.get("price_change_percentage_24h")
        coin.image_url = row.get("image")


def _make_logo_png(raw: bytes) -> bytes:
    """Resize a downloaded coin logo onto a 16x16 black RGB tile as PNG bytes."""
    src = Image.open(io.BytesIO(raw)).convert("RGBA").resize(
        (LOGO_SIZE, LOGO_SIZE), Image.LANCZOS
    )
    tile = Image.new("RGB", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0))
    tile.paste(src, (0, 0), src)  # use alpha as mask so transparency reads black
    out = io.BytesIO()
    tile.save(out, format="PNG")
    return out.getvalue()


def ensure_blank_logo(bb) -> None:
    """Upload a transparent 16x16 tile so the 'logo' id is always drawable."""
    tile = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0, 0))
    out = io.BytesIO()
    tile.save(out, format="PNG")
    bb.assets_upload(APP, BLANK_LOGO, out.getvalue())


def ensure_logo(bb, coin: Coin, timeout: float = 10.0) -> None:
    """Download a coin's logo once and upload it as a device asset."""
    if coin.logo_tried or not coin.image_url:
        return
    coin.logo_tried = True
    filename = f"{coin.id}.png"
    try:
        req = urllib.request.Request(
            coin.image_url, headers={"User-Agent": "busybar-crypto-ticker"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        bb.assets_upload(APP, filename, _make_logo_png(raw))
        coin.logo_file = filename
    except (urllib.error.URLError, TimeoutError, OSError,
            BusyBarRequestError, BusyBarAPIError) as err:
        print(f"  (no logo for {coin.symbol}: {err})")


def fmt_price(price: float | None, currency: str) -> str:
    """Human price with sensible precision; '$' prefix for USD."""
    if price is None:
        return "--"
    prefix = "$" if currency == "usd" else ""
    if price >= 1000:
        body = f"{price:,.0f}"
    elif price >= 1:
        body = f"{price:,.2f}"
    elif price >= 0.01:
        body = f"{price:.4f}"
    else:
        body = f"{price:.6f}"
    return prefix + body


def _right_x(text: str) -> int:
    """Left x so `text` (small font, ~4px/char) is right-aligned on the front."""
    return max(0, 72 - len(text) * 4 - 1)


def state_indicator(paused: bool) -> list[types.DisplayElement]:
    """Bottom-right status glyph: amber pause bars, or a green play triangle.

    Always returns the same three ids (st1/st2/st3) so a redraw replaces the
    previous glyph in place -- no display_clear, so the built-in UI never
    flashes through. The unused third bar is parked transparent when paused.
    """
    if paused:
        return [
            types.RectangleElement(id="st1", x=64, y=10, width=2, height=5,
                                   fill="solid", fill_colors=[AMBER],
                                   border_width=0, display=FRONT),
            types.RectangleElement(id="st2", x=68, y=10, width=2, height=5,
                                   fill="solid", fill_colors=[AMBER],
                                   border_width=0, display=FRONT),
            types.RectangleElement(id="st3", x=64, y=10, width=1, height=1,
                                   fill="solid", fill_colors=[TRANSPARENT],
                                   border_width=0, display=FRONT),
        ]
    return [
        types.RectangleElement(id="st1", x=64, y=10, width=2, height=5,
                               fill="solid", fill_colors=[GREEN],
                               border_width=0, display=FRONT),
        types.RectangleElement(id="st2", x=66, y=11, width=2, height=3,
                               fill="solid", fill_colors=[GREEN],
                               border_width=0, display=FRONT),
        types.RectangleElement(id="st3", x=68, y=12, width=2, height=1,
                               fill="solid", fill_colors=[GREEN],
                               border_width=0, display=FRONT),
    ]


def coin_front(coin: Coin, currency: str, paused: bool) -> list[types.DisplayElement]:
    """Logo left; symbol + change on top, price below, status glyph bottom-right.

    Everything stays within the top rows so nothing clips at the bottom edge.
    """
    up = coin.change is not None and coin.change >= 0
    color = GREEN if up else RED

    price = fmt_price(coin.price, currency)
    if coin.change is None:
        change = "--"
    else:
        change = f"{'+' if up else '-'}{abs(coin.change):.1f}%"

    left = TEXT_X if coin.logo_file else 1
    elements: list[types.DisplayElement] = []
    # Always draw the 'logo' id (blank tile when we have none) so a redraw
    # replaces the previous coin's logo instead of ghosting it.
    elements.append(types.ImageElement(
        id="logo", x=0, y=0, path=coin.logo_file or BLANK_LOGO, display=FRONT,
    ))

    elements.append(types.TextElement(
        id="sym", x=left, y=1, text=coin.symbol,
        font="small", color=WHITE, display=FRONT,
    ))
    elements.append(types.TextElement(
        id="change", x=_right_x(change), y=1, text=change,
        font="small", color=color, display=FRONT,
    ))
    elements.append(types.TextElement(
        id="price", x=left, y=8, text=price,
        font="small", color=color, display=FRONT,
    ))
    elements.extend(state_indicator(paused))
    return elements


class PricePoller:
    """Refresh prices on a background thread so the display stays responsive."""

    def __init__(self, coins: list[Coin], currency: str, interval: float) -> None:
        self._coins = coins
        self._currency = currency
        self._interval = interval
        self._lock = threading.Lock()
        self._version = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fetch_prices(self._coins, self._currency)
                with self._lock:
                    self._version += 1
            except (urllib.error.URLError, TimeoutError, ValueError) as err:
                print(f"  (price fetch failed: {err})")
            self._stop.wait(self._interval)


def push(bb, elements: list[types.DisplayElement], led_color: str | None = None) -> None:
    """Draw the front elements; ``led_color`` lights the top-surface LEDs."""
    bb.display_draw(types.DisplayElements(
        application_name=APP, led_notification_color=led_color, elements=elements,
    ))


class InputListener:
    """Stream button/wheel events off a background thread via the async client.

    Events are decoded protobuf dicts, e.g. ``{'encoder_event': {'delta': 1}}``
    for a wheel detent or ``{'button_event': {'button': 'START'}}`` for a press
    (a release carries ``'action': 'RELEASE'``).
    """

    def __init__(self, address: str, token: str | None) -> None:
        self._address = address
        self._token = token
        self._queue: queue.Queue[dict] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll(self) -> list[dict]:
        """Drain and return any input events seen since the last call."""
        events: list[dict] = []
        try:
            while True:
                events.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return events

    def _run(self) -> None:
        try:
            asyncio.run(self._listen())
        except Exception as err:  # pragma: no cover - best-effort input
            print(f"  (bar controls unavailable: {err})")

    async def _listen(self) -> None:
        kwargs: dict = {"token": self._token} if self._token else {}
        bb = AsyncBusyBar(self._address, **kwargs)
        try:
            async for msg in bb.stream_status_ws():
                if self._stop.is_set():
                    break
                if not isinstance(msg, dict):
                    continue
                for upd in msg.get("updates", []):
                    event = upd.get("input")
                    if event:
                        self._queue.put(event)
        finally:
            await bb.aclose()


def run_test() -> None:
    """Fetch and print prices once, without touching the bar."""
    coins = make_coins()
    fetch_prices(coins, CURRENCY)
    for coin in coins:
        change = "n/a" if coin.change is None else f"{coin.change:+.2f}%"
        print(f"{coin.symbol:<6} {fmt_price(coin.price, CURRENCY):>12}  {change}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BUSY Bar crypto ticker (front only)")
    parser.add_argument(
        "--host", default=DEFAULT_HOST,
        help=f"BUSY Bar host ip[:port] (default {DEFAULT_HOST}, USB; emulator 127.0.0.1:8080)",
    )
    parser.add_argument("--token", default=None, help="Wi-Fi access-key PIN, if required")
    parser.add_argument("--card-seconds", type=float, default=CARD_SECONDS,
                        help=f"Seconds each coin is shown (default {CARD_SECONDS:g})")
    parser.add_argument("--test", action="store_true",
                        help="Print fetched prices and exit (no device)")
    parser.add_argument("--once", action="store_true",
                        help="Draw a single coin frame and exit")
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    coins = make_coins()
    if not coins:
        print("No coins configured (edit the COINS list in app.py).")
        return

    poller = PricePoller(coins, CURRENCY, REFRESH_SECONDS)
    poller.start()
    controls = InputListener(args.host, args.token)
    controls.start()

    with connect(args.host, args.token) as bb:
        version = bb.version().version
        print(f"Connected to BUSY Bar (firmware {version or 'unknown'})")
        bb.display_clear()

        # Wait briefly for the first price refresh so we don't draw blanks.
        deadline = time.monotonic() + 10.0
        while poller.version == 0 and time.monotonic() < deadline:
            time.sleep(0.1)

        # Fetch and upload each coin's logo once, now that we have URLs.
        ensure_blank_logo(bb)
        for coin in coins:
            ensure_logo(bb, coin)

        if args.once:
            push(bb, coin_front(coins[0], CURRENCY, False))
            poller.stop()
            controls.stop()
            return

        card = 0
        last_card = time.monotonic()
        last_version = -1
        paused = False
        n = len(coins)
        try:
            while True:
                now = time.monotonic()
                changed = False

                # Bar controls: START toggles pause; the wheel steps coins.
                for event in controls.poll():
                    if "encoder_event" in event:
                        delta = int(event["encoder_event"].get("delta", 0))
                        if delta:
                            card = (card + delta) % n
                            paused = True
                            last_card = now
                            changed = True
                    elif "button_event" in event:
                        be = event["button_event"]
                        # A press has no 'action'; a release sets it to RELEASE.
                        if be.get("button") == "START" and be.get("action") != "RELEASE":
                            paused = not paused
                            last_card = now
                            changed = True
                            print("Paused." if paused else "Resumed.")

                if not paused and now - last_card >= args.card_seconds:
                    card = (card + 1) % n
                    last_card = now
                    changed = True
                if poller.version != last_version:
                    last_version = poller.version
                    changed = True

                if changed:
                    # Every draw sends the same id set, so redrawing replaces
                    # each element in place. We deliberately do NOT clear first:
                    # a delete-then-draw would flash the bar's built-in UI
                    # through the gap between the two calls. While paused the
                    # top-surface LEDs under the button glow amber to match.
                    try:
                        push(bb, coin_front(coins[card], CURRENCY, paused),
                             AMBER if paused else None)
                    except (BusyBarRequestError, BusyBarAPIError) as err:
                        print(f"  (skipped frame: {err})")
                time.sleep(FRAME_INTERVAL)
        except KeyboardInterrupt:
            pass
        finally:
            poller.stop()
            controls.stop()
            try:
                bb.display_clear()
                bb.assets_delete(APP)
            except (BusyBarRequestError, BusyBarAPIError):
                pass
            print("\nDisplay cleared. Bye.")


if __name__ == "__main__":
    main()
