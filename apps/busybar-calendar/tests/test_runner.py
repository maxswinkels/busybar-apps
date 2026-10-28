import asyncio
import itertools
from datetime import UTC, datetime, timedelta

from busylib.exceptions import BusyBarAPIError

from busybar_calendar.config import Config
from busybar_calendar.events import CalendarRef, Event
from busybar_calendar.runner import Runner

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
REF = CalendarRef("Work", "Work", 0)
CONFIG = Config.model_validate({"sources": [{"name": "Work", "type": "ics", "url": "https://x"}]})
TITLED = ("draw", ["cal_xpm", "cal_title", "cal_time"])  # the reminder
BUSY = ("draw", ["cal_xpm", "cal_title", "cal_time", "cal_busy"])  # the status
MAYBE = ("draw", ["cal_xpm_maybe", "cal_title", "cal_time"])  # a tentative reminder
MAYBE_BUSY = ("draw", ["cal_xpm_maybe", "cal_title", "cal_time", "cal_busy"])  # its status
CLEAR = ("clear", "busybar-calendar")


class FakeBar:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail = 0
        self.someone_else_on_screen = False

    async def api_request(self, method, path, json_payload=None, **kwargs):
        assert (method, path) == ("POST", "/api/display/draw")
        if self.fail:
            self.fail -= 1
            raise ConnectionError("unreachable")
        self.calls.append(("draw", [e["id"] for e in json_payload["elements"]]))

    async def display_clear(self, **kwargs):
        if self.someone_else_on_screen:
            raise BusyBarAPIError("Bad Request", status_code=400, method="DELETE")
        self.calls.append(("clear", kwargs.get("application_name")))

    async def audio_play(self, *, stock_path=None, **kwargs):
        self.calls.append(("sound", stock_path.rsplit("/", 1)[-1]))


class FakeSource:
    name = "Work"

    def __init__(self, events) -> None:
        self.events = events
        self.fail = False

    async def fetch(self, start, end):
        if self.fail:
            raise OSError("server down")
        return list(self.events)

    async def aclose(self):
        pass


def make(events, config=CONFIG):
    bar, source = FakeBar(), FakeSource(events)
    return Runner(config, bar, [source]), bar, source


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def meeting(start_min=10, length=30, title="Sync", response="accepted", uid="m"):
    start = at(start_min)
    return Event(uid, title, start, start + timedelta(minutes=length), REF, response)


async def test_full_life_of_an_event():
    runner, bar, _ = make([meeting()])
    await runner.refresh(at(0))
    await runner.tick(at(0))
    await runner.tick(at(5))
    await runner.tick(at(6))  # unchanged, no redraw
    await runner.tick(at(10))
    await runner.tick(at(40))
    assert bar.calls == [
        CLEAR,  # whatever an earlier run left behind
        TITLED,
        ("sound", "calendar_reminder_ends.snd"),
        BUSY,  # the reminder's elements plus BUSY: updated in place, no clear, no flicker
        ("sound", "calendar_event_starts.snd"),
        CLEAR,
    ]


async def test_tentative_event_is_drawn_yellow_and_updated_in_place():
    runner, bar, _ = make([meeting(response="tentative")])
    await runner.refresh(at(5))
    await runner.tick(at(5))
    await runner.tick(at(10))
    # Yellow for the reminder and the status alike; the status only adds BUSY.
    assert [c for c in bar.calls if c[0] != "sound"] == [CLEAR, MAYBE, MAYBE_BUSY]


async def test_accepted_event_takes_the_bar_from_a_tentative_one_and_gives_it_back():
    # Maybe 0-60 is on; Sure 20-50 starts in the middle of it.
    runner, bar, _ = make(
        [
            meeting(0, 60, "Maybe", "tentative", uid="t"),
            meeting(20, 30, "Sure", "accepted", uid="a"),
        ]
    )
    await runner.refresh(at(10))
    for minute in (10, 14, 16, 21, 51, 61):
        await runner.tick(at(minute))
    assert [c for c in bar.calls if c[0] != "sound"] == [
        CLEAR,
        MAYBE_BUSY,  # 10: the tentative event is on, in yellow
        CLEAR,  # 15: Sure's reminder takes over - red, so its own element ids
        TITLED,
        BUSY,  # 20: Sure's status
        CLEAR,  # 50: Sure is over, and the yellow one is back
        MAYBE_BUSY,
        CLEAR,  # 60: nothing left
    ]


async def test_changing_your_answer_redraws_the_screen():
    # Marked "maybe" from the phone while the reminder is up: yellow without waiting.
    runner, bar, source = make([meeting(response="accepted")])
    await runner.refresh(at(5))
    await runner.tick(at(5))
    source.events = [meeting(response="tentative")]
    await runner.refresh(at(6))
    await runner.tick(at(6))
    assert [c for c in bar.calls if c[0] != "sound"] == [CLEAR, TITLED, CLEAR, MAYBE]
    # Not a second chime for the same reminder.
    assert [c for c in bar.calls if c[0] == "sound"] == [("sound", "calendar_reminder_ends.snd")]


async def test_same_elements_update_in_place():
    config = Config.model_validate(
        {
            "display": {"busy_label": ""},
            "sources": [{"name": "Work", "type": "ics", "url": "https://x"}],
        }
    )
    runner, bar, _ = make([meeting()], config)
    await runner.refresh(at(5))
    await runner.tick(at(5))
    await runner.tick(at(10))
    # Reminder and status share their element ids: no clear, so no flicker.
    assert [c for c in bar.calls if c[0] != "sound"] == [CLEAR, TITLED, TITLED]


async def test_first_draw_after_start_clears_leftovers():
    runner, bar, _ = make([meeting()])
    await runner.refresh(at(20))
    await runner.tick(at(20))
    # Restarted mid-meeting: no chime, and the old run's drawing goes first.
    assert bar.calls == [CLEAR, BUSY]


async def test_layout_change_clears_before_drawing():
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "Work",
                    "type": "ics",
                    "url": "https://x",
                    "status": {"show": "meeting", "sound": False},
                    "reminder": {"sound": False},
                }
            ]
        }
    )
    runner, bar, _ = make([meeting()], config)
    await runner.refresh(at(5))
    await runner.tick(at(5))
    await runner.tick(at(10))
    # The meeting animation has other ids than the reminder, which a draw
    # would otherwise leave underneath it.
    assert bar.calls == [CLEAR, TITLED, CLEAR, ("draw", ["cal_status"])]


async def test_clear_when_another_app_owns_the_screen():
    runner, bar, _ = make([meeting()])
    bar.someone_else_on_screen = True
    await runner.refresh(at(0))
    await runner.tick(at(0))
    await runner.tick(at(1))
    assert bar.calls == []  # 400 means nothing of ours is there: done, no retry loop
    bar.someone_else_on_screen = False
    await runner.tick(at(5))
    assert bar.calls[0] == TITLED  # known clear already, so no second clear


async def test_failed_draw_is_retried():
    runner, bar, _ = make([meeting()])
    bar.fail = 1
    await runner.refresh(at(5))
    await runner.tick(at(5))
    assert bar.calls == [CLEAR]
    await runner.tick(at(5.5))
    assert bar.calls == [
        CLEAR,
        CLEAR,  # the failed draw left the screen unknown
        TITLED,
        ("sound", "calendar_reminder_ends.snd"),
    ]


async def test_failed_refresh_keeps_events():
    runner, bar, source = make([meeting()])
    await runner.refresh(at(0))
    source.fail = True
    await runner.refresh(at(9))
    await runner.tick(at(12))
    assert BUSY in bar.calls


async def test_renamed_event_redraws_without_chime():
    runner, bar, source = make([meeting()])
    await runner.refresh(at(10))
    await runner.tick(at(10))
    source.events = [meeting(title="Sync (moved room)")]
    await runner.refresh(at(12))
    await runner.tick(at(12))
    assert [c[0] for c in bar.calls] == ["clear", "draw", "sound", "draw"]


async def test_run_wakes_on_time_and_clears_on_stop():
    clock = [at(4.999)]
    runner, bar, _ = make([meeting()])
    runner.now = lambda: clock[0]
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    await asyncio.sleep(0.05)
    clock[0] = at(5)  # the reminder opens; the loop wakes within its 0.2s floor
    await asyncio.sleep(0.4)
    stop.set()
    await task
    assert [c[0] for c in bar.calls] == ["clear", "draw", "sound", "clear"]


async def test_a_source_configured_wrong_stops_the_service():
    from busybar_calendar.sources import FatalSourceError

    runner, _, source = make([meeting()])
    await runner.refresh(at(0))

    async def wrong_password(start, end):
        raise FatalSourceError("Work: 401, check the credentials")

    source.fetch = wrong_password
    try:
        await runner.refresh(at(1))
    except FatalSourceError as error:
        assert "401" in str(error)
    else:
        raise AssertionError("a 401 must stop the service, not be retried forever")


async def test_play_carries_on_when_the_bar_refuses_a_draw(monkeypatch):
    from busybar_calendar import playback as cli
    from busybar_calendar.playback import play

    async def no_wait(stop, moment):
        return False

    monkeypatch.setattr(cli, "_wait", no_wait)

    class Busy409Bar(FakeBar):
        async def api_request(self, method, path, json_payload=None, **kwargs):
            title = next(e["text"] for e in json_payload["elements"] if e["id"] == "cal_title")
            if title == "Second":
                raise BusyBarAPIError("Not drawn due to low priority", status_code=409)
            return await super().api_request(method, path, json_payload=json_payload, **kwargs)

    bar = Busy409Bar()
    runner = Runner(CONFIG, bar, [])
    events = [meeting(title="First"), meeting(title="Second"), meeting(title="Third")]
    await play(runner, events, 1, asyncio.Event(), chime=False)
    drawn = [c for c in bar.calls if c[0] == "draw"]
    # Second's two screens were refused; First and Third still played in full.
    assert len(drawn) == 4


async def test_left_over_elements_are_cleared_first():
    # A meeting's status, then the next meeting's reminder: BUSY must not stay up.
    runner, bar, _ = make([meeting(start_min=0, length=60, title="Long"), meeting(start_min=30)])
    await runner.refresh(at(10))
    await runner.tick(at(10))
    await runner.tick(at(26))
    # (No chime for Long: its status opened ten minutes before the first tick.)
    assert bar.calls == [CLEAR, BUSY, CLEAR, TITLED, ("sound", "calendar_reminder_ends.snd")]


async def test_chimes_of_events_that_are_gone_are_forgotten():
    runner, _, source = make([meeting()])
    await runner.refresh(at(5))
    await runner.tick(at(5))
    assert runner._sounded == {("reminder", meeting().key)}
    source.events = []  # the meeting is over and out of the window
    await runner.refresh(at(2000))
    assert runner._sounded == set()


async def test_title_rules_keep_an_event_off_the_bar():
    config = Config.model_validate(
        {
            "sources": [{"name": "Work", "type": "ics", "url": "https://x"}],
            "titles": [
                {"match": "Focus*", "reminder": {"enabled": False}, "status": {"enabled": False}}
            ],
        }
    )
    runner, bar, _ = make([meeting(title="Focus time"), meeting(60, title="Sync")], config)
    await runner.refresh(at(0))
    for minute in (6, 20):  # the focus block's reminder, then its middle
        await runner.tick(at(minute))
    assert bar.calls == [CLEAR]
    await runner.tick(at(56))  # the Sync reminder still shows, with its sound
    assert bar.calls[1:] == [TITLED, ("sound", "calendar_reminder_ends.snd")]


async def test_an_edited_config_redraws_at_once_without_a_second_chime():
    runner, bar, _ = make([meeting()])
    await runner.refresh(at(10))
    await runner.tick(at(10))
    assert bar.calls[1:] == [BUSY, ("sound", "calendar_event_starts.snd")]

    edited = Config.model_validate(
        {
            "defaults": {"status": {"show": "meeting"}},
            "sources": [{"name": "Work", "type": "ics", "url": "https://x"}],
        }
    )
    fresh = FakeSource([meeting()])
    runner.reconfigure(edited, [fresh])
    assert runner.sources == [fresh] and runner.config is edited
    await runner.refresh(at(10.5))
    await runner.tick(at(10.5))
    # The bar's own status in place of the titled screen: cleared first, no chime.
    assert bar.calls[3:] == [CLEAR, ("draw", ["cal_status"])]


async def test_reconfigure_keeps_the_events_of_sources_still_configured():
    runner, _, _ = make([meeting()])
    await runner.refresh(at(0))
    runner.reconfigure(CONFIG, [FakeSource([])])  # same name: events kept until re-read
    assert len(runner.events) == 1
    renamed = FakeSource([])
    renamed.name = "Home"
    runner.reconfigure(CONFIG, [renamed])
    assert runner.events == []


async def test_run_calls_its_hook_at_every_wake():
    runner, _, _ = make([])
    clock = itertools.count()
    runner.now = lambda: at(2 * next(clock))  # time flies: every sleep is the 0.2s floor
    stop = asyncio.Event()
    calls = []

    async def hook():
        calls.append(1)
        if len(calls) == 2:
            stop.set()

    await asyncio.wait_for(runner.run(stop, hook), 2)
    assert calls == [1, 1]
