"""Showing events on the bar outside the schedule: replay a day, the demo, test frames."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import logging
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from . import connect, render, schedule
from .config import Config
from .events import CalendarRef, Event
from .runner import Runner

logger = logging.getLogger(__name__)


def _day(text: str, tz: tzinfo) -> date:
    today = datetime.now(tz).date()
    named = {"today": today, "tomorrow": today + timedelta(days=1)}
    return named.get(text) or date.fromisoformat(text)


async def _wait(stop: asyncio.Event, moment: datetime) -> bool:
    """Sleep until `moment`; True if `stop` was set first."""
    delay = max(0.0, (moment - datetime.now(UTC)).total_seconds())
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), delay)
    return stop.is_set()


async def _show(runner: Runner, screen: schedule.Screen | None, *, chime: bool) -> None:
    """Draw (or, with no screen, clear) and carry on if the bar refuses: a 409 means
    something above our priority holds the screen - a menu, a button press."""
    try:
        if screen is None:
            await runner.clear()
        else:
            await runner.show(screen, chime=chime)
    except Exception as error:
        logger.warning("bar refused the update (%s); carrying on", error)


async def play(
    runner: Runner,
    events: list[Event],
    seconds: int,
    stop: asyncio.Event,
    *,
    chime: bool,
    tz: tzinfo | None = None,
) -> None:
    """
    Show events one after another, each in a slot of `seconds`.

    A third of the slot is the reminder, counting down to a start moved to
    its end; the rest is the status. Titles, settings and drawings are the
    events' own; only the times are not.
    """
    reminder_part = timedelta(seconds=max(5, seconds // 3))
    status_part = timedelta(seconds=seconds) - reminder_part
    for number, event in enumerate(events, 1):
        reminder, status = runner.settings_for(event)
        start = datetime.now(UTC).replace(microsecond=0) + reminder_part
        moved = dataclasses.replace(event, start=start, end=start + status_part)
        if tz is not None:
            print(
                f"{number:>2}/{len(events)}  {event.start.astimezone(tz):%H:%M}-"
                f"{event.end.astimezone(tz):%H:%M}  {event.title}  [{event.response}]",
                flush=True,
            )
        if reminder.enabled:
            await _show(runner, schedule.Screen("reminder", moved, reminder, status), chime=chime)
        if await _wait(stop, moved.start):
            return
        shown = schedule.shows_status(moved, status)
        await _show(
            runner,
            schedule.Screen("status", moved, reminder, status) if shown else None,
            chime=chime,
        )
        if await _wait(stop, moved.end):
            return


async def replay(config: Config, day: str, seconds: int, quiet: bool) -> int:
    """Play a day's real events on the bar, one after another, in short slots."""
    tz = connect.floating_tz(config)
    day = _day(day, tz)
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    items = connect.make_sources(config)
    try:
        found = await asyncio.gather(*(item.fetch(day_start, day_end) for item in items))
    finally:
        await connect.close_all(items)
    events = sorted(
        (e for events in found for e in events if day_start <= e.start < day_end),
        key=lambda e: (e.start, e.calendar.order),
    )
    if not events:
        print(f"No events on {day:%a %d %b}.")
        return 0
    print(f"Replaying {len(events)} events of {day:%a %d %b}, {seconds}s each.")
    bar = connect.make_bar(config)
    runner = Runner(config, bar, [])
    stop = connect.stop_on_signals()
    try:
        await play(runner, events, seconds, stop, chime=not quiet, tz=tz)
    finally:
        with contextlib.suppress(Exception):
            await runner.clear()
        await bar.aclose()
    return 0


DEMO_EVENTS = (
    ("Design review", "accepted"),
    ("1:1 with Alex", "accepted"),
    ("Quarterly planning with the design team", "tentative"),
)


def _demo_events() -> list[Event]:
    ref = CalendarRef("demo", "demo", 0)
    now = datetime.now(UTC)
    return [
        Event(f"demo-{i}", title, now, now + timedelta(minutes=30), ref, response)
        for i, (title, response) in enumerate(DEMO_EVENTS)
    ]


async def run_demo(config: Config, seconds: int = 20) -> int:
    """Loop a made-up schedule on the bar until a signal: reminders, then BUSY."""
    bar = connect.make_bar(config)
    runner = Runner(config, bar, [])
    stop = connect.stop_on_signals()
    try:
        while not stop.is_set():
            await play(runner, _demo_events(), seconds, stop, chime=False)
    finally:
        with contextlib.suppress(Exception):
            await runner.clear()
        await bar.aclose()
    return 0


async def draw_test_frame(config: Config) -> int:
    """Draw one made-up event in progress - the red calendar and BUSY - and exit."""
    event = dataclasses.replace(_demo_events()[0], start=datetime.now(UTC) - timedelta(minutes=2))
    reminder, status = config.settings_for("demo", "demo")
    screen = schedule.Screen("status", event, reminder, status)
    bar = connect.make_bar(config)
    try:
        drawing = render.drawing(
            screen, config.display, config.bar.application_name, config.bar.priority
        )
        await render.send(bar, drawing)
    finally:
        await bar.aclose()
    return 0


async def demo(config: Config, args: argparse.Namespace) -> int:
    """Draw a made-up event, to see the layout on the bar."""
    now = datetime.now(UTC)
    start = now + timedelta(minutes=args.minutes)
    if args.status:
        start = now - timedelta(minutes=1)
    event = Event(
        "demo", args.title, start, start + timedelta(minutes=30), CalendarRef("demo", "demo", 0)
    )
    first = config.sources[0].name
    reminder, status = config.settings_for(first, first, title=args.title)
    if args.show:
        status = status.model_copy(update={"show": args.show})
    screen = schedule.Screen("status" if args.status else "reminder", event, reminder, status)
    if args.font:
        config.display.title_font = args.font
    drawing = render.drawing(
        screen, config.display, config.bar.application_name, config.bar.priority
    )
    print(json.dumps(drawing.payload(), indent=2, ensure_ascii=False))
    bar = connect.make_bar(config)
    try:
        with contextlib.suppress(Exception):  # 400 when nothing of ours is up
            await bar.display_clear(application_name=config.bar.application_name)
        await render.send(bar, drawing)
        if args.sound and (sound := render.sound(screen, config.display)):
            await bar.audio_play(stock_path=sound, application_name=config.bar.application_name)
        await asyncio.sleep(args.seconds)
        with contextlib.suppress(Exception):
            await bar.display_clear(application_name=config.bar.application_name)
    finally:
        await bar.aclose()
    return 0
