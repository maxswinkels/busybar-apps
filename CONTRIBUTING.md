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

Your app folder must contain exactly these files:

### `app.py` (required)

Your application's main Python file. Requirements:

- Single Python file (no subdirectories or multi-file packages)
- Only stdlib imports + `busybar.py` from the emulator repo
- App ID in format `yourname.appname` (e.g., `maxswinkels.clock`)
- Displays on a 72×16 RGB LED matrix
- Uses fonts: `tiny`, `small`, `normal`, `condensed`, `bold`, `large`, `extra_large`
- Uses colors in `0xRRGGBBAA` format (alpha channel required)

**Example stub:**
```python
#!/usr/bin/env python3
"""My app: a brief description in the docstring.

    python3 apps/my-app-slug/app.py [--host 127.0.0.1:8080]
"""
from busybar import BusyBar, host_from_argv, run_loop

APP = "yourname.myapp"
bar = BusyBar(host_from_argv())

def tick():
    # Your app logic here
    pass

if __name__ == "__main__":
    run_loop(tick, interval=1.0)
```

### `manifest.yaml` (required)

Metadata about your app. All fields are required:

```yaml
name: My App Name
author: github_username
description: A one-line description (max 200 characters).
tags:
  - category
  - effect
  - info
preview: ./preview.png
```

**Field constraints:**
- `name`: 1–50 characters, title case
- `author`: Your GitHub username (used for linking)
- `description`: 1–200 characters, plain text
- `tags`: Array of lowercase tags (1–5 recommended); common tags are `clock`, `weather`, `info`, `effect`, `animation`, `game`, `monitor`
- `preview`: Path to preview image (relative to app folder)

### `preview.png` or `preview.gif` (required)

A visual preview of your app in action.

**Format:**
- PNG or GIF
- Dimensions: 720×160 pixels (that's 72×16 LEDs × 10 pixels scale)
- Shows your app running on the BUSY Bar's LED display

**How to generate:**
1. Run your app against the [BUSY Bar Emulator](https://github.com/maxswinkels/busybar-emulator)
2. Screenshot or screen-record the LED display area
3. Scale it up (or down) to 720×160 pixels
4. Save as `preview.png` or `preview.gif` in your app folder

## Step 4: Test your submission

Before opening a pull request, verify:

- ✓ Folder is in `apps/<your-slug>/`
- ✓ `app.py` exists and runs: `python app.py --host 127.0.0.1:8080`
- ✓ `manifest.yaml` is valid YAML with all required fields
- ✓ `preview.png` or `preview.gif` exists (720×160)
- ✓ App respects the 72×16 display size
- ✓ All imports are stdlib or `busybar.py`
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
- [ ] `preview.png`/`.gif` included (720×160)
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
- **Document in the docstring:** The first line of your `app.py` docstring appears in app descriptions
- **Test visually:** Use the emulator to preview your app on the LED grid before submitting
- **Color your preview:** Make sure your preview PNG actually represents what your app displays
- **Follow conventions:** Use the APP ID format `yourname.appname` to avoid conflicts

## Questions?

Check the [BUSY Bar Emulator repo](https://github.com/maxswinkels/busybar-emulator) for API docs and example apps.

Happy building!
