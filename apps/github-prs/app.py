#!/usr/bin/env python3
"""GitHub PRs: your open pull requests at a glance, and a flash when one is ready to merge.

    export GITHUB_TOKEN=ghp_xxxx           # or just be logged in with the GitHub CLI (gh auth login)
    python3 app.py                         # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --host 127.0.0.1:8080   # emulator or a Wi-Fi bar
    python3 app.py --repo owner/name       # only these repos (repeat it for more)
    python3 app.py --no-reviews            # only your own PRs, not ones you've been asked to review
    python3 app.py --demo                  # made up PRs that change every few seconds, no network
    python3 app.py --test                  # draw one frame of made up PRs and exit

Top row: how many of your PRs are ready to merge (green), running checks (yellow), failing (red)
and waiting on something like a review (blue). The bottom row scrolls whatever needs you:
problems first in red with the reason, then the ones ready to merge in green, then reviews you've
been asked for in blue. The back screen lists them all.

When something changes it flashes a banner for a few seconds and blinks the LED: ready to merge,
checks failed, conflicts, approved, changes asked, review requested, merged or closed. Nothing
flashes on the first check after starting, so you don't get a pile of them at once.

The token is read from GITHUB_TOKEN (or GH_TOKEN). If neither is set and the GitHub CLI is
installed and logged in, it uses `gh auth token`. It needs read access to the repos your PRs are
in, so a classic token with the `repo` scope, or a fine-grained one with pull requests and commit
statuses set to read. It checks every 2 minutes by default (--interval), which is 30 requests an
hour against the 5000 GitHub allows a signed-in user. On Wi-Fi with an HTTP API password set on
the bar, pass --password or set BUSYBAR_HTTP_PASSWORD.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP = "github-prs"
W, H = 72, 16
IDLE_PRIORITY = 30   # an ambient app, anything more important can have the screen
ALERT_PRIORITY = 60  # a banner for a few seconds, then back to 30

GREEN = "#22C55EFF"
YELLOW = "#FACC15FF"
RED = "#EF4444FF"
BLUE = "#60A5FAFF"
ORANGE = "#F97316FF"
PURPLE = "#C084FCFF"
GREY = "#6B7280FF"
DIM = "#9CA3AFFF"
WHITE = "#FFFFFFFF"

# Stock sounds on the real bar need a folder in front of the name. The emulator doesn't have
# these, which is fine: sound is best effort and off unless you pass --sound.
SOUND_GOOD = "busy/session_completed"
SOUND_BAD = "busy/countdown_finish"
SOUND_INFO = "shared/calendar_event_starts"


def parse_args():
    p = argparse.ArgumentParser(description="Your open GitHub pull requests on the BUSY Bar.")
    p.add_argument("--host", default="10.0.4.20", help="bar address (USB is always 10.0.4.20)")
    p.add_argument("--password", default=os.environ.get("BUSYBAR_HTTP_PASSWORD", ""),
                   help="the bar's HTTP API password, only needed on Wi-Fi")
    p.add_argument("--repo", action="append", default=[], help="only show PRs in this repo (repeatable)")
    p.add_argument("--no-reviews", action="store_true", help="leave out PRs you've been asked to review")
    p.add_argument("--interval", type=int, default=120, help="seconds between checks (default 120, min 30)")
    p.add_argument("--sound", action="store_true", help="play a sound with each banner")
    p.add_argument("--demo", action="store_true", help="made up PRs that change every few seconds")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    return p.parse_args()


# --- BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs) --------------------------------

class Bar:
    def __init__(self, host, password=""):
        self.base = "http://" + host.replace("http://", "").rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if password:
            self.headers["X-API-Token"] = password

    def _send(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def draw(self, elements, priority=IDLE_PRIORITY, led=None):
        """True if it's on screen, False if something more important has the screen (409)."""
        body = {"application_name": APP, "priority": priority, "elements": elements}
        if led:
            body["led_notification_color"] = led
        status = self._send("POST", "/api/display/draw", body)
        if status == 409:
            return False
        if status != 200:
            raise RuntimeError("the bar refused a draw (HTTP %d)" % status)
        return True

    def clear(self):
        try:
            self._send("DELETE", "/api/display/draw?application_name=" + APP)
        except OSError:
            pass

    def play(self, sound):
        try:
            self._send("POST", "/api/audio/play", {"application_name": APP, "stock_path": sound})
        except OSError:
            pass


def text(id_, txt, x, y, color=WHITE, font="small", align="top_left", **kw):
    # The real bar needs a font on every text element, the emulator just defaults it.
    return {"id": id_, "type": "text", "text": str(txt), "x": x, "y": y,
            "font": font, "color": color, "align": align, **kw}


# --- GitHub ----------------------------------------------------------------------------------------

QUERY = """
query($mine: String!, $reviews: String!, $wantReviews: Boolean!) {
  mine: search(query: $mine, type: ISSUE, first: 50) { nodes { ...PR } }
  reviews: search(query: $reviews, type: ISSUE, first: 50) @include(if: $wantReviews) { nodes { ...PR } }
}
fragment PR on PullRequest {
  url number title isDraft mergeStateStatus reviewDecision
  repository { nameWithOwner }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""


class RateLimited(Exception):
    def __init__(self, wait):
        super().__init__("rate limited")
        self.wait = wait


def find_token():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def graphql(token, query, variables=None):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        method="POST",
        headers={"Authorization": "bearer " + token, "Content-Type": "application/json",
                 "User-Agent": "busybar-github-prs",
                 # mergeStateStatus is still a preview field
                 "GraphQL-Features": "merge_info_preview"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("GitHub said the token isn't valid (401). Check GITHUB_TOKEN or run gh auth login.")
        if e.code in (403, 429):
            reset = e.headers.get("x-ratelimit-reset")
            wait = max(60, int(reset) - int(time.time())) if reset and reset.isdigit() else 300
            raise RateLimited(wait)
        raise
    if body.get("errors") and not body.get("data"):
        raise RuntimeError("; ".join(err.get("message", "?") for err in body["errors"]))
    return body.get("data") or {}


def fetch_prs(token, repos, reviews):
    scope = " ".join("repo:" + r for r in repos)
    data = graphql(token, QUERY, {
        "mine": ("is:pr is:open archived:false author:@me " + scope).strip(),
        "reviews": ("is:pr is:open archived:false review-requested:@me " + scope).strip(),
        "wantReviews": reviews,
    })
    prs = {}
    for role, key in (("mine", "mine"), ("review", "reviews")):
        for n in (data.get(key) or {}).get("nodes") or []:
            if not n or "url" not in n:
                continue
            if n["url"] in prs:
                prs[n["url"]]["roles"].add(role)
                continue
            commits = n["commits"]["nodes"]
            rollup = (commits[0]["commit"].get("statusCheckRollup") or {}).get("state") if commits else None
            prs[n["url"]] = {
                "url": n["url"], "repo": n["repository"]["nameWithOwner"], "number": n["number"],
                "title": n["title"], "draft": n["isDraft"], "merge": n["mergeStateStatus"],
                "review": n["reviewDecision"], "rollup": rollup, "roles": {role},
            }
    return list(prs.values())


def final_states(token, urls):
    """Whether PRs that dropped off the open list were merged or closed."""
    if not urls:
        return {}
    fields = "\n".join('p%d: resource(url: %s) { ... on PullRequest { url state } }' % (i, json.dumps(u))
                       for i, u in enumerate(urls[:20]))
    data = graphql(token, "query { %s }" % fields)
    return {v["url"]: v["state"] for v in data.values() if v and v.get("url")}


# --- working out what's going on -------------------------------------------------------------------

def bucket(pr):
    if pr["draft"] or pr["merge"] == "DRAFT":
        return "draft"
    if pr["rollup"] in ("FAILURE", "ERROR"):
        return "failed"
    if pr["rollup"] in ("PENDING", "EXPECTED"):
        return "running"
    if pr["merge"] in ("CLEAN", "HAS_HOOKS") and pr["review"] != "CHANGES_REQUESTED":
        return "ready"
    return "waiting"


def problem(pr):
    if bucket(pr) == "draft":
        return None
    if bucket(pr) == "failed":
        return "checks failed"
    if pr["merge"] == "DIRTY":
        return "conflicts"
    if pr["review"] == "CHANGES_REQUESTED":
        return "changes asked"
    return None


def short(pr):
    return "%s#%d" % (pr["repo"].split("/")[-1], pr["number"])


BANNERS = {  # title (fits 72px in the small font), colour, sound
    "failed": ("CHECKS FAILED", RED, SOUND_BAD),
    "conflicts": ("CONFLICTS", ORANGE, SOUND_BAD),
    "changes": ("CHANGES ASKED", ORANGE, SOUND_BAD),
    "merged": ("MERGED", PURPLE, SOUND_GOOD),
    "ready": ("READY TO MERGE", GREEN, SOUND_GOOD),
    "approved": ("APPROVED", GREEN, SOUND_GOOD),
    "review": ("REVIEW REQUEST", BLUE, SOUND_INFO),
    "closed": ("CLOSED", GREY, SOUND_INFO),
}
ORDER = list(BANNERS)  # most important first


def snapshot(prs):
    return {p["url"]: {"bucket": bucket(p), "merge": p["merge"], "review": p["review"],
                       "roles": set(p["roles"]), "repo": p["repo"], "number": p["number"]} for p in prs}


def changes(before, prs):
    """Banners worth showing since the last check: one per PR, whichever matters most."""
    if before is None:
        return [], []
    banners = []
    for p in prs:
        was = before.get(p["url"])
        mine = "mine" in p["roles"]
        kinds = []
        if was:
            b = bucket(p)
            if b == "failed" and was["bucket"] != "failed":
                kinds.append("failed")
            if p["merge"] == "DIRTY" and was["merge"] != "DIRTY" and b != "draft":
                kinds.append("conflicts")
            if mine and p["review"] == "CHANGES_REQUESTED" and was["review"] != "CHANGES_REQUESTED":
                kinds.append("changes")
            if b == "ready" and was["bucket"] != "ready":
                kinds.append("ready")
            if mine and p["review"] == "APPROVED" and was["review"] != "APPROVED":
                kinds.append("approved")
        elif mine and bucket(p) == "failed":
            kinds.append("failed")  # opened and failed before the next check
        if "review" in p["roles"] and not (was and "review" in was["roles"]) and bucket(p) != "draft":
            kinds.append("review")
        if kinds:
            banners.append((min(kinds, key=ORDER.index), short(p)))
    now = {p["url"] for p in prs}
    gone = [u for u, w in before.items() if u not in now and "mine" in w["roles"]]
    return banners, gone


# --- drawing --------------------------------------------------------------------------------------

def line_segments(prs):
    segs = []
    issues = [p for p in prs if problem(p)]
    if issues:
        label = "ISSUE " if len(issues) == 1 else "ISSUES "
        segs.append((label + "   ".join("%s %s" % (short(p), problem(p)) for p in issues), RED))
    ready = [p for p in prs if bucket(p) == "ready"]
    if ready:
        segs.append(("READY " + "  ".join(short(p) for p in ready), GREEN))
    asked = [p for p in prs if "review" in p["roles"] and "mine" not in p["roles"] and bucket(p) != "draft"]
    if asked:
        segs.append(("REVIEW " + "  ".join(short(p) for p in asked), BLUE))
    if not segs:
        running = sum(1 for p in prs if bucket(p) == "running")
        segs.append(("%d running..." % running if running else "nothing needs you", DIM))
    return segs


def line_element(seg):
    words, color = seg
    return text("line", words, 0, 8, color, width=W, scroll_rate=1200,
                scroll_start_delay=1500, scroll_repeat_delay=2500)


def line_seconds(seg):
    # About 4.5px a character in the small font, scrolling at 20px a second, plus the start delay.
    overflow = max(0, len(seg[0]) * 4.5 - W)
    return max(6.0, 1.5 + overflow / 20.0 + 2.0)


def idle_frame(prs, seg):
    counts = {k: 0 for k in ("ready", "running", "failed", "waiting")}
    for p in prs:
        if bucket(p) in counts:
            counts[bucket(p)] += 1
    if not prs:
        return [text("empty", "no open PRs", 36, 8, DIM, align="center")]
    els = []
    for i, (k, color) in enumerate((("ready", GREEN), ("running", YELLOW), ("failed", RED), ("waiting", BLUE))):
        c = color if counts[k] else DIM
        els.append({"id": "bar_" + k, "type": "rectangle", "x": i * 18 + 1, "y": 0, "width": 2, "height": 7,
                    "fill": "solid", "fill_colors": [c], "border_width": 0})
        els.append(text("n_" + k, counts[k], i * 18 + 5, 0, c))
    els.append(line_element(seg))
    order = {"failed": 0, "ready": 1, "running": 2, "waiting": 3, "draft": 4}
    code = {"ready": "OK", "running": "..", "failed": "XX", "waiting": "--", "draft": "dr"}
    for i, p in enumerate(sorted(prs, key=lambda p: order[bucket(p)])[:7]):
        els.append(text("back_%d" % i, ("%s %s %s" % (code[bucket(p)], short(p), p["title"]))[:40],
                        0, i * 11, WHITE, display="back"))
    return els


def banner_frame(kind, detail):
    title, color, _ = BANNERS[kind]
    return [
        {"id": "b_box", "type": "rectangle", "x": 0, "y": 0, "width": W, "height": H, "radius": 2,
         "fill": "none", "border_width": 1, "border_color": color},
        text("b_title", title, 36, 1, color, align="top_mid"),
        text("b_detail", detail, 36, 8, WHITE, align="top_mid", width=68, scroll_rate=1200),
    ]


class Screen:
    """Keeps track of what's on the bar so element ids never pile up."""

    def __init__(self, bar, sound):
        self.bar, self.sound = bar, sound
        self.ids = None  # ids of the frame on screen, None if we need a full redraw

    def full(self, elements):
        ids = sorted(e["id"] for e in elements)
        if ids != self.ids:
            self.bar.clear()
        ok = self.bar.draw(elements)
        self.ids = ids if ok else None
        return ok

    def line(self, seg):
        if self.ids is None:
            return False  # lost the screen, the next full redraw puts everything back
        if not self.bar.draw([line_element(seg)]):
            self.ids = None
            return False
        return True

    def banner(self, kind, detail, seconds):
        self.bar.clear()
        shown = self.bar.draw(banner_frame(kind, detail), priority=ALERT_PRIORITY, led=BANNERS[kind][1])
        if shown and self.sound:
            self.bar.play(BANNERS[kind][2])
        if shown:
            time.sleep(seconds)
        # Let go of the higher priority, otherwise our own priority 30 frame gets refused.
        self.bar.clear()
        self.ids = None
        return shown


# --- made up PRs for --demo and --test --------------------------------------------------------------

def demo_pr(repo, number, title, rollup="SUCCESS", merge="CLEAN", review=None, roles=("mine",)):
    return {"url": "https://github.com/acme/%s/pull/%d" % (repo, number), "repo": "acme/" + repo,
            "number": number, "title": title, "draft": False, "merge": merge, "review": review,
            "rollup": rollup, "roles": set(roles)}


def demo_steps():
    web = lambda **kw: demo_pr("web", 12, "New checkout page", **kw)
    api = demo_pr("api", 7, "Faster search")
    docs = demo_pr("docs", 31, "Fix typos", merge="BLOCKED", review="REVIEW_REQUIRED", roles=("review",))
    return [
        [web(rollup="PENDING", merge="BLOCKED"), api],
        [web(rollup="FAILURE", merge="UNSTABLE"), api],
        [web(rollup="PENDING", merge="BLOCKED"), api, docs],
        [web(), api, docs],
        [web(), docs],
    ]


def test_prs():
    return [
        demo_pr("web", 12, "New checkout page", rollup="FAILURE", merge="UNSTABLE"),
        demo_pr("api", 7, "Faster search"),
        demo_pr("app", 3, "Dark mode"),
        demo_pr("cli", 9, "Better errors", rollup="PENDING", merge="BLOCKED"),
        demo_pr("docs", 31, "Fix typos", merge="BLOCKED", review="REVIEW_REQUIRED", roles=("review",)),
    ]


# --- main loop -------------------------------------------------------------------------------------

def main(args, bar):
    screen = Screen(bar, args.sound)

    if args.test:
        prs = test_prs()
        screen.full(idle_frame(prs, line_segments(prs)[0]))
        return

    token = None
    if not args.demo:
        token = find_token()
        if not token:
            sys.exit("No GitHub token. Set GITHUB_TOKEN (or log in with gh auth login), or try --demo.")

    interval = 4 if args.demo else max(30, args.interval)
    banner_seconds = 2.5 if args.demo else 6
    steps = demo_steps() if args.demo else None
    step = 0

    print("github-prs -> %s  (Ctrl-C to stop)" % bar.base)
    before = None
    prs = []
    segs = [("checking GitHub...", DIM)]
    seg_i, seg_until, next_check = 0, 0.0, 0.0

    while True:
        now = time.monotonic()
        if now >= next_check:
            next_check = now + interval
            try:
                if args.demo:
                    prs = steps[step % len(steps)]
                    step += 1
                else:
                    prs = fetch_prs(token, args.repo, not args.no_reviews)
                banners, gone = changes(before, prs)
                if gone:
                    gone_states = {u: "MERGED" for u in gone} if args.demo else final_states(token, gone)
                    for u in gone:
                        state = gone_states.get(u)
                        if state in ("MERGED", "CLOSED"):
                            w = before[u]
                            banners.append(("merged" if state == "MERGED" else "closed",
                                            "%s#%d" % (w["repo"].split("/")[-1], w["number"])))
                before = snapshot(prs)
                for kind, detail in sorted(banners, key=lambda b: ORDER.index(b[0]))[:4]:
                    print("  %s: %s" % (BANNERS[kind][0].lower(), detail))
                    screen.banner(kind, detail, banner_seconds)
            except RateLimited as e:
                print("GitHub rate limit hit, waiting %ds" % e.wait)
                next_check = time.monotonic() + e.wait
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                print("couldn't check GitHub: %s (trying again next time)" % e)
            segs = line_segments(prs) if before is not None else segs
            seg_i = 0
            if screen.full(idle_frame(prs, segs[0])):
                seg_until = time.monotonic() + line_seconds(segs[0])
            else:
                seg_until = time.monotonic() + 5
        elif now >= seg_until:
            if screen.ids is None:
                screen.full(idle_frame(prs, segs[seg_i % len(segs)]))  # get the screen back
                seg_until = now + 5
            elif len(segs) > 1:
                seg_i = (seg_i + 1) % len(segs)
                screen.line(segs[seg_i])
                seg_until = now + line_seconds(segs[seg_i])
            else:
                seg_until = now + 5
        time.sleep(0.25)


def stop(*_):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    args = parse_args()
    bar = Bar(args.host, args.password)
    try:
        main(args, bar)
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if not args.test:
            bar.clear()  # hand the screen back
