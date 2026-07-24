#!/usr/bin/env python3
"""Clock widget: big time, refreshed every second.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
"""
import json
import sys
import time
import urllib.error
import urllib.request

APP = "demo.clock"

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

def draw(elements, **extra):
    body = {"application_name": APP, "elements": elements, **extra}
    req = urllib.request.Request(BASE + "/api/display/draw",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5):
        pass

def text(txt, x=0, y=0, font="normal", color="0xFFFFFFFF", **kw):
    return {"type": "text", "text": str(txt), "x": x, "y": y, "font": font, "color": color, **kw}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def tick():
    hhmm = time.strftime("%H:%M:%S", time.localtime())
    try:
        # align lets the device place elements without measuring text width.
        draw([text(hhmm, x=36, y=15, font="extra_large", align="bottom_mid")])
    except urllib.error.HTTPError as e:
        # A higher-priority app owns the screen: keep ticking and retry next second.
        if e.code != 409:
            raise
        print("display busy (409), retrying...")


if __name__ == "__main__":
    print(f"clock → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            tick()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code} — {e.read().decode('utf-8', 'ignore')}")
    except urllib.error.URLError as e:
        sys.exit(f"error: cannot reach {BASE} — {e.reason}")
