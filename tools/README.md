# Tools

Three small tools that turn "did this submission work, and does it have a decent
preview?" into two commands. Nothing here needs the emulator checked out.

```bash
npm run preview -- waves          # record + render apps/waves/preview.gif
npm run check -- waves            # validate against CONTRIBUTING.md
npm run check -- --all            # validate the whole gallery
```

## Why previews are generated rather than screen-recorded

An app's entire visual output is the stream of `POST /api/display/draw` bodies
plus whatever it uploads to `/api/assets/upload`. `busyrec.py` stands in for the
bar, records that stream with timestamps, and `render.mjs` replays it through
the emulator's own renderer.

Replaying off the clock is what fixes the stutter. The previous route captured a
live browser canvas on a `setInterval(50)` timer and then wrote every frame with
a fixed 50 ms delay; the timer drifted, so recorded motion and played-back
timing disagreed. Here a frame is rendered *at* the timestamp it claims.

Three other differences from the old capture:

- 720x160 is rendered directly at a 10 px LED pitch, instead of resampling a
  936x208 canvas by 1.3x with smoothing (which smeared the grid).
- one global 256-colour palette instead of a fresh palette per frame, so
  colours stop shimmering and the file compresses far better.
- identical consecutive frames are folded together, and the frame rate follows
  the app's own draw cadence, so a 11.4 Hz app is not sampled at 20 fps into
  alternating 50/100 ms delays.

## `busyrec.py` — record

```bash
python3 tools/busyrec.py waves --seconds 6
python3 tools/busyrec.py text-display -- --text "HELLO"     # args after -- go to the app
python3 tools/busyrec.py waves --steal-at 3                 # force a 409 mid-run
python3 tools/busyrec.py waves --upstream 127.0.0.1:8080    # proxy a real emulator or bar
```

Stdlib only. It binds a port, runs `app.py --host 127.0.0.1:<port>`, records
every call, then SIGINTs the app and reports what it saw: draw rate, element id
churn, rejected draws, how it handled a 409, whether it exited cleanly and
released the screen.

The built-in stub deliberately reproduces three firmware behaviours from
`busybar-emulator/server.js`, because these are where submissions actually
break:

- draw-body validation (`id` required, colours `#RRGGBBAA`, priority 1-100)
- the accumulating 100-element cap, which kills apps that mint fresh ids per
  frame after about 100 frames
- priority arbitration, which returns 409

`server.js` remains the source of truth. When either side changes, check they
still agree. `tools/conformance-probe/` is an app whose only job is to fire the
edge cases (missing ids, `0x` colours, priority 0 and 101, the accumulating cap,
and 409 arbitration in both directions). Record it against both backends and
diff the status codes:

```bash
node server.js &                                                   # in busybar-emulator
python3 tools/busyrec.py tools/conformance-probe --seconds 20 --out /tmp/stub.busyrec
python3 tools/busyrec.py /tmp/stub.busyrec --conformance 127.0.0.1:8080
```

The probe prints its own case-by-case results to stdout, which the recording
captures, so you can also diff a standalone run against an `--upstream` run
directly.

Apps that need button or wheel input (`/api/status/ws`) are not stubbed; record
those with `--upstream` against a running emulator.

## `render.mjs` — replay

```bash
npm run preview -- waves --seconds 8 --loop
npm run preview -- clock --png
npm run preview -- waves --from waves.busyrec        # skip re-recording
```

`--loop` trims to the best seamless loop point, which is usually the right call
for animated apps and cuts the file size a lot. `--fps` defaults to `auto`.
If an app draws nothing (alert apps only draw when they have something to
report), it retries once with `--test`.

Rendering uses `vendor/`, copied unmodified from busybar-emulator; see
[`vendor/VENDOR.md`](render/vendor/VENDOR.md).

## `busycheck.py` — validate

Enforces the CONTRIBUTING.md rules that the site build does not: kebab-case
slug, `APP` matching the folder, preview exactly 720x160 in PNG or GIF, colours
`#RRGGBBAA`, every element carrying an `id`, `--host` defaulting to `10.0.4.20`.

`--run` additionally records the app and folds in the behaviour findings.

## A note on `npm run preview`

This used to be `astro preview` (serve the built site). That is now
`npm run serve`.
