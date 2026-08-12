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
- Uses colors in `#RRGGBBAA` format (alpha channel required)

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

**How to generate:** one command, no emulator needed.

```bash
npm ci                        # once
npm run preview -- your-app-slug
```

That runs your app against a stand-in for the bar, records everything it draws,
and writes `apps/your-app-slug/preview.gif` at exactly 720×160. Useful flags:

- `--loop` for animated apps: trims to a seamless loop and shrinks the file a lot
- `--png` for a still instead of a recording
- `--seconds N` to record longer (default 6), `--start N` to skip past a loading state
- anything after `--` is passed to your app: `npm run preview -- my-app -- --city Utrecht`

If your app only draws when it has something to report, give it a `--test` flag
that draws one frame and exits; the tool uses it automatically.

Two cases still need real hardware or a running
[emulator](https://github.com/maxswinkels/busybar-emulator): apps that drive
BUSY modes (`PUT /api/busy/snapshot`), because theme animations are firmware
side, and apps that read button or wheel input over `/api/status/ws`. For the
latter, start the emulator and add `--upstream 127.0.0.1:8080`.

### `requirements.txt` (optional)

Only for apps with dependencies: standard pip format, one package per line (e.g. `busylib>=1.0`). Leave it out entirely for stdlib-only apps; its absence is what earns the zero-install badge. Users install once with `pip install -r requirements.txt`; [busybar-manager](https://github.com/maxswinkels/busybar-manager) creates the virtualenv from it automatically when it runs your app.

## Step 4: Test your submission

Run the checker. It enforces everything on this page, so if it is happy your PR
will be too:

```bash
npm run check -- your-app-slug --run
```

`--run` also runs your app and reports how it behaved: how often it draws,
whether it survives losing the screen, whether it exits cleanly. Fix the errors,
and read the warnings — they are the things reviewers would otherwise raise by
hand.

It checks, among other things:

- ✓ Folder is `apps/<your-slug>/` and the slug is kebab-case
- ✓ `APP` in `app.py` matches the folder name
- ✓ `app.py` accepts `--host` and defaults to `10.0.4.20`
- ✓ `manifest.yaml` has all required fields and no extra ones
- ✓ `preview.png` / `preview.gif` is exactly 720×160
- ✓ Every draw element has an `id`, and colours are `#RRGGBBAA`
- ✓ Elements stay inside the 72×16 display, and element ids do not accumulate
- ✓ The app tolerates a `409` and releases the screen when it stops

Two things it cannot check, which are still on you: that the code is your own or
properly licensed (MIT preferred), and that any dependency in `requirements.txt`
is one you can justify in the PR.

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
