#!/usr/bin/env python3
"""RSS scroller for BUSY Bar.

Downloads an RSS/Atom feed locally on the PC, extracts headlines, builds a
single scrolling ticker, and pushes it to BUSY Bar via the HTTP API.

Examples:
    python3 rss_scroller.py
    python3 rss_scroller.py --feed https://www.ansa.it/sito/ansait_rss.xml
    python3 rss_scroller.py --host 127.0.0.1:8080
    python3 rss_scroller.py --refresh 60 --max-items 20 --scroll-rate 420

The BUSY Bar is expected at 10.0.4.20 over USB by default.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

APP = "rss-scroller"

DEFAULT_HOST = "10.0.4.20"
DEFAULT_FEED = "https://www.ansa.it/sito/ansait_rss.xml"
DEFAULT_REFRESH_SEC = 120
DEFAULT_MAX_ITEMS = 15
DEFAULT_SCROLL_RATE = 2000  # pixels per minute
DEFAULT_COLOR = "#FFFFFFFF"
DEFAULT_SEPARATOR = "   |   "

DISPLAY_WIDTH = 72
DISPLAY_Y = 5
DISPLAY_FONT = "normal"

SCROLL_START_DELAY_MS = 500
SCROLL_REPEAT_DELAY_MS = 1000

USER_AGENT = "busy-bar-rss-scroller/1.0"


def parse_args():
    p = argparse.ArgumentParser(description="RSS/Atom scroller for BUSY Bar")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"BUSY Bar host (default: {DEFAULT_HOST})")
    p.add_argument("--feed", default=DEFAULT_FEED,
                   help="RSS or Atom feed URL")
    p.add_argument("--refresh", type=int, default=DEFAULT_REFRESH_SEC,
                   help=f"RSS refresh interval in seconds (default: {DEFAULT_REFRESH_SEC})")
    p.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS,
                   help=f"maximum number of headlines (default: {DEFAULT_MAX_ITEMS})")
    p.add_argument("--scroll-rate", type=int, default=DEFAULT_SCROLL_RATE,
                   help=f"BUSY Bar scroll rate in pixels/minute (default: {DEFAULT_SCROLL_RATE})")
    p.add_argument("--color", default=DEFAULT_COLOR,
                   help=f"text color as #RRGGBBAA (default: {DEFAULT_COLOR})")
    p.add_argument("--separator", default=DEFAULT_SEPARATOR,
                   help="separator between headlines")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="HTTP timeout for RSS download and BUSY Bar calls")
    return p.parse_args()


def normalize_host(host):
    host = host.strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    return "http://" + host


def clean_text(value):
    """Decode entities, strip HTML-ish tags, and normalize whitespace."""
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _local_name(tag):
    """Return an XML tag without namespace."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _first_child_text(node, names):
    wanted = set(names)
    for child in node.iter():
        if _local_name(child.tag) in wanted:
            text = clean_text("".join(child.itertext()))
            if text:
                return text
    return ""


def fetch_feed(url, timeout=10.0):
    """Download and parse an RSS 2.0 / RSS 1.0 / Atom feed.

    Returns a list of dicts with title/link.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()

    root = ET.fromstring(payload)
    root_name = _local_name(root.tag).lower()
    items = []

    if root_name == "feed":
        # Atom
        for entry in root:
            if _local_name(entry.tag).lower() != "entry":
                continue
            title = _first_child_text(entry, {"title"})
            link = ""
            for child in entry:
                if _local_name(child.tag).lower() == "link":
                    rel = child.attrib.get("rel", "alternate")
                    href = child.attrib.get("href", "")
                    if href and rel in ("alternate", ""):
                        link = href
                        break
            if title:
                items.append({"title": title, "link": link})
    else:
        # RSS 2.0 / RDF-style feeds
        for node in root.iter():
            if _local_name(node.tag).lower() != "item":
                continue
            title = _first_child_text(node, {"title"})
            link = _first_child_text(node, {"link"})
            if title:
                items.append({"title": title, "link": link})

    return items


def dedupe_items(items):
    seen = set()
    out = []
    for item in items:
        key = (item.get("link") or item.get("title") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_ticker(items, max_items, separator):
    titles = []
    for item in items[:max_items]:
        title = clean_text(item.get("title", ""))
        if title:
            titles.append(title)
    return separator.join(titles)


def _request(base, path, *, method="GET", data=None, headers=None, timeout=5):
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers=headers or {},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def draw_ticker(base, ticker, scroll_rate, color, timeout):
    body = {
        "application_name": APP,
        "priority": 30,
        "elements": [
            {
                "id": "rss",
                "timeout": 0,
                "type": "text",
                "text": ticker,
                "x": 0,
                "y": DISPLAY_Y,
                "font": DISPLAY_FONT,
                "color": color,
                "width": DISPLAY_WIDTH,
                "scroll_rate": scroll_rate,
                "scroll_start_delay": SCROLL_START_DELAY_MS,
                "scroll_repeat_delay": SCROLL_REPEAT_DELAY_MS,
            }
        ],
    }

    data = json.dumps(body).encode("utf-8")
    try:
        with _request(
            base,
            "/api/display/draw",
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        ):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print("[busy] display occupied by a higher-priority app; will retry")
            return False
        detail = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"BUSY Bar HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach BUSY Bar at {base}: {e.reason}") from e


def clear_display(base, timeout):
    query = urllib.parse.urlencode({"application_name": APP})
    try:
        with _request(
            base,
            "/api/display/draw?" + query,
            method="DELETE",
            timeout=timeout,
        ):
            pass
    except Exception:
        pass


def main():
    args = parse_args()
    base = normalize_host(args.host)
    refresh_sec = max(5, args.refresh)
    max_items = max(1, args.max_items)

    print(f"rss_scroller -> {base}")
    print(f"feed: {args.feed}")
    print(f"refresh: {refresh_sec}s | max items: {max_items} | scroll rate: {args.scroll_rate}")
    print("Ctrl-C to stop.")

    last_items = []
    last_ticker = None

    try:
        while True:
            cycle_started = time.monotonic()

            try:
                fresh_items = fetch_feed(args.feed, timeout=args.timeout)
                fresh_items = dedupe_items(fresh_items)

                if fresh_items:
                    last_items = fresh_items
                    print(f"[rss] fetched {len(fresh_items)} item(s)")
                else:
                    print("[rss] feed parsed but contained no usable items")

            except Exception as e:
                print(f"[rss] fetch failed: {e}")
                if last_items:
                    print("[rss] keeping last valid headlines")

            ticker = build_ticker(last_items, max_items, args.separator)

            if ticker:
                # Redraw only when content changes. The BUSY Bar handles
                # continuous scrolling on-device.
                if ticker != last_ticker:
                    try:
                        drawn = draw_ticker(
                            base,
                            ticker,
                            args.scroll_rate,
                            args.color,
                            args.timeout,
                        )
                        if drawn:
                            last_ticker = ticker
                            print("[busy] ticker updated")
                    except Exception as e:
                        print(f"[busy] draw failed: {e}")
                else:
                    print("[busy] no headline changes")
            else:
                print("[rss] nothing to display yet")

            elapsed = time.monotonic() - cycle_started
            time.sleep(max(1.0, refresh_sec - elapsed))

    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        clear_display(base, args.timeout)


if __name__ == "__main__":
    main()
