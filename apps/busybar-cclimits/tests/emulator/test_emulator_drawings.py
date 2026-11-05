"""What the app draws, and how the runner keeps it up, held to the emulator's bar."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from busylib.client import AsyncBusyBar
from emulator_support import Emulator, write_feed

from busybar_cclimits import connect, render
from busybar_cclimits.config import Config
from busybar_cclimits.runner import Runner
from busybar_cclimits.samples import SAMPLES

APP = "busybar-cclimits"
DAY = 86400


def config_for(emulator: Emulator, feed: Path | None = None, **display) -> Config:
    data: dict = {
        "bar": {"address": emulator.host},
        "display": display,
        "source": {"kind": "statusline"},
    }
    if feed is not None:
        data["feed"] = {"path": str(feed)}
    return Config.model_validate(data)


@pytest.fixture
async def bar(emulator: Emulator) -> AsyncIterator[AsyncBusyBar]:
    """The same client the service uses, pointed at the emulator."""
    client = connect.make_bar(config_for(emulator))
    yield client
    await client.aclose()


@pytest.mark.parametrize("name", list(SAMPLES))
async def test_the_bar_takes_every_sample(emulator: Emulator, bar: AsyncBusyBar, name: str):
    sample = SAMPLES[name]
    now = time.time()
    drawn = render.elements(sample.feed(now), now, sample.display_config())
    await render.clear(bar, APP)
    await render.send(bar, render.drawing(drawn, now + 60, APP, 50))
    frame = emulator.frame()
    assert (frame["application_name"], frame["priority"]) == (APP, 50)
    assert [e["id"] for e in frame["elements"]] == [e.id for e in drawn]


async def test_a_reset_leaves_nothing_of_the_countdown_behind(
    emulator: Emulator, bar: AsyncBusyBar, feed_path: Path
):
    now = time.time()
    write_feed(feed_path, now, five_hour=(42, 5), seven_day=(18, 3 * DAY))
    runner = Runner(config_for(emulator, feed_path), bar)
    await runner.tick(now)
    assert "cc0_clock" in emulator.mine()
    # Five seconds on the window has reset: the countdown gives way to "--". The bar
    # keeps every element an app drew, so this only works if the runner clears first.
    await runner.tick(now + 6)
    mine = emulator.mine()
    assert "cc0_clock" not in mine and mine["cc0_text"]["text"] == "--"


async def test_it_gives_way_to_a_higher_priority_and_takes_the_screen_back(
    emulator: Emulator, bar: AsyncBusyBar, feed_path: Path, caplog: pytest.LogCaptureFixture
):
    now = time.time()
    write_feed(feed_path, now, five_hour=(42, 3600), seven_day=(18, 3 * DAY))
    runner = Runner(config_for(emulator, feed_path), bar)
    await runner.tick(now)
    assert emulator.mine() is not None

    # A calendar reminder (91) takes the screen; the bar refuses this app's refresh
    # with a 409, which is waiting, not a failure to back off from.
    emulator.post("/api/_scenario/steal", priority=91)
    await runner.tick(now + 60)
    assert emulator.mine() is None
    assert "refused" not in caplog.text

    # It ends, and the bar shows nothing until this app sends its drawing again.
    emulator.post("/api/_scenario/reset")
    await runner.tick(now + 120)
    assert emulator.mine() is not None


async def test_redraws_update_in_place(emulator: Emulator, bar: AsyncBusyBar, feed_path: Path):
    """Element ids stay the same, so the bar's per-app set, capped at 100, does not grow."""
    now = time.time()
    write_feed(feed_path, now, five_hour=(42, 3600), seven_day=(18, 3 * DAY))
    runner = Runner(config_for(emulator, feed_path), bar)
    for step in range(6):
        write_feed(feed_path, now, five_hour=(42 + 10 * step, 3600), seven_day=(18, 3 * DAY))
        await runner.tick(now + 60 * step)
    stored = {
        int(match.group(1))
        for _, path, note in emulator.calls(now - 1)
        if path == "/api/display/draw" and (match := re.search(r"\((\d+) stored\)", note))
    }
    assert stored == {8}
    assert emulator.mine()["cc0_fill"]["fill_colors"] == ["#FF2A00FF"]  # 92%: red
