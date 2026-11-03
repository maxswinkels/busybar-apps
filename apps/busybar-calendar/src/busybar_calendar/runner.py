"""
The loop: refresh the sources, decide the screen, draw it when it changes.

The loop sleeps until the next moment the screen can change - a reminder
opening, an event starting or ending - or the next refresh, whichever is
first, so a reminder appears on the second rather than on the next poll.

A source that fails for a while keeps its last good events, so a calendar
server hiccup does not take a running meeting off the bar. One that answers
401, 403 or 404 is configured wrong, and the loop stops and says so rather
than retrying it forever. A bar that refuses a
drawing (unreachable, or something above our priority on screen) is tried
again shortly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from busylib import types
from busylib.exceptions import BusyBarAPIError

from . import render, schedule
from .config import Config, ReminderSettings, StatusSettings
from .events import Event
from .sources import FatalSourceError, Source

logger = logging.getLogger(__name__)

# Upper bound on a sleep, so a laptop waking up catches up within a minute.
MAX_SLEEP = timedelta(seconds=60)
RETRY = timedelta(seconds=15)
# A sound plays only for a screen that has just opened: restarting this
# service in the middle of a meeting must not chime again.
SOUND_GRACE = timedelta(seconds=60)
# Events still running were in the past when they started; look back far
# enough to see any of them (longer ones count as all-day and are skipped).
LOOKBACK = timedelta(hours=24)

_UNKNOWN = object()


class Bar(Protocol):
    async def api_request(self, method: str, path: str, **kwargs: Any) -> Any: ...

    async def display_clear(self, **kwargs: Any) -> types.SuccessResponse: ...

    async def audio_play(self, *, stock_path: str | None = None, **kwargs: Any) -> Any: ...


class Runner:
    def __init__(
        self,
        config: Config,
        bar: Bar,
        sources: Sequence[Source],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.bar = bar
        self.sources = list(sources)
        self.now = now
        self._events: dict[str, list[Event]] = {}
        self._settings: dict[tuple[str, ...], tuple[ReminderSettings, StatusSettings]] = {}
        self._shown: object = _UNKNOWN
        # The element ids this service last left on the bar; None when unknown.
        self._drawn: frozenset[str] | None = None
        self._sounded: set[tuple[str, str]] = set()
        self._next_refresh = datetime.min.replace(tzinfo=UTC)

    @property
    def app(self) -> str:
        return self.config.bar.application_name

    @property
    def events(self) -> list[Event]:
        return [event for events in self._events.values() for event in events]

    def settings_for(self, event: Event) -> tuple[ReminderSettings, StatusSettings]:
        key = (event.calendar.source, event.calendar.name, event.calendar.id, event.title)
        if key not in self._settings:
            self._settings[key] = self.config.settings_for(*key)
        return self._settings[key]

    async def refresh(self, now: datetime) -> None:
        self._settings.clear()  # keyed by title too: keep only the current events'
        start = now - LOOKBACK
        end = now + timedelta(hours=self.config.sync.lookahead_hours)
        results = await asyncio.gather(
            *(source.fetch(start, end) for source in self.sources), return_exceptions=True
        )
        for result in results:
            if isinstance(result, FatalSourceError):
                raise result  # wrong credentials or URL: say so and stop
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, BaseException):
                kept = len(self._events.get(source.name, []))
                logger.warning(
                    "%s: refresh failed (%s); keeping %d events", source.name, result, kept
                )
                continue
            self._events[source.name] = result
            logger.debug("%s: %d events", source.name, len(result))
        # Forget the chimes of events that are gone, or a service that runs for
        # months keeps every screen it ever showed.
        current = {event.key for event in self.events}
        self._sounded = {moment for moment in self._sounded if moment[1] in current}

    def reconfigure(self, config: Config, sources: Sequence[Source]) -> None:
        """
        Carry on with an edited config: read the calendars again at once, and
        redraw what is up - without a second chime for a screen that had one.
        Events already read are kept for the sources still configured, should
        the first read with the new config fail.
        """
        names = {source.name for source in sources}
        self._events = {name: found for name, found in self._events.items() if name in names}
        self.config = config
        self.sources = list(sources)
        self._settings.clear()
        self._next_refresh = datetime.min.replace(tzinfo=UTC)
        self._shown = _UNKNOWN

    def _opened_at(self, screen: schedule.Screen) -> datetime:
        if screen.kind == "status":
            return screen.event.start
        return screen.event.start - timedelta(minutes=screen.reminder.minutes_before)

    async def clear(self) -> None:
        """Remove this service's drawing, and only this service's."""
        try:
            await self.bar.display_clear(application_name=self.app)
        except BusyBarAPIError as error:
            # The bar answers 400 when this application has nothing on
            # screen - another app owns the canvas - which is already clear.
            if error.status_code != 400:
                raise
        self._drawn = frozenset()

    async def _draw(self, drawing: render.Drawing) -> None:
        """
        Put a drawing up in place of the last one.

        A draw merges into what the application already shows, by element id:
        it updates the ids it names and leaves the others up. So clear first
        only when something would be left over - a reminder's elements under
        a status animation - and otherwise update in place, which does not
        flicker: a reminder becoming its event only adds BUSY.
        """
        ids = frozenset(element.id for element in drawing.elements)
        if self._drawn is None or not self._drawn <= ids:
            await self.clear()
        self._drawn = None  # unknown until the draw is confirmed
        await render.send(self.bar, drawing)
        self._drawn = ids

    async def show(self, screen: schedule.Screen, *, chime: bool) -> None:
        """Put a screen on the bar, with its sound if `chime` and its settings want one."""
        await self._draw(
            render.drawing(screen, self.config.display, self.app, self.config.bar.priority)
        )
        logger.info(
            "%s: %s (%s, %s) until %s",
            screen.kind,
            screen.event.title,
            screen.event.calendar.name,
            screen.event.response,
            screen.until.isoformat(),
        )
        sound = render.sound(screen, self.config.display) if chime else None
        if sound is None:
            return
        try:
            await self.bar.audio_play(stock_path=sound, application_name=self.app)
        except Exception as error:  # a missing sound is not worth a redraw
            logger.warning("could not play %s: %s", sound, error)

    async def tick(self, now: datetime) -> None:
        screen = schedule.choose(now, self.events, self.settings_for)
        key = screen.key if screen is not None else None
        if key == self._shown:
            return
        try:
            if screen is None:
                await self.clear()
                logger.info("cleared")
            else:
                # Chime once per screen, and only one that has just opened:
                # restarting in the middle of a meeting must not chime again.
                moment = (screen.kind, screen.event.key)
                fresh = now - self._opened_at(screen) <= SOUND_GRACE
                await self.show(screen, chime=fresh and moment not in self._sounded)
                self._sounded.add(moment)
            self._shown = key
        except Exception as error:
            logger.warning("bar refused the update (%s); retrying", error)
            self._shown = _UNKNOWN
            self._drawn = None

    async def run(
        self, stop: asyncio.Event, before_each: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        """Until `stop`; `before_each` runs at every wake, at least once a minute."""
        try:
            while not stop.is_set():
                if before_each is not None:
                    await before_each()
                now = self.now()
                if now >= self._next_refresh:
                    await self.refresh(now)
                    self._next_refresh = now + timedelta(minutes=self.config.sync.poll_minutes)
                await self.tick(now)

                wake = min(now + MAX_SLEEP, self._next_refresh)
                change = schedule.next_change(now, self.events, self.settings_for)
                if change is not None:
                    wake = min(wake, change)
                if self._shown is _UNKNOWN:
                    wake = min(wake, now + RETRY)
                timeout = max(0.2, (wake - self.now()).total_seconds())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout)
        finally:
            try:
                await self.clear()
            except Exception as error:
                logger.warning("could not clear the bar on exit: %s", error)
