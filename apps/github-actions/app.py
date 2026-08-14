#!/usr/bin/env python3
"""GitHub Actions: live workflow runs, with a progress bar and a red hold on the failed job.

    export GITHUB_TOKEN=ghp_xxxx              # optional for public repos, but see rate limits
    python3 app.py --repo owner/name          # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --repo a/b --repo a/c      # watch several repos
    python3 app.py --host 127.0.0.1:8080      # emulator or a Wi-Fi bar
    python3 app.py --demo                     # scripted cycle, no network
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APP = "github-actions"
W, H = 72, 16
API = "https://api.github.com"

WHITE = "#FFFFFFFF"
BLUE = "#3B82F6FF"
GREEN = "#22C55EFF"
RED = "#EF4444FF"
AMBER = "#F59E0BFF"
TRACK = "#2A2A2AFF"


def parse_args():
    p = argparse.ArgumentParser(description="GitHub Actions monitor for BUSY Bar")
    p.add_argument("--host", default="10.0.4.20")
    p.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                   help="repository to watch, repeatable (or set GITHUB_REPOS)")
    p.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                   help="GitHub token (or set GITHUB_TOKEN)")
    p.add_argument("--active-interval", type=int, default=15,
                   help="seconds between polls while a run is active")
    p.add_argument("--idle-interval", type=int, default=30,
                   help="seconds between polls while nothing is running")
    p.add_argument("--success-hold", type=int, default=6,
                   help="seconds to celebrate a green run")
    p.add_argument("--failure-hold", type=int, default=300,
                   help="seconds to hold the screen on a red run")
    p.add_argument("--no-sound", action="store_true")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    p.add_argument("--demo", action="store_true", help="scripted cycle on fake data")
    p.add_argument("--tour", action="store_true",
                   help="walk every screen in turn, on fake data")
    return p.parse_args()


# ---------------------------------------------------------------------------
# BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs)
# ---------------------------------------------------------------------------

def _base(host):
    return "http://" + host.replace("http://", "").replace("https://", "").rstrip("/")


def _draw(host, elements, priority=30, led=None):
    body = {"application_name": APP, "priority": priority, "elements": elements}
    if led:
        body["led_notification_color"] = led
    req = urllib.request.Request(
        _base(host) + "/api/display/draw", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"},
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


def _play(host, stock_path):
    body = {"application_name": APP, "stock_path": stock_path}
    req = urllib.request.Request(
        _base(host) + "/api/audio/play", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.getcode()
    except urllib.error.HTTPError as e:
        return e.code


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    """Raised once the hourly budget is gone, carrying the epoch it resets at."""

    def __init__(self, until):
        self.until = until
        super().__init__("rate limited until " +
                         time.strftime("%H:%M:%S", time.localtime(until)))


class GitHub:
    """Thin API client that caches by ETag, so polls that change nothing are free.

    GitHub does not count a 304 Not Modified against the rate limit, which is what
    lets this app poll every 15 seconds without eating into the hourly budget.
    A run in flight changes on every poll though, so those are real requests: an
    unauthenticated 60/hour budget will not survive one, hence the token warning.
    """

    def __init__(self, token):
        self.token = token
        self.etags = {}
        self.cache = {}
        self.blocked_until = 0.0

    def get(self, path):
        url = API + path
        # Out of budget: serve the last known answer instead of hammering a
        # door that is going to stay shut until the window rolls over.
        if time.time() < self.blocked_until:
            return self.cache.get(url)
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "busybar-github-actions/1.0",
        })
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        if url in self.etags:
            req.add_header("If-None-Match", self.etags[url])
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode("utf-8", "ignore"))
                if r.headers.get("ETag"):
                    self.etags[url] = r.headers["ETag"]
                self.cache[url] = body
                return body
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return self.cache.get(url)
            if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                reset = e.headers.get("x-ratelimit-reset")
                try:
                    self.blocked_until = float(reset)
                except (TypeError, ValueError):
                    self.blocked_until = time.time() + 60
                raise RateLimited(self.blocked_until)
            raise


def _ts(value):
    """Parse a GitHub ISO-8601 timestamp into epoch seconds."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _now():
    return datetime.now(timezone.utc).timestamp()


def median(values):
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def fetch_runs(gh, repo):
    data = gh.get(f"/repos/{repo}/actions/runs?per_page=25") or {}
    return data.get("workflow_runs") or []


def typical_duration(runs, workflow_id):
    """Median wall-clock of the last completed runs of this workflow, for an ETA."""
    durations = []
    for run in runs:
        if run.get("workflow_id") != workflow_id or run.get("status") != "completed":
            continue
        start, end = _ts(run.get("run_started_at")), _ts(run.get("updated_at"))
        if start and end and end > start:
            durations.append(end - start)
    return median(durations[:10])


def progress(run, runs, jobs):
    """Blend two weak signals into one bar: finished jobs, and elapsed vs typical."""
    fractions = []
    if jobs:
        done = sum(1 for j in jobs if j.get("status") == "completed")
        fractions.append(done / float(len(jobs)))
    start = _ts(run.get("run_started_at")) or _ts(run.get("created_at"))
    typical = typical_duration(runs, run.get("workflow_id"))
    if start and typical:
        fractions.append((_now() - start) / typical)
    if not fractions:
        return 0.15
    # Never let it sit at 100%: a full bar on an unfinished run reads as a hang.
    return max(0.03, min(0.95, max(fractions)))


def fmt_clock(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Per-glyph advance widths for ASCII 32..126, taken from the device font atlas.
# A flat estimate is too coarse: it scrolls names that would sit centred just fine.
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


def _text(eid, txt, y, font, color):
    el = {"id": eid, "type": "text", "text": txt, "y": y, "font": font, "color": color}
    if fits(txt, font):
        el.update({"x": W // 2, "align": "top_mid"})
    else:
        el.update({"x": 0, "align": "top_left", "width": W,
                   "scroll_rate": 600, "scroll_start_delay": 800, "scroll_repeat_delay": 1200})
    return el


def park(eid, kind="text"):
    """Move an element out of view.

    The firmware locks an id to the type it was first drawn with: re-sending an
    existing rectangle id as a text element returns 400 on hardware, even though
    the recorder accepts it. So a parked rectangle has to stay a rectangle.
    """
    if kind == "rect":
        return {"id": eid, "type": "rectangle", "x": -400, "y": 0, "width": 1,
                "height": 1, "fill": "solid", "fill_colors": ["#00000000"],
                "border_width": 0}
    return {"id": eid, "type": "text", "text": " ", "x": -400, "y": 0,
            "font": "tiny", "color": "#00000000"}


CONFETTI = 8


class Screen:
    """Owns a fixed id set (t1, t2, track, bar, c0..c7) and pushes only what changed."""

    def __init__(self, host):
        self.host = host
        self.priority = 0
        self.sent = {}

    def _send(self, elements, priority, led=None):
        # The firmware refuses a lower-priority draw even from the current owner,
        # so stepping down from an alert means releasing the screen first.
        if priority < self.priority:
            _clear(self.host)
            self.sent.clear()
        self.priority = priority

        changed = [el for el in elements if self.sent.get(el["id"]) != el]
        if not changed and not led:
            return True
        status = _draw(self.host, changed or elements, priority=priority, led=led)
        if status == 409:
            return False
        if status >= 400:
            print(f"draw failed: HTTP {status}", file=sys.stderr)
            return False
        for el in changed:
            self.sent[el["id"]] = el
        return True

    def release(self):
        if self.sent or self.priority:
            _clear(self.host)
            self.sent.clear()
            self.priority = 0

    def show(self, top, bottom, bottom_color, fraction=None, bar_color=BLUE,
             priority=30, led=None, confetti=0.0):
        elements = [
            _text("t1", top, 0, "small", WHITE),
            _text("t2", bottom, 8, "tiny", bottom_color),
        ]
        if fraction is None:
            elements += [park("track", "rect"), park("bar", "rect")]
        else:
            filled = max(1, int(round(W * max(0.0, min(1.0, fraction)))))
            # border_width must be 0: rectangles default to a 1px white border,
            # which on a 2px-high bar swallows the fill entirely.
            elements += [
                {"id": "track", "type": "rectangle", "x": 0, "y": 14, "width": W,
                 "height": 2, "fill": "solid", "fill_colors": [TRACK], "border_width": 0},
                {"id": "bar", "type": "rectangle", "x": 0, "y": 14, "width": filled,
                 "height": 2, "fill": "solid", "fill_colors": [bar_color], "border_width": 0},
            ]
        elements += self._confetti(confetti)
        self._send(elements, priority=priority, led=led)

    def _confetti(self, t):
        """A short burst of falling pixels, reusing the same ids every frame."""
        out = []
        for i in range(CONFETTI):
            if t <= 0.0:
                out.append(park("c%d" % i, "rect"))
                continue
            # Deterministic scatter: no randomness, so a frame is reproducible.
            x = (i * 9 + 4) % W
            y = int((t * 22 + i * 3) % 20) - 2
            if 0 <= y < 13:
                out.append({"id": "c%d" % i, "type": "rectangle", "x": x, "y": y,
                            "width": 1, "height": 2, "fill": "solid", "border_width": 0,
                            "fill_colors": [GREEN if i % 2 else WHITE]})
            else:
                out.append(park("c%d" % i, "rect"))
        return out


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def demo_state(elapsed):
    """Scripted running -> success -> running -> failure cycle."""
    phase = elapsed % 19.0
    if phase < 1.5:
        return None
    if phase < 7.5:
        return {"kind": "running", "repo": "example-app", "branch": "main",
                "elapsed": (phase - 1.5) * 28, "fraction": (phase - 1.5) / 6.3}
    if phase < 11.5:
        return {"kind": "success", "repo": "example-app", "branch": "main",
                "elapsed": 154, "age": phase - 7.5}
    if phase < 15.0:
        return {"kind": "running", "repo": "example-app", "branch": "fix/login",
                "elapsed": (phase - 11.5) * 32, "fraction": (phase - 11.5) / 3.7}
    return {"kind": "failure", "repo": "example-app", "job": "build (macos-latest)",
            "elapsed": 112}


def tour_state(elapsed):
    """Longer walk that also covers the multi-repo failure headline."""
    t = elapsed % 25.0
    if t < 2.0:
        return None                                            # idle: screen released
    if t < 8.0:
        return {"kind": "running", "repo": "example-app", "branch": "main",
                "elapsed": (t - 2.0) * 26, "fraction": (t - 2.0) / 6.3}
    if t < 13.0:
        return {"kind": "success", "repo": "example-app", "branch": "main",
                "elapsed": 154, "age": t - 8.0}
    if t < 17.0:
        return {"kind": "running", "repo": "example-app", "branch": "fix/login",
                "elapsed": (t - 13.0) * 30, "fraction": (t - 13.0) / 4.2}
    if t < 21.0:
        return {"kind": "failure", "repo": "example-app", "job": "build (macos-latest)",
                "elapsed": 112}
    return {"kind": "failure", "repo": "other-service", "job": "test", "elapsed": 88,
            "multi": True}                                     # repo named in headline


# ---------------------------------------------------------------------------

QUEUED_STATES = ("queued", "waiting", "pending", "requested")


def pick_pending(runs):
    """The newest run that has not finished, queued ones included.

    Used only to decide how often to poll: a queued run means something is about
    to happen, so keep checking at the active interval.
    """
    live = [r for r in runs if r.get("status") in QUEUED_STATES + ("in_progress",)]
    live.sort(key=lambda r: _ts(r.get("created_at")) or 0, reverse=True)
    return live[0] if live else None


def pick_active(runs):
    """The newest run that is actually executing. Queued runs are not shown.

    A finished run briefly reports status "completed" with conclusion still null
    while GitHub settles the verdict. Count that as running, otherwise the screen
    drops back to idle for a poll or two before the result lands.
    """
    live = [r for r in runs
            if r.get("status") == "in_progress"
            or (r.get("status") == "completed" and r.get("conclusion") is None)]
    live.sort(key=lambda r: _ts(r.get("created_at")) or 0, reverse=True)
    return live[0] if live else None


def failed_job_name(gh, repo, run_id):
    try:
        data = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs") or {}
    except Exception:
        return None
    for job in data.get("jobs") or []:
        if job.get("conclusion") in ("failure", "timed_out"):
            return job.get("name")
    return None


def main():
    args = parse_args()
    repos = args.repo or [r.strip() for r in
                          os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]
    if args.test and not args.demo and not args.tour and not repos:
        print("no repos given, drawing demo data", file=sys.stderr)
        args.demo = True
    configured = bool(args.demo or args.tour or repos)
    if not configured:
        print("no repos: pass --repo owner/name (or set GITHUB_REPOS), or use --demo",
              file=sys.stderr)
    if repos and not args.token:
        print("no GITHUB_TOKEN: unauthenticated polling is capped at 60 requests/hour, "
              "which one running workflow will exhaust. Set GITHUB_TOKEN for 5000/hour.",
              file=sys.stderr)

    gh = GitHub(args.token)
    screen = Screen(args.host)
    started = time.monotonic()

    runs_by_repo = {}
    last_poll = 0.0
    known = {}            # run id -> conclusion already accounted for
    first_poll = True     # the first poll only records history, it never celebrates
    finished = None       # {kind, until, ...} for the success/failure hold
    sound_done = set()
    last_label = None     # only log when the screen actually changes state
    rate_warned = False

    print(f"{APP} -> {_base(args.host)}  (Ctrl-C to stop)")
    try:
        while True:
            now = time.monotonic()

            if not configured:
                screen.show("GH ACTIONS", "SET --REPO", AMBER, priority=30)
                if args.test:
                    break
                time.sleep(2.0)
                continue

            if args.demo or args.tour:
                state = tour_state(now - started) if args.tour else demo_state(now - started)
                render_demo(screen, state, args)
                if args.test:
                    break
                time.sleep(0.3)
                continue

            # Poll fast while anything is queued or running, so a run that only
            # sits in the queue for a few seconds is not missed entirely.
            pending_now = any(pick_pending(r) for r in runs_by_repo.values())
            interval = args.active_interval if pending_now else args.idle_interval
            if now - last_poll >= interval or not last_poll:
                for repo in repos:
                    try:
                        runs_by_repo[repo] = fetch_runs(gh, repo)
                    except RateLimited as e:
                        if not rate_warned:  # say it once, not every poll
                            print(f"{e}; showing the last known state until then."
                                  + ("" if args.token else
                                     " Set GITHUB_TOKEN to raise the cap to 5000/hour."),
                                  file=sys.stderr, flush=True)
                            rate_warned = True
                    except Exception as e:
                        print(f"{repo}: fetch error ({e})", file=sys.stderr, flush=True)
                if gh.blocked_until and time.time() >= gh.blocked_until:
                    gh.blocked_until, rate_warned = 0.0, False
                last_poll = now

                for repo, runs in runs_by_repo.items():
                    for run in runs:
                        rid, conclusion = run.get("id"), run.get("conclusion")
                        # conclusion is null for a moment after status flips to
                        # completed; wait for the verdict rather than recording it.
                        if run.get("status") != "completed" or conclusion is None:
                            continue
                        if known.get(rid) == conclusion:
                            continue
                        known[rid] = conclusion
                        if first_poll:
                            continue  # startup: learn the history, do not replay it
                        if conclusion in ("failure", "timed_out"):
                            finished = {"kind": "failure", "repo": repo, "run": run,
                                        "until": now + args.failure_hold}
                        elif conclusion == "success":
                            finished = {"kind": "success", "repo": repo, "run": run,
                                        "until": now + args.success_hold, "at": now}
                first_poll = False

            if finished and now >= finished["until"]:
                finished = None

            active, active_repo = None, None
            for repo, runs in runs_by_repo.items():
                candidate = pick_active(runs)
                if candidate and (not active or (_ts(candidate.get("created_at")) or 0)
                                  > (_ts(active.get("created_at")) or 0)):
                    active, active_repo = candidate, repo

            if active:
                label = "%s %s %s/%s" % (active_repo, active.get("status"),
                                         active.get("name"), active.get("head_branch"))
                render_active(screen, gh, active_repo, active, runs_by_repo[active_repo], args)
            elif finished:
                label = "%s %s" % (finished["repo"], finished["kind"])
                render_finished(screen, gh, finished, now, args, sound_done,
                                multi=len(repos) > 1)
            else:
                label = "idle"
                screen.release()
            if label != last_label:
                # flush: this app is usually run detached, where stdout is
                # block-buffered and the log would otherwise arrive minutes late.
                print(f"[{time.strftime('%H:%M:%S')}] {label}", flush=True)
                last_label = label

            if args.test:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        _clear(args.host)


def render_active(screen, gh, repo, run, runs, args):
    name = repo.split("/")[-1]
    branch = run.get("head_branch") or "?"
    start = _ts(run.get("run_started_at")) or _ts(run.get("created_at")) or _now()
    elapsed = _now() - start

    jobs = []
    try:
        jobs = (gh.get(f"/repos/{repo}/actions/runs/{run['id']}/jobs") or {}).get("jobs") or []
    except Exception:
        pass
    screen.show(name, f"{branch} {fmt_clock(elapsed)}", BLUE,
                fraction=progress(run, runs, jobs), bar_color=BLUE, priority=30)


def render_finished(screen, gh, finished, now, args, sound_done, multi=False):
    run, repo = finished["run"], finished["repo"]
    name = repo.split("/")[-1]
    start = _ts(run.get("run_started_at")) or _ts(run.get("created_at"))
    took = fmt_clock((_ts(run.get("updated_at")) or _now()) - start) if start else ""

    fresh = run["id"] not in sound_done
    if fresh:
        sound_done.add(run["id"])

    if finished["kind"] == "success":
        if fresh and not args.no_sound:
            _play(screen.host, "calendar_reminder_ends")
        screen.show(name, ("PASSED " + took).strip(), GREEN, fraction=1.0,
                    bar_color=GREEN, priority=30, led=GREEN if fresh else None,
                    confetti=now - finished.get("at", now))
    else:
        job = failed_job_name(gh, repo, run["id"]) or (run.get("name") or "run")
        # Job names are often just "build", so name the repo too once more than
        # one is being watched: the line scrolls if it no longer fits.
        headline = f"{name} {job}" if multi else job
        if fresh and not args.no_sound:
            _play(screen.host, "calendar_event_starts")
        screen.show(headline, "FAILED " + (run.get("head_branch") or ""), RED,
                    fraction=1.0, bar_color=RED, priority=60, led=RED if fresh else None)


def render_demo(screen, state, args):
    if state is None:
        screen.release()
        return
    kind = state["kind"]
    if kind == "running":
        screen.show(state["repo"], f"{state['branch']} {fmt_clock(state['elapsed'])}",
                    BLUE, fraction=min(0.95, state["fraction"]), bar_color=BLUE)
    elif kind == "success":
        screen.show(state["repo"], "PASSED " + fmt_clock(state["elapsed"]), GREEN,
                    fraction=1.0, bar_color=GREEN, confetti=state["age"])
    else:
        job = state["job"]
        headline = f"{state['repo']} {job}" if state.get("multi") else job
        screen.show(headline, "FAILED main", RED, fraction=1.0, bar_color=RED,
                    priority=60)


if __name__ == "__main__":
    main()
