"""Unit tests for GCal Glance application (Aero Horizon)."""

import json
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

import app
from app import (
    APP,
    GRID_WIDTH,
    AlertState,
    CalendarEvent,
    DisplayElement,
    GlyphType,
    _expand_rrule,
    _parse_ical_datetime,
    _unescape_ical,
    _unfold_ical,
    _update_events,
    clean_location,
    draw,
    evaluate_display,
    evaluate_display_with_hardware,
    fetch_events,
    fmt_12h,
    fmt_compact_12h,
    fmt_countdown,
    fmt_remaining,
    format_idle_badge,
    format_idle_marquee,
    make_glyph_elements,
    render_aero_active,
    render_aero_alert,
    render_aero_breather,
    resolve_glyph_tint_color,
    resolve_glyph_type,
    resolve_label,
    sanitize_display_text,
)


def make_el_map(elements: list[DisplayElement]) -> dict[str, dict[str, Any]]:
    return {str(el["id"]): dict(el) for el in elements}


@pytest.fixture(autouse=True)
def reset_module_state() -> Generator[None, None, None]:
    """Reset module-level mutable state before and after each test."""
    app.fired_checkpoints.clear()
    app.active_alert = None
    _update_events([])
    yield
    app.fired_checkpoints.clear()
    app.active_alert = None
    _update_events([])


# --- 1. iCal Parsing & Timezone Tests ---------------------------------------


def test_parse_ical_datetime_utc() -> None:
    dt = _parse_ical_datetime("20260815T140000Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2026 and dt.month == 8 and dt.day == 15
    assert dt.hour == 14 and dt.minute == 0 and dt.second == 0


def test_parse_ical_datetime_tzid() -> None:
    # EDT is UTC-4 during August
    dt = _parse_ical_datetime("20260815T100000", "TZID=America/New_York")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 14 and dt.minute == 0


def test_parse_ical_datetime_allday_events(monkeypatch: pytest.MonkeyPatch) -> None:
    # By default (ignore_all_day=False), all-day events are parsed into a valid datetime
    dt = _parse_ical_datetime("20260815", "VALUE=DATE")
    assert dt is not None

    # When ignore_all_day=True, all-day events return None
    monkeypatch.setattr(app.get_config(), "ignore_all_day", True)
    assert _parse_ical_datetime("20260815", "VALUE=DATE") is None
    assert _parse_ical_datetime("20260815") is None


def test_unfold_and_unescape_ical() -> None:
    raw = (
        "SUMMARY:Line 1\r\n  continued\r\n"
        "DESCRIPTION:Escaped\\, chars\\; and\\nnewline\r\n"
        "LOCATION:Room\\, 3B"
    )
    unfolded = _unfold_ical(raw)
    assert len(unfolded) == 3
    assert unfolded[0] == "SUMMARY:Line 1 continued"
    assert _unescape_ical(unfolded[1]) == "DESCRIPTION:Escaped, chars; and\nnewline"
    assert _unescape_ical(unfolded[2]) == "LOCATION:Room, 3B"


# --- 2. HTTP Interaction Tests (using mock_busy_bar_api fixture) -------------

SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:event-1@google.com
SUMMARY:Sprint Standup
LOCATION:Room 2B
DTSTART:20300815T140000Z
DTEND:20300815T143000Z
DESCRIPTION:Daily sync
END:VEVENT
BEGIN:VEVENT
UID:event-2@google.com
SUMMARY:[dnd] Deep Focus Block
LOCATION:Desk
DTSTART:20300815T150000Z
DTEND:20300815T160000Z
END:VEVENT
END:VCALENDAR
"""


@pytest.mark.asyncio
async def test_fetch_events_success(mock_busy_bar_api: respx.MockRouter) -> None:
    ical_url = "https://calendar.google.com/calendar/ical/feed/basic.ics"
    mock_busy_bar_api.get(ical_url).respond(status_code=200, text=SAMPLE_ICS)

    async with httpx.AsyncClient() as client:
        events = await fetch_events(client, ical_url)

    assert events is not None
    assert len(events) == 2
    assert events[0].uid == "event-1@google.com"
    assert events[0].summary == "Sprint Standup"
    assert events[0].location == "Room 2B"
    assert events[1].uid == "event-2@google.com"
    assert events[1].summary == "[dnd] Deep Focus Block"
    assert events[1].location == "Desk"


@pytest.mark.asyncio
async def test_fetch_events_recurring_rrule_upcoming(
    mock_busy_bar_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recurring event with past DTSTART should produce upcoming occurrences."""
    # Freeze datetime to 2026-08-21 14:00:00 UTC (10:00 AM EDT)
    fixed_now = datetime(2026, 8, 21, 14, 0, 0, tzinfo=timezone.utc)

    # 1. Weekly recurring Friday 18:00 UTC (2:00 PM EDT) started in Jan 2026
    # 2. Future single event on Saturday Aug 22, 2026
    recurring_ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:recurring-weekly@google.com
SUMMARY:Weekly Team Sync
LOCATION:Meet
DTSTART:20260102T180000Z
DTEND:20260102T183000Z
RRULE:FREQ=WEEKLY;BYDAY=FR
END:VEVENT
BEGIN:VEVENT
UID:future-single@google.com
SUMMARY:Saturday Workshop
LOCATION:Office
DTSTART:20260822T130000Z
DTEND:20260822T140000Z
END:VEVENT
END:VCALENDAR
"""
    ical_url = "https://calendar.google.com/calendar/ical/feed/basic.ics"
    mock_busy_bar_api.get(ical_url).respond(status_code=200, text=recurring_ics)

    # Monkeypatch datetime.now in app module
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            if tz is not None:
                return fixed_now.astimezone(tz)
            return fixed_now

    monkeypatch.setattr(app, "datetime", FixedDateTime)

    async with httpx.AsyncClient() as client:
        events = await fetch_events(client, ical_url)

    assert events is not None
    # The immediate next event MUST be the occurrence on 2026-08-21 18:00 UTC
    assert len(events) >= 2
    assert events[0].summary == "Weekly Team Sync"
    assert events[0].start == datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)
    assert events[0].end == datetime(2026, 8, 21, 18, 30, 0, tzinfo=timezone.utc)
    assert events[1].summary == "Saturday Workshop"


@pytest.mark.asyncio
async def test_fetch_events_recurring_exdate_and_recurrence_id(
    mock_busy_bar_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXDATE skips cancelled occurrences; RECURRENCE-ID overrides instances."""
    fixed_now = datetime(2026, 8, 21, 14, 0, 0, tzinfo=timezone.utc)

    # Weekly Friday event at 18:00 UTC.
    # Aug 21 instance is overridden with RECURRENCE-ID and moved to 19:00 UTC.
    # Aug 28 instance is excluded via EXDATE.
    ics_content = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:recurring-base@google.com
SUMMARY:Friday Coffee
DTSTART:20260102T180000Z
DTEND:20260102T183000Z
RRULE:FREQ=WEEKLY;BYDAY=FR
EXDATE:20260828T180000Z
END:VEVENT
BEGIN:VEVENT
UID:recurring-base@google.com
RECURRENCE-ID:20260821T180000Z
SUMMARY:Friday Coffee (Rescheduled)
DTSTART:20260821T190000Z
DTEND:20260821T193000Z
END:VEVENT
BEGIN:VEVENT
UID:cancelled-event@google.com
SUMMARY:Cancelled Meeting
STATUS:CANCELLED
DTSTART:20260821T200000Z
DTEND:20260821T210000Z
END:VEVENT
END:VCALENDAR
"""
    ical_url = "https://calendar.google.com/calendar/ical/feed/basic.ics"
    mock_busy_bar_api.get(ical_url).respond(status_code=200, text=ics_content)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            if tz is not None:
                return fixed_now.astimezone(tz)
            return fixed_now

    monkeypatch.setattr(app, "datetime", FixedDateTime)

    async with httpx.AsyncClient() as client:
        events = await fetch_events(client, ical_url)

    assert events is not None
    # Cancelled event must not appear
    assert not any(e.summary == "Cancelled Meeting" for e in events)
    # Aug 21 occurrence is overridden to 19:00
    assert events[0].summary == "Friday Coffee (Rescheduled)"
    assert events[0].start == datetime(2026, 8, 21, 19, 0, 0, tzinfo=timezone.utc)
    # Aug 28 occurrence was in EXDATE, so next base occurrence is Sep 04
    assert events[1].summary == "Friday Coffee"
    assert events[1].start == datetime(2026, 9, 4, 18, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_fetch_events_monthly_rrule(
    mock_busy_bar_api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monthly BYDAY recurrence (e.g. 3rd Friday of month)."""
    fixed_now = datetime(2026, 8, 21, 14, 0, 0, tzinfo=timezone.utc)

    # 3rd Friday of every month at 14:00 America/New_York (18:00 UTC)
    # Aug 21, 2026 is 3rd Friday of August 2026
    monthly_ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:monthly-3fr@google.com
SUMMARY:Bill + Gabriel
DTSTART;TZID=America/New_York:20251121T140000
DTEND;TZID=America/New_York:20251121T144000
RRULE:FREQ=MONTHLY;BYDAY=3FR
END:VEVENT
END:VCALENDAR
"""
    ical_url = "https://calendar.google.com/calendar/ical/feed/basic.ics"
    mock_busy_bar_api.get(ical_url).respond(status_code=200, text=monthly_ics)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            if tz is not None:
                return fixed_now.astimezone(tz)
            return fixed_now

    monkeypatch.setattr(app, "datetime", FixedDateTime)

    async with httpx.AsyncClient() as client:
        events = await fetch_events(client, ical_url)

    assert events is not None
    assert len(events) >= 1
    assert events[0].summary == "Bill + Gabriel"
    assert events[0].start == datetime(2026, 8, 21, 18, 0, 0, tzinfo=timezone.utc)
    assert events[0].end == datetime(2026, 8, 21, 18, 40, 0, tzinfo=timezone.utc)


def test_expand_rrule_differential_validation_against_dateutil() -> None:
    """Validate stdlib _expand_rrule directly against dateutil.rrule across rules."""
    import re
    from zoneinfo import ZoneInfo

    from dateutil import rrule

    def normalize_for_dateutil(rrule_str: str) -> str:
        rrule_str = re.sub(r"UNTIL=(\d{8})(?!T)", r"UNTIL=\1T235959Z", rrule_str)
        rrule_str = re.sub(r"UNTIL=(\d{8}T\d{6})(?!Z)", r"UNTIL=\1Z", rrule_str)
        return rrule_str

    tz_ny = ZoneInfo("America/New_York")
    test_cases = [
        (datetime(2025, 1, 1, 9, 0, tzinfo=tz_ny), "FREQ=DAILY;INTERVAL=3"),
        (
            datetime(2025, 1, 1, 10, 0, tzinfo=tz_ny),
            "FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH",
        ),
        (
            datetime(2025, 1, 1, 11, 0, tzinfo=tz_ny),
            "FREQ=WEEKLY;COUNT=10;BYDAY=MO,WE,FR",
        ),
        (datetime(2025, 11, 21, 14, 0, tzinfo=tz_ny), "FREQ=MONTHLY;BYDAY=3FR"),
        (datetime(2025, 1, 15, 16, 30, tzinfo=tz_ny), "FREQ=MONTHLY;BYMONTHDAY=15"),
        (datetime(2024, 8, 15, 12, 0, tzinfo=tz_ny), "FREQ=YEARLY"),
    ]

    window_start = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    window_end = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

    for dtstart, rule_str in test_cases:
        stdlib_res = _expand_rrule(dtstart, rule_str, window_start, window_end)

        norm_rule = normalize_for_dateutil(rule_str)
        du_rule = rrule.rrulestr(norm_rule, dtstart=dtstart)
        local_tz = dtstart.tzinfo or timezone.utc
        local_win_start = window_start.astimezone(local_tz)
        local_win_end = window_end.astimezone(local_tz)
        du_res = [
            occ.astimezone(timezone.utc)
            for occ in du_rule.between(local_win_start, local_win_end, inc=True)
        ]

        assert stdlib_res == du_res, f"Mismatch for {rule_str}"


@pytest.mark.asyncio
async def test_fetch_events_http_error_returns_none(
    mock_busy_bar_api: respx.MockRouter,
) -> None:
    ical_url = "https://calendar.google.com/calendar/ical/feed/basic.ics"
    mock_busy_bar_api.get(ical_url).respond(
        status_code=500, text="Internal Server Error"
    )

    async with httpx.AsyncClient() as client:
        events = await fetch_events(client, ical_url)

    assert events is None


@pytest.mark.asyncio
async def test_fetch_events_network_failure_returns_none(
    mock_busy_bar_api: respx.MockRouter,
) -> None:
    ical_url = "https://calendar.google.com/calendar/ical/feed/basic.ics"
    mock_busy_bar_api.get(ical_url).mock(
        side_effect=httpx.ConnectError("Connection refused")
    )

    async with httpx.AsyncClient() as client:
        events = await fetch_events(client, ical_url)

    assert events is None


@pytest.mark.asyncio
async def test_draw_dispatches_proper_payload(
    mock_busy_bar_api: respx.MockRouter,
) -> None:
    draw_route = mock_busy_bar_api.post("/api/display/draw").respond(
        status_code=200, json={"status": "ok"}
    )

    elements: list[DisplayElement] = [
        {"id": "clock_txt", "type": "text", "text": "10:48 AM"}
    ]
    async with httpx.AsyncClient() as client:
        await draw(client, elements, priority=45, led_notification_color="#FBBC05FF")

    assert draw_route.called
    req = draw_route.calls.last.request
    payload = json.loads(req.content)
    assert payload["application_name"] == APP
    assert payload["elements"] == elements
    assert payload["priority"] == 45
    assert payload["led_notification_color"] == "#FBBC05FF"


@pytest.mark.asyncio
async def test_draw_raises_on_http_error(mock_busy_bar_api: respx.MockRouter) -> None:
    mock_busy_bar_api.post("/api/display/draw").respond(
        status_code=409, text="Display busy"
    )

    elements: list[DisplayElement] = [
        {"id": "clock_txt", "type": "text", "text": "Conflict test"}
    ]
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await draw(client, elements)
        assert exc_info.value.response.status_code == 409


# --- 3. Aero Horizon Invariant Element Map & Procedural Glyphs --------------


REQUIRED_INVARIANT_IDS = {
    "clock_txt",
    "badge_bg",
    "badge_txt",
    "mid_txt",
    "tele_txt",
    "glyph_0",
    "glyph_1",
    "glyph_2",
    "title_txt",
    "radar_bg",
    "tick_25",
    "tick_50",
    "tick_75",
    "bar_fill",
    "playhead",
    "tight_pip",
}


def test_invariant_element_ids_present_across_all_states() -> None:
    now = datetime.now(timezone.utc)
    future_evt = CalendarEvent(
        "e1", "Design Review", now + timedelta(minutes=40), now + timedelta(minutes=70)
    )
    active_evt = CalendarEvent(
        "e2", "Sprint Standup", now - timedelta(minutes=10), now + timedelta(minutes=20)
    )
    alert = AlertState(future_evt, 5, now + timedelta(seconds=15))

    states = [
        evaluate_display(now, []),
        evaluate_display(now, [future_evt]),
        evaluate_display(now, [active_evt]),
        render_aero_alert(now, alert, [future_evt])[0],
        render_aero_breather(now, future_evt)[0],
        render_aero_active(now, active_evt, [future_evt], alert)[0],
    ]

    for state_elements in states:
        element_ids = {el["id"] for el in state_elements}
        for req_id in REQUIRED_INVARIANT_IDS:
            assert req_id in element_ids, f"Missing required element ID {req_id}"


def test_procedural_micro_glyphs() -> None:
    glyph_types: list[GlyphType] = [
        "video",
        "coffee",
        "focus",
        "travel",
        "fitness",
        "celebrate",
        "overtime",
        "calendar",
    ]
    for gtype in glyph_types:
        elems = make_glyph_elements(gtype, "#4285F4FF")
        assert len(elems) == 3
        assert [e["id"] for e in elems] == ["glyph_0", "glyph_1", "glyph_2"]
        assert all(e["type"] == "rectangle" for e in elems)


def test_resolve_glyph_type() -> None:
    e_video = CalendarEvent(
        "1", "Team Zoom Sync", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_video) == "video"

    e_coffee = CalendarEvent(
        "2",
        "Quick Coffee & Catchup",
        datetime.now(timezone.utc),
        datetime.now(timezone.utc),
    )
    assert resolve_glyph_type(e_coffee) == "coffee"

    e_focus = CalendarEvent(
        "3",
        "Deep Work [focus]",
        datetime.now(timezone.utc),
        datetime.now(timezone.utc),
    )
    assert resolve_glyph_type(e_focus) == "focus"

    e_overtime = CalendarEvent(
        "4", "Late Sync", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_overtime, is_overtime=True) == "overtime"

    e_cal = CalendarEvent(
        "5", "General Task", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_cal) == "calendar"

    # UTF-8 Emoji detection
    e_bus = CalendarEvent(
        "6", "🚌 Travel", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_bus) == "travel"

    e_plane = CalendarEvent(
        "7", "Flight to ORD ✈️", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_plane) == "travel"

    e_run = CalendarEvent(
        "8", "Morning Run 🏃", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_run) == "fitness"

    e_party = CalendarEvent(
        "9", "Team Party 🎉", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_party) == "celebrate"

    e_chill = CalendarEvent(
        "10", "😎 Decompress", datetime.now(timezone.utc), datetime.now(timezone.utc)
    )
    assert resolve_glyph_type(e_chill) == "coffee"


# --- 4. Aero Horizon FSM & Lifecycle Tests -----------------------------------


def test_fsm_idle_state_no_events() -> None:
    now = datetime.now(timezone.utc)
    elements, extra = evaluate_display_with_hardware(now, [])

    el_map = make_el_map(elements)
    assert el_map["mid_txt"]["text"] == "Schedule Clear"
    assert el_map["badge_txt"]["text"] == "FREE"
    assert el_map["title_txt"]["text"] == "Schedule clear"
    assert el_map["radar_bg"]["fill_colors"] == ["#141414FF"]
    assert extra["priority"] == 30


def test_fsm_idle_state_with_upcoming_events() -> None:
    now = datetime.now(timezone.utc)
    evt1 = CalendarEvent(
        "e1",
        "Sprint Planning",
        now + timedelta(minutes=42),
        now + timedelta(minutes=72),
        location="Room 3B",
    )
    evt2 = CalendarEvent(
        "e2", "1-on-1", now + timedelta(minutes=100), now + timedelta(minutes=130)
    )

    elements, _ = evaluate_display_with_hardware(now, [evt1, evt2])
    el_map = make_el_map(elements)

    assert el_map["mid_txt"]["text"] == "42m Focus Runway"
    assert "NEXT" in el_map["badge_txt"]["text"]
    assert "Sprint Planning" in el_map["title_txt"]["text"]
    assert "Room 3B" in el_map["title_txt"]["text"]
    assert "[THEN" in el_map["title_txt"]["text"]
    assert "1-on-1" in el_map["title_txt"]["text"]
    assert "queued" not in el_map["title_txt"]["text"].lower()

    # Check 3-hour radar meeting block rendering
    radar_blk_0 = el_map.get("radar_blk_0")
    assert radar_blk_0 is not None
    assert radar_blk_0["width"] > 0
    assert radar_blk_0["x"] > 0


def test_fsm_checkpoint_alert_sequence() -> None:
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        "e1",
        "Architecture Review",
        now + timedelta(minutes=4),
        now + timedelta(minutes=34),
        location="Room 2A",
    )

    # T-4m triggers the 5 MIN checkpoint banner
    res_5m, meta_5m = evaluate_display_with_hardware(now, [evt])
    el_5m = make_el_map(res_5m)
    assert el_5m["badge_txt"]["text"] == "IN 5 MIN"
    assert "Architecture Review" in el_5m["title_txt"]["text"]
    assert meta_5m["priority"] == 45
    assert meta_5m["led_notification_color"] == "#FBBC05FF"

    # Active alert banner persists on next tick
    res_persist, _ = evaluate_display_with_hardware(now + timedelta(seconds=5), [evt])
    el_persist = make_el_map(res_persist)
    assert el_persist["badge_txt"]["text"] == "IN 5 MIN"

    # After 15s expiration, returns to idle focus runway
    res_idle, meta_idle = evaluate_display_with_hardware(
        now + timedelta(seconds=20), [evt]
    )
    el_idle = make_el_map(res_idle)
    assert el_idle["badge_txt"]["text"] != "IN 5 MIN"
    assert "Focus Runway" in el_idle["mid_txt"]["text"]
    assert meta_idle["priority"] == 30

    # T-2m checkpoint
    res_2m, _ = evaluate_display_with_hardware(now + timedelta(minutes=2), [evt])
    el_2m = make_el_map(res_2m)
    assert el_2m["badge_txt"]["text"] == "IN 2 MIN"

    # At start time: enters Active Meeting Phase 1 Title Verification
    res_active_p1, _ = evaluate_display_with_hardware(
        now + timedelta(minutes=4, seconds=5), [evt]
    )
    el_active_p1 = make_el_map(res_active_p1)
    assert el_active_p1["badge_txt"]["text"] == "LIVE CALL"


def test_fsm_active_phase_1_verification() -> None:
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        "e1",
        "Quarterly Planning",
        now - timedelta(seconds=10),
        now + timedelta(minutes=30),
        location="Boardroom",
    )

    elements, extra = evaluate_display_with_hardware(now, [evt])
    el_map = make_el_map(elements)

    assert el_map["badge_txt"]["text"] == "LIVE CALL"
    assert "Boardroom" in el_map["title_txt"]["text"]
    assert el_map["tele_txt"]["color"] == "#EA4335FF"
    assert extra["priority"] == 60
    assert extra["led_notification_color"] == "#EA4335FF"


def test_fsm_active_phase_2_flight_deck() -> None:
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        "e1",
        "Quarterly Planning",
        now - timedelta(minutes=15),
        now + timedelta(minutes=15),
    )
    next_evt = CalendarEvent(
        "e2",
        "1-on-1 Catchup",
        now + timedelta(minutes=30),
        now + timedelta(minutes=60),
    )

    elements, _ = evaluate_display_with_hardware(now, [evt, next_evt])
    el_map = make_el_map(elements)

    assert el_map["badge_txt"]["text"] == "LIVE"
    assert "15m left" in el_map["tele_txt"]["text"]
    assert "[NEXT" in el_map["title_txt"]["text"]
    assert "1-on-1 Catchup" in el_map["title_txt"]["text"]

    # Precision Horizon Milestone Ticks & Playhead Needle
    assert el_map["tick_25"]["x"] == 18
    assert el_map["tick_50"]["x"] == 36
    assert el_map["tick_75"]["x"] == 54
    assert el_map["playhead"]["fill_colors"] == ["#FFFFFFFF"]
    assert el_map["playhead"]["x"] == 36  # 50% elapsed of 30m call = 36px


def test_fsm_active_phase_3_wrapup_and_tight_turn() -> None:
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        "e1",
        "Design Review",
        now - timedelta(minutes=28),
        now + timedelta(minutes=2),  # 2 minutes remaining -> Wrap-up
    )
    # Next event starts in 6 minutes -> 4m gap (<5m -> Tight Turn)
    tight_next = CalendarEvent(
        "e2",
        "Executive Sync",
        now + timedelta(minutes=6),
        now + timedelta(minutes=36),
    )
    # Mark earlier 15m checkpoint as already fired
    app.fired_checkpoints.add(("e2", 15))

    elements, extra = evaluate_display_with_hardware(now, [evt, tight_next])
    el_map = make_el_map(elements)

    assert el_map["badge_txt"]["text"] == "WRAP UP"
    assert "rem" in el_map["tele_txt"]["text"]
    assert "[TIGHT TURN]" in el_map["title_txt"]["text"]
    assert el_map["tight_pip"]["x"] == 70
    assert el_map["tight_pip"]["fill_colors"] == ["#FBBC05FF"]
    assert extra["priority"] == 60
    assert extra["led_notification_color"] == "#FBBC05FF"


def test_fsm_active_overtime_phase() -> None:
    now = datetime.now(timezone.utc)
    evt = CalendarEvent(
        "e1",
        "Sprint Standup",
        now - timedelta(minutes=34),
        now - timedelta(minutes=4),  # 4 minutes overdue
    )

    elements, extra = evaluate_display_with_hardware(now, [evt])
    el_map = make_el_map(elements)

    assert el_map["badge_txt"]["text"] == "+04m OVER"
    assert "End:" in el_map["tele_txt"]["text"]
    assert "Overrun" in el_map["title_txt"]["text"]
    assert el_map["bar_fill"]["width"] == GRID_WIDTH
    assert extra["priority"] == 60
    assert extra["led_notification_color"] == "#EA4335FF"


def test_fsm_interrupted_active_state_preserves_horizon() -> None:
    now = datetime.now(timezone.utc)
    current_meeting = CalendarEvent(
        "m1",
        "Current Strategy Meeting",
        now - timedelta(minutes=15),
        now + timedelta(minutes=15),
    )
    next_meeting = CalendarEvent(
        "m2",
        "Next Architecture Sync",
        now + timedelta(minutes=4),
        now + timedelta(minutes=34),
    )

    elements, _ = evaluate_display_with_hardware(now, [current_meeting, next_meeting])
    el_map = make_el_map(elements)

    # Top banner shows the upcoming checkpoint alert
    assert el_map["badge_txt"]["text"] == "IN 5 MIN"
    assert "[NEXT] Next Architecture" in el_map["title_txt"]["text"]
    assert "on call" in el_map["tele_txt"]["text"]

    # Bottom horizon bar PRESERVES current meeting's 50% progress!
    assert el_map["bar_fill"]["width"] == 36
    assert el_map["bar_fill"]["fill_colors"] == ["#EA4335FF"]
    assert el_map["playhead"]["x"] == 36


def test_render_aero_breather() -> None:
    now = datetime.now(timezone.utc)
    next_meeting = CalendarEvent(
        "m1", "Design Review", now + timedelta(minutes=15), now + timedelta(minutes=45)
    )

    elements, extra = render_aero_breather(now, next_meeting)
    el_map = make_el_map(elements)

    assert el_map["badge_txt"]["text"] == "15m BREAK"
    assert "Breather Window" in el_map["title_txt"]["text"]
    assert el_map["bar_fill"]["fill_colors"] == ["#34A853FF"]
    assert extra["priority"] == 30
    assert extra["led_notification_color"] == "#34A853FF"


def test_fsm_ended_event_pruning() -> None:
    now = datetime.now(timezone.utc)
    past_event = CalendarEvent(
        "past-1", "Past Sync", now - timedelta(minutes=60), now - timedelta(minutes=30)
    )
    app.fired_checkpoints.add(("past-1", 5))

    evaluate_display(now, [past_event])
    assert ("past-1", 5) not in app.fired_checkpoints


# --- 5. Helper & Formatting Tests -------------------------------------------


def test_resolve_label_precedence() -> None:
    now = datetime.now(timezone.utc)
    e_dnd = CalendarEvent("1", "[dnd] Critical release", now, now + timedelta(hours=1))
    assert resolve_label(e_dnd) == "DO NOT DISTURB"

    e_focus = CalendarEvent(
        "2", "Deep focus block [focus]", now, now + timedelta(hours=1)
    )
    assert resolve_label(e_focus) == "DEEP WORK"

    e_break = CalendarEvent(
        "3", "Team Coffee", now, now + timedelta(hours=1), color="#34A853"
    )
    assert resolve_label(e_break) == "ON BREAK"

    e_generic = CalendarEvent("4", "Regular Sync", now, now + timedelta(hours=1))
    assert resolve_label(e_generic) == "IN MEETING"


def test_fmt_12h_format() -> None:
    dt = datetime(2026, 8, 15, 14, 5, tzinfo=timezone.utc)
    res = fmt_12h(dt, include_ampm=True)
    assert "AM" in res or "PM" in res
    assert fmt_12h(dt, include_ampm=False).endswith(":05")


def test_fmt_countdown() -> None:
    assert fmt_countdown(45) == "00:45"
    assert fmt_countdown(125) == "02:05"
    assert fmt_countdown(3660) == "1h01m"


def test_fmt_remaining() -> None:
    assert fmt_remaining(timedelta(minutes=45)) == "45m"
    assert fmt_remaining(timedelta(minutes=90)) == "1h30m"


def test_clean_location() -> None:
    # Physical rooms
    assert clean_location("Room 3B") == "Room 3B"
    assert clean_location("  Building A, Floor 2  ") == "Building A, Floor 2"
    assert clean_location("") == ""

    # Google Meet URLs
    assert clean_location("https://meet.google.com/abc-defg-hij") == "Google Meet"
    assert (
        clean_location("", description="Join: https://meet.google.com/xyz")
        == "Google Meet"
    )
    assert clean_location("Room 3B - https://meet.google.com/abc") == "Room 3B"

    # Zoom URLs
    assert clean_location("https://us02web.zoom.us/j/123456789?pwd=xyz") == "Zoom"
    assert clean_location("", description="Zoom link: https://zoom.us/j/999") == "Zoom"

    # Teams URLs
    assert (
        clean_location("https://teams.microsoft.com/l/meetup-join/19%3ameeting...")
        == "Teams"
    )

    # Webex & Generic URLs
    assert clean_location("https://company.webex.com/meet/user") == "Webex"
    assert clean_location("https://custom-service.com/room1") == "Video Link"


def test_fmt_compact_12h() -> None:
    now_local = datetime.now().astimezone()

    dt_am = now_local.replace(hour=5, minute=51)
    assert fmt_compact_12h(dt_am) == "5:51A"

    dt_am_zero = now_local.replace(hour=10, minute=0)
    assert fmt_compact_12h(dt_am_zero) == "10A"

    dt_pm = now_local.replace(hour=13, minute=30)
    assert fmt_compact_12h(dt_pm) == "1:30P"

    dt_noon = now_local.replace(hour=12, minute=0)
    assert fmt_compact_12h(dt_noon) == "12P"

    dt_midnight = now_local.replace(hour=0, minute=0)
    assert fmt_compact_12h(dt_midnight) == "12A"


def test_format_idle_badge() -> None:
    now = datetime.now().astimezone()

    # 1. Empty schedule
    assert format_idle_badge(now, []) == "FREE"

    # 2. Next event today
    evt_today = CalendarEvent(
        "1",
        "Standup",
        now.replace(hour=10, minute=30),
        now.replace(hour=11, minute=0),
    )
    assert format_idle_badge(now, [evt_today]) == "NEXT 10:30A"

    # 3. Next event tomorrow
    evt_tomorrow = CalendarEvent(
        "2",
        "Travel",
        (now + timedelta(days=1)).replace(hour=5, minute=51),
        (now + timedelta(days=1)).replace(hour=7, minute=30),
    )
    assert format_idle_badge(now, [evt_tomorrow]) == "TMRW 5:51A"

    # 4. Next event within 7 days (weekday only)
    future_date = now + timedelta(days=3)
    day_name = future_date.strftime("%a").upper()
    evt_future = CalendarEvent(
        "3",
        "All Hands",
        future_date.replace(hour=15, minute=0),
        future_date.replace(hour=16, minute=0),
    )
    assert format_idle_badge(now, [evt_future]) == f"{day_name} 3P"

    # 5. Next event past 7 days (weekday + month/day)
    far_future_date = now + timedelta(days=9)
    far_day_name = far_future_date.strftime("%a").upper()
    evt_far = CalendarEvent(
        "4",
        "Offsite",
        far_future_date.replace(hour=10, minute=0),
        far_future_date.replace(hour=11, minute=0),
    )
    assert (
        format_idle_badge(now, [evt_far])
        == f"{far_day_name} {far_future_date.month}/{far_future_date.day} 10A"
    )


def test_format_idle_marquee() -> None:
    now = datetime.now().astimezone().replace(hour=10, minute=0)

    # 1. Empty schedule
    assert format_idle_marquee(now, [], lookahead_count=6) == "Schedule clear"

    # 2. Single event today (starts with time)
    evt_today = CalendarEvent(
        "1",
        "Standup",
        now.replace(hour=10, minute=30),
        now.replace(hour=11, minute=0),
        location="Room 2B",
    )
    res_today = format_idle_marquee(now, [evt_today], lookahead_count=6)
    assert res_today.startswith("10:30 AM - Standup")
    assert "Room 2B" in res_today
    assert "queued" not in res_today.lower()

    # 3. Multiple events today with lookahead_count = 3
    evt_today_2 = CalendarEvent(
        "2",
        "Design Review",
        now.replace(hour=14, minute=0),
        now.replace(hour=15, minute=0),
        location="https://meet.google.com/abc",
    )
    evt_today_3 = CalendarEvent(
        "3",
        "1-on-1 Sync",
        now.replace(hour=16, minute=0),
        now.replace(hour=16, minute=30),
    )
    res_multi = format_idle_marquee(
        now, [evt_today, evt_today_2, evt_today_3], lookahead_count=3
    )
    assert res_multi.startswith("10:30 AM - Standup - Room 2B")
    assert "[THEN 2:00 PM] Design Review (Google Meet)" in res_multi
    assert "[THEN 4:00 PM] 1-on-1 Sync" in res_multi

    # 4. Next event tomorrow (starts directly with title, NOT 'Tomorrow')
    evt_tomorrow = CalendarEvent(
        "4",
        "Quarterly Sync",
        (now + timedelta(days=1)).replace(hour=9, minute=0),
        (now + timedelta(days=1)).replace(hour=10, minute=0),
        location="Auditorium",
    )
    res_tomorrow = format_idle_marquee(now, [evt_tomorrow], lookahead_count=6)
    assert res_tomorrow.startswith("Quarterly Sync - Auditorium")
    assert "tomorrow" not in res_tomorrow.lower()

    # 5. Multi-day lookahead crossing into subsequent days
    res_cross = format_idle_marquee(now, [evt_tomorrow, evt_today_3], lookahead_count=2)
    assert res_cross.startswith("Quarterly Sync - Auditorium")
    assert "[THEN" in res_cross


def test_resolve_glyph_tint_color() -> None:
    assert resolve_glyph_tint_color("travel") == "#FDD663FF"
    assert resolve_glyph_tint_color("video") == "#AECBFAFF"
    assert resolve_glyph_tint_color("focus") == "#D7AEFBFF"
    assert resolve_glyph_tint_color("coffee") == "#81C995FF"
    assert resolve_glyph_tint_color("fitness") == "#81C995FF"
    assert resolve_glyph_tint_color("celebrate") == "#F28B82FF"
    assert resolve_glyph_tint_color("overtime") == "#F28B82FF"
    assert resolve_glyph_tint_color("calendar") == "#AECBFAFF"


def test_no_queued_string_across_all_lifecycle_states() -> None:
    now = datetime.now(timezone.utc)
    evt1 = CalendarEvent(
        "e1", "Event 1", now - timedelta(minutes=10), now + timedelta(minutes=20)
    )
    evt2 = CalendarEvent(
        "e2", "Event 2", now + timedelta(minutes=30), now + timedelta(minutes=60)
    )
    evt3 = CalendarEvent(
        "e3", "Event 3", now + timedelta(minutes=90), now + timedelta(minutes=120)
    )
    alert = AlertState(evt2, 5, now + timedelta(seconds=15))

    states = [
        evaluate_display(now, []),
        evaluate_display(now, [evt2, evt3]),
        evaluate_display(now, [evt1, evt2, evt3]),
        render_aero_alert(now, alert, [evt2, evt3])[0],
        render_aero_breather(now, evt2)[0],
        render_aero_active(now, evt1, [evt2, evt3], alert)[0],
    ]

    for state_elements in states:
        for elem in state_elements:
            if elem.get("type") == "text":
                txt = elem.get("text", "")
                assert "queued" not in txt.lower(), (
                    f"Found 'queued' in element {elem['id']}: {txt}"
                )


def test_sanitize_display_text() -> None:
    assert sanitize_display_text("🚌 Travel") == "Travel"
    assert sanitize_display_text("😎 Decompress") == "Decompress"
    assert sanitize_display_text("⚡ Tight Turn: Meeting") == "Tight Turn: Meeting"
    assert (
        sanitize_display_text("Sprint Planning \u00b7 Room 3B")
        == "Sprint Planning - Room 3B"
    )
    assert (
        sanitize_display_text("\u201cHello\u201d & \u2018World\u2019\u2026")
        == "\"Hello\" & 'World'..."
    )
    assert sanitize_display_text("") == ""


def test_lookahead_count_validation() -> None:
    """Test that lookahead_count is constrained between 2 and 10."""
    from pydantic import ValidationError

    from app import AppConfig

    # Valid values
    assert AppConfig().lookahead_count == 6
    assert AppConfig(lookahead_count=2).lookahead_count == 2
    assert AppConfig(lookahead_count=6).lookahead_count == 6
    assert AppConfig(lookahead_count=10).lookahead_count == 10

    # Invalid values below 2
    with pytest.raises(ValidationError):
        AppConfig(lookahead_count=1)
    with pytest.raises(ValidationError):
        AppConfig(lookahead_count=0)

    # Invalid values above 10
    with pytest.raises(ValidationError):
        AppConfig(lookahead_count=11)


def test_app_name_and_config_env_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test APP identifier and environment variable alias fallback resolution."""
    from app import APP, AppConfig

    assert APP == "gcal-glance"

    # Primary GCAL_GLANCE_ prefix
    monkeypatch.setenv("GCAL_GLANCE_LOOKAHEAD_COUNT", "8")
    monkeypatch.setenv("GCAL_GLANCE_HOST", "192.168.1.50")
    cfg = AppConfig()
    assert cfg.lookahead_count == 8
    assert cfg.host == "192.168.1.50"

    # Backward compatibility with GCAL_ prefix
    monkeypatch.delenv("GCAL_GLANCE_LOOKAHEAD_COUNT", raising=False)
    monkeypatch.delenv("GCAL_GLANCE_HOST", raising=False)
    monkeypatch.setenv("GCAL_LOOKAHEAD_COUNT", "4")
    monkeypatch.setenv("GCAL_HOST", "10.0.0.99")
    cfg = AppConfig()
    assert cfg.lookahead_count == 4
    assert cfg.host == "10.0.0.99"

    # Backward compatibility with CALSYNC_ prefix
    monkeypatch.delenv("GCAL_LOOKAHEAD_COUNT", raising=False)
    monkeypatch.delenv("GCAL_HOST", raising=False)
    monkeypatch.setenv("CALSYNC_LOOKAHEAD_COUNT", "3")
    monkeypatch.setenv("CALSYNC_HOST", "127.0.0.1")
    cfg = AppConfig()
    assert cfg.lookahead_count == 3
    assert cfg.host == "127.0.0.1"


def test_build_peek_queue() -> None:
    from app import build_peek_queue

    now = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)

    # 1. Empty events
    assert build_peek_queue(now, [], lookahead_count=6) == []

    # 2. Events today + events tomorrow + events next week
    evt_today_1 = CalendarEvent(
        "1", "Standup", now.replace(hour=10, minute=30), now.replace(hour=11, minute=0)
    )
    evt_today_2 = CalendarEvent(
        "2", "Design", now.replace(hour=14, minute=0), now.replace(hour=15, minute=0)
    )
    evt_tmrw = CalendarEvent(
        "3",
        "1-on-1",
        now.replace(day=17, hour=9, minute=0),
        now.replace(day=17, hour=9, minute=30),
    )
    evt_future_1 = CalendarEvent(
        "4",
        "Review",
        now.replace(day=18, hour=10, minute=0),
        now.replace(day=18, hour=11, minute=0),
    )
    evt_future_2 = CalendarEvent(
        "5",
        "All Hands",
        now.replace(day=19, hour=14, minute=0),
        now.replace(day=19, hour=15, minute=0),
    )

    all_events = [evt_today_1, evt_today_2, evt_tmrw, evt_future_1, evt_future_2]

    # With lookahead_count = 2 (today's 2 + next 2 future = 4)
    q = build_peek_queue(now, all_events, lookahead_count=2)
    assert len(q) == 4
    assert [e.uid for e in q] == ["1", "2", "3", "4"]

    # When today has NO events (only future events)
    q_no_today = build_peek_queue(
        now, [evt_tmrw, evt_future_1, evt_future_2], lookahead_count=2
    )
    assert len(q_no_today) == 2
    assert [e.uid for e in q_no_today] == ["3", "4"]


def test_format_peek_bookmark_and_delta() -> None:
    from app import format_peek_bookmark, format_relative_delta

    now = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)

    # Today's event
    evt_today = CalendarEvent(
        "1", "Standup", now.replace(hour=10, minute=30), now.replace(hour=11, minute=0)
    )
    assert format_peek_bookmark(now, evt_today, 1, 4) == "1/4"
    assert format_relative_delta(now, evt_today) == "in 30m"

    # Active LIVE event
    evt_live = CalendarEvent(
        "2", "Call", now.replace(hour=9, minute=30), now.replace(hour=10, minute=30)
    )
    assert format_relative_delta(now, evt_live) == "LIVE"

    # Ended event (e.g. ended 15m ago vs ended 1h ago)
    evt_ended_15m = CalendarEvent(
        "3", "Breakfast", now.replace(hour=9, minute=15), now.replace(hour=9, minute=45)
    )
    assert format_relative_delta(now, evt_ended_15m) == "ended 15m"

    evt_ended_1h = CalendarEvent(
        "3b", "Breakfast", now.replace(hour=8, minute=0), now.replace(hour=9, minute=0)
    )
    assert format_relative_delta(now, evt_ended_1h) == "ended 1h"

    # Tomorrow event
    evt_tmrw = CalendarEvent(
        "4",
        "Sync",
        now.replace(day=17, hour=9, minute=0),
        now.replace(day=17, hour=10, minute=0),
    )
    assert format_peek_bookmark(now, evt_tmrw, 2, 4) == "TMRW 2/4"

    # Within 7 days event (e.g. Wednesday 8/19)
    evt_wed = CalendarEvent(
        "5",
        "Demo",
        now.replace(day=19, hour=14, minute=0),
        now.replace(day=19, hour=15, minute=0),
    )
    assert format_peek_bookmark(now, evt_wed, 3, 4) == "WED 3/4"

    # Far future event (e.g. 10 days out)
    evt_far = CalendarEvent(
        "6",
        "Offsite",
        now.replace(day=26, hour=9, minute=0),
        now.replace(day=26, hour=17, minute=0),
    )
    assert format_peek_bookmark(now, evt_far, 4, 4) == "WED 8/26 4/4"


def test_render_aero_peek() -> None:
    from app import AgendaPeekState, render_aero_peek

    now = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    evt1 = CalendarEvent(
        "1",
        "Sprint Sync",
        now.replace(hour=11, minute=0),
        now.replace(hour=11, minute=30),
        location="Room 3B",
    )
    evt2 = CalendarEvent(
        "2",
        "Design",
        now.replace(hour=14, minute=0),
        now.replace(hour=15, minute=0),
        location="https://meet.google.com/abc",
    )

    peek = AgendaPeekState(
        queue=[evt1, evt2],
        selected_index=0,
        expires_at=now + timedelta(seconds=6),
    )

    elements, extra = render_aero_peek(now, peek)
    assert extra["priority"] == 50

    # Invariant element IDs present
    elem_ids = {e["id"] for e in elements}
    assert "clock_txt" in elem_ids
    assert "badge_bg" in elem_ids
    assert "badge_txt" in elem_ids
    assert "title_txt" in elem_ids
    assert "radar_bg" in elem_ids
    assert "stepper_0" in elem_ids
    assert "stepper_1" in elem_ids


def test_decode_input_events() -> None:
    from app import decode_input_events

    # Construct mock protobuf frame for StateUpdate.input:
    # State.updates (f=2,w=2) -> StateUpdate.input (f=11,w=2) -> button_event (f=1,w=2)
    btn_payload = bytes([0x08, 0x00, 0x10, 0x00])  # BTN_OK, PRESS
    input_ev = bytes([0x0A, len(btn_payload)]) + btn_payload
    state_upd = bytes([0x5A, len(input_ev)]) + input_ev
    frame = bytes([0x12, len(state_upd)]) + state_upd

    events = decode_input_events(frame)
    assert len(events) == 1
    assert events[0] == ("button", (0, 0))


def test_apply_hardware_input_alert_dismissal_and_exit() -> None:
    import app
    from app import (
        ACTION_PRESS,
        BTN_BACK,
        BTN_OK,
        BTN_START,
        AgendaPeekState,
        AlertState,
        apply_hardware_input,
    )

    now = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    evt = CalendarEvent(
        "1", "Standup", now + timedelta(minutes=5), now + timedelta(minutes=35)
    )

    # 1. Early alert dismissal on START button
    app.active_alert = AlertState(
        event=evt, checkpoint_minutes=5, expires_at=now + timedelta(seconds=15)
    )
    changed = apply_hardware_input(now, "button", (BTN_START, ACTION_PRESS), [evt])
    assert changed is True
    assert app.active_alert is None

    # 2. Early alert dismissal on BACK button
    app.active_alert = AlertState(
        event=evt, checkpoint_minutes=5, expires_at=now + timedelta(seconds=15)
    )
    changed = apply_hardware_input(now, "button", (BTN_BACK, ACTION_PRESS), [evt])
    assert changed is True
    assert app.active_alert is None

    # 3. Exit peek mode on BACK button
    app.active_peek = AgendaPeekState(
        queue=[evt], selected_index=0, expires_at=now + timedelta(seconds=6)
    )
    changed = apply_hardware_input(now, "button", (BTN_BACK, ACTION_PRESS), [evt])
    assert changed is True
    assert app.active_peek is None

    # 4. Manual sync on OK button
    app.manual_sync_flash_until = None
    changed = apply_hardware_input(now, "button", (BTN_OK, ACTION_PRESS), [evt])
    assert changed is True
    assert app.manual_sync_flash_until is not None
    assert app.manual_sync_flash_until > now


def test_apply_hardware_input_encoder_dial() -> None:
    import app
    from app import CalendarEvent, apply_hardware_input

    now = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    evt1 = CalendarEvent(
        "1", "Standup", now.replace(hour=10, minute=30), now.replace(hour=11, minute=0)
    )
    evt2 = CalendarEvent(
        "2", "Design", now.replace(hour=14, minute=0), now.replace(hour=15, minute=0)
    )
    events = [evt1, evt2]

    app.active_peek = None

    # Step forward (+1 CW) opens peek view at event 0
    changed = apply_hardware_input(now, "encoder", 1, events)
    assert changed is True
    assert app.active_peek is not None
    assert app.active_peek.selected_index == 0

    # Step forward again (+1 CW) moves to event 1
    apply_hardware_input(now, "encoder", 1, events)
    assert app.active_peek.selected_index == 1

    # Step backward (-1 CCW) moves back to event 0
    apply_hardware_input(now, "encoder", -1, events)
    assert app.active_peek.selected_index == 0


def test_top_led_halo_states() -> None:
    import app
    from app import (
        LED_COLOR_ALERT,
        LED_COLOR_MEETING,
        LED_COLOR_SYNC_ACK,
        AlertState,
        CalendarEvent,
        evaluate_display_with_hardware,
    )

    now = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)

    # 1. Idle state: LED off (None)
    app.active_alert = None
    app.active_peek = None
    app.manual_sync_flash_until = None
    _, extra = evaluate_display_with_hardware(now, [])
    assert extra["led_notification_color"] is None

    # 2. Alert state: LED amber
    evt_soon = CalendarEvent(
        "1", "Standup", now + timedelta(minutes=5), now + timedelta(minutes=35)
    )
    app.active_alert = AlertState(
        event=evt_soon, checkpoint_minutes=5, expires_at=now + timedelta(seconds=15)
    )
    _, extra = evaluate_display_with_hardware(now, [evt_soon])
    assert extra["led_notification_color"] == LED_COLOR_ALERT

    # 3. Active meeting state: LED blue
    app.active_alert = None
    evt_active = CalendarEvent(
        "2", "Review", now - timedelta(minutes=10), now + timedelta(minutes=20)
    )
    _, extra = evaluate_display_with_hardware(now, [evt_active])
    assert extra["led_notification_color"] == LED_COLOR_MEETING

    # 4. Manual sync flash overrides LED color
    app.manual_sync_flash_until = now + timedelta(seconds=0.5)
    _, extra = evaluate_display_with_hardware(now, [])
    assert extra["led_notification_color"] == LED_COLOR_SYNC_ACK
