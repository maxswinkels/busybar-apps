#!/usr/bin/env python3
"""Busy Defaults: activates one of the BUSY Bar's built-in modes from the command line.

    python app.py --mode busy                 # the BUSY profile, as configured on the bar
    python app.py --mode on_air               # the CUSTOM profile, themed on_air
    python app.py --mode keep_out             # the CUSTOM profile, themed keep_out
    python app.py --mode off                  # stop the running session
    python app.py --list                      # show the stored profiles
    python app.py --mode busy --host 127.0.0.1:8080  # emulator or a Wi-Fi bar

--mode takes either a slot or a theme: 'busy' runs the BUSY profile with its own
stored theme, 'off' stops the session, and any theme name runs the CUSTOM profile
with that theme. Themes are checked against the set stock firmware ships, so a
typo fails up front instead of silently leaving the bar on its default. Draws
nothing itself -- it hands the profile to the timer and the bar renders its own
built-in mode.

The process then stays alive and releases the mode when you stop it (Ctrl-C or
SIGTERM), but only if the mode it started is still the one running: if you switch
modes on the bar, or another tool takes over, it leaves that session alone. It
never takes the display, so anything else can draw over it in the meantime.
"""
import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.request

APP = "busy-defaults"

KNOWN_THEMES = [
    "busy",
    "keep_out",
    "dnd",
    "meeting",
    "on_call",
    "lunch",
    "back_soon",
    "booked",
    "flow",
    "chill_time",
    "on_air",
    "coding",
    "low_social_battery"
]

# --- BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs) ----------


def request(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def get_profile(base, slot):
    """GET /api/busy/profiles/{slot} -> the profile stored in that slot."""
    return request(base, f"/api/busy/profiles/{slot}")


def set_snapshot(base, snapshot):
    """PUT /api/busy/snapshot -> run the timer from this state."""
    body = {"snapshot": snapshot, "snapshot_timestamp_ms": int(time.time() * 1000)}
    request(base, "/api/busy/snapshot", method="PUT", body=body)


# --- app -------------------------------------------------------------------

def resolve(mode):
    """Map --mode onto (slot, theme override). None theme = keep the stored one."""
    if mode == "busy":
        return "busy", None
    return "custom", mode


def snapshot_for(profile, theme=None):
    """Turn a stored profile into a snapshot that starts it from the top."""
    settings = profile["timer_settings"]
    kind = settings["type"]
    snap = {"type": kind, "card_id": profile["id"], "is_paused": False}

    if kind == "SIMPLE":
        snap["time_left_ms"] = settings["total_time_ms"]
    elif kind == "INTERVAL":
        snap["current_interval"] = 0
        snap["current_interval_time_total_ms"] = settings["interval_work_ms"]
        snap["current_interval_time_left_ms"] = settings["interval_work_ms"]
        snap["interval_settings"] = settings
    elif kind != "INFINITE":
        raise SystemExit(f"unsupported timer type in profile: {kind}")

    # busy_bar_settings rides along with every snapshot; theme lives in there.
    bar = dict(profile["busy_bar_settings"])
    if theme:
        bar["theme"] = theme
    snap["busy_bar_settings"] = bar
    return snap


def stop_snapshot(base):
    """NOT_STARTED still needs busy_bar_settings; reuse whatever is running."""
    bar = request(base, "/api/busy/snapshot")["snapshot"]["busy_bar_settings"]
    return {"type": "NOT_STARTED", "busy_bar_settings": bar}


def is_still_ours(current, started):
    """Does the bar still run the mode we started? Times advance, identity doesn't."""
    return (current.get("type") == started["type"]
            and current.get("card_id") == started.get("card_id")
            and current.get("busy_bar_settings", {}).get("theme")
            == started["busy_bar_settings"]["theme"])


def hold(base, started):
    """Idle until stopped, then release the mode -- if it is still ours."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            time.sleep(1.0)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        try:
            current = request(base, "/api/busy/snapshot")["snapshot"]
            if is_still_ours(current, started):
                set_snapshot(base, {"type": "NOT_STARTED",
                                    "busy_bar_settings": current["busy_bar_settings"]})
                print("\nreleased.")
            else:
                print("\nmode changed elsewhere; left it running.")
        except (urllib.error.URLError, KeyError, ValueError) as e:
            print(f"\ncould not release the mode: {e}")


def describe(profile, theme=None):
    settings = profile["timer_settings"]
    detail = settings["type"].lower()
    if settings["type"] == "SIMPLE":
        detail += f" {settings['total_time_ms'] // 60000}min"
    elif settings["type"] == "INTERVAL":
        detail += (f" {settings['interval_work_ms'] // 60000}/"
                   f"{settings['interval_rest_ms'] // 60000}min "
                   f"x{settings['interval_work_cycles_count']}")
    return (f"{profile['title']!r}  timer={detail}  "
            f"theme={theme or profile['busy_bar_settings']['theme']}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="themes: " + ", ".join(KNOWN_THEMES),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="10.0.4.20",
                   help="bar address; USB default, emulator: 127.0.0.1:8080")
    p.add_argument("--mode", default="busy", choices=KNOWN_THEMES + ["off"],
                   help="'busy' for the BUSY profile, 'off' to stop, or a theme "
                        "name to run the CUSTOM profile with that theme")
    p.add_argument("--list", action="store_true",
                   help="print the stored profiles and exit")
    args = p.parse_args()

    base = "http://" + args.host.replace("http://", "").rstrip("/")

    try:
        if args.list:
            for slot in ("busy", "custom"):
                print(f"{slot:7} {describe(get_profile(base, slot))}")
            return
        if not args.mode:
            p.error("one of --mode or --list is required")

        if args.mode == "off":
            set_snapshot(base, stop_snapshot(base))
            print(f"{APP} → {base}  session stopped")
            return

        slot, theme = resolve(args.mode)
        profile = get_profile(base, slot)
        snap = snapshot_for(profile, theme)
        set_snapshot(base, snap)
        print(f"{APP} → {base}  started {slot}: {describe(profile, theme)}"
              "  (Ctrl-C to release)")
        hold(base, snap)
    except urllib.error.HTTPError as e:
        sys.exit(f"{e.code} {e.reason}: {e.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {base}: {e.reason}")


if __name__ == "__main__":
    main()
