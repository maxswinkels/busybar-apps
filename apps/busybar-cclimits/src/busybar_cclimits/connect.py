"""The plumbing the bar commands share: the client, a sample feed, signals."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from busylib.client import AsyncBusyBar

from .config import Config, ConfigError


def make_bar(config: Config) -> AsyncBusyBar:
    token = config.bar.token.resolve()
    if config.bar.address:
        return AsyncBusyBar(addr=config.bar.address, token=token)
    if not token:
        raise ConfigError("give bar.address, or a BUSY Cloud token with the bar scope as bar.token")
    return AsyncBusyBar(token=token)


def stop_on_signals() -> asyncio.Event:
    """An event that SIGINT or SIGTERM sets, so every mode can stop cleanly."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop
