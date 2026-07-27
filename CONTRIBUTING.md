# Contributing: Submit your app

Thanks for building for BUSY Bar! Here's how to submit your app to the community gallery.

## Step 1: Fork and clone

Fork this repository, then clone your fork locally:

```bash
git clone https://github.com/YOUR_USERNAME/busybar-apps.git
cd busybar-apps
```

## Step 2: Create your app folder

Create a new directory in `apps/` with a **kebab-case slug** that matches your app's name:

```bash
mkdir apps/your-app-slug
```

**Slug rules:**
- Use lowercase letters, numbers, and hyphens only
- Keep it short and descriptive
- The folder name is the app's identifier

## Step 3: Add your files

Your app folder must contain these files:

### `app.py` (required)

Your application's main Python file. Requirements:

- One Python entrypoint, self-contained in your app folder (no shared config or helper files outside it)
- Accepts `--host <ip[:port]>` and defaults to `10.0.4.20` (USB), so the same file runs unchanged against a USB bar, a Wi-Fi bar, or the emulator
- App ID (`application_name`) is simply your app's name, matching the folder slug (e.g., `clock`)
- Displays on a 72×16 RGB LED matrix
- Uses fonts: `tiny`, `small`, `normal`, `condensed`, `bold`, `large`, `extra_large`
- Uses colors in `0xRRGGBBAA` format (alpha channel required)

**Dependencies are allowed.** You don't have to rewrite an existing app to submit it. Stdlib-only apps are preferred: they get the "zero-install" badge in the gallery (grab one file and run it, no setup). But if your app is built on a library, just add a `requirements.txt` (see below). Pre-approved:

- [`busylib`](https://pypi.org/project/busylib/): Flipper's official Python client for the BUSY Bar
- [`requests`](https://pypi.org/project/requests/)

Other packages are reviewed case-by-case; briefly explain in your PR why the app needs them.

**Example stub (zero-install, stdlib only):**
```python
#!/usr/bin/env python3
"""My app: a brief description in the docstring.

    python app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
"""
import json
import sys
import time
import urllib.error
import urllib.request

APP = "my-app"

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

def text(txt, x=0, y=0, font="normal", color="0xFFFFFFFF", **kw):
    return {"type": "text", "text": str(txt), "x": x, "y": y, "font": font, "color": color, **kw}

# --- app -------------------------------------------------------------------

def tick():
    try:
        draw([text("HELLO", x=36, y=8, font="large", align="center")])
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409 = a higher-priority app owns the display
            raise

if __name__ == "__main__":
    print(f"myapp → {BASE}  (Ctrl-C to stop)")
    try:
        while True:
            tick()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopped.")
```

**Using `busylib` instead:** keep the same `--host` convention and add a `requirements.txt`:

```python
#!/usr/bin/env python3
"""My app: a brief description in the docstring."""
import argparse

from busylib import BusyBar

p = argparse.ArgumentParser()
p.add_argument("--host", default="10.0.4.20")  # USB default; emulator: --host 127.0.0.1:8080
args = p.parse_args()

bb = BusyBar(args.host)
```

### `manifest.yaml` (required)

Metadata about your app. All fields are required unless marked optional:

```yaml
name: My App Name
author: github_username
description: A one-line description (max 200 characters).
tags:
  - category
  - effect
  - info
preview: ./preview.png
repo: https://github.com/github_username/my-app  # optional
```

**Field constraints:**
- `name`: 1–50 characters, title case
- `author`: Your GitHub username (used for linking)
- `description`: 1–200 characters, plain text
- `tags`: Array of lowercase tags (1–5 recommended); common tags are `clock`, `weather`, `info`, `effect`, `animation`, `game`, `monitor`
- `preview`: Path to preview image (relative to app folder)
- `repo` (optional): Full URL to your own GitHub repository for this app. When set, the app page links here instead of to the monorepo folder.

### `preview.png` or `preview.gif` (required)

A visual preview of your app in action.

**Format:**
- PNG or GIF
- Dimensions: 720×160 pixels (that's 72×16 LEDs × 10 pixels scale)
- Shows your app running on the BUSY Bar's LED display
- Must be actual emulator or hardware output. Mocked-up or AI-generated previews are rejected (they hide layout bugs the real display would show)

**How to generate:**
1. Run your app against the [BUSY Bar Emulator](https://github.com/maxswinkels/busybar-emulator)
2. Screenshot or screen-record the LED display area
3. Scale it up (or down) to 720×160 pixels
4. Save as `preview.png` or `preview.gif` in your app folder

### `requirements.txt` (optional)

Only for apps with dependencies: standard pip format, one package per line (e.g. `busylib>=1.0`). Leave it out entirely for stdlib-only apps; its absence is what earns the zero-install badge. Users install once with `pip install -r requirements.txt`; the [emulator](https://github.com/maxswinkels/busybar-emulator)'s Apps tab creates a virtualenv from it automatically.

## Step 4: Test your submission

Before opening a pull request, verify:

- ✓ Folder is in `apps/<your-slug>/`
- ✓ `app.py` exists and runs: `python app.py --host 127.0.0.1:8080`
- ✓ `manifest.yaml` is valid YAML with all required fields
- ✓ `preview.png` or `preview.gif` exists (720×160) and is real emulator/hardware output
- ✓ App respects the 72×16 display size
- ✓ Dependencies (if any) are listed in `requirements.txt`, and the app runs in a fresh venv: `python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python app.py --host 127.0.0.1:8080`
- ✓ Code is your own or properly licensed (MIT preferred)

## Step 5: Open a pull request

Push to your fork and open a PR against the main branch:

```bash
git add apps/your-app-slug/
git commit -m "Add my-app-slug"
git push origin main
```

Then go to GitHub and open a pull request. Use this checklist in your PR description:

```markdown
- [ ] Folder structure is correct (`apps/<slug>/`)
- [ ] `manifest.yaml` has all required fields
- [ ] `app.py` tested against emulator or real hardware
- [ ] `preview.png`/`.gif` included (720×160, real emulator/hardware output)
- [ ] Dependencies (if any) listed in `requirements.txt`
- [ ] Code is my own or MIT-licensed
- [ ] Folder name (slug) follows kebab-case rules
```

## Step 6: CI validation

Once you open the PR:

- GitHub Actions will run `npm run build`, which validates your manifest against the content schema
- The site will build a preview of your app page
- If validation fails, the PR shows which fields need fixing

Fix any errors and push again; the PR updates automatically.

## Tips

- **Keep it simple:** A single focused app is better than a complex, feature-heavy one
- **Prefer zero-install:** if stdlib is enough, skip `requirements.txt`; one-file apps are the easiest for others to grab
- **Document in the docstring:** The first line of your `app.py` docstring appears in app descriptions
- **Test visually:** Use the emulator to preview your app on the LED grid before submitting
- **Color your preview:** Make sure your preview PNG actually represents what your app displays
- **Follow conventions:** use your folder slug as the APP ID; slugs are unique in the gallery, so it cannot clash

## Questions?

Check the [BUSY Bar Emulator repo](https://github.com/maxswinkels/busybar-emulator) for API docs and example apps.

Happy building!
