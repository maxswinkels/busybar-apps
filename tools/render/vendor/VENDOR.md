# Vendored from busybar-emulator

These files are copied **byte-for-byte** from
[busybar-emulator](https://github.com/maxswinkels/busybar-emulator). They are
what makes a generated preview look exactly like the emulator, so they must not
be edited here.

| File | Source | Vendored at |
|---|---|---|
| `renderer.js` | `web/src/lib/renderer.js` | `46648bc` |
| `atlas.js` | `web/src/lib/atlas.js` | `46648bc` |
| `font-atlas.json` | `public/fonts/font-atlas.json` | `46648bc` |

## Deltas

**None.** That is deliberate. `renderer.js` is a browser module: it reaches for
`document`, `Image`, `fetch`, `FontFace`, `requestAnimationFrame` and
`performance`. Rather than patch it, `../shim.mjs` provides those globals, so
re-vendoring is a plain copy:

```bash
cp ../../busybar-emulator/web/src/lib/{renderer,atlas}.js tools/render/vendor/
cp ../../busybar-emulator/public/fonts/font-atlas.json tools/render/vendor/
diff -q tools/render/vendor/renderer.js ../busybar-emulator/web/src/lib/renderer.js
```

(busybar-manager vendors the same renderer for its live mirror, but with three
patches. Keeping this copy clean is the difference between a `cp` and a merge.)

## What the shim has to keep working

If a future emulator change touches any of these, `../shim.mjs` needs the
matching update:

- `document.createElement("canvas")` for the background and image-sampling
  canvases, and `ctx.drawImage` receiving `Image` instances.
- `new Image()` with an `onload` callback and `naturalWidth`/`naturalHeight`.
  The shim resolves these out of a pre-populated decode cache; see the comment
  in `shim.mjs` for why a miss cannot be repaired mid-pass.
- `fetch` for `/public/fonts/font-atlas.json`, `/public/icons.json` and
  `/api/_animations`, plus `/assets/<app>/<file>` for uploaded assets.
- `requestAnimationFrame`, which the shim captures rather than schedules. This
  is how the driver controls the clock, and therefore how frame timing stays
  exact.
- `performance.now()`, read once when `createRenderer` is called to seed the
  frame delta.

## Not reproduced here

`font-atlas.json` covers text. Stock icons (`/public/icons/`) and stock
animation frames (`/public/animations/`, 22 MB) are **not** vendored: only one
gallery app uses each. When a checkout of busybar-emulator sits next to this
repo, `render.mjs` picks those up automatically; otherwise it renders without
them and says so.
