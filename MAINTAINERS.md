# Reviewing apps

App submissions get tried out by hand before they are merged. If you would like
to help with that, this page describes what the job actually is.

**[Sign up as a reviewer →](https://github.com/maxswinkels/busybar-apps/issues/new?template=reviewer-signup.yml)**

## Why reviews are hands-on

CI only runs `npm run build`, which validates the manifest against the content
schema and nothing else. The `check` job that runs `busycheck` is explicitly
advisory and never fails a PR: a submission is discussed, not blocked on style.

So everything that actually breaks an app gets found by running it. The same
handful of problems keep coming back: element ids that accumulate every frame,
colours missing their alpha byte, an `APP` constant that does not match the
folder, a preview at the wrong size, an app that treats a `409` as fatal.

## What a review looks like

1. **Read `app.py` in full before running it.** You are about to execute a
   stranger's code: check what it does with the network and the filesystem, and
   that anything in `requirements.txt` is justified in the PR. Only `busylib`
   and `requests` are pre-approved.
2. **Run the checker:** `npm run check -- <slug> --run --verbose`. It does the
   static pass and then records the app and reports how it behaved.
3. **Take the screen away from it:**
   `python3 tools/busyrec.py apps/<slug> --seconds 8 --steal-at 3`. A
   higher-priority app takes over at t=3. The app must keep trying, not crash.
   `409` is normal on a real bar.
4. **Regenerate the preview and compare:** `npm run preview -- <slug>`. A
   preview is just a file; it can come from a different version of the app or be
   hand-made. Regenerating it is what proves the code in the PR produces what
   the gallery will show. Never merge on a green check alone.

## What needs judgement

The tools cover the mechanical rules. These do not:

- **Polling interval.** Anything hitting a third-party API needs a default that
  is safe on that API's unauthenticated rate limit. 60 s against GitHub is
  borderline; 300 s is not.
- **Failure handling.** A 404 or 401 upstream should fail loudly and stop, not
  retry forever in silence.
- **Blocking the display.** `priority` should be modest; 30 is the convention
  for ambient apps. 100 belongs only to something genuinely urgent.
- **Readability at 72×16.** Text at `tiny` is legible; three columns of it is
  not. This is what the regenerated preview is for.
- **Secrets.** Read from the environment, never a literal in `app.py`, and
  documented in the docstring.

## What you need

- Python 3, Node 22 and a checkout of this repo.
- A BUSY Bar **or** the [emulator](https://github.com/maxswinkels/busybar-emulator).
  Hardware is not required; the emulator has the same HTTP API, fonts and
  pixels. The one exception is a BUSY-mode app driving
  `PUT /api/busy/snapshot`, which cannot be previewed locally at all.

## What it is not

- No SLA and no rota. Pick up a PR when you have half an hour.
- No merge rights on day one. Leaving a review comment on an open PR is the
  whole contribution; that is genuinely useful on its own.
- No obligation to be thorough about everything. "I ran it for ten minutes and
  the element ids grow" is a better review than silence.

## Tone

Lead with what is good about the app, then group findings as blocking / worth
fixing / nice to have, and quote `file:line` for each. Submissions come from
people who built something for fun and want to share it. Keep it short, direct
and warm.

## Signing up

Open a [reviewer signup issue](https://github.com/maxswinkels/busybar-apps/issues/new?template=reviewer-signup.yml)
and say roughly how much time you have and what you would like to look at.
Reviewing a single open PR without asking anyone first is also a perfectly good
way to start.
