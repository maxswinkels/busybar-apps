"""
The service: keeps the bar in step with the feed the status line writes.

It looks at the feed every couple of seconds and redraws when what the bar
should show changes - a new percentage, a reset passing, the weekly time
moving on - and, unchanged, every `refresh_seconds`: a calendar reminder or
a BUSY session draws over this app, and when it ends the bar does not bring
this drawing back by itself. While something with a higher priority holds
the screen the bar refuses the draw (409), which only means waiting.

Every element carries `display_until`, a few refreshes ahead, so the bar
takes the drawing down by itself if this service stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from busylib.exceptions import BusyBarAPIError

from . import limits, render
from .config import Config

logger = logging.getLogger(__name__)

POLL = 2.0  # seconds between looks at the feed
EXPIRE_AFTER = 3  # refreshes a drawing outlives its last send
RETRY_MAX = 300.0  # seconds, the longest wait after the bar failed


class Runner:
    def __init__(
        self,
        config: Config,
        bar: render.Bar,
        clock: Callable[[], float] = time.time,
        poller: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.bar = bar
        self.clock = clock
        # Refreshes the feed file before each look at it: the usage API source.
        self.poller = poller
        self._stamp: tuple[int, int] | None = None
        self.feed: limits.Feed | None = None
        self._key: str | None = None  # what the bar shows; None when nothing
        # The ids this app has on the bar; None when unknown - as at the start, when
        # a run before this one may have left others - so the next draw clears first.
        self._ids: frozenset[str] | None = None
        self._sent_at = float("-inf")
        self._retry_at = float("-inf")
        self._failures = 0
        self._said_empty = False

    @property
    def app(self) -> str:
        return self.config.bar.application_name

    @property
    def feed_path(self) -> Path:
        return self.config.feed.path

    def read_feed(self) -> limits.Feed | None:
        """The feed, read again only when the file changed."""
        try:
            stat = self.feed_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            self.feed = limits.read(self.feed_path) if stamp is not None else None
        return self.feed

    async def clear(self) -> None:
        await render.clear(self.bar, self.app)
        self._ids = frozenset()

    async def _draw(self, elements: list[Any], now: float) -> None:
        ids = frozenset(element.id for element in elements)
        if self._ids is None or not self._ids <= ids:
            await self.clear()
        until = now + EXPIRE_AFTER * self.config.display.refresh_seconds
        drawing = render.drawing(elements, until, self.app, self.config.bar.priority)
        self._ids = None  # unknown until the bar confirms
        try:
            await render.send(self.bar, drawing)
        except BusyBarAPIError as error:
            if error.status_code != 409:
                raise
            # A higher priority holds the screen; the elements are stored all the same.
            logger.debug("the bar has something above this app; trying again later")
        self._ids = ids

    async def tick(self, now: float) -> None:
        if self.poller is not None:
            try:
                await self.poller(now)
            except Exception as error:  # the bar keeps the last numbers
                logger.warning("usage: %s", error)
        if now < self._retry_at:
            return
        feed = self.read_feed()
        display = self.config.display
        shown = render.visible(feed, now, display)
        elements = render.elements(feed, now, display) if shown and feed is not None else None
        key = render.key(elements) if elements is not None else None
        if feed is None or not feed.windows:
            self._note_empty(feed)
        else:
            self._said_empty = False
        stale = now - self._sent_at >= display.refresh_seconds
        if key == self._key and (key is None or not stale):
            return
        try:
            if elements is None:
                await self.clear()
                logger.info("cleared")
            else:
                await self._draw(elements, now)
                if key != self._key:
                    logger.info("drawn: %s", summary(feed, now, display))
            self._key = key
            self._sent_at = now
            self._failures = 0
        except Exception as error:
            self._failures += 1
            wait = min(RETRY_MAX, 5.0 * 2 ** (self._failures - 1))
            self._retry_at = now + wait
            self._key = None if elements is not None else "unknown"
            self._ids = None
            logger.warning("the bar refused the update (%s); trying again in %.0fs", error, wait)

    def _note_empty(self, feed: limits.Feed | None) -> None:
        if self._said_empty:
            return
        self._said_empty = True
        if feed is None:
            logger.warning(
                "no limits yet: %s does not exist - is the status line hook installed? "
                "(busybar-cclimits hook install)",
                self.feed_path,
            )
        else:
            logger.warning(
                "the status line runs, but Claude Code sends it no rate limits: they come "
                "for claude.ai subscriptions, after a session's first response"
            )

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                await self.tick(self.clock())
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), POLL)
        finally:
            try:
                await self.clear()
            except Exception as error:
                logger.warning("could not clear the bar on exit: %s", error)


def summary(feed: limits.Feed | None, now: float, display: Any) -> str:
    if feed is None:
        return "nothing"
    windows = feed.at(now)
    parts = []
    for row in display.rows:
        window = windows.get(row.window)
        if window is None:
            parts.append(f"{row.window} -")
            continue
        left = window.left(now)
        reset = f", resets in {limits.duration(left)}" if left is not None else ", reset"
        parts.append(f"{row.window} {window.used:.0f}%{reset}")
    return "; ".join(parts)
