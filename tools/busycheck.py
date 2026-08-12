#!/usr/bin/env python3
"""busycheck: validate a gallery app against CONTRIBUTING.md.

    python3 tools/busycheck.py waves        # one app
    python3 tools/busycheck.py --all        # the whole gallery
    python3 tools/busycheck.py waves --run  # also record it and check behaviour

CI only ever ran `astro build`, which validates the manifest schema and nothing
else. Every rule in CONTRIBUTING.md that lives outside that schema has had to be
enforced by hand, over and over, in maintainer fix-up commits: preview size, the
kebab-case slug, APP matching the folder, #RRGGBBAA colours, element ids. Those
rules live here now.

Stdlib only, and the YAML reader is a deliberately small subset parser: the
manifests are flat maps with one string list, so this avoids making contributors
install PyYAML just to check their own submission.
"""

import argparse
import ast
import os
import re
import struct
import subprocess
import sys

DISPLAY_W, DISPLAY_H = 72, 16
PREVIEW_W, PREVIEW_H = 720, 160
PREVIEW_MAX_BYTES = 1_500_000
NAME_MAX, DESC_MAX = 50, 200
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{8}$")
COLOR_LIKE_RE = re.compile(r"^(#|0x)[0-9a-fA-F]{6,8}$")
FONTS = ("tiny", "small", "normal", "condensed", "bold", "large", "extra_large", "global")
MANIFEST_KEYS = {"name", "author", "description", "tags", "preview", "repo"}
REQUIRED_KEYS = {"name", "author", "description", "preview"}
PREAPPROVED = {"busylib", "requests"}


class Report:
    def __init__(self, slug):
        self.slug = slug
        self.items = []

    def error(self, msg):
        self.items.append(("error", msg))

    def warn(self, msg):
        self.items.append(("warn", msg))

    def info(self, msg):
        self.items.append(("info", msg))

    @property
    def errors(self):
        return sum(1 for level, _ in self.items if level == "error")


# --------------------------------------------------------------------------
# tiny YAML subset (flat scalars + one list), enough for manifest.yaml
# --------------------------------------------------------------------------

def parse_manifest(text):
    data, key = {}, None
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        if line.lstrip().startswith("- ") and key:
            data.setdefault(key, []).append(_scalar(line.lstrip()[2:]))
            continue
        if ":" not in line:
            continue
        name, _, value = line.partition(":")
        key = name.strip()
        value = value.strip()
        data[key] = _scalar(value) if value else []
    return data


def _scalar(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# --------------------------------------------------------------------------
# image headers, without Pillow
# --------------------------------------------------------------------------

def image_size(path):
    """Return (kind, width, height) for a PNG or GIF, or (None, 0, 0)."""
    with open(path, "rb") as fh:
        head = fh.read(32)
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        w, h = struct.unpack(">II", head[16:24])
        return "png", w, h
    if head[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", head[6:10])
        return "gif", w, h
    return None, 0, 0


# --------------------------------------------------------------------------
# static checks
# --------------------------------------------------------------------------

def check_manifest(app_dir, slug, rep):
    path = os.path.join(app_dir, "manifest.yaml")
    if not os.path.isfile(path):
        rep.error("manifest.yaml is missing")
        return {}
    with open(path, encoding="utf-8") as fh:
        data = parse_manifest(fh.read())

    for key in sorted(REQUIRED_KEYS - set(data)):
        rep.error("manifest.yaml: '%s' is required" % key)
    for key in sorted(set(data) - MANIFEST_KEYS):
        rep.error("manifest.yaml: unknown key '%s' (the site build rejects extras)" % key)

    name = data.get("name", "")
    if isinstance(name, str) and len(name) > NAME_MAX:
        rep.error("manifest.yaml: name is %d characters, the limit is %d" % (len(name), NAME_MAX))
    desc = data.get("description", "")
    if isinstance(desc, str) and len(desc) > DESC_MAX:
        rep.error("manifest.yaml: description is %d characters, the limit is %d. "
                  "The site build fails on this." % (len(desc), DESC_MAX))
    tags = data.get("tags") or []
    if not tags:
        rep.warn("manifest.yaml: no tags, so the app cannot be filtered for in the gallery")
    for tag in tags if isinstance(tags, list) else []:
        if tag != str(tag).lower():
            rep.error("manifest.yaml: tag '%s' must be lowercase" % tag)
    if len(tags) > 5:
        rep.warn("manifest.yaml: %d tags, 1-5 is the convention" % len(tags))
    repo = data.get("repo")
    if repo and not str(repo).startswith("http"):
        rep.error("manifest.yaml: repo must be a full URL")
    return data


def check_preview(app_dir, slug, manifest, rep):
    declared = str(manifest.get("preview") or "").lstrip("./")
    candidates = [declared] if declared else []
    candidates += ["preview.gif", "preview.png"]
    found = next((c for c in candidates if c and os.path.isfile(os.path.join(app_dir, c))), None)
    if not found:
        rep.error("no preview image: add preview.png or preview.gif (720x160). "
                  "Generate one with: npm run preview -- %s" % slug)
        return
    if declared and declared != found:
        rep.error("manifest.yaml points at '%s' but that file does not exist" % declared)

    path = os.path.join(app_dir, found)
    kind, width, height = image_size(path)
    if kind is None:
        rep.error("%s is not a PNG or GIF" % found)
        return
    size = os.path.getsize(path)
    if (width, height) != (PREVIEW_W, PREVIEW_H):
        rep.error("%s is %dx%d, CONTRIBUTING.md requires %dx%d (72x16 LEDs at 10 px). "
                  "Regenerate with: npm run preview -- %s"
                  % (found, width, height, PREVIEW_W, PREVIEW_H, slug))
    if size > PREVIEW_MAX_BYTES:
        rep.warn("%s is %.1f MB; keep previews under %.1f MB so the gallery stays quick "
                 "(try --loop or a shorter --seconds)"
                 % (found, size / 1e6, PREVIEW_MAX_BYTES / 1e6))


def check_app_py(app_dir, slug, rep):
    path = os.path.join(app_dir, "app.py")
    if not os.path.isfile(path):
        rep.error("app.py is missing")
        return
    with open(path, encoding="utf-8") as fh:
        source = fh.read()

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        rep.error("app.py does not parse: line %s, %s" % (exc.lineno, exc.msg))
        return

    # APP id must match the folder slug: the gallery keys on the folder, and the
    # bar keys on application_name, so a mismatch silently splits the two.
    app_ids = [node.value.value for node in ast.walk(tree)
               if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id in ("APP", "APP_ID", "APP_NAME")
                       for t in node.targets)
               and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if not app_ids:
        if '"application_name"' in source or "'application_name'" in source:
            rep.warn("no top-level APP constant; the convention is APP = \"%s\" matching the "
                     "folder" % slug)
    elif app_ids[0] != slug:
        rep.error("APP is %r but the folder is %r: they must match (the gallery and the bar "
                  "would disagree about the app's identity)" % (app_ids[0], slug))

    # --host, defaulting to the USB address
    if "--host" not in source:
        rep.error("app.py does not accept --host: it must, so the same file runs against a bar "
                  "and the emulator (CONTRIBUTING.md step 3)")
    elif "10.0.4.20" not in source:
        rep.error("--host does not default to 10.0.4.20 (the USB address)")

    _check_elements(tree, rep)

    hosts = sorted({m.group(1) for m in re.finditer(r"https?://([a-zA-Z0-9.-]+)", source)
                    if not m.group(1).startswith(("127.0.0.1", "10.0.4.20", "localhost"))})
    if hosts:
        rep.info("talks to %s" % ", ".join(hosts[:6]))

    if "busy/snapshot" in source:
        rep.warn("drives BUSY modes: the emulator stores the snapshot but never renders theme "
                 "animations, so a preview has to come from real hardware")

    if "--test" not in source:
        rep.info("no --test flag; adding one (draw a single frame, then exit) makes the app "
                 "smoke-testable, as 11 of the gallery apps already are")

    req = os.path.join(app_dir, "requirements.txt")
    if os.path.isfile(req):
        with open(req, encoding="utf-8") as fh:
            pkgs = [re.split(r"[<>=!~\[]", line.strip())[0].lower()
                    for line in fh if line.strip() and not line.startswith("#")]
        extra = [p for p in pkgs if p not in PREAPPROVED]
        if extra:
            rep.info("depends on %s (only busylib and requests are pre-approved; other packages "
                     "are reviewed case by case)" % ", ".join(extra))


COLOR_FIELDS = ("color", "border_color", "led_notification_color")


def _bad_colors(tree):
    """Colour literals in positions the bar actually reads as a colour.

    Flagging every 6-digit hex in the file is wrong: home-assistant rasterises
    SVG icons and passes plain #RRGGBB to the SVG renderer, which is fine. Only
    values bound to a colour field reach /api/display/draw.
    """
    out = []

    def look(name, node):
        if name in COLOR_FIELDS and isinstance(node, ast.Constant) \
                and isinstance(node.value, str) and COLOR_LIKE_RE.match(node.value) \
                and not COLOR_RE.match(node.value):
            out.append((getattr(node, "lineno", 0), node.value))
        if name == "fill_colors" and isinstance(node, (ast.List, ast.Tuple)):
            for item in node.elts:
                look("color", item)

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    look(key.value, value)
        elif isinstance(node, ast.keyword) and node.arg:
            look(node.arg, node.value)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    look(target.id.lower(), node.value)
    return out


def _check_elements(tree, rep):
    """Look at dict literals that are clearly draw elements."""
    missing_id, bad_font, seen = 0, set(), 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        values = {k.value: v for k, v in zip(node.keys, node.values)
                  if isinstance(k, ast.Constant)}
        kind = values.get("type")
        if not (isinstance(kind, ast.Constant) and kind.value in
                ("text", "rectangle", "image", "animation", "countdown")):
            continue
        seen += 1
        # `{"type": ..., **kw}` parses with a None key. Helper builders take the
        # id through those kwargs (mac-monitor and moneybird both do), so a dict
        # that unpacks anything cannot be judged statically.
        unpacks = any(k is None for k in node.keys)
        if "id" not in keys and not unpacks:
            missing_id += 1
        font = values.get("font")
        if isinstance(font, ast.Constant) and font.value not in FONTS:
            bad_font.add(font.value)

    if missing_id:
        rep.error("%d draw element literal(s) have no 'id'. Every element needs one "
                  "(^[a-zA-Z0-9._-]+$) since API 25.0.0, and the bar 400s without it." % missing_id)
    for font in sorted(bad_font):
        rep.error("unknown font %r: the device fonts are %s" % (font, ", ".join(FONTS[:-1])))
    for lineno, value in _bad_colors(tree)[:3]:
        rep.error("app.py:%d: colour %r must be #RRGGBBAA with the alpha byte (API 25.0.0); "
                  "the bar 400s on anything else" % (lineno, value))
    if seen:
        rep.info("%d element literal(s) inspected" % seen)


def check_slug(slug, rep):
    if not SLUG_RE.match(slug):
        rep.error("folder name %r is not kebab-case (lowercase letters, digits, hyphens)" % slug)


# --------------------------------------------------------------------------
# runtime check
# --------------------------------------------------------------------------

def check_runtime(app_dir, slug, rep, extra_args, seconds, upstream):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, os.path.join(root, "tools", "busyrec.py"), slug,
           "--seconds", str(seconds), "--json",
           "--out", os.path.join(root, ".busycheck-tmp.json")]
    if upstream:
        cmd += ["--upstream", upstream]
    if extra_args:
        cmd += ["--"] + extra_args
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    try:
        import json
        payload = json.loads(proc.stdout)
    except ValueError:
        rep.error("could not record the app: %s" % (proc.stderr.strip()[-300:] or "no output"))
        return
    finally:
        try:
            os.remove(os.path.join(root, ".busycheck-tmp.json"))
        except OSError:
            pass
    for item in payload.get("findings", []):
        rep.items.append((item["level"], item["message"]))


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def check_app(app_dir, args):
    slug = os.path.basename(app_dir.rstrip("/"))
    rep = Report(slug)
    check_slug(slug, rep)
    manifest = check_manifest(app_dir, slug, rep)
    check_preview(app_dir, slug, manifest, rep)
    check_app_py(app_dir, slug, rep)
    if args.run:
        check_runtime(app_dir, slug, rep, args.app_args, args.seconds, args.upstream)
    return rep


def print_report(rep, verbose):
    icon = {"error": "FAIL", "warn": "WARN", "info": "note"}
    shown = [i for i in rep.items if verbose or i[0] != "info"]
    if not shown:
        print("  %-24s ok" % rep.slug)
        return
    print("  %s" % rep.slug)
    for level, msg in shown:
        first = True
        for line in _wrap(msg, 72):
            print("    %-5s %s" % (icon[level] if first else "", line))
            first = False


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = word if not line else line + " " + word
    if line:
        out.append(line)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="busycheck",
        description="Validate gallery apps against CONTRIBUTING.md.")
    parser.add_argument("app", nargs="?", help="app slug or folder")
    parser.add_argument("--all", action="store_true", help="check every app in apps/")
    parser.add_argument("--run", action="store_true",
                        help="also run the app and check its behaviour")
    parser.add_argument("--seconds", type=float, default=6.0, help="runtime check length")
    parser.add_argument("--upstream", help="run the behaviour check through a real emulator")
    parser.add_argument("--verbose", "-v", action="store_true", help="include notes")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures too")
    args, rest = parser.parse_known_args(argv)
    args.app_args = rest[1:] if rest and rest[0] == "--" else []

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    apps_dir = os.path.join(root, "apps")
    if args.all:
        targets = [os.path.join(apps_dir, d) for d in sorted(os.listdir(apps_dir))
                   if os.path.isfile(os.path.join(apps_dir, d, "app.py"))]
    elif args.app:
        candidate = args.app if os.path.isdir(args.app) else os.path.join(apps_dir, args.app)
        if not os.path.isdir(candidate):
            sys.exit("busycheck: no such app %r" % args.app)
        targets = [candidate]
    else:
        parser.print_help()
        return 2

    print("busycheck: %d app(s)\n" % len(targets))
    reports = [check_app(t, args) for t in targets]
    for rep in reports:
        print_report(rep, args.verbose)

    errors = sum(r.errors for r in reports)
    warns = sum(1 for r in reports for level, _ in r.items if level == "warn")
    print("\n%d error(s), %d warning(s) across %d app(s)" % (errors, warns, len(reports)))
    if errors:
        return 1
    return 1 if (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
