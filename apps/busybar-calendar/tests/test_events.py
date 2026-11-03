from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from busybar_calendar.events import CalendarRef, parse

REF = CalendarRef("src", "cal", 0)
KYIV = ZoneInfo("Europe/Kyiv")
WINDOW = (datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC))


def ics(*events: str) -> str:
    body = "\n".join(f"BEGIN:VEVENT\n{e.strip()}\nEND:VEVENT" for e in events)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\n{body}\nEND:VCALENDAR\n".replace(
        "\n", "\r\n"
    )


def run(data, **kwargs):
    kwargs.setdefault("floating_tz", KYIV)
    return parse(data, REF, *WINDOW, **kwargs)


def test_timed_event_in_utc():
    [event] = run(ics("UID:a\nSUMMARY:Standup\nDTSTART:20261013T090000Z\nDTEND:20261013T091500Z"))
    assert event.title == "Standup"
    assert event.start == datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 10, 13, 9, 15, tzinfo=UTC)


def test_tzid_and_floating_times():
    data = ics(
        "UID:a\nSUMMARY:Zoned\nDTSTART;TZID=America/New_York:20261013T090000\n"
        "DTEND;TZID=America/New_York:20261013T100000",
        "UID:b\nSUMMARY:Floating\nDTSTART:20261013T090000\nDTEND:20261013T100000",
    )
    zoned, floating = sorted(run(data), key=lambda e: e.title, reverse=True)
    assert zoned.start == datetime(2026, 10, 13, 13, 0, tzinfo=UTC)
    # Kyiv is UTC+3 in October.
    assert floating.start == datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


def test_duration_and_no_end():
    data = ics(
        "UID:a\nSUMMARY:Dur\nDTSTART:20261013T090000Z\nDURATION:PT45M",
        "UID:b\nSUMMARY:Point\nDTSTART:20261013T120000Z",
    )
    by_title = {e.title: e for e in run(data)}
    assert (by_title["Dur"].end - by_title["Dur"].start).total_seconds() == 45 * 60
    assert by_title["Point"].end == by_title["Point"].start


def test_skips_like_google_and_more():
    data = ics(
        "UID:allday\nSUMMARY:Holiday\nDTSTART;VALUE=DATE:20261014\nDTEND;VALUE=DATE:20261015",
        "UID:long\nSUMMARY:Offsite\nDTSTART:20261014T080000Z\nDTEND:20261015T080000Z",
        "UID:cancel\nSUMMARY:Gone\nSTATUS:CANCELLED\nDTSTART:20261014T090000Z\n"
        "DTEND:20261014T100000Z",
        "UID:free\nSUMMARY:Lunch\nTRANSP:TRANSPARENT\nDTSTART:20261014T110000Z\n"
        "DTEND:20261014T120000Z",
        "UID:declined\nSUMMARY:Nope\nDTSTART:20261014T130000Z\nDTEND:20261014T140000Z\n"
        "ATTENDEE;PARTSTAT=DECLINED:mailto:Me@Example.com\n"
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com",
        "UID:keep\nSUMMARY:Kept\nDTSTART:20261014T150000Z\nDTEND:20261014T160000Z\n"
        "ATTENDEE;PARTSTAT=DECLINED:mailto:other@example.com",
    )
    titles = [e.title for e in run(data, emails=["me@example.com"])]
    assert titles == ["Kept"]
    # Free events are shown when asked for.
    titles = [e.title for e in run(data, emails=["me@example.com"], skip_free=False)]
    assert titles == ["Lunch", "Kept"]


def test_recurrence_with_exdate_and_override():
    data = ics(
        "UID:r\nSUMMARY:Daily\nDTSTART:20261012T080000Z\nDTEND:20261012T081500Z\n"
        "RRULE:FREQ=DAILY;COUNT=5\nEXDATE:20261014T080000Z",
        "UID:r\nRECURRENCE-ID:20261015T080000Z\nSUMMARY:Daily (moved)\n"
        "DTSTART:20261015T100000Z\nDTEND:20261015T101500Z",
    )
    events = run(data)
    assert [(e.title, e.start.day, e.start.hour) for e in events] == [
        ("Daily", 12, 8),
        ("Daily", 13, 8),
        ("Daily (moved)", 15, 10),
        ("Daily", 16, 8),
    ]
    assert len({e.key for e in events}) == 4


def test_several_documents_and_a_broken_one():
    good = ics("UID:a\nSUMMARY:One\nDTSTART:20261013T090000Z\nDTEND:20261013T100000Z")
    other = ics("UID:b\nSUMMARY:Two\nDTSTART:20261013T080000Z\nDTEND:20261013T083000Z")
    events = run([good, "not a calendar", other])
    assert [e.title for e in events] == ["Two", "One"]


def test_untitled_and_whitespace():
    data = ics(
        "UID:a\nDTSTART:20261013T090000Z\nDTEND:20261013T100000Z",
        "UID:b\nSUMMARY:  Sprint\\n  review \nDTSTART:20261013T110000Z\nDTEND:20261013T120000Z",
    )
    assert [e.title for e in run(data)] == ["Busy", "Sprint review"]


ME = ["me@example.com"]


def responses(*attendee_lines, organizer=None, emails=ME):
    extra = "\n".join(attendee_lines)
    if organizer:
        extra += f"\nORGANIZER:mailto:{organizer}"
    data = ics(f"UID:a\nSUMMARY:X\nDTSTART:20261013T090000Z\nDTEND:20261013T100000Z\n{extra}")
    events = run(data, emails=emails)
    return events[0].response if events else None


def test_response_follows_my_attendee_entry():
    other = "ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com"
    assert responses(other, "ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@example.com") == "accepted"
    assert responses(other, "ATTENDEE;PARTSTAT=TENTATIVE:mailto:ME@example.com") == "tentative"
    assert responses(other, "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:me@example.com") == (
        "needs-action"
    )
    # RFC 5545: no PARTSTAT means no answer yet.
    assert responses(other, "ATTENDEE:mailto:me@example.com") == "needs-action"
    assert responses(other, "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com") is None
    assert responses(other, "ATTENDEE;PARTSTAT=DELEGATED:mailto:me@example.com") is None


def test_my_own_entry_wins_even_when_i_organise():
    mine = "ATTENDEE;PARTSTAT=TENTATIVE:mailto:me@example.com"
    assert responses(mine, organizer="me@example.com") == "tentative"


def test_response_without_my_entry():
    other = "ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com"
    assert responses() == "accepted"  # no attendees: an event of my own
    assert responses(other, organizer="me@example.com") == "accepted"
    assert responses(other, organizer="boss@example.com") == "needs-action"  # via a group
    assert responses(other, emails=[]) == "accepted"  # no address to tell by


# Shapes of the published feeds the docs promise to read.
OUTLOOK_FEED = """BEGIN:VCALENDAR
METHOD:PUBLISH
PRODID:Microsoft Exchange Server 2010
VERSION:2.0
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:040000008200E00074C5B7101A82E008
SUMMARY:Weekly planning
DTSTART;TZID=W. Europe Standard Time:20261013T100000
DTEND;TZID=W. Europe Standard Time:20261013T110000
RRULE:FREQ=WEEKLY;BYDAY=TU
END:VEVENT
END:VCALENDAR
"""

GOOGLE_FEED = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
X-WR-TIMEZONE:Europe/Kyiv
BEGIN:VEVENT
DTSTART:20261013T060000Z
DTEND:20261013T063000Z
UID:abc123@google.com
SUMMARY:Standup
STATUS:CONFIRMED
TRANSP:OPAQUE
END:VEVENT
END:VCALENDAR
"""


def test_outlook_windows_timezones_across_the_dst_change():
    data = OUTLOOK_FEED.replace("\n", "\r\n")
    window = (datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 11, 4, tzinfo=UTC))
    # The fallback zone is wrong on purpose: the feed's own VTIMEZONE must win.
    events = parse(data, REF, *window, floating_tz=ZoneInfo("America/New_York"))
    berlin = ZoneInfo("Europe/Berlin")
    assert [e.start.astimezone(berlin).strftime("%d %H:%M") for e in events] == [
        "13 10:00",
        "20 10:00",
        "27 10:00",
        "03 10:00",  # 10:00 local, before and after DST
    ]
    assert [e.start.hour for e in events] == [8, 8, 9, 9]  # in UTC


def test_google_secret_address_feed():
    [event] = run(GOOGLE_FEED.replace("\n", "\r\n"))
    assert (event.title, event.start) == ("Standup", datetime(2026, 10, 13, 6, 0, tzinfo=UTC))


def test_tentative_marking_counts_when_there_is_no_attendee_entry():
    outlook = "X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE"
    status = "STATUS:TENTATIVE"
    busy = "X-MICROSOFT-CDO-BUSYSTATUS:BUSY"
    assert responses(outlook) == "tentative"  # a published Outlook feed
    assert responses(status) == "tentative"
    assert responses(busy) == "accepted"
    assert responses(outlook, emails=[]) == "tentative"  # no address of mine to look for
    # My own attendee entry still decides when there is one.
    mine = "ATTENDEE;PARTSTAT=ACCEPTED:mailto:me@example.com"
    assert responses(mine, status) == "accepted"
