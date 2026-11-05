"""The service, `test` and the gallery's app.py, run as they are run, against the emulator."""

from __future__ import annotations

import signal
import time
from pathlib import Path

from conftest import REPO
from emulator_support import Emulator, wait_for, write_feed

DAY = 86400


def test_the_service_follows_the_feed_and_clears_when_stopped(
    emulator: Emulator, write_config, feed_path: Path, start
):
    write_feed(feed_path, time.time(), five_hour=(42, 3600), seven_day=(18, 3 * DAY))
    service = start("-m", "busybar_cclimits", "-c", str(write_config()), "run")
    first = wait_for(emulator.mine, f"the service's first drawing:\n{service.text()}")
    width = first["cc0_fill"]["width"]
    assert first["cc1_text"]["text"] == "18%"

    write_feed(feed_path, time.time(), five_hour=(84, 3600), seven_day=(18, 3 * DAY))

    def grown() -> dict | None:
        mine = emulator.mine()
        return mine if mine and mine["cc0_fill"]["width"] > width else None

    assert wait_for(grown, "the 5-hour bar to grow")["cc0_fill"]["fill_colors"] == ["#FFC800FF"]

    feed_path.unlink()
    wait_for(lambda: emulator.mine() is None, "the bar to be cleared without a feed")

    write_feed(feed_path, time.time(), five_hour=(10, 3600), seven_day=(18, 3 * DAY))
    wait_for(emulator.mine, "the service to draw again")
    assert service.stop(signal.SIGTERM) == 0, service.text()
    assert emulator.mine() is None
    assert "busybar-cclimits" in service.text() and "started" in service.text()


def test_the_test_command_draws_a_sample(emulator: Emulator, write_config, start):
    run = start("-m", "busybar_cclimits", "-c", str(write_config()), "test")

    def drawn() -> dict | None:
        mine = emulator.mine()
        return mine if mine and "cc0_clock" in mine else None

    wait_for(drawn, f"the sample:\n{run.text()}")
    assert run.stop(signal.SIGINT) in (0, 130), run.text()


def test_app_py_test_draws_one_frame_and_exits(emulator: Emulator, start):
    run = start(str(REPO / "app.py"), "--host", emulator.host, "--test")
    assert run.wait() == 0, run.text()
    mine = emulator.mine()
    assert mine is not None and mine["cc0_label"]["text"] == "5H"


def test_app_py_without_limits_plays_the_demo(emulator: Emulator, start):
    run = start(str(REPO / "app.py"), "--host", emulator.host, "--source", "statusline")
    wait_for(emulator.mine, f"the demo:\n{run.text()}")
    assert "playing a demo" in run.text()
    assert run.stop(signal.SIGTERM) == 0, run.text()
    assert emulator.mine() is None
