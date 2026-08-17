#!/usr/bin/env python3
"""Adversarial probe: fire the edge cases at whatever host it is given.

Run once through the busyrec stub and once through --upstream against the real
emulator, then diff the recorded status codes. Any difference is stub drift.
"""
import json
import sys
import urllib.error
import urllib.request

APP = "conform"


def host():
    return sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "10.0.4.20"


BASE = "http://" + host()


def post(path, body, ctype="application/json"):
    data = json.dumps(body).encode() if ctype == "application/json" else body
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def el(eid, **kw):
    base = {"id": eid, "type": "text", "text": "x", "x": 0, "y": 0, "color": "#FFFFFFFF"}
    base.update(kw)
    return base


def draw(elements, app=APP, **extra):
    body = {"application_name": app, "elements": elements}
    body.update(extra)
    return post("/api/display/draw", body)


cases = []
cases.append(("valid draw", draw([el("a")])))
cases.append(("missing id", draw([{"type": "text", "text": "x", "x": 0, "y": 0}])))
cases.append(("bad id chars", draw([el("has space")])))
cases.append(("6-digit colour", draw([el("b", color="#FFFFFF")])))
cases.append(("0x colour", draw([el("c", color="0xFFFFFFFF")])))
cases.append(("bad border_color", draw([el("d", type="rectangle", width=4, height=4,
                                           border_color="#FFF")])))
cases.append(("bad fill_colors", draw([el("e", type="rectangle", width=4, height=4,
                                          fill="solid", fill_colors=["#FFFFFF"])])))
cases.append(("empty elements", draw([])))
cases.append(("priority 0", draw([el("f")], priority=0)))
cases.append(("priority 101", draw([el("f")], priority=101)))
cases.append(("no application_name", post("/api/display/draw", {"elements": [el("g")]})))
cases.append(("app_id alias", post("/api/display/draw",
                                   {"app_id": APP, "elements": [el("h")]})))
cases.append(("bad led colour", draw([el("i")], led_notification_color="#FFFFFF")))

# The accumulating cap: fresh ids every draw, until the stored set passes 100.
cap_hit = None
for i in range(140):
    code = draw([el("grow%d" % i)])
    if code != 200:
        cap_hit = (i, code)
        break
cases.append(("element cap (draw #%s)" % (cap_hit[0] if cap_hit else "none"),
              cap_hit[1] if cap_hit else 200))

# Release this app's accumulated set, or the cap above masks every draw below.
urllib.request.urlopen(urllib.request.Request(
    BASE + "/api/display/draw?application_name=" + APP, method="DELETE"), timeout=5)

# Priority arbitration: a second app with lower and then higher priority.
post("/api/display/draw", {"application_name": "other", "elements": [el("z")], "priority": 80})
cases.append(("lower priority", draw([el("k")], priority=50)))
cases.append(("equal priority, other app", draw([el("k")], priority=80)))
cases.append(("higher priority", draw([el("k")], priority=90)))
cases.append(("same app, equal priority", draw([el("k")], priority=90)))

print(json.dumps([{"case": c, "status": s} for c, s in cases]))
