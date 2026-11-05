import json

import pytest
from busylib.exceptions import BusyBarAPIError

from busybar_cclimits.config import Config
from busybar_cclimits.runner import Runner

NOW = 1_800_000_000.0


class FakeBar:
    def __init__(self):
        self.calls = []
        self.fail: list[int | Exception] = []  # popped per call

    def _maybe_fail(self):
        if self.fail:
            error = self.fail.pop(0)
            if isinstance(error, int):
                raise BusyBarAPIError("refused", status_code=error)
            raise error

    async def api_request(self, method, path, **kwargs):
        self.calls.append(("draw", kwargs["json_payload"]))
        self._maybe_fail()

    async def display_clear(self, **kwargs):
        self.calls.append(("clear", kwargs))
        self._maybe_fail()

    def kinds(self):
        return [kind for kind, _ in self.calls]


def write_feed(path, five=42.0, five_resets=NOW + 3600, seen=NOW):
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "seen_at": seen,
                "updated_at": seen,
                "rate_limits": {
                    "five_hour": {"used_percentage": five, "resets_at": five_resets},
                    "seven_day": {"used_percentage": 10, "resets_at": NOW + 86400 * 3},
                },
            }
        )
    )


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "limits.json"
    config = Config.model_validate({"feed": {"path": str(path)}})
    bar = FakeBar()
    return path, config, bar, Runner(config, bar, clock=lambda: NOW)


async def test_first_draw_clears_leftovers_then_updates_in_place(setup):
    path, config, bar, runner = setup
    write_feed(path)
    await runner.tick(NOW)
    assert bar.kinds() == ["clear", "draw"]
    payload = bar.calls[1][1]
    assert payload["priority"] == 50
    until = {e["display_until"] for e in payload["elements"]}
    assert until == {str(int(NOW + 3 * config.display.refresh_seconds))}

    await runner.tick(NOW + 2)  # nothing changed
    assert bar.kinds() == ["clear", "draw"]

    write_feed(path, five=60)
    await runner.tick(NOW + 4)
    assert bar.kinds() == ["clear", "draw", "draw"]  # same ids: no clear


async def test_redraws_every_refresh_to_take_the_screen_back(setup):
    path, config, bar, runner = setup
    write_feed(path)
    await runner.tick(NOW)
    await runner.tick(NOW + config.display.refresh_seconds - 1)
    assert bar.kinds().count("draw") == 1
    await runner.tick(NOW + config.display.refresh_seconds)
    assert bar.kinds().count("draw") == 2


async def test_a_reset_changes_the_ids_and_clears_first(setup):
    path, _config, bar, runner = setup
    write_feed(path, five_resets=NOW + 10)
    await runner.tick(NOW)
    await runner.tick(NOW + 10)  # countdown -> "--" text
    assert bar.kinds() == ["clear", "draw", "clear", "draw"]
    ids = {e["id"] for e in bar.calls[-1][1]["elements"]}
    assert "cc0_text" in ids and "cc0_clock" not in ids


async def test_higher_priority_on_screen_is_not_an_error(setup):
    path, _config, bar, runner = setup
    write_feed(path)
    # Nothing of this app's to clear (400), and the draw is refused (409).
    bar.fail = [400, 409]
    await runner.tick(NOW)
    assert runner._retry_at == float("-inf")  # no back-off
    await runner.tick(NOW + 2)
    assert bar.kinds() == ["clear", "draw"]  # waits for the refresh


async def test_failures_back_off(setup):
    path, _config, bar, runner = setup
    write_feed(path)
    bar.fail = [OSError("down")]
    await runner.tick(NOW)
    assert runner._retry_at == NOW + 5
    await runner.tick(NOW + 4)
    assert bar.kinds() == ["clear"]
    await runner.tick(NOW + 5)
    assert bar.kinds() == ["clear", "clear", "draw"]


async def test_clears_when_the_feed_goes(setup, caplog):
    path, _config, bar, runner = setup
    await runner.tick(NOW)
    assert bar.kinds() == []  # nothing drawn, nothing to clear
    assert "hook install" in caplog.text
    write_feed(path)
    await runner.tick(NOW)
    path.unlink()
    await runner.tick(NOW + 2)
    assert bar.kinds() == ["clear", "draw", "clear"]


async def test_with_claude_gives_the_bar_back_when_idle(tmp_path):
    path = tmp_path / "limits.json"
    config = Config.model_validate(
        {"feed": {"path": str(path)}, "display": {"show": "with_claude", "idle_minutes": 1}}
    )
    bar = FakeBar()
    runner = Runner(config, bar)
    write_feed(path, seen=NOW)
    await runner.tick(NOW + 30)
    assert bar.kinds() == ["clear", "draw"]
    await runner.tick(NOW + 61)
    assert bar.kinds() == ["clear", "draw", "clear"]
