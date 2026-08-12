---
name: app-preview
description: Generate a gallery app's 720x160 preview.png or preview.gif by recording its draw stream and replaying it deterministically, saved into the app's folder. Takes the app slug plus optional --seconds, --loop, --png and any arguments the app itself needs. No emulator checkout, browser or Playwright required.
disable-model-invocation: true
---

# Capture an app preview

Produces the `preview.gif` / `preview.png` that `CONTRIBUTING.md` requires:
720x160, real render output, in the app's folder.

Arguments: `<app-slug> [--seconds N] [--loop] [--png] [-- <app args>]`

This used to drive Playwright against a running emulator and sample its canvas
on a timer. It no longer does: `tools/busyrec.py` records the app's draw stream
and `tools/render/render.mjs` replays it through the emulator's renderer
(vendored under `tools/render/vendor/`). One command, no emulator, no browser,
and the timing comes out exact instead of jittery.

## Steps

1. **Generate it.** From the repo root:

   ```bash
   npm run preview -- <app-slug> --seconds 6
   ```

   Add `--loop` for anything animated: it trims to a seamless loop point and
   usually cuts the file size substantially. Add `--png` for a still. Pass app
   arguments after `--`, e.g. `npm run preview -- text-display -- --text "HI"`.

   The command records first (you will see the busyrec report, which is worth
   reading) and then renders. It writes straight to `apps/<slug>/preview.gif`
   unless you pass `--out`.

2. **Look at it.** Read the generated file. Check the app is actually visible,
   is not clipped at the edges of the 72x16 grid, and that the window caught the
   interesting part of the app rather than a loading state. `--start <seconds>`
   skips further into the recording.

3. **Check it passes.** `npm run check -- <app-slug>` must report no errors:
   that is what validates the 720x160 dimensions and the file size budget.

4. **Report** the path, dimensions, frame count and size, plus anything the
   busyrec report flagged about the app itself.

## Notes

- **An app that draws nothing is usually not broken.** Alert-style apps
  (`iss-alert`, `pollen-alarm`, `buienradar-alarm`) only draw when they have
  something to report. The tool retries once with `--test` automatically; if an
  app has no `--test` flag, it genuinely has nothing to preview right now.
- **BUSY-mode apps cannot be previewed this way.** An app that drives
  `PUT /api/busy/snapshot` (like `busy-defaults`) relies on the firmware's theme
  animations, which the emulator stores but never renders. Those previews still
  have to come off real hardware. `busycheck` flags such apps so this is not a
  surprise.
- **Apps that need button or wheel input** (`/api/status/ws`) are not covered by
  the built-in stub. Start the emulator and record through it:
  `npm run preview -- <slug> --upstream 127.0.0.1:8080`.
- Apps needing credentials read them from the environment; pass them through
  with `busyrec --env KEY=VALUE` and `--from` the resulting recording.
- Stock icons and stock animations render only when a `busybar-emulator`
  checkout sits next to this repo; the tool says so when it needs one.
