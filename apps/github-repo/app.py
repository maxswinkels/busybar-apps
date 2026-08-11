#!/usr/bin/env python3
"""GitHub Repo: one repository's stars, forks, open issues and open PRs.

    python app.py --repo torvalds/linux              # BUSY Bar over USB (10.0.4.20)
    python app.py --repo torvalds/linux --host 127.0.0.1:8080   # emulator / Wi-Fi bar
    python app.py --repo torvalds/linux --test       # fake numbers, draw once, exit

Front (72x16): the owner's avatar, then a 2x2 grid of the counts - stars
(yellow) and forks (blue) on top, open issues (green) and open PRs (purple)
below, abbreviated as "198k" or "1.2M". When stars, issues or PRs move, that
metric takes the whole panel for a few seconds - big icon, the change (a gain
in white, a drop in red for stars, green for issues and PRs), LED pulsing in
its color - then the grid returns. Several changes take turns. Forks never
flash.

Back (160x80, OLED, dual-display devices only): the same counts in full with
labels, the primary language, and how long ago the repo was pushed to.

Avatars come from ``github.com/<owner>.png`` in whatever format the account
uploaded, JPEG for many. Pillow (the only dependency, imported optionally)
decodes those; without it only PNG avatars work and the rest fall back to a
tile with the owner's initial.

Counts are cached in ``counts.json`` beside this file, so a restart flashes
whatever moved while the app was down. One file holds every repo watched, so
instances on different repos share it safely. ``--no-cache`` turns it off.

``open_issues_count`` folds in pull requests, so open PRs are counted
separately and subtracted: two REST calls per poll against 60/hour
unauthenticated. ``--gh-token`` (or ``GITHUB_TOKEN``) raises that to 5000 and
reaches private repos.
"""

import argparse
import io
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone

# Optional: only needed to decode non-PNG avatars. Without it those owners
# fall back to the initial tile and the app stays stdlib-only.
try:
    from PIL import Image, ImageEnhance
except ImportError:
    Image = None

APP = "github-repo"

DEFAULT_HOST = "10.0.4.20"  # USB address; use --host for Wi-Fi or the emulator
DEVICE_TIMEOUT = 10  # s, HTTP timeout for device calls
GITHUB_TIMEOUT = 15  # s, HTTP timeout for api.github.com
FRAME_INTERVAL = 2.0  # s between redraws (the poll runs on --interval)
ELEMENT_TIMEOUT = 15  # s; a dead app clears itself off the display
FLASH_SECONDS = 6.0  # how long a count stays highlighted after it grows
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # a 64 px avatar is a few KB; cap the read

# Counts from previous runs, all repos in one file beside app.py. Runtime
# file, not one to commit. Bump the version to retire an old layout.
CACHE_VERSION = 1
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "counts.json")

UA = "busybar-github-repo/1.0"

# --- colors ----------------------------------------------------------------

C_STARS = "#FFD33DFF"  # GitHub star yellow
C_FORKS = "#58A6FFFF"  # blue
C_ISSUES = "#3FB950FF"  # open-issue green
C_PULLS = "#A371F7FF"  # pull-request purple
C_FLASH = "#FFFFFFFF"  # a count that just went up
# A drop reads differently per metric: losing stars is bad news, while issues
# and PRs going down means they were closed. Colour the number accordingly.
C_DOWN_BAD = "#F85149FF"  # GitHub danger red
C_DOWN_GOOD = "#3FB950FF"  # same green as the issue icon
# The front's gamma curve crushes anything below ~#808080 to nothing, so its
# dim tone has to be this bright; only the OLED back can use a true grey.
C_DIM = "#8B949EFF"  # avatar tile outline and the rule beside it
C_LABEL = "#6E7681FF"  # back-display labels and footer
WHITE = "#FFFFFFFF"
TRANSPARENT = "#00000000"

# Same order everywhere: front grid cells, back rows, delta bookkeeping.
METRICS = (
    ("stars", "STARS", C_STARS),
    ("forks", "FORKS", C_FORKS),
    ("issues", "ISSUES", C_ISSUES),
    ("pulls", "PULLS", C_PULLS),
)
METRIC_COLOR = {key: color for key, _label, color in METRICS}

# Colour for a metric that went down. Stars and forks lost are bad, issues and
# PRs closed are good, so the same minus sign means opposite things.
DOWN_COLOR = {"stars": C_DOWN_BAD, "forks": C_DOWN_BAD,
              "issues": C_DOWN_GOOD, "pulls": C_DOWN_GOOD}

# Which metrics flash their change. Forks are deliberately out: not news.
FLASH_METRICS = ("stars", "issues", "pulls")

# --- front layout (72x16) --------------------------------------------------

AVATAR = 16  # the avatar tile is 16x16, flush left
SEP_X = 17  # 1 px vertical rule between avatar and the grid
COL_X = (19, 46)  # icon left edge, column 0 and 1
ROW_Y = (0, 9)  # icon top edge, row 0 and 1
# `small` draws its digits on rows y+2..y+6, so these land them on rows 1-5
# and 10-14, centered on the icons.
TEXT_Y = (-1, 8)

# Flash layout: one metric takes the whole stats panel, avatar untouched.
FLASH_ICON_X = 19
FLASH_ICON_Y = 1  # 14 px tall, so rows 1-14 of the 16 px display
FLASH_TEXT_L = FLASH_ICON_X + 18 + 2  # first free column right of the icon
FLASH_TEXT_X = (FLASH_TEXT_L + 72) // 2  # centre of the remaining space
FLASH_TEXT_W = 72 - FLASH_TEXT_L

OFFSCREEN = -200  # where an element goes to be invisible

# --- device fonts ----------------------------------------------------------

# Advance width per printable ASCII char, encoded as (width + 48).
FONT_WIDTHS = {
    "tiny": "32464552334424254444444444224444444444444445465545444566465353443444443342242644443444464444245",
    "small": "22464552334423234344444444234444555555555245465555554546444333444444443442342644443434464444245",
    "normal": "53466872334634456666666666234646866666666456586666666668666353654555555554454655554555666554346",
    "extra_large": "436:9;:3557734368588888888335757<88887788578798888887899887464854888877885787988888878998876368",
}


def text_width(text, font):
    """Pixel width of `text` in a device font (unknown chars count as 4 px)."""
    table = FONT_WIDTHS[font]
    return sum(ord(table[ord(c) - 32]) - 48 if 32 <= ord(c) < 127 else 4
               for c in text)


# --- icons, baked into the panel image -------------------------------------

# One bitmap per metric, all the same size; the grid derives TEXT_DX from it.
ICONS = {
    "stars": ("....#....",
              "...###...",
              ".#######.",
              "..#####..",
              "...###...",
              "..##.##..",
              ".##...##."),
    "forks": (".##...##.",
              ".##...##.",
              "..#...#..",
              "..#####..",
              "....#....",
              "...###...",
              "...###..."),
    "issues": ("...###...",
               "..#...#..",
               ".#.....#.",
               ".#..#..#.",
               ".#.....#.",
               "..#...#..",
               "...###..."),
    "pulls": (".#...#...",
              "#.#.###..",
              ".#...#.#.",
              ".#.....#.",
              ".#.....#.",
              "#.#...#.#",
              ".#.....#."),
}

ICON_W = max(len(row) for rows in ICONS.values() for row in rows)
TEXT_DX = ICON_W + 1  # a count starts one column right of its icon

# The same icons at 18x14 for the full-panel flash. Hand-drawn, not
# pixel-doubled: at this size there is room for real curves. Tune them with
# preview_icons.py.
BIG_ICONS = {
    "stars": ("........##........",
              "........##........",
              ".......####.......",
              "......######......",
              "##################",
              ".################.",
              "..##############..",
              "...############...",
              "....##########....",
              "....##########....",
              "....####..####....",
              "....###....###....",
              "...###......###...",
              "...##........##..."),

    "forks": ("..####......####..",
              ".######....######.",
              ".##..##....##..##.",
              ".######....######.",
              "..####......####..",
              "...##........##...",
              "...##........##...",
              "...############...",
              "........##........",
              "........##........",
              "......######......",
              "......##..##......",
              "......##..##......",
              "......######......"),

    "issues": ("......######......",
               "....##########....",
               "...####....####...",
               "..###........###..",
               ".###..........###.",
               ".##............##.",
               ".##....####....##.",
               ".##....####....##.",
               ".##............##.",
               ".###..........###.",
               "..###........###..",
               "...####....####...",
               "....##########....",
               "......######......"),

    "pulls": ("..####....##......",
              ".######..###......",
              ".##..##.######....",
              ".######..###.##...",
              "..####....##.##...",
              "...##........##...",
              "...##........##...",
              "...##........##...",
              "...##........##...",
              "..####......####..",
              ".######....######.",
              ".##..##....##..##.",
              ".######....######.",
              "..####......####.."),
}


# --------------------------------------------------------------------------
# PNG encoding (stdlib only)
# --------------------------------------------------------------------------

def make_png(width, height, rgba):
    """Encode raw RGBA bytes into a PNG."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        raw.extend(rgba[y * width * 4:(y + 1) * width * 4])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def hex_rgba(color):
    """'#RRGGBBAA' -> (r, g, b, a)."""
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4, 6))


class Layer:
    """A transparent 72x16 RGBA buffer that icons get stamped into."""

    W, H = 72, 16

    def __init__(self):
        self.rgba = bytearray(self.W * self.H * 4)

    def put(self, x, y, rgb):
        if 0 <= x < self.W and 0 <= y < self.H:
            o = (y * self.W + x) * 4
            self.rgba[o:o + 4] = bytes(rgb)

    def blit(self, rows, ox, oy, color):
        rgb = hex_rgba(color)
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch == "#":
                    self.put(ox + dx, oy + dy, rgb)

    def rule(self):
        """The 1 px divider between the avatar and the stats panel."""
        for y in range(3, 13):
            self.put(SEP_X, y, hex_rgba(C_DIM))

    def png(self):
        return make_png(self.W, self.H, bytes(self.rgba))


def build_panel_png(separator):
    """The idle front layer: the four grid icons, transparent everywhere else.

    The rule is only drawn for a real avatar; the fallback tile has its own
    outline and a rule beside it would just be clutter."""
    layer = Layer()
    for cell, (key, _label, color) in enumerate(METRICS):
        layer.blit(ICONS[key], COL_X[cell % 2], ROW_Y[cell // 2], color)
    if separator:
        layer.rule()
    return layer.png()


def build_flash_png(key, separator):
    """One metric's flash layer: its 18x14 icon alone on the stats panel.

    Each metric owns its own layer and element id for the whole run, so no id
    ever has to repoint at a different asset."""
    layer = Layer()
    layer.blit(BIG_ICONS[key], FLASH_ICON_X, FLASH_ICON_Y, METRIC_COLOR[key])
    if separator:
        layer.rule()
    return layer.png()


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

def gh_get(url, token, timeout=GITHUB_TIMEOUT):
    """GET a GitHub API URL; return (decoded JSON body, response headers)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))
        return body, resp.headers


def count_open_pulls(repo, token):
    """Number of open pull requests, from the paginator rather than search.

    With ``per_page=1`` the last page number in the ``Link`` header *is* the
    count; no header means it fits on one page. ``/search/issues`` would be the
    obvious source but is unreliable unauthenticated - 422 on some repos, a
    silent 0 on others. A 404 means PRs are disabled, so a genuine zero.
    """
    try:
        body, headers = gh_get(
            f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=1",
            token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 0
        raise
    last = re.search(r'[?&]page=(\d+)>;\s*rel="last"', headers.get("Link", ""))
    return int(last.group(1)) if last else len(body)


def fetch_stats(repo, token):
    """Return the four counts plus the metadata the back display shows.

    ``open_issues_count`` counts issues *and* pull requests, so the open-PR
    total is looked up separately and subtracted back out. If only that second
    call fails, ``pulls`` comes back None and the issue count is left as-is.
    """
    data, _ = gh_get(f"https://api.github.com/repos/{repo}", token)
    try:
        pulls = count_open_pulls(repo, token)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"  open-PR lookup failed ({exc}); issue count includes PRs",
              file=sys.stderr)
        pulls = None

    open_items = int(data.get("open_issues_count", 0))
    return {
        "full_name": data.get("full_name", repo),
        "owner": (data.get("owner") or {}).get("login", repo.split("/")[0]),
        "stars": int(data.get("stargazers_count", 0)),
        "forks": int(data.get("forks_count", 0)),
        "issues": max(0, open_items - pulls) if pulls is not None else open_items,
        "pulls": pulls,
        "language": data.get("language"),
        "pushed_at": data.get("pushed_at"),
    }


def fake_stats(repo):
    """Deterministic numbers for --test, so the layout can be checked without
    spending rate limit."""
    owner = repo.partition("/")[0]
    return {
        "full_name": repo,
        "owner": owner or "octocat",
        "stars": 198432,
        "forks": 51204,
        "issues": 2341,
        "pulls": 287,
        "language": "Python",
        "pushed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def download_avatar(owner, size):
    """Raw avatar bytes from GitHub at the requested pixel size, or None."""
    url = f"https://github.com/{owner}.png?size={size}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as resp:
            blob = resp.read(MAX_AVATAR_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"  avatar fetch failed ({exc}); using initial tile")
        return None
    if len(blob) > MAX_AVATAR_BYTES:
        print(f"  avatar is over {MAX_AVATAR_BYTES // 1024} KB; "
              f"using initial tile")
        return None
    return blob


def fetch_avatar_png(owner):
    """The owner's avatar as a 16x16 PNG the bar can draw, or None.

    GitHub resizes server-side but keeps the uploaded encoding, JPEG for many
    accounts. With Pillow the source is fetched at 4x and resampled here;
    without it, only a ready-made 16x16 PNG can be used.
    """
    if Image is None:
        blob = download_avatar(owner, AVATAR)
        if blob is None:
            return None
        if blob[:8] != b"\x89PNG\r\n\x1a\n" or len(blob) < 24:
            print("  avatar is not a PNG and Pillow is not installed "
                  "(pip install -r requirements.txt); using initial tile")
            return None
        width, height = struct.unpack(">II", blob[16:24])
        if (width, height) != (AVATAR, AVATAR):
            print(f"  avatar is {width}x{height}, not {AVATAR}x{AVATAR}; "
                  f"using initial tile")
            return None
        return blob

    blob = download_avatar(owner, AVATAR * 4)
    if blob is None:
        return None
    try:
        with Image.open(io.BytesIO(blob)) as src:
            img = src.convert("RGBA")
        img = punch_up(img)
        if img.size != (AVATAR, AVATAR):
            img = img.resize((AVATAR, AVATAR), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except (OSError, ValueError) as exc:
        print(f"  avatar could not be decoded ({exc}); using initial tile")
        return None


def punch_up(img):
    """Lift mid-tones and saturation so the avatar survives the matrix gamma.

    The display raises each channel to about the power 2.9, gutting the
    mid-tones a 16x16 photo is mostly made of; ^1/2 here cancels half of that.
    Do not swap in ``autocontrast``: stretching the histogram rims a flat logo
    on a light field in black, where a gamma lift leaves logos untouched.
    """
    lut = [min(255, int(255 * (i / 255) ** (1 / 2.0))) for i in range(256)]
    rgb, alpha = img.convert("RGB"), img.getchannel("A")
    rgb = ImageEnhance.Color(rgb.point(lut * 3)).enhance(1.25)
    rgb.putalpha(alpha)
    return rgb


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def fmt_compact(n):
    """Abbreviate a count to at most four characters: 287, 1.2k, 51k, 1.2M."""
    if n is None:
        return "-"
    n = int(n)
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= scale:
            scaled = n / scale
            if scaled < 10:
                return f"{int(scaled * 10) / 10:.1f}{suffix}"
            return f"{int(scaled)}{suffix}"
    return str(n)


def fmt_delta(n):
    """Signed change, compact: +12, -3, -1.2k.

    `fmt_compact` only abbreviates upwards, so the magnitude is what gets
    shortened and the sign is put back on the front.
    """
    return f"{'+' if n > 0 else '-'}{fmt_compact(abs(n))}"


def delta_color(key, change):
    """White for a gain, the metric's drop colour for a loss."""
    return C_FLASH if change > 0 else DOWN_COLOR[key]


def fmt_full(n):
    """Thousands-separated count for the back display."""
    return "-" if n is None else f"{int(n):,}"


def fmt_ago(iso):
    """'2h ago' / '3d ago' from a GitHub ISO-8601 timestamp."""
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    for scale, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= scale:
            return f"{int(secs // scale)}{suffix} ago"
    return "just now"


# --------------------------------------------------------------------------
# Device API
# --------------------------------------------------------------------------

def api_request(host, method, path, body=None, content_type=None):
    """Call the bar; return (status, body_bytes).

    An unreachable device is status 0, not an exception: a bar on Wi-Fi drops
    the odd request and the caller just retries next frame.
    """
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(f"http://{host}{path}", data=body,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DEVICE_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e).encode()


def upload_asset(host, filename, blob):
    """Upload one asset into the app's namespace."""
    query = urllib.parse.urlencode({"application_name": APP, "file": filename})
    status, body = api_request(host, "POST", f"/api/assets/upload?{query}",
                               body=blob,
                               content_type="application/octet-stream")
    if status == 0:
        return False  # unreachable; draw() reports it once per frame already
    if status not in (200, 201, 204):
        print(f"  upload of {filename} returned HTTP {status}: {body[:120]}")
        return False
    return True


def text_el(eid, text, x, y, font, color, display="front", align="top_left",
            **extra):
    return {"id": eid, "type": "text", "text": str(text), "x": int(x),
            "y": int(y), "font": font, "color": color, "align": align,
            "display": display, "timeout": ELEMENT_TIMEOUT, **extra}


def image_el(eid, path, x, y, display="front"):
    return {"id": eid, "type": "image", "path": path, "x": int(x), "y": int(y),
            "display": display, "timeout": ELEMENT_TIMEOUT}


def rect_el(eid, x, y, width, height, color, display="front", radius=0,
            outline=False):
    """A solid rectangle, or a 1 px outline of one when `outline` is set."""
    return {"id": eid, "type": "rectangle", "x": int(x), "y": int(y),
            "width": int(width), "height": int(height), "radius": radius,
            "fill": "none" if outline else "solid",
            "fill_colors": [TRANSPARENT if outline else color],
            "border_width": 1 if outline else 0,
            "border_color": color if outline else TRANSPARENT,
            "display": display, "timeout": ELEMENT_TIMEOUT}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def front_elements(stats, flash, has_avatar):
    """The 72x16 front: the grid of counts, or one metric's flash over it.

    ``flash`` is None or a ``(key, gain)`` pair, which hands the whole stats
    panel to that metric. The avatar stays in both states.

    Every id is emitted every frame, hidden ones included: omitting an id does
    not erase it, the device keeps the last version until its timeout. Hidden
    means parked at ``OFFSCREEN`` and each id keeps one fixed image path,
    because hiding by alpha or repointing a path both rely on firmware
    behaviour the emulator has but a device might not.
    """
    els = []
    for key in FLASH_METRICS:
        showing = flash is not None and flash[0] == key
        els.append(image_el(f"fl_{key}", f"flash_{key}.png",
                            0 if showing else OFFSCREEN, 0))
    els.append(image_el("panel", "panel.png", OFFSCREEN if flash else 0, 0))

    if has_avatar:
        els.append(image_el("avatar", "avatar.png", 0, 0))
    else:
        # Stand-in when the avatar is unusable. A centre-anchored `large`
        # capital lands on rows 3-11, the middle of the tile.
        initial = (stats["owner"][:1] or "?").upper()
        els.append(rect_el("tile", 0, 0, AVATAR, AVATAR, C_DIM, outline=True))
        els.append(text_el("initial", initial, AVATAR // 2, AVATAR // 2,
                           "large", WHITE, align="center"))

    for cell, (key, _label, color) in enumerate(METRICS):
        x = OFFSCREEN if flash else COL_X[cell % 2] + TEXT_DX
        els.append(text_el(f"n_{key}", fmt_compact(stats[key]), x,
                           TEXT_Y[cell // 2], "small", color))

    # The font steps down on a jump too wide for the space beside the icon.
    gain = fmt_delta(flash[1]) if flash else "+0"
    font = ("extra_large" if text_width(gain, "extra_large") <= FLASH_TEXT_W
            else "normal")
    els.append(text_el("flash", gain, FLASH_TEXT_X if flash else OFFSCREEN, 8,
                       font, delta_color(flash[0], flash[1]) if flash
                       else C_FLASH, align="center"))
    return els


def back_elements(stats, flashing, deltas):
    """The 160x80 back dashboard: full name, labelled counts, footer."""
    name = stats["full_name"]
    title = text_el("b_title", name, 2, 0, "normal", WHITE, display="back")
    if text_width(name, "normal") > 156:
        title.update({"width": 156, "scroll_rate": 900,
                      "scroll_start_delay": 1500, "scroll_repeat_delay": 2500})
    els = [title, rect_el("b_rule", 2, 12, 156, 1, C_LABEL, display="back")]

    for row, (key, label, color) in enumerate(METRICS):
        y = 16 + row * 12
        els.append(text_el(f"b_l_{key}", label, 2, y, "small", C_LABEL,
                           display="back"))
        els.append(text_el(f"b_v_{key}", fmt_full(stats[key]), 158, y, "small",
                           C_FLASH if key in flashing else color,
                           display="back", align="top_right"))
        # Only the number carries the change's colour; the label and the count
        # beside it keep their own.
        change = deltas.get(key, 0)
        els.append(text_el(f"b_d_{key}", fmt_delta(change) if change else " ",
                           92, y, "small",
                           delta_color(key, change) if change else TRANSPARENT,
                           display="back", align="top_right"))

    footer = " . ".join(p for p in (stats.get("language"),
                                    fmt_ago(stats.get("pushed_at"))) if p)
    els.append(text_el("b_foot", footer or "no activity", 2, 68, "tiny",
                       C_LABEL, display="back"))
    return els


def draw(host, elements, led=None):
    """POST one frame. A 409 means a higher-priority app owns the display."""
    payload = {"application_name": APP, "elements": elements}
    if led:
        payload["led_notification_color"] = led
    status, body = api_request(host, "POST", "/api/display/draw",
                               body=json.dumps(payload).encode(),
                               content_type="application/json")
    if status == 409:
        print("  409: another app holds the display at higher priority")
    elif status == 0:
        print(f"  bar unreachable ({body[:120].decode(errors='replace')}); "
              f"retrying next frame")
    elif status not in (200, 201, 204):
        print(f"  draw returned HTTP {status}: {body[:160]}")


# --------------------------------------------------------------------------
# Run loop
# --------------------------------------------------------------------------

def describe(stats):
    return (f"{stats['full_name']}: {fmt_full(stats['stars'])} stars, "
            f"{fmt_full(stats['forks'])} forks, "
            f"{fmt_full(stats['issues'])} issues, "
            f"{fmt_full(stats['pulls'])} PRs")


def exit_if_missing(repo, exc, token):
    """Exit 1 on the 404 that means the repository is not there.

    A wrong name never fixes itself, so it is fatal rather than something the
    first-fetch loop retries. Without a token a private repo 404s too, so the
    message covers both.
    """
    if not (isinstance(exc, urllib.error.HTTPError) and exc.code == 404):
        return
    extra = ("" if token else
             " If it is private, pass --gh-token or set GITHUB_TOKEN.")
    print(f"error: repository {repo} not found.{extra}", file=sys.stderr)
    sys.exit(1)


def rate_limit_hint(exc):
    """Turn GitHub's opaque 403 into the advice that actually helps."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
        reset = exc.headers.get("X-RateLimit-Reset")
        when = ""
        if reset and reset.isdigit():
            local = datetime.fromtimestamp(int(reset), timezone.utc).astimezone()
            when = f" (resets {local.strftime('%H:%M')})"
        return (f"GitHub rate limit reached{when}. Unauthenticated runs get 60 "
                f"requests an hour; pass --gh-token or set GITHUB_TOKEN for "
                f"5000.")
    return None


# --------------------------------------------------------------------------
# Count cache: what the numbers were when this repo was last watched
# --------------------------------------------------------------------------

def read_cache(path):
    """The whole cache document, or a fresh empty one.

    Anything unreadable - missing, truncated, invalid, wrong version - counts
    as "no history yet". Losing it costs one restart's deltas, never the app.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("version") == CACHE_VERSION and isinstance(data.get("repos"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"version": CACHE_VERSION, "repos": {}}


def load_counts(path, repo):
    """The counts saved for `repo` last time, or {} if it has not been seen."""
    entry = read_cache(path)["repos"].get(repo.lower()) or {}
    return {key: entry[key] for key, _label, _color in METRICS
            if isinstance(entry.get(key), int)}


def save_counts(path, repo, stats):
    """Merge this repo's counts into the shared cache file.

    Re-read immediately before writing and replaced atomically, so instances
    watching different repos keep their own entry and never see a half-written
    file. Two saving the same instant can still cost one a single update.
    """
    data = read_cache(path)
    entry = {key: stats[key] for key, _label, _color in METRICS
             if isinstance(stats.get(key), int)}
    entry["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["repos"][repo.lower()] = entry

    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)  # atomic: readers see old or new, never partial
    except OSError as exc:
        print(f"  could not write {path} ({exc}); deltas will not survive a restart")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def queue_change(queue, key, change):
    """Add a change to the flash queue, folding it into a pending one.

    A metric that moves again while queued keeps its place and sums the two,
    so the same icon never flashes twice in a row with a half-count on each.
    A gain and a matching loss cancel out and the entry drops: nothing to show.
    """
    for entry in queue:
        if entry[0] == key:
            entry[1] += change
            if entry[1] == 0:
                queue.remove(entry)
            return
    queue.append([key, change])


def run(args):
    host = args.host.replace("http://", "").rstrip("/")
    # --test is deterministic and offline, so it never reads or writes history.
    cache = None if (args.no_cache or args.test) else CACHE_FILE

    if args.test:
        stats = fake_stats(args.repo)
    else:
        # The first poll decides the owner, so it has to land before the loop
        # starts. Retry rather than die - an autostarted widget has nobody
        # watching for a traceback - backing off up to the poll interval.
        delay = 15
        while True:
            try:
                stats = fetch_stats(args.repo, args.gh_token)
                break
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                exit_if_missing(args.repo, exc, args.gh_token)
                print(f"  {rate_limit_hint(exc) or f'first fetch failed: {exc}'}")
                if args.once:
                    return
                print(f"  retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, max(args.interval, 15))
    print(describe(stats))

    skip_avatar = args.no_avatar or args.test  # --test stays offline
    avatar = None if skip_avatar else fetch_avatar_png(stats["owner"])

    def upload_assets():
        """Push every icon layer, and the avatar, to the bar.

        All uploaded up front so a flash costs nothing at draw time. Retried
        from the loop until it lands, or a bar unreachable for a moment at
        startup would leave the grid iconless for the whole run.
        """
        avatar_ok = bool(avatar) and upload_asset(host, "avatar.png", avatar)
        ok = upload_asset(host, "panel.png",
                          build_panel_png(separator=avatar_ok))
        for key in FLASH_METRICS:
            ok = upload_asset(host, f"flash_{key}.png",
                              build_flash_png(key, separator=avatar_ok)) and ok
        return ok and (avatar_ok or not avatar), avatar_ok

    assets_ok, has_avatar = upload_assets()

    previous = {key: stats[key] for key, _, _ in METRICS}
    # [key, gain] pairs waiting their turn, oldest first; one at a time.
    queue = []
    active = None  # (key, gain) currently on the panel
    active_until = 0.0
    next_poll = time.monotonic() + args.interval

    # Anything that grew while the app was down flashes now, as it would have
    # live. Keyed on full_name so a renamed repo still matches its history.
    if cache:
        saved = load_counts(cache, stats["full_name"])
        for key in FLASH_METRICS:
            was, count = saved.get(key), stats[key]
            if was is not None and count is not None and count != was:
                print(f"  since last run: {key} {fmt_delta(count - was)}"
                      f" -> {count}")
                queue_change(queue, key, count - was)
        save_counts(cache, stats["full_name"], stats)

    while True:
        now = time.monotonic()
        if not assets_ok:
            assets_ok, has_avatar = upload_assets()
        if not args.test and not args.once and now >= next_poll:
            next_poll = now + args.interval
            try:
                fresh = fetch_stats(args.repo, args.gh_token)
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                hint = rate_limit_hint(exc)
                print(f"  {hint or f'fetch error: {exc}'}; keeping the last frame")
            else:
                for key, _label, _color in METRICS:
                    old, new = previous.get(key), fresh[key]
                    if old is not None and new is not None and new != old:
                        print(f"  {key} {fmt_delta(new - old)} -> {new}")
                        if key in FLASH_METRICS:
                            queue_change(queue, key, new - old)
                    previous[key] = new
                stats = fresh
                if cache:
                    save_counts(cache, stats["full_name"], stats)
                print(describe(stats))

        # Retire a finished flash, promote the next: several gains from one
        # poll show one after another.
        if active is not None and now >= active_until:
            active = None
        if active is None and queue:
            active = tuple(queue.pop(0))
            active_until = now + FLASH_SECONDS

        # The back has room for every pending gain, so it lists them all
        # instead of following the front's cycle.
        deltas = {key: gain for key, gain in queue}
        if active is not None:
            deltas[active[0]] = active[1]
        led = METRIC_COLOR[active[0]] if active is not None else None

        draw(host, front_elements(stats, active, has_avatar)
             + back_elements(stats, set(deltas), deltas), led=led)

        if args.once or args.test:
            return
        time.sleep(FRAME_INTERVAL)


def main():
    parser = argparse.ArgumentParser(
        description="Show one GitHub repository's stars, forks, open issues "
                    "and open pull requests on the BUSY Bar.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"device host (default: {DEFAULT_HOST})")
    parser.add_argument("--repo", default="maxswinkels/busybar-apps",
                        help="repository as owner/name")
    parser.add_argument("--gh-token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub token (default: $GITHUB_TOKEN). Raises the "
                             "rate limit to 5000/hour and reaches private repos")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between GitHub polls (default: 300)")
    parser.add_argument("--no-avatar", action="store_true",
                        help="always draw the initial tile, never fetch the avatar")
    parser.add_argument("--once", action="store_true",
                        help="draw a single frame and exit")
    parser.add_argument("--test", action="store_true",
                        help="use fake numbers, draw once, exit 0")
    parser.add_argument("--no-cache", action="store_true",
                        help="do not read or write counts.json, so a restart "
                             "shows no catch-up flashes")
    args = parser.parse_args()

    if "/" not in args.repo.strip("/"):
        parser.error("--repo must look like owner/name")
    args.repo = args.repo.strip("/")

    print(f"{APP} -> {args.repo} on {args.host}  (Ctrl-C to stop)")
    try:
        run(args)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
