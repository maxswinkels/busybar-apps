#!/usr/bin/env node
/* Replay a .busyrec into apps/<slug>/preview.gif (or .png).
 *
 *   npm run preview -- waves
 *   npm run preview -- waves --seconds 8 --loop
 *   npm run preview -- text-display -- --text "HELLO"
 *
 * Rendering uses the emulator's own renderer, vendored byte-for-byte under
 * vendor/, so previews look exactly like the emulator. What changes is WHEN
 * frames are produced: the browser capture sampled a live canvas on a 50 ms
 * timer and then claimed every frame was 50 ms apart, which is where the
 * stutter came from. Here each frame is rendered AT the timestamp it claims,
 * off the clock entirely, so content and timing cannot drift apart.
 *
 * Three more things the old capture got wrong, fixed here:
 *   - it downscaled a 936x208 canvas to 720x160 (a 1.3x non-integer resample
 *     with smoothing), which smeared the LED grid. We render straight at a
 *     10 px pitch.
 *   - it quantized every frame to its own 256-colour palette, so colours
 *     shimmered between frames. We build one global palette.
 *   - it never deduplicated, so a mostly-static app still cost 120 frames.
 */
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
// gifenc has no "exports" map and its `main` is CJS, so Node's ESM loader will
// not hand out named exports. Reach for the ESM build it ships as `module`.
import { GIFEncoder, quantize, applyPalette } from 'gifenc/dist/gifenc.esm.js'
import { installShims } from './shim.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..', '..')
const W = 720, H = 160          // 72x16 LEDs at a 10 px pitch
const DEFAULT_MAX_BYTES = 1_000_000

// ---------------------------------------------------------------- arguments

function parseArgs(argv) {
  const opts = {
    seconds: 6, fps: 'auto', start: null, png: false, loop: false,
    out: null, from: null, maxBytes: DEFAULT_MAX_BYTES, appArgs: [],
    emulatorAssets: path.resolve(ROOT, '..', 'busybar-emulator', 'public'),
    keepRecording: false, upstream: null,
  }
  const rest = []
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--') { opts.appArgs = argv.slice(i + 1); break }
    else if (a === '--seconds') opts.seconds = Number(argv[++i])
    else if (a === '--fps') { const v = argv[++i]; opts.fps = v === 'auto' ? 'auto' : Number(v) }
    else if (a === '--start') opts.start = Number(argv[++i])
    else if (a === '--png') opts.png = true
    else if (a === '--loop') opts.loop = true
    else if (a === '--out') opts.out = argv[++i]
    else if (a === '--from') opts.from = argv[++i]
    else if (a === '--upstream') opts.upstream = argv[++i]
    else if (a === '--max-bytes') opts.maxBytes = Number(argv[++i])
    else if (a === '--emulator-assets') opts.emulatorAssets = argv[++i]
    else if (a === '--keep-recording') opts.keepRecording = true
    else if (a === '--help' || a === '-h') { usage(); process.exit(0) }
    else if (a.startsWith('-')) { console.error('unknown option ' + a); usage(); process.exit(2) }
    else rest.push(a)
  }
  opts.slug = rest[0]
  if (!opts.slug && !opts.from) { usage(); process.exit(2) }
  return opts
}

function usage() {
  console.log(`usage: npm run preview -- <slug> [options] [-- <app args>]

  --seconds N        length of the preview (default 6)
  --fps N|auto       frames per second (default auto: follow the app's cadence)
  --start T          skip into the recording, seconds (default: the first draw)
  --png              write a single-frame preview.png instead of a GIF
  --loop             trim to the best seamless loop point
  --out PATH         output file (default apps/<slug>/preview.gif)
  --from FILE        replay an existing .busyrec instead of recording
  --upstream HOST    record through a running emulator or a real bar
  --max-bytes N      shrink the GIF until it fits (default 1000000)
  --emulator-assets  path to busybar-emulator/public, for stock icons
  --keep-recording   do not delete the .busyrec afterwards`)
}

// ------------------------------------------------------------------ recording

function record(opts, extraArgs = []) {
  const out = path.join(ROOT, '.busyrec-tmp.json')
  const appArgs = [...opts.appArgs, ...extraArgs]
  const args = [path.join(ROOT, 'tools', 'busyrec.py'), opts.slug,
    '--seconds', String(opts.seconds + 2), '--out', out]
  if (opts.upstream) args.push('--upstream', opts.upstream)
  if (appArgs.length) args.push('--', ...appArgs)
  const res = spawnSync('python3', args, { stdio: 'inherit', cwd: ROOT })
  if (!fs.existsSync(out)) {
    console.error('\nrender: no recording produced, cannot make a preview')
    process.exit(res.status || 1)
  }
  return out
}

const hasDrawnFrame = (rec) => rec.events.some((e) => e.frame && e.frame.elements.length)

// Accept `waves`, `apps/waves`, or an absolute path. Reviewing a pull request
// means pointing this at a worktree that does not carry the tools itself.
function resolveAppDir(slug) {
  for (const candidate of [slug, path.resolve(ROOT, slug), path.join(ROOT, 'apps', slug)]) {
    if (candidate && fs.existsSync(path.join(candidate, 'app.py'))) return path.resolve(candidate)
  }
  return null
}

// Alert apps only draw when they have something to report, so on a quiet day
// they produce an empty recording. They carry a --test flag for exactly this;
// use it rather than writing out a blank preview.
function recordWithFallback(opts) {
  let file = record(opts)
  let rec = JSON.parse(fs.readFileSync(file, 'utf8'))
  if (hasDrawnFrame(rec) || opts.appArgs.includes('--test')) return { file, rec }
  const dir = resolveAppDir(opts.slug)
  const source = dir && path.join(dir, 'app.py')
  const supportsTest = source && fs.readFileSync(source, 'utf8').includes('"--test"')
  if (!supportsTest) return { file, rec }
  console.log('\nrender: nothing was drawn, retrying with --test\n')
  file = record(opts, ['--test'])
  rec = JSON.parse(fs.readFileSync(file, 'utf8'))
  return { file, rec }
}

// ------------------------------------------------------------------ timeline

function buildTimeline(rec) {
  const frames = []                 // {t, application_name, elements}
  const assets = new Map()          // key -> [{t, sha}]
  for (const e of rec.events) {
    if (e.asset && e.sha) {
      if (!assets.has(e.asset)) assets.set(e.asset, [])
      assets.get(e.asset).push({ t: e.t, sha: e.sha })
    }
    if (e.frame) frames.push({ t: e.t, ...e.frame })
  }
  return { frames, assets }
}

/* Pick a frame rate that matches what the app actually does.
 *
 * Sampling an 11.4 Hz app on a fixed 20 fps grid aliases: most output frames
 * repeat their predecessor and some do not, so after deduplication the delays
 * alternate 50/100 ms and the result judders even though every frame is
 * correct. Matching the app's own cadence gives one output frame per draw and a
 * uniform delay.
 *
 * The exception is anything the renderer animates BETWEEN draws: scrolling text
 * integrates the frame delta, and animations advance on their own clock. Those
 * need a high rate no matter how rarely the app redraws.
 */
function autoFps(drawn, start, limit) {
  const inWindow = drawn.filter((f) => f.t >= start && f.t <= limit)
  const continuous = inWindow.some((f) => (f.elements || []).some(
    (el) => el.scroll_rate > 0 || el.type === 'animation' || el.type === 'countdown'))
  if (continuous || inWindow.length < 3) return 20

  const gaps = []
  for (let i = 1; i < inWindow.length; i++) gaps.push(inWindow[i].t - inWindow[i - 1].t)
  gaps.sort((a, b) => a - b)
  const median = gaps[gaps.length >> 1]
  if (!median || !isFinite(median)) return 20
  // GIF delays are whole centiseconds, so land on one exactly rather than
  // letting the encoder round every frame slightly differently.
  const cs = Math.min(20, Math.max(4, Math.round(median * 100)))
  return 100 / cs
}

const frameAt = (frames, t) => {
  let lo = 0, hi = frames.length - 1, best = null
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (frames[mid].t <= t) { best = frames[mid]; lo = mid + 1 } else hi = mid - 1
  }
  return best
}

// ------------------------------------------------------------------ resolver

function makeResolver(rec, assets, emuPublic) {
  const blobs = new Map()
  for (const [sha, b64] of Object.entries(rec.assets || {})) {
    blobs.set(sha, Buffer.from(b64, 'base64'))
  }
  const fontAtlas = fs.readFileSync(path.join(HERE, 'vendor', 'font-atlas.json'))
  const missing = new Set()

  // Which upload was current for this asset key at this point in the recording.
  // Apps rotate filenames (waves cycles frame0..frame3), so "latest upload of
  // this name at or before the draw that referenced it" is the only correct
  // answer; keying by name alone would collapse every frame onto four images.
  const assetAt = (key, at) => {
    const log = assets.get(key)
    if (!log) return null
    let found = null
    for (const entry of log) { if (entry.t <= at + 1e-6) found = entry; else break }
    const sha = (found || log[0]).sha
    const buf = blobs.get(sha)
    return buf ? { buf, sha } : null
  }

  const readIf = (file) => { try { return fs.readFileSync(file) } catch { return null } }

  const debug = !!process.env.BUSY_DEBUG
  const hit = (buffer, key) => (buffer ? { buffer, key } : null)

  const resolve = (url) => {
    const [p, qs] = String(url).split('?')
    if (p === '/public/fonts/font-atlas.json') return hit(fontAtlas, 'font-atlas')
    if (p.startsWith('/assets/')) {
      const name = decodeURI(p.slice('/assets/'.length))
      const v = new URLSearchParams(qs || '').get('v')
      const found = assetAt(name, v == null ? Infinity : Number(v))
      // Key on the content hash, not the URL: the renderer appends a changing
      // ?v= on every draw to defeat browser caching, and we must not let that
      // turn into a fresh decode of bytes we already have.
      return found ? { buffer: found.buf, key: 'sha:' + found.sha } : null
    }
    if (emuPublic) {
      if (p === '/public/icons.json') return hit(readIf(path.join(emuPublic, 'icons.json')) || Buffer.from('{}'), 'icons.json')
      if (p.startsWith('/public/icons/')) {
        const file = path.join(emuPublic, p.slice('/public/'.length))
        return hit(readIf(file), 'file:' + file)
      }
      if (p.startsWith('/animations/')) {
        const file = path.join(emuPublic, p)
        return hit(readIf(file), 'file:' + file)
      }
    }
    // Without an emulator checkout the bundled catalogues come back empty, which
    // is harmless: the renderer asks for them on every run whether or not the app
    // uses them. Only an actual miss on a referenced file is worth reporting.
    if (p === '/public/icons.json') return hit(Buffer.from('{}'), 'icons.json')
    if (p === '/api/_animations') return hit(Buffer.from('{}'), 'anims')
    if (p.startsWith('/public/icons/')) missing.add('stock icons')
    else if (p.startsWith('/animations/')) missing.add('stock animations')
    return null
  }

  return {
    missing,
    resolve: debug
      ? (url) => { const r = resolve(url); console.error('[resolve] %s -> %s', url, r ? r.key : 'MISS'); return r }
      : resolve,
  }
}

// -------------------------------------------------------------------- render

async function renderFrames(rec, opts) {
  const { frames, assets } = buildTimeline(rec)
  const drawn = frames.filter((f) => f.elements && f.elements.length)
  if (!drawn.length) {
    console.error('render: the recording contains no drawn frame, nothing to preview')
    process.exit(1)
  }

  const start = opts.start != null ? opts.start : drawn[0].t
  // Stop before the app clears the screen on its way out, or we would capture
  // the emulator's idle scroller instead of the app. A blank that is redrawn
  // right away is not an exit: swapping screens means DELETE then POST, which
  // leaves one empty frame between two full ones. Only a blank that HOLDS ends
  // the window, or a multi-screen app gets cut off at its first transition.
  const BLANK_HOLD = 0.25
  const cleared = frames.find((f, i) => {
    if (f.t <= start || (f.elements && f.elements.length)) return false
    const next = frames.slice(i + 1).find((g) => g.elements && g.elements.length)
    return !next || next.t - f.t >= BLANK_HOLD
  })
  const limit = Math.min(start + opts.seconds, cleared ? cleared.t : Infinity,
    (rec.duration_ms || 0) / 1000)
  const fps = opts.fps === 'auto' ? autoFps(drawn, start, limit) : opts.fps
  const count = Math.max(1, Math.floor((limit - start) * fps))

  const emuPublic = fs.existsSync(opts.emulatorAssets) ? opts.emulatorAssets : null
  const resolver = makeResolver(rec, assets, emuPublic)
  const decoded = new Map()      // key -> decoded native image
  const misses = new Map()       // key -> raw bytes still needing a decode
  const { frames: rafSlot, makeCanvas } = installShims(resolver.resolve, decoded, misses)
  const { loadImage } = await import('@napi-rs/canvas')

  // Decode whatever the last render asked for but did not have yet. Assets are
  // content-addressed and finite, so this converges after one pass in practice.
  const decodeMisses = async () => {
    if (!misses.size) return false
    for (const [key, buffer] of [...misses]) {
      misses.delete(key)
      try { decoded.set(key, await loadImage(buffer)) } catch { decoded.set(key, null) }
    }
    return true
  }

  const step = 1 / fps
  let virtualMs = 0
  globalThis.performance = { now: () => virtualMs }

  const { createRenderer } = await import('./vendor/renderer.js')
  const { loadAtlas } = await import('./vendor/atlas.js')

  // Decode every uploaded asset before the first frame. The renderer caches a
  // failed load ("loading: ver") and will not retry it, so a miss cannot be
  // repaired mid-pass: everything it might reach for has to be ready in advance.
  for (const [sha, b64] of Object.entries(rec.assets || {})) {
    try { decoded.set('sha:' + sha, await loadImage(Buffer.from(b64, 'base64'))) } catch { /* not an image */ }
  }

  const cv = makeCanvas(W, H)
  const oled = makeCanvas(160, 80)
  const ctx = cv.getContext('2d')

  const renderPass = async () => {
    let active = drawn[0]
    const model = {
      frame: { application_name: null, elements: [] },
      brightness: 80, volume: 0, name: 'BUSY BAR', battery_charge: 100, connected: true,
    }
    // Seed the renderer's internal `last` so its very first delta is exactly one
    // frame. It reads performance.now() when constructed, so this must be set
    // before createRenderer runs, and it is why DT is exactly 1/fps throughout.
    virtualMs = (start - step) * 1000
    const renderer = createRenderer(cv, oled, () => model, () => active.t)
    await loadAtlas()
    await new Promise((r) => setImmediate(r))    // let the icon/animation fetches settle
    renderer.start()                             // hands us its frame callback via rAF
    if (!rafSlot.pending) throw new Error('renderer did not register a frame callback')

    // NOTE: uploaded .anim assets never make it into a pass. The renderer pulls
    // them with fetch() and caches them per renderer, so the promise cannot
    // settle inside this loop and a repeat pass starts from an empty cache.
    // Yielding per frame does fix it, but do not: the decoder is greyscale-only,
    // so a colour plate (nyc-subway's ALERT/REROUTED/PLANNED) then renders as
    // grey streaks. Skipping it is the lesser wrong until that decoder can read
    // colour. Previews for those apps have to come off real hardware.
    const out = []
    for (let i = 0; i < count; i++) {
      const t = start + i * step
      const at = frameAt(frames, t)
      if (at && at.elements && at.elements.length) active = at
      model.frame = { application_name: active.application_name, elements: active.elements }
      virtualMs = t * 1000
      const cb = rafSlot.pending
      rafSlot.pending = null
      cb(virtualMs)
      out.push(new Uint8ClampedArray(ctx.getImageData(0, 0, W, H).data))
    }
    renderer.stop()
    return out
  }

  // A bundled icon or animation frame can only be discovered by rendering. If
  // one turns up, decode it and run the pass again with a fresh renderer, since
  // its image cache has already given up on that URL.
  let pixels = await renderPass()
  for (let attempt = 0; attempt < 2 && misses.size; attempt++) {
    await decodeMisses()
    pixels = await renderPass()
  }
  return { pixels, warnings: [...resolver.missing], count, start, limit, fps }
}

// -------------------------------------------------------------------- encode

const same = (a, b) => {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i += 4) if (a[i] !== b[i] || a[i + 1] !== b[i + 1] || a[i + 2] !== b[i + 2]) return false
  return true
}

// Trim to the point where the animation comes back round to its first frame, so
// periodic apps loop seamlessly instead of jumping at the wrap.
function findLoop(pixels) {
  const min = Math.max(4, Math.floor(pixels.length * 0.2))
  const dist = (a, b) => {
    let sum = 0
    for (let i = 0; i < a.length; i += 64) { const d = a[i] - b[i]; sum += d * d }
    return sum
  }
  let best = pixels.length, bestScore = Infinity
  for (let j = min; j < pixels.length; j++) {
    const score = dist(pixels[0], pixels[j])
    if (score < bestScore) { bestScore = score; best = j }
  }
  return best
}

function encodeGif(pixels, fps, colors) {
  const delayMs = 1000 / fps
  // One palette for the whole GIF: per-frame palettes made colours shimmer
  // between frames and forced a local colour table into every single frame.
  const stride = Math.max(1, Math.floor(pixels.length / 12))
  const sample = []
  for (let i = 0; i < pixels.length; i += stride) sample.push(pixels[i])
  const merged = new Uint8ClampedArray(sample.length * pixels[0].length)
  sample.forEach((frame, i) => merged.set(frame, i * pixels[0].length))
  const palette = quantize(merged, colors)

  const gif = GIFEncoder()
  let pendingDelay = 0, written = 0, first = true
  for (let i = 0; i < pixels.length; i++) {
    pendingDelay += delayMs
    // Fold identical consecutive frames into the previous frame's delay.
    if (i + 1 < pixels.length && same(pixels[i], pixels[i + 1])) continue
    const index = applyPalette(pixels[i], palette)
    gif.writeFrame(index, W, H, first ? { palette, delay: pendingDelay, repeat: 0 }
      : { delay: pendingDelay })
    first = false
    written++
    pendingDelay = 0
  }
  gif.finish()
  return { bytes: Buffer.from(gif.bytes()), written }
}

// ---------------------------------------------------------------------- main

async function main() {
  const opts = parseArgs(process.argv.slice(2))
  const recorded = opts.from
    ? { file: opts.from, rec: JSON.parse(fs.readFileSync(opts.from, 'utf8')) }
    : recordWithFallback(opts)
  const recPath = recorded.file
  const rec = recorded.rec
  const slug = path.basename(opts.slug || rec.app)
  const appDir = resolveAppDir(opts.slug || rec.app) || path.join(ROOT, 'apps', slug)
  const outPath = opts.out || path.join(appDir, opts.png ? 'preview.png' : 'preview.gif')

  let { pixels, warnings, start, limit, fps } = await renderFrames(rec, opts)

  if (opts.png) {
    const cv = (await import('@napi-rs/canvas')).createCanvas(W, H)
    const ctx = cv.getContext('2d')
    const img = ctx.createImageData(W, H)
    img.data.set(pixels[Math.min(pixels.length - 1, Math.floor(pixels.length / 2))])
    ctx.putImageData(img, 0, 0)
    fs.writeFileSync(outPath, cv.toBuffer('image/png'))
    report(outPath, slug, { frames: 1, fps: 0, warnings, start, limit })
  } else {
    if (opts.loop) pixels = pixels.slice(0, findLoop(pixels))
    let colors = 256, result = encodeGif(pixels, fps, colors)
    // Step down only as far as the budget needs: frame rate first (least
    // visible on a 72x16 display), then palette depth.
    const ladder = [[fps, 256], [fps, 128], [Math.max(10, Math.round(fps / 2)), 128],
      [Math.max(10, Math.round(fps / 2)), 64]]
    for (let i = 1; i < ladder.length && result.bytes.length > opts.maxBytes; i++) {
      const [f, c] = ladder[i]
      const thinned = f === fps ? pixels : pixels.filter((_, idx) => idx % Math.round(fps / f) === 0)
      result = encodeGif(thinned, f, c)
      colors = c
      if (f !== fps) { /* keep fps for reporting */ }
    }
    fs.writeFileSync(outPath, result.bytes)
    report(outPath, slug, { frames: result.written, fps, colors, warnings, start, limit,
      bytes: result.bytes.length, maxBytes: opts.maxBytes })
  }

  if (!opts.from && !opts.keepRecording) fs.rmSync(recPath, { force: true })
}

function report(outPath, slug, info) {
  const rel = path.relative(ROOT, outPath)
  console.log(`\nrender: ${slug}`)
  console.log(`  output        ${rel}  (${W}x${H})`)
  if (info.bytes) {
    console.log(`  size          ${(info.bytes / 1000).toFixed(0)} kB${
      info.bytes > info.maxBytes ? '  (over budget, consider --seconds or --loop)' : ''}`)
    console.log(`  frames        ${info.frames} written at ${info.fps} fps, ${info.colors} colours`)
  }
  console.log(`  window        ${info.start.toFixed(2)}s -> ${info.limit.toFixed(2)}s of the recording`)
  for (const w of info.warnings) {
    console.log(`  WARN          this app uses ${w}, which need a busybar-emulator checkout `
      + `next to this repo (or --emulator-assets) to render`)
  }
}

main().catch((err) => { console.error(err); process.exit(1) })
