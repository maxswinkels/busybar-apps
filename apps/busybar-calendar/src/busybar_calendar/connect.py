"""The plumbing every command shares: the bar client, the calendar sources, signals."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

from busylib.client import AsyncBusyBar

from . import sources
from .config import Config, ConfigError
from .runner import Runner


def floating_tz(config: Config) -> tzinfo:
    if config.sync.timezone:
        return ZoneInfo(config.sync.timezone)
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local


def make_sources(config: Config) -> list[sources.Source]:
    tz = floating_tz(config)
    return [
        sources.build(source, index, tz, config.sync.skip_free)
        for index, source in enumerate(config.sources)
    ]


def make_bar(config: Config) -> AsyncBusyBar:
    token = config.bar.token.resolve()
    if config.bar.address:
        return AsyncBusyBar(addr=config.bar.address, token=token)
    if not token:
        raise ConfigError("give bar.address, or a bar-scope cloud token as bar.token")
    return AsyncBusyBar(token=token)


async def close_all(items: list[sources.Source]) -> None:
    for item in items:
        await item.aclose()


async def read_calendars(config: Config, now: datetime) -> Runner:
    """The events and their settings as the service sees them at `now`, with no bar."""
    items = make_sources(config)
    runner = Runner(config, bar=None, sources=items)  # type: ignore[arg-type]
    try:
        await runner.refresh(now)
    finally:
        await close_all(items)
    return runner


def stop_on_signals() -> asyncio.Event:
    """An event that SIGINT or SIGTERM sets, so every mode can stop cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
