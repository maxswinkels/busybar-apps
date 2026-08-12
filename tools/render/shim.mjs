/* Browser globals for the vendored emulator renderer, so it runs under Node.
 *
 * The whole point is that tools/render/vendor/{renderer,atlas}.js stay
 * BYTE-IDENTICAL to busybar-emulator. Every adaptation lives here instead, so
 * re-vendoring is a plain `cp` and previews keep matching the emulator exactly.
 *
 * Two of these shims do more than paper over a missing API:
 *
 *   requestAnimationFrame captures the renderer's frame callback instead of
 *   scheduling it. That hands us the clock: we call the callback with the exact
 *   timestamp we want each output frame to represent, so frame timing is exact
 *   rather than whatever a real timer managed to hit. This is what makes the
 *   GIFs smooth, and it makes text scrolling (which integrates DT) deterministic.
 *
 *   Image resolves out of a pre-populated decode cache, synchronously. In a
 *   browser the renderer skips elements whose asset has not arrived yet
 *   (`if (!rec.ready) return`), which is why live captures show blank frames at
 *   the start. Here a cache hit fires onload before the `src` setter returns, so
 *   nothing is ever half-loaded. A miss is reported to the driver, which decodes
 *   it and re-renders the same frame: see decodeMisses() in render.mjs.
 *
 * `resolve(url)` must return `{ buffer, key }` or null. `key` identifies the
 * bytes (a content hash for uploaded assets, a file path for bundled ones) and
 * is what the decode cache is keyed on, so the renderer's `?v=` cache-busting
 * never causes a redundant decode.
 */
import { createCanvas, Image as NapiImage } from '@napi-rs/canvas'

export function installShims(resolve, decoded, misses) {
  const frames = { pending: null }

  class ShimImage {
    constructor() {
      this._img = null
      this.onload = null
      this.onerror = null
      this.crossOrigin = null
    }
    get naturalWidth() { return this._img ? this._img.width : 0 }
    get naturalHeight() { return this._img ? this._img.height : 0 }
    get width() { return this.naturalWidth }
    get height() { return this.naturalHeight }
    get complete() { return this._img != null }
    set src(url) {
      const found = resolve(url)
      if (!found) { if (this.onerror) this.onerror(new Error('not found: ' + url)); return }
      const hit = decoded.get(found.key)
      if (!hit) { misses.set(found.key, found.buffer); return }
      this._img = hit
      if (this.onload) this.onload()
    }
  }

  // The renderer hands ShimImage instances straight to ctx.drawImage, which
  // needs the underlying native handle.
  const patchDrawImage = (ctx) => {
    const original = ctx.drawImage.bind(ctx)
    ctx.drawImage = (src, ...rest) => original(src instanceof ShimImage ? src._img : src, ...rest)
    return ctx
  }

  const makeCanvas = (w = 1, h = 1) => {
    const cv = createCanvas(w, h)
    const getContext = cv.getContext.bind(cv)
    cv.getContext = (type, attrs) => {
      const ctx = getContext(type, attrs)
      return ctx && type === '2d' ? patchDrawImage(ctx) : ctx
    }
    return cv
  }

  globalThis.document = {
    createElement: (tag) => (tag === 'canvas' ? makeCanvas() : {}),
    fonts: { add() {} },
    body: {},
  }
  globalThis.Image = ShimImage
  globalThis.FontFace = class { load() { return Promise.resolve(this) } }
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => 'monospace' })
  globalThis.requestAnimationFrame = (cb) => { frames.pending = cb; return 1 }
  globalThis.cancelAnimationFrame = () => { frames.pending = null }
  globalThis.fetch = async (url) => {
    const found = resolve(url)
    const buf = found && found.buffer
    return {
      ok: buf != null,
      status: buf != null ? 200 : 404,
      async json() { return JSON.parse(buf.toString('utf8')) },
      async arrayBuffer() { return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) },
      async text() { return buf.toString('utf8') },
    }
  }

  return { frames, makeCanvas, NapiImage }
}
