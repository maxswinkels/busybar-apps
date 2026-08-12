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

1. **Get the branch.** For a PR number:

   ```bash
   gh pr view <n> --json title,author,body,files,additions,deletions
   gh pr checkout <n>
   ```

   Note which app folder the PR touches; that slug drives everything below. A PR
   should add exactly one `apps/<slug>/` folder. Read the PR body for anything
   the author says about credentials or required arguments.

2. **Read the code.** Open `apps/<slug>/app.py` in full before running it. You
   are about to execute a stranger's code: check what it does with the network
   and the filesystem, and that any dependency in `requirements.txt` is
   justified in the PR (only `busylib` and `requests` are pre-approved).

3. **Validate and run.**

   ```bash
   npm run check -- <slug> --run --verbose
   ```

   This does the static pass (slug, manifest, preview dimensions, element ids,
   colours, `--host` default) and then records the app and reports its
   behaviour. If it needs arguments or credentials, the report says so; re-run
   with `-- --flag value`, or `busyrec --env KEY=VALUE`.

   Then check it survives losing the screen:

   ```bash
   python3 tools/busyrec.py <slug> --seconds 8 --steal-at 3
   ```

   A higher-priority app takes over at t=3. The app must keep running and keep
   trying, not crash or give up. 409 is normal on a real bar.

4. **Regenerate the preview and compare.**

   ```bash
   npm run preview -- <slug> --seconds 6 --loop --out /tmp/review-<slug>.gif
   ```

   Read both `/tmp/review-<slug>.gif` and the submitted `apps/<slug>/preview.gif`
   and say whether they show the same app. A mismatch is not automatically a
   problem (different data, different moment) but a preview showing something
   the code cannot produce is.

5. **Check it against real hardware if it is plugged in.** Optional but worth it
   for anything doing layout work, since the stub cannot catch everything:

   ```bash
   python3 tools/busyrec.py <slug> --seconds 6 --upstream 10.0.4.20
   ```

6. **Write the review.** Group findings as blocking / worth fixing / nice to
   have, and quote `file:line` for each. Draft a PR comment in Max's voice:
   short, direct, warm, concrete, and English regardless of the PR's language.
   Lead with what is good about the app. Do not post it; show it and let Max
   decide.

7. **Clean up.** `git checkout main` (or the branch you started on), and remove
   any `.busyrec` files and `apps/<slug>/.venv` the run created.

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
