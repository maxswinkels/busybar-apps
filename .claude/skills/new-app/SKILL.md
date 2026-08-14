---
name: new-app
description: Scaffold a new gallery app folder (apps/<slug>/ with app.py + manifest.yaml) following CONTRIBUTING.md, a self-contained, stdlib-only app that drives the BUSY Bar over its HTTP API. Takes the app slug as its argument.
disable-model-invocation: true
---

# Scaffold a new gallery app

Creates `apps/<slug>/` with a minimal, runnable `app.py` and a `manifest.yaml`,
matching this gallery's conventions in `CONTRIBUTING.md` (self-contained,
stdlib-only, `--host` aware, `#RRGGBBAA` colors, app id = slug). Add the
720×160 `preview.png` / `preview.gif` afterwards with the `app-preview` skill.

The slug is the skill argument: `$ARGUMENTS`.

## Step 1: validate the slug

- Take the slug from `$ARGUMENTS`; if none was given, ask for one.
- It must be **kebab-case**: lowercase letters, digits, and hyphens only. The
  folder name is the app's identifier and its `application_name`.
- Refuse if `apps/<slug>/` already exists.

## Step 2: create `apps/<slug>/app.py`

Use exactly this skeleton (a zero-install, stdlib-only app). Replace `<slug>`
with the slug, fill the docstring `<Name>` / `<one-line description>` (its first
line becomes the gallery description, keep it to one sentence), and replace the
`HELLO` placeholder with the app's first frame.

```python
#!/usr/bin/env python3
"""<Name>: <one-line description>.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
"""
import json
import sys
import time
import urllib.error
import urllib.request

APP = "<slug>"

# --- BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs) ----------

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

def text(txt, x=0, y=0, font="normal", color="#FFFFFFFF", **kw):
    # Every element needs an id, and colors are #RRGGBBAA (API 25.0.0+).
    return {"id": "0", "type": "text", "text": str(txt), "x": x, "y": y, "font": font, "color": color, **kw}

# --- app -------------------------------------------------------------------

def tick():
    try:
        draw([text("HELLO", x=36, y=8, font="large", align="center")])
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = a higher-priority app owns the display
            raise

if __name__ == "__main__":
    print(f"<slug> → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            tick()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")
```

Conventions this encodes (keep them):

- **Self-contained, stdlib only.** One file, no shared helpers outside the app
  folder. That earns the gallery's zero-install badge. If you need a library,
  add a `requirements.txt` (only `busylib` and `requests` are pre-approved) and
  keep the same `--host` convention.
- **`--host` aware, USB default `10.0.4.20`.** The same file runs unchanged
  against a USB bar, a Wi-Fi bar, or the emulator (`--host 127.0.0.1:8080`).
- **`APP` = the folder slug**, passed as `application_name` on every draw; it
  scopes clearing and asset uploads and must be unique in the gallery.
- **Raw-dict elements with an `id` and `#RRGGBBAA` colors.** Fonts are the
  device set: `tiny small normal condensed bold large extra_large`. Center on
  the 72×16 matrix with `x=36, y=8, align="center"` (align anchors the element
  box, so no text measuring is needed). Give each element a unique `id` when a
  frame has more than one.
- **Tolerate 409.** A higher-priority app may own the screen; keep ticking and
  retry rather than crashing. A one-shot app can call `tick()` once instead of
  looping.

## Step 3: create `apps/<slug>/manifest.yaml`

```yaml
name: <Title Case Name>
author: <github_username>
description: <one-line description, max 200 chars>
tags:
  - <tag>
preview: ./preview.png
```

Field rules (from `CONTRIBUTING.md`): `name` 1–50 chars, title case; `author`
the GitHub username; `description` 1–200 chars, plain text; `tags` 1–5 lowercase
(common: `clock weather info effect animation game monitor`); `preview` points
at the file added in step 4 (use `./preview.gif` for a GIF). Add `repo: <url>`
only if the app also lives in its own repository.

## Step 4: add a preview and test

- Generate the 720×160 preview with the **app-preview** skill:
  `app-preview <slug>` (add `--gif` for a recording). It saves
  `apps/<slug>/preview.<ext>`; make sure `manifest.yaml`'s `preview:` matches.
- Sanity-check the app runs against a running emulator:
  `python3 apps/<slug>/app.py --host 127.0.0.1:8080`.

Then submit per `CONTRIBUTING.md` (fork, add `apps/<slug>/`, open a PR).
