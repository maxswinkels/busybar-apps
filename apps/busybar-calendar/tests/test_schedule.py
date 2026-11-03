from datetime import UTC, datetime, timedelta

from busybar_calendar.config import ReminderSettings, StatusSettings
from busybar_calendar.events import CalendarRef, Event
from busybar_calendar.schedule import choose, next_change, upcoming

T0 = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
WORK = CalendarRef("icloud", "Work", 0)
HOME = CalendarRef("icloud", "Home", 1)


def event(title, start_min, length_min=30, calendar=WORK):
    start = T0 + timedelta(minutes=start_min)
    return Event(title, title, start, start + timedelta(minutes=length_min), calendar)


def settings(reminder=None, status=None):
    reminder = reminder or ReminderSettings()
    status = status or StatusSettings()
    return lambda _event: (reminder, status)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_reminder_then_status_then_nothing():
    events = [event("Sync", 10)]
    s = settings()
    assert choose(at(0), events, s) is None
    assert choose(at(5), events, s).kind == "reminder"
    assert choose(at(9.99), events, s).kind == "reminder"
    assert choose(at(10), events, s).kind == "status"
    assert choose(at(39.99), events, s).kind == "status"
    assert choose(at(40), events, s) is None


def test_disabled_settings():
    events = [event("Sync", 10)]
    assert choose(at(6), events, settings(ReminderSettings(enabled=False))) is None
    assert choose(at(12), events, settings(status=StatusSettings(enabled=False))) is None


def test_next_reminder_replaces_running_status():
    # The newest event wins: the reminder for 09:30 over the 09:00 meeting.
    events = [event("Long", 0, 90), event("Next", 30)]
    screen = choose(at(26), events, settings())
    assert (screen.kind, screen.event.title) == ("reminder", "Next")
    screen = choose(at(31), events, settings())
    assert (screen.kind, screen.event.title) == ("status", "Next")
    screen = choose(at(70), events, settings())
    assert (screen.kind, screen.event.title) == ("status", "Long")


def test_same_start_later_calendar_wins():
    events = [event("Home thing", 0, calendar=HOME), event("Work thing", 0, calendar=WORK)]
    assert choose(at(1), events, settings()).event.title == "Home thing"


def test_screen_keys_change_with_what_is_drawn():
    events = [event("Sync", 10)]
    s = settings()
    assert choose(at(5), events, s).key == choose(at(6), events, s).key
    assert choose(at(5), events, s).key != choose(at(10), events, s).key
    renamed = [Event("Sync", "Renamed", events[0].start, events[0].end, WORK)]
    assert choose(at(5), events, s).key != choose(at(5), renamed, s).key


def test_next_change():
    events = [event("Sync", 10), event("Later", 120)]
    s = settings(ReminderSettings(minutes_before=10))
    assert next_change(at(-30), events, s) == at(0)
    assert next_change(at(0), events, s) == at(10)
    assert next_change(at(10), events, s) == at(40)
    assert next_change(at(40), events, s) == at(110)
    assert next_change(at(200), events, s) is None


def answered(title, start_min, length_min, response, calendar=WORK):
    start = T0 + timedelta(minutes=start_min)
    end = start + timedelta(minutes=length_min)
    return Event(title, title, start, end, calendar, response)


def test_accepted_beats_tentative_whichever_is_newer():
    # Tentative, newer: the accepted meeting keeps the bar.
    events = [answered("Accepted", 0, 60, "accepted"), answered("Maybe", 30, 30, "tentative")]
    assert choose(at(26), events, settings()).event.title == "Accepted"  # no reminder for Maybe
    assert choose(at(40), events, settings()).event.title == "Accepted"
    # Accepted, newer: it wins, as it would anyway.
    events = [answered("Maybe", 0, 60, "tentative"), answered("Accepted", 30, 30, "accepted")]
    assert (
        choose(at(26), events, settings()).kind,
        choose(at(26), events, settings()).event.title,
    ) == (
        "reminder",
        "Accepted",
    )
    assert choose(at(40), events, settings()).event.title == "Accepted"
    assert choose(at(61), events, settings()) is None


def test_same_start_accepted_wins_over_calendar_order():
    events = [
        answered("Focus time", 0, 115, "tentative", calendar=HOME),
        answered("Sync with assistant", 0, 25, "accepted", calendar=WORK),
    ]
    assert choose(at(1), events, settings()).event.title == "Sync with assistant"
    # Once it ends, the tentative one is all that is left.
    assert choose(at(26), events, settings()).event.title == "Focus time"


def shown(minute, events):
    screen = choose(at(minute), events, settings())
    return None if screen is None else (screen.kind, screen.event.title)


def test_accepted_wins_over_tentative_at_the_same_time():
    # Same start and end; the tentative one is on the earlier calendar and listed first.
    events = [
        answered("Maybe", 0, 60, "tentative", calendar=HOME),
        answered("Sure", 0, 60, "accepted", calendar=WORK),
    ]
    assert shown(30, events) == ("status", "Sure")
    assert shown(30, events[::-1]) == ("status", "Sure")  # and not by list order


def test_accepted_wins_when_it_starts_after_the_tentative_one():
    # Maybe 0-60, Sure 20-80: the accepted reminder (15) and status (20) take the
    # bar, and the tentative event does not come back while Sure is still on.
    events = [answered("Maybe", 0, 60, "tentative"), answered("Sure", 20, 60, "accepted")]
    assert shown(10, events) == ("status", "Maybe")
    assert shown(16, events) == ("reminder", "Sure")
    assert shown(30, events) == ("status", "Sure")
    assert shown(65, events) == ("status", "Sure")  # Maybe is over by now
    assert shown(81, events) is None


def test_accepted_wins_when_it_starts_before_the_tentative_one():
    # Sure 0-40, Maybe 20-80: Maybe's status does not cut in at 20, and takes the
    # bar when Sure ends.
    events = [answered("Sure", 0, 40, "accepted"), answered("Maybe", 20, 60, "tentative")]
    assert shown(10, events) == ("status", "Sure")
    assert shown(25, events) == ("status", "Sure")
    assert shown(39.9, events) == ("status", "Sure")
    assert shown(40, events) == ("status", "Maybe")
    assert shown(81, events) is None


def test_accepted_inside_a_longer_tentative_event():
    # Maybe 0-120 with Sure 30-60 in the middle: Sure for its slot, Maybe around it.
    events = [answered("Maybe", 0, 120, "tentative"), answered("Sure", 30, 30, "accepted")]
    assert [shown(m, events) for m in (10, 26, 45, 60, 90)] == [
        ("status", "Maybe"),
        ("reminder", "Sure"),
        ("status", "Sure"),
        ("status", "Maybe"),
        ("status", "Maybe"),
    ]


def test_a_tentative_reminder_does_not_interrupt_an_accepted_event():
    # Maybe starts at 40 and would remind from 35, while Sure runs until 60.
    events = [answered("Sure", 0, 60, "accepted"), answered("Maybe", 40, 30, "tentative")]
    assert shown(36, events) == ("status", "Sure")
    assert shown(45, events) == ("status", "Sure")
    assert shown(61, events) == ("status", "Maybe")


def test_a_chain_of_shifted_events_always_shows_the_accepted_one():
    events = [
        answered("Maybe 1", 0, 50, "tentative"),
        answered("Sure", 20, 50, "accepted"),
        answered("Maybe 2", 40, 50, "tentative"),
    ]
    assert [shown(m, events) for m in (10, 30, 55, 69, 71, 85)] == [
        ("status", "Maybe 1"),
        ("status", "Sure"),
        ("status", "Sure"),
        ("status", "Sure"),
        ("status", "Maybe 2"),
        ("status", "Maybe 2"),
    ]


def test_an_accepted_event_without_a_status_leaves_the_bar_to_the_tentative_one():
    # The settings decide what an event shows; an accepted event that puts nothing
    # up (a rule turned its status and reminder off) cannot take the bar from anyone.
    events = [answered("Quiet", 0, 60, "accepted"), answered("Maybe", 0, 60, "tentative")]

    def for_event(event):
        if event.title == "Quiet":
            return ReminderSettings(enabled=False), StatusSettings(enabled=False)
        return ReminderSettings(), StatusSettings()

    assert choose(at(30), events, for_event).event.title == "Maybe"


def test_screen_key_follows_the_response():
    # The look depends on it, so an answer changed from the phone redraws the screen.
    s = settings()
    accepted = [answered("Sync", 0, 60, "accepted")]
    maybe = [answered("Sync", 0, 60, "tentative")]
    assert choose(at(5), accepted, s).key != choose(at(5), maybe, s).key
    assert choose(at(5), maybe, s).key == choose(at(6), maybe, s).key


def test_tentative_beats_unanswered_and_ties_fall_back_to_newest():
    events = [answered("No reply", 0, 60, "needs-action"), answered("Maybe", 10, 30, "tentative")]
    assert choose(at(20), events, settings()).event.title == "Maybe"
    events = [answered("Maybe A", 0, 60, "tentative"), answered("Maybe B", 30, 30, "tentative")]
    assert choose(at(40), events, settings()).event.title == "Maybe B"


def test_tentative_events_can_stay_off_the_busy_status():
    events = [answered("Maybe", 10, 30, "tentative"), answered("Sure", 60, 30, "accepted")]
    quiet = settings(status=StatusSettings(tentative=False))
    # The reminder still comes, the status does not; accepted events are unaffected.
    assert choose(at(6), events, quiet).kind == "reminder"
    assert choose(at(20), events, quiet) is None
    assert choose(at(70), events, quiet).kind == "status"
    # By default a tentative event shows its status like any other.
    assert choose(at(20), events, settings()).kind == "status"


def test_upcoming_finds_the_next_screen_change():
    events = [event("Sync", 10), event("Later", 120)]
    s = settings(ReminderSettings(minutes_before=5))
    assert upcoming(at(0), events, s)[0] == at(5)  # Sync's reminder opens
    assert upcoming(at(0), events, s)[1].kind == "reminder"
    moment, screen = upcoming(at(6), events, s)
    assert (moment, screen.kind, screen.event.title) == (at(10), "status", "Sync")
    moment, screen = upcoming(at(20), events, s)  # Sync ends at 40: Later's reminder at 115
    assert (moment, screen.event.title) == (at(115), "Later")
    assert upcoming(at(200), events, s) is None
