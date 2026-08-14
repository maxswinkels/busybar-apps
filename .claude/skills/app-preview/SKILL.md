---
name: app-preview
description: Run a gallery app in the BUSY Bar emulator and capture a 720×160 preview PNG or GIF of the LED display (the CONTRIBUTING.md format), saved into the app's folder. Takes the app slug plus optional --gif [seconds] and --out <path>.
disable-model-invocation: true
---

# Capture an app preview

Automates the preview step from `CONTRIBUTING.md`: run an app against the
emulator, capture the LED display canvas at 720×160 (72×16 LEDs × 10 px), and
save it as `preview.png` / `preview.gif` in the app's folder. Rendering happens
in a real browser via Playwright, so the preview is pixel-identical to the
emulator UI.

Arguments: `<app-slug> [app-args...] [--gif [seconds]] [--out <path>]`

- `<app-slug>`: a folder under `apps/` (e.g. `clock`, `flightradar`). Runs
  `apps/<app-slug>/app.py`. Anything between the slug and the flags is passed to
  the app as args.
- `--gif [seconds]`: record a GIF (default 6 s, max 15, the storage body cap is
  8 MB) instead of a PNG snapshot.
- `--out <path>`: output file. Default `apps/<app-slug>/preview.png` (or `.gif`
  with `--gif`), exactly where the gallery expects it.

This skill needs the [BUSY Bar Emulator](https://github.com/maxswinkels/busybar-emulator)
checked out as a sibling folder (`../busybar-emulator`); it renders the LEDs.
The emulator no longer runs apps itself, so this skill launches the app as a
plain subprocess pointed at it.

## Steps

1. **Emulator.** Check `curl -sf http://127.0.0.1:8080/api/version`. If it is not
   up, start `node server.js` from `../busybar-emulator` in the background and
   remember that you own it (stop it again in step 6). If its `web/dist/` is
   missing, build it first: `npm --prefix ../busybar-emulator/web run build`.

2. **Run the app.** Clear the screen first so nothing leaks into the capture:
   `curl -sf -X DELETE 'http://127.0.0.1:8080/api/display/draw'`. Then launch the
   app pointed at the emulator, in the background, and remember its PID:
   - Stdlib app (no `requirements.txt`):
     `python3 apps/<app-slug>/app.py --host 127.0.0.1:8080 [app-args...]`
   - App with `requirements.txt`: create a venv once and run from it:
     `python3 -m venv apps/<app-slug>/.venv`,
     `apps/<app-slug>/.venv/bin/pip install -q -r apps/<app-slug>/requirements.txt`,
     then run `apps/<app-slug>/.venv/bin/python app.py --host 127.0.0.1:8080 [app-args...]`
     from inside the app folder (some apps read relative paths).
   Give it a couple of seconds to draw. Confirm something is actually on screen
   before capturing (poll the SSE snapshot or `GET /api/status`); slow-starting
   apps may need longer.

3. **Open the UI.** Load the Playwright tools in ONE ToolSearch call
   (`select:mcp__playwright__browser_navigate,mcp__playwright__browser_evaluate,mcp__playwright__browser_close`),
   navigate to `http://127.0.0.1:8080`, then wait ~2 s so the SSE stream and the
   app's first frames have arrived.

4. **Capture.** Use `browser_evaluate` with the matching snippet from this
   skill's directory. Pass only the `async () => { ... }` arrow function as the
   `function` parameter, dropping the leading `//` comment lines:
   - PNG: `capture_png.js` as-is.
   - GIF: `record_gif.js`, first replacing the `SECONDS = 6` literal if the user
     gave a duration. The evaluate call blocks for the full recording; that is
     expected.
   The snippet stores the result in emulator storage under `_preview.png` /
   `_preview.gif` and returns the byte size.

5. **Retrieve and clean up storage.**
   ```bash
   curl -sf -o <out> 'http://127.0.0.1:8080/api/storage/read?path=_preview.png'
   curl -sf -X DELETE 'http://127.0.0.1:8080/api/storage/remove?path=_preview.png'
   ```
   (same with `.gif`). The remove matters: emulator storage persists to
   `.data/state.json` and must not accumulate preview blobs.

6. **Tear down.** Kill the app process from step 2 (`kill <pid>`), release the
   screen (`curl -sf -X DELETE 'http://127.0.0.1:8080/api/display/draw'`), close
   the browser tab, and stop the emulator if you started it in step 1.

7. **Verify and report.** Run `file <out>` and check it reports the right type
   and `720 x 160`. Report the saved path, dimensions, file size, and (for GIFs)
   the frame count returned by the snippet.

## Notes

- The GIF path imports gifenc from unpkg inside the page, so it needs network
  access; the PNG path is fully offline.
- Capture storage keys are namespaced `_preview.*` and always removed; never
  leave them behind, even on failure. Kill the app process even on failure so it
  does not keep drawing to the emulator.
- The preview must be real emulator (or hardware) output at 720×160, per
  `CONTRIBUTING.md`; that is exactly what this skill produces.
