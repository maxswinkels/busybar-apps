"""
Previews of the front display without a bar: each sample's drawing, rendered
the way the bar renders it, as 720x160 LED-style PNGs plus the gallery's
preview.gif - or, with --from-snapshots, the 72x16 frames `busybar-cclimits
snapshot` saved from a real bar, turned into the same LED-style images.

Text is set in the device's own fonts, from the glyph atlas the BUSY Bar
emulator bakes from the firmware (github.com/maxswinkels/busybar-emulator,
public/fonts/font-atlas.json; not shipped here), placed the way its renderer
places them. The one difference is the countdown: the emulator draws it in a
10px bold font, the firmware in 5px digits as wide as the small font's, on
rows y-9..y-5 of a bottom anchor, with the hours zero-padded - "01:42:07",
27 pixels (measured on a bar with firmware 1.2.4); here it is the small font,
on those rows.

    uv run --with pillow python scripts/render_previews.py \
        --atlas .../font-atlas.json --out docs/images --gif preview.gif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from busybar_cclimits import render
from busybar_cclimits.samples import DEMO_STEPS, SAMPLES, Sample

W, H = 72, 16
SCALE = 10
NOW = 1_800_000_000.0
# Firmware ascent and line height per font (the emulator's FW_METRICS).
FW_METRICS = {"tiny": (5, 6), "small": (7, 9)}
ALIGN = {
    "top_left": (0, 0),
    "top_mid": (0.5, 0),
    "top_right": (1, 0),
    "mid_left": (0, 0.5),
    "center": (0.5, 0.5),
    "mid_right": (1, 0.5),
    "bottom_left": (0, 1),
    "bottom_mid": (0.5, 1),
    "bottom_right": (1, 1),
}


def color(value: str) -> tuple[int, int, int, float]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16), int(v[6:8], 16) / 255


class Canvas:
    def __init__(self, atlas: dict) -> None:
        self.atlas = atlas
        self.buf = [[(0.0, 0.0, 0.0)] * W for _ in range(H)]

    def px(self, x: int, y: int, c: tuple[int, int, int, float]) -> None:
        if 0 <= x < W and 0 <= y < H:
            r, g, b, a = c
            old = self.buf[y][x]
            self.buf[y][x] = tuple(o * (1 - a) + n * a for o, n in zip(old, (r, g, b), strict=True))

    def mask(self, text: str, font: str) -> tuple[int, int, set[tuple[int, int]]]:
        f = self.atlas[font]
        ascent, lineh = FW_METRICS[font]
        dy = ascent - f["ascent"]
        pen, right, lit = 0, 0, set()
        for ch in text:
            g = f["glyphs"].get(str(ord(ch))) or f["glyphs"]["63"]
            for ry, row in enumerate(g["rows"]):
                if not row:
                    continue
                bits, total = int(row, 16), len(row) * 4
                for rx in range(g["w"]):
                    if (bits >> (total - 1 - rx)) & 1:
                        lit.add((pen + g["ox"] + rx, g["oy"] + dy + ry))
            right = max(right, pen + g["ox"] + g["w"])
            pen += g["adv"]
        return max(1, pen, right), lineh, lit

    def place(self, el: dict, w: int, h: int) -> tuple[int, int]:
        ax, ay = ALIGN.get(el.get("align") or "top_left", (0, 0))
        return el.get("x", 0) - round(w * ax), el.get("y", 0) - round(h * ay)

    def text(self, el: dict, text: str, font: str, raise_by: int = 0) -> None:
        w, h, lit = self.mask(text, font)
        x0, y0 = self.place(el, w, h)
        c = color(el.get("color") or "#FFFFFFFF")
        for x, y in lit:
            self.px(x0 + x, y0 + y - raise_by, c)

    def draw(self, elements: list[dict], now: float) -> None:
        for el in elements:
            kind = el["type"]
            if kind == "rectangle":
                x0, y0 = self.place(el, el["width"], el["height"])
                c = color(el["fill_colors"][0])
                for y in range(el["height"]):
                    for x in range(el["width"]):
                        self.px(x0 + x, y0 + y, c)
            elif kind == "text":
                self.text(el, el["text"], el["font"])
            elif kind == "countdown":
                left = max(0, int(el["timestamp"]) - int(now))
                h, rem = divmod(left, 3600)
                m, s = divmod(rem, 60)
                # As the firmware shows it: hours zero-padded, when there are any.
                shown = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                # Small-font digits sit 2 rows into a 9-row box; the firmware's
                # countdown digits end 5 rows above its bottom anchor.
                self.text(el, shown, "small", raise_by=2)

    def frame(self) -> list[list[tuple[int, int, int]]]:
        return [[tuple(round(v) for v in px) for px in row] for row in self.buf]


def leds(frame: list[list[tuple[int, int, int]]]) -> Image.Image:
    """
    The 72x16 frame as a 720x160 picture of the LED matrix, in the colours as
    drawn: the bar's gamma turns them into LED light much as a screen turns
    sRGB into light, so applying it here as well would darken them twice.
    """
    img = Image.new("RGB", (W * SCALE, H * SCALE), (8, 8, 9))
    d = ImageDraw.Draw(img)
    for y, row in enumerate(frame):
        for x, px in enumerate(row):
            c = px if max(px) > 0 else (20, 20, 22)
            x0, y0 = x * SCALE + 1, y * SCALE + 1
            d.rounded_rectangle((x0, y0, x0 + SCALE - 3, y0 + SCALE - 3), radius=2, fill=c)
    return img


def render_sample(atlas: dict, sample: Sample, now: float = NOW) -> Image.Image:
    drawn = render.elements(sample.feed(NOW), now, sample.display_config())
    canvas = Canvas(atlas)
    canvas.draw([e.model_dump(mode="json", exclude_none=True) for e in drawn], now)
    return leds(canvas.frame())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--atlas", help="the emulator's public/fonts/font-atlas.json")
    p.add_argument("--from-snapshots", help="a folder of 72x16 PNGs from `snapshot`")
    p.add_argument("--out", required=True)
    p.add_argument("--gif", help="also write the demo as this GIF")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.from_snapshots:
        for path in sorted(Path(args.from_snapshots).glob("*.png")):
            small = Image.open(path).convert("RGB")
            frame = [[small.getpixel((x, y)) for x in range(W)] for y in range(H)]
            leds(frame).save(out / path.name)
            print(out / path.name)
        return 0

    atlas = json.loads(Path(args.atlas).read_text())
    for name, sample in SAMPLES.items():
        render_sample(atlas, sample).save(out / f"{name}.png")
        print(out / f"{name}.png")
    if args.gif:
        frames = []
        for step, (five, week) in enumerate(DEMO_STEPS):
            sample = Sample("", five, week=week, five_left=4 * 3600 - step * 1800)
            for second in range(3):
                frames.append(render_sample(atlas, sample, NOW + second))
        frames[0].save(
            args.gif,
            save_all=True,
            append_images=frames[1:],
            duration=500,
            loop=0,
            optimize=True,
        )
        print(args.gif)
    return 0


if __name__ == "__main__":
    sys.exit(main())
