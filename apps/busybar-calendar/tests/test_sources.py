from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from busybar_calendar.caldav import RemoteCalendar
from busybar_calendar.config import Config
from busybar_calendar.sources import CalDAVSource, select

BASE = "https://dav.example.com/dav.php/calendars/me@example.com"
MINE = RemoteCalendar(f"{BASE}/4711/", "Doe, Jane")
OPS = RemoteCalendar(f"{BASE}/5001/", "Ops Maintenances")
SRE = RemoteCalendar(f"{BASE}/5002/", "Infra Maintenances")
ROOM_A = RemoteCalendar(f"{BASE}/6001/", "Office - Room 1 (6 people)")
ROOM_B = RemoteCalendar(f"{BASE}/6002/", "Office - Room 2 (8 people)")
ALL = [MINE, OPS, SRE, ROOM_A, ROOM_B]


def names(calendars):
    return [c.name for c in calendars]


def test_calendar_id_is_the_last_url_segment():
    assert MINE.id == "4711"
    assert RemoteCalendar("https://x/cal/My%20Cal", "My Cal").id == "My Cal"


def test_no_filter_means_all_in_server_order():
    assert select(ALL, [], []) == (ALL, [])


def test_include_by_name_id_url_and_wildcard():
    assert select(ALL, ["Doe, Jane"], []) == ([MINE], [])
    assert select(ALL, ["4711"], []) == ([MINE], [])
    assert select(ALL, [f"{BASE}/4711/"], []) == ([MINE], [])
    assert select(ALL, [f"{BASE}/4711"], []) == ([MINE], [])  # trailing slash optional
    assert names(select(ALL, ["* Maintenances"], [])[0]) == [
        "Ops Maintenances",
        "Infra Maintenances",
    ]


def test_include_order_decides_overlaps_and_matches_once():
    chosen, _ = select(ALL, ["500*", "4711", "Infra Maintenances"], [])
    # Listed order, each calendar once: the last listed wins an overlap.
    assert names(chosen) == ["Ops Maintenances", "Infra Maintenances", "Doe, Jane"]


def test_exclude_by_wildcard_and_id():
    assert select(ALL, [], ["Office - Room *"]) == ([MINE, OPS, SRE], [])
    assert select(ALL, ["*"], ["Office - Room *", "5001"]) == ([MINE, SRE], [])


def test_entries_that_match_nothing_are_reported():
    chosen, unmatched = select(ALL, ["4711", "Typo"], ["Annex - Room *"])
    assert chosen == [MINE]
    assert unmatched == ["Typo", "Annex - Room *"]


def test_names_are_case_sensitive_and_exact():
    assert select(ALL, ["doe, jane"], [])[0] == []
    assert select(ALL, ["Doe"], [])[0] == []


def test_overrides_by_id_layer_over_name():
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "Mail",
                    "type": "caldav",
                    "url": "https://x",
                    "reminder": {"minutes_before": 3},
                    "calendars": {
                        "Doe, Jane": {"reminder": {"minutes_before": 7, "sound": False}},
                        "4711": {"reminder": {"minutes_before": 10}},
                    },
                }
            ]
        }
    )
    reminder, _ = config.settings_for("Mail", "Doe, Jane", "4711")
    assert (reminder.minutes_before, reminder.sound) == (10, False)
    reminder, _ = config.settings_for("Mail", "Other", "99")
    assert (reminder.minutes_before, reminder.sound) == (3, True)


class FakeClient:
    def __init__(self, calendars, documents):
        self.calendars = calendars
        self.documents = documents

    async def discover(self):
        return self.calendars

    async def events(self, calendar, start, end):
        return self.documents.get(calendar.id, [])


def ics(uid, start):
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    end = (start + timedelta(minutes=30)).strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nSUMMARY:{uid}\r\nDTSTART:{stamp}\r\nDTEND:{end}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


async def test_source_fetches_only_selected_calendars_with_ids():
    config = Config.model_validate(
        {
            "sources": [
                {
                    "name": "Mail",
                    "type": "caldav",
                    "url": "https://x",
                    "include": ["Infra Maintenances", "4711"],
                    "exclude": ["Office - Room *"],
                }
            ]
        }
    )
    start = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    source = CalDAVSource(config.sources[0], 0, ZoneInfo("UTC"), True)
    source._client = FakeClient(
        ALL,
        {
            "4711": [ics("mine", start)],
            "5002": [ics("sre", start)],
            "6001": [ics("room", start)],
        },
    )
    events = await source.fetch(start - timedelta(hours=1), start + timedelta(hours=1))
    by_title = {e.title: e.calendar for e in events}
    assert set(by_title) == {"mine", "sre"}
    assert (by_title["mine"].id, by_title["mine"].name) == ("4711", "Doe, Jane")
    # Listed last, so it outranks Infra Maintenances when both start together.
    assert by_title["mine"].order > by_title["sre"].order
    await source.aclose()


class FailingClient(FakeClient):
    def __init__(self, error):
        super().__init__([], {})
        self.error = error

    async def discover(self):
        raise self.error


async def test_wrong_url_or_password_is_fatal_but_a_server_error_is_not():
    import pytest

    from busybar_calendar.caldav import CalDAVAuthError, CalDAVError
    from busybar_calendar.sources import FatalSourceError

    config = Config.model_validate(
        {"sources": [{"name": "M", "type": "caldav", "url": "https://x"}]}
    )
    start = datetime(2026, 10, 13, tzinfo=UTC)
    for error, fatal in (
        (CalDAVAuthError("401", 401), True),
        (CalDAVError("404", 404), True),
        (CalDAVError("no current-user-principal"), True),
        (CalDAVError("503", 503), False),
    ):
        source = CalDAVSource(config.sources[0], 0, ZoneInfo("UTC"), True)
        source._client = FailingClient(error)
        with pytest.raises(FatalSourceError if fatal else CalDAVError):
            await source.fetch(start, start + timedelta(hours=1))
        await source.aclose()


async def test_a_failed_relisting_keeps_the_known_calendars(caplog):
    import httpx

    from busybar_calendar.caldav import CalDAVAuthError, CalDAVError
    from busybar_calendar.sources import REDISCOVER_EVERY, FatalSourceError

    config = Config.model_validate(
        {"sources": [{"name": "M", "type": "caldav", "url": "https://x", "include": ["4711"]}]}
    )
    start = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    window = (start - timedelta(hours=1), start + timedelta(hours=1))
    source = CalDAVSource(config.sources[0], 0, ZoneInfo("UTC"), True)
    client = FakeClient(ALL, {"4711": [ics("mine", start)]})
    source._client = client
    listed = []

    async def discover():
        listed.append(1)
        return ALL

    client.discover = discover
    for _ in range(REDISCOVER_EVERY):
        await source.fetch(*window)
    assert len(listed) == 1  # listed once, then again only every REDISCOVER_EVERY

    for hiccup in (CalDAVError("503", 503), httpx.ConnectTimeout("timed out")):

        async def failing(error=hiccup):
            raise error

        client.discover = failing
        events = await source.fetch(*window)  # due to list again; the server hiccups
        assert [e.title for e in events] == ["mine"]
        assert "going on with the ones it knows" in caplog.text

    client.discover = discover
    await source.fetch(*window)  # listed again at the very next refresh
    assert len(listed) == 2

    async def refused():
        raise CalDAVAuthError("401", 401)

    client.discover = refused
    for _ in range(REDISCOVER_EVERY):
        try:
            await source.fetch(*window)
        except FatalSourceError:
            break  # a password that stopped working still stops the service
    else:
        raise AssertionError("a 401 while listing again must be fatal")
    await source.aclose()
