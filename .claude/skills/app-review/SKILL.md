---
name: app-review
description: Review a community app submission end to end. Checks out the pull request, validates it against CONTRIBUTING.md, runs the app and watches every call it makes to the bar, regenerates the preview to compare against the submitted one, and writes a review summary plus a draft PR comment. Takes a PR number, or an app slug to review what is already checked out.
disable-model-invocation: true
---

# Review an app submission

Turns a submission into a written review in one pass. Arguments:
`<pr-number | app-slug> [-- <app args>]`.

This exists because the only CI gate is `npm run build`, which validates the
manifest schema and nothing else. Everything that actually breaks a submission
gets found by running it, and the same handful of problems keep recurring:
accumulating element ids, colours missing the alpha byte, an `APP` constant that
does not match the folder, a preview at the wrong size, an app that treats a 409
as fatal.

## The one thing to get right

**Never merge on a green check alone, and never trust the preview you were
sent.** A preview is just a file; it can be from a different version of the app,
from another device, or hand-made. Regenerating it (step 4) is what verifies
that the code in the PR actually produces what the gallery will show.

## Steps

1. **Get the branch into a worktree.** A submission is almost always based on a
   commit older than these tools, so checking it out in place would take
   `tools/` away with it. Use a worktree and keep running the tools from `main`:

   ```bash
   gh pr view <n> --json title,author,body,files,additions,deletions
   git fetch origin pull/<n>/head:pr<n>-review -f
   git worktree add /tmp/pr<n> pr<n>-review
   ```

   Both `busycheck.py` and `render.mjs` accept an absolute app folder, so every
   command below points at `/tmp/pr<n>/apps/<slug>/` while running from this
   checkout. Note which app folder the PR touches; a PR should add exactly one.
   Read the PR body for anything the author says about credentials or arguments.

2. **Read the code.** Open `/tmp/pr<n>/apps/<slug>/app.py` in full before running it. You
   are about to execute a stranger's code: check what it does with the network
   and the filesystem, and that any dependency in `requirements.txt` is
   justified in the PR (only `busylib` and `requests` are pre-approved).

3. **Validate and run.**

   ```bash
   python3 tools/busycheck.py /tmp/pr<n>/apps/<slug> --run --verbose
   ```

   This does the static pass (slug, manifest, preview dimensions, element ids,
   colours, `--host` default) and then records the app and reports its
   behaviour. If it needs arguments or credentials, the report says so; re-run
   with `-- --flag value`, or `busyrec --env KEY=VALUE`.

   Then check it survives losing the screen:

   ```bash
   python3 tools/busyrec.py /tmp/pr<n>/apps/<slug> --seconds 8 --steal-at 3
   ```

   A higher-priority app takes over at t=3. The app must keep running and keep
   trying, not crash or give up. 409 is normal on a real bar.

4. **Regenerate the preview and compare.**

   ```bash
   node tools/render/render.mjs /tmp/pr<n>/apps/<slug> --seconds 6 --loop \
     --out /tmp/review-<slug>.gif
   ```

   Read both `/tmp/review-<slug>.gif` and the submitted preview and say whether
   they show the same app. Different data or a different moment is fine. Two
   things are not: a preview showing something the code cannot produce, and a
   preview with no LED grid in it. The second means it was upscaled from the raw
   72x16 framebuffer with smoothing rather than captured as LEDs, which is what
   `CONTRIBUTING.md` means by real emulator output. It is easy to spot side by
   side: the device font is a 1-bpp bitmap, so genuine output has hard square
   pixels and no grey edges.

5. **Check it against real hardware if it is plugged in.** Optional but worth it
   for anything doing layout work, since the stub cannot catch everything:

   ```bash
   python3 tools/busyrec.py /tmp/pr<n>/apps/<slug> --seconds 6 --upstream 10.0.4.20
   ```

6. **Write the review.** Group findings as blocking / worth fixing / nice to
   have, and quote `file:line` for each. Draft a PR comment in Max's voice:
   short, direct, warm, concrete, and English regardless of the PR's language.
   Lead with what is good about the app. Do not post it; show it and let Max
   decide.

7. **Clean up.** Remove the worktree and its branch, plus anything the run
   left behind:

   ```bash
   git worktree remove /tmp/pr<n> --force
   git branch -D pr<n>-review
   ```

## What to look for beyond the automated checks

The tools cover the mechanical rules. These need judgement:

- **Polling interval.** Anything hitting a third-party API needs a default that
  is safe on that API's unauthenticated rate limit. 60 s against GitHub is
  borderline; 300 s is not.
- **Failure handling.** A 404 or 401 from an upstream API should fail loudly and
  stop, not retry forever in silence. Check what happens with a wrong argument.
- **Blocking the display.** `priority` should be modest (30 is the convention
  for ambient apps). A 100 belongs only to something genuinely urgent.
- **Readability at 72x16.** Text at `tiny` is legible; three columns of it is
  not. This is what the regenerated preview is for.
- **Secrets.** Read from the environment, never a literal in `app.py`, and
  documented in the docstring.

## What the stub cannot see

`busyrec` deliberately reproduces the firmware behaviours that break submissions:
draw validation, the accumulating 100-element cap (`MAX_ELEMENTS`, and the count
is checked before the priority check, exactly as on the bar) and priority
arbitration including the refusal to step down without releasing. Four things it
does not reproduce, each of which has shipped a broken app:

- **Element ids are type-locked on the device.** An id once drawn as a
  `rectangle` and later re-sent as a `text` returns `400 Bad request` on firmware
  1.1.1. `validate_draw_body` never looks at `type` and `merge_elements` replaces
  by id, so busyrec and the emulator both accept it silently. It bites the "park
  it off-screen at `x=-400`" idiom: a parked rectangle has to stay a rectangle
  (1x1, `fill_colors: ["#00000000"]`), not become a blank text element. Found on
  `github-actions`, where every draw failed once the progress bar was hidden.
- **Draw latency scales with element count, and the preview hides it.** The bar
  spends about 3.6 ms per element (1 element ~5.6 ms, 48 ~169 ms, 96 ~356 ms),
  so a full-screen rect animation runs at 3 to 6 fps there. busyrec answers in
  well under a millisecond and the renderer replays the app's own recorded
  timing, so the same app looks perfectly smooth in the regenerated preview.
  Count elements per frame rather than trusting it. Anything animating with more
  than ~30 belongs on the image path: upload a 72x16 PNG to
  `/api/assets/upload`, draw one `image` element, flat ~50 ms/frame (~19 fps)
  regardless of complexity. `pixel-fire`, `audio-visualizer` and `nyan-cat` were
  all rewritten that way after crashing or crawling on hardware.
- **Rapid re-upload of one asset filename returns `508`** (the asset is locked
  while a draw reads it). Image-push apps must rotate a ring of ~4 filenames.
  busyrec stores every upload without locking, so a single-filename app records
  flawlessly and stalls on the bar.
- **Native text sits lower on the device than in the renderer**: about 1px for
  `bold`, 2px for `small`. Image and sprite elements land identically. So any app
  that aligns text against a sprite, or stacks rows inside the 16px height, can
  look right in the preview and be clipped or overlapping on hardware. Step 5, or
  a human looking at a bar, is the only real check. `flightradar` and
  `moneybird-invoice-paid` both had to be tuned on the device.

## Pitfalls

- **A blank recording usually means arguments, not a broken app.** Alert apps
  only draw when they have something to report; the report tells you when
  `--test` is the answer.
- **BUSY-mode apps cannot be previewed locally at all.** If the app drives
  `PUT /api/busy/snapshot`, the emulator stores it and renders nothing, so the
  preview has to come from hardware. Do not ask the contributor to regenerate a
  preview that cannot be generated.
- **Element ids that grow per frame are the single most common real bug** and
  it is invisible in a short run. The recorder reports the stored set growing;
  trust that over "it worked when I tried it".
- **Kill the app if you interrupt a run**, and clear the screen afterwards
  (`curl -X DELETE http://127.0.0.1:8080/api/display/draw`) if you used
  `--upstream` against the emulator or a bar.
