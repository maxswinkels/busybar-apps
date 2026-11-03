"""
Regenerate the screenshots in docs/images from a real bar.

Draws each layout on the bar named in a busybar-calendar config, reads the
front display back through GET /api/screen, and saves it as LED-style PNGs,
plus GIFs for the moving ones. Sounds stay off. Needs Pillow:

    uv run --with pillow python scripts/capture_screens.py -c config.toml docs/images

With the service running, a capture takes its screen down: the drawing goes up
over whatever the service has on the bar - under another application name too,
the firmware swaps the canvas - and what it replaces does not come back by
itself, as the service only redraws at its next change. Stop the service first,
or touch its config file afterwards, which makes it redraw at once.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw

from busybar_calendar import render
from busybar_calendar.config import DisplayConfig, ReminderSettings, StatusSettings, load
from busybar_calendar.connect import make_bar
from busybar_calendar.events import CalendarRef, Event
from busybar_calendar.schedule import Screen

W, H = 72, 16
CELL, DOT, PAD = 10, 8, 16
BACKGROUND, UNLIT = (12, 12, 12), (28, 28, 28)


def led(rgb: bytes) -> Image.Image:
    """A front-display frame as the bar shows it: square LEDs on a dark panel."""
    image = Image.new("RGB", (W * CELL + 2 * PAD, H * CELL + 2 * PAD), BACKGROUND)
    draw = ImageDraw.Draw(image)
    for y in range(H):
        for x in range(W):
            i = (y * W + x) * 3
            colour = tuple(rgb[i : i + 3])
            if max(colour) < 20:
                colour = UNLIT
            left, top = PAD + x * CELL, PAD + y * CELL
            draw.rounded_rectangle((left, top, left + DOT, top + DOT), radius=2, fill=colour)
    return image


def scenarios(now: datetime):
    """(file name, screen, display settings, frames: 1 for a PNG, more for a GIF)."""
    ref = CalendarRef("Work", "Work", 0)

    def event(title: str, start_in: int, length: int = 30, response: str = "accepted") -> Event:
        start = now + timedelta(minutes=start_in)
        return Event("doc", title, start, start + timedelta(minutes=length), ref, response)

    reminder, status, small = ReminderSettings(), StatusSettings(), DisplayConfig()
    meeting = StatusSettings(show="meeting")
    hidden = ReminderSettings(show_name=False)
    cyrillic = event("Планування спринту", 5)
    yield "reminder", Screen("reminder", event("Design review", 5), reminder, status), small, 18
    yield "status", Screen("status", event("Design review", -2, 45), reminder, status), small, 1
    maybe = event("Design review", -2, 45, "tentative")
    yield "status-tentative", Screen("status", maybe, reminder, status), small, 1
    in_progress = event("Design review", -2, 45)
    yield "status-meeting", Screen("status", in_progress, reminder, meeting), small, 18
    long_title = event("Quarterly planning with the design team", 10)
    yield "long-title", Screen("reminder", long_title, reminder, status), small, 24
    global_font = DisplayConfig(title_font="global")
    yield "cyrillic-global", Screen("reminder", cyrillic, reminder, status), global_font, 24
    yield "cyrillic-transliterated", Screen("reminder", cyrillic, reminder, status), small, 24
    yield "hidden-name", Screen("reminder", event("Design review", 5), hidden, status), small, 1


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-c", "--config", default="config.toml")
    parser.add_argument("out", type=Path)
    parser.add_argument("only", nargs="*", help="capture only these names")
    args = parser.parse_args()

    config = load(args.config)
    app = config.bar.application_name
    args.out.mkdir(parents=True, exist_ok=True)
    bar = make_bar(config)
    try:
        for name, screen, display, count in scenarios(datetime.now(UTC)):
            if args.only and name not in args.only:
                continue
            with contextlib.suppress(Exception):  # 400 when nothing of ours is up
                await bar.display_clear(application_name=app)
            await render.send(bar, render.drawing(screen, display, app, config.bar.priority))
            await asyncio.sleep(2.5)  # let the cloud deliver it and the scroll begin
            frames, stamps = [], []
            for _ in range(count):
                frames.append(led(await bar.screen(0)))
                stamps.append(time.monotonic())
            if count > 1:
                gaps = [int((b - a) * 1000) for a, b in itertools.pairwise(stamps)]
                frames[0].save(
                    args.out / f"{name}.gif",
                    save_all=True,
                    append_images=frames[1:],
                    duration=[*gaps, gaps[-1]],
                    loop=0,
                    optimize=True,
                )
            frames[len(frames) // 2].save(args.out / f"{name}.png", optimize=True)
            print(f"{name}: {count} frame(s)")
    finally:
        with contextlib.suppress(Exception):
            await bar.display_clear(application_name=app)
        await bar.aclose()


if __name__ == "__main__":
    asyncio.run(main())
