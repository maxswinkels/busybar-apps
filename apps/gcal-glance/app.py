#!/usr/bin/env python3
"""GCal Glance: Google Calendar Flight Deck & Dynamic Runway (Aero Horizon).

python app.py                        # BUSY Bar over USB (always 10.0.4.20)
python app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
python app.py --demo                 # test with rich simulated schedule
python app.py --ical-url <URL>       # Google Calendar secret iCal URL
"""

import argparse
import asyncio
import calendar
import json
import re
import sys
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Final, Literal, TypedDict

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore

import httpx
from pydantic import AliasChoices, AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP = "gcal-glance"
GRID_WIDTH = 72
RADAR_WINDOW_MIN = 360  # 6-Hour rolling action window (1px = 5 min)

# --- Type Definitions -------------------------------------------------------

ElementType = Literal["rectangle", "text"]
GlyphType = Literal[
    "video",
    "coffee",
    "focus",
    "travel",
    "fitness",
    "celebrate",
    "overtime",
    "calendar",
]
FontType = Literal["tiny", "small"]
AlignType = Literal["top_left", "top_right"]
FillStyle = Literal["solid", "none"]


class LabelMapping(TypedDict):
    """Configuration mapping for an event category."""

    color: str
    icon: GlyphType
    label: str


class RectangleElement(TypedDict, total=False):
    """BUSY Bar rectangle display primitive."""

    id: str
    type: Literal["rectangle"]
    x: int
    y: int
    width: int
    height: int
    fill: FillStyle
    fill_colors: list[str]
    border_width: int
    border_color: str


class TextElement(TypedDict, total=False):
    """BUSY Bar text display primitive."""

    id: str
    type: Literal["text"]
    x: int
    y: int
    font: FontType
    color: str
    text: str
    align: AlignType
    width: int
    scroll_rate: int
    scroll_start_delay: int


DisplayElement = RectangleElement | TextElement


class DisplayExtra(TypedDict):
    """Auxiliary display control metadata (priority, LED notification color)."""

    priority: int
    led_notification_color: str | None


# --- Settings (pydantic-settings) -------------------------------------------
# All fields are readable from environment variables with the GCAL_GLANCE_ prefix
# (or GCAL_ / CALSYNC_ fallbacks).
# Example: GCAL_GLANCE_ALERT_BANNER_DURATION_SECONDS=30


class AppConfig(BaseSettings):
    model_config: ClassVar[SettingsConfigDict] = {
        "env_prefix": "GCAL_GLANCE_",
        "env_file": (Path(__file__).resolve().parent / ".env", ".env"),
        "extra": "ignore",
    }

    # BUSY Bar host (overridable by --host CLI arg too)
    host: str = Field(
        default="10.0.4.20",
        validation_alias=AliasChoices(
            "GCAL_GLANCE_HOST", "GCAL_HOST", "CALSYNC_HOST", "host"
        ),
    )

    # Google Calendar secret iCal feed URL
    ical_url: AnyHttpUrl | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_ICAL_URL", "GCAL_ICAL_URL", "CALSYNC_ICAL_URL", "ical_url"
        ),
    )

    # Run with a simulated demo schedule instead of a live calendar
    demo: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_DEMO", "GCAL_DEMO", "CALSYNC_DEMO", "demo"
        ),
    )

    # Alert interval sequence before event start (minutes, descending order)
    upcoming_alert_sequence_minutes: list[int] = Field(
        default=[15, 5, 2, 0],
        validation_alias=AliasChoices(
            "GCAL_GLANCE_UPCOMING_ALERT_SEQUENCE_MINUTES",
            "GCAL_UPCOMING_ALERT_SEQUENCE_MINUTES",
            "CALSYNC_UPCOMING_ALERT_SEQUENCE_MINUTES",
            "upcoming_alert_sequence_minutes",
        ),
    )

    # How long (seconds) each upcoming-event alert banner stays on screen
    alert_banner_duration_seconds: int = Field(
        default=15,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_ALERT_BANNER_DURATION_SECONDS",
            "GCAL_ALERT_BANNER_DURATION_SECONDS",
            "CALSYNC_ALERT_BANNER_DURATION_SECONDS",
            "alert_banner_duration_seconds",
        ),
    )

    # How often (seconds) to re-fetch the calendar feed
    calendar_poll_interval_seconds: int = Field(
        default=120,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_CALENDAR_POLL_INTERVAL_SECONDS",
            "GCAL_CALENDAR_POLL_INTERVAL_SECONDS",
            "CALSYNC_CALENDAR_POLL_INTERVAL_SECONDS",
            "calendar_poll_interval_seconds",
        ),
    )

    # Rolling proximity radar window in minutes (default: 360 = 6h = 5 min/px)
    radar_window_minutes: int = Field(
        default=360,
        ge=60,
        le=1440,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_RADAR_WINDOW_MINUTES",
            "GCAL_RADAR_WINDOW_MINUTES",
            "CALSYNC_RADAR_WINDOW_MINUTES",
            "radar_window_minutes",
        ),
    )

    # Ignore all-day calendar events (default: False)
    ignore_all_day: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_IGNORE_ALL_DAY",
            "GCAL_IGNORE_ALL_DAY",
            "CALSYNC_IGNORE_ALL_DAY",
            "ignore_all_day",
        ),
    )

    # Number of upcoming events to cycle in idle marquee & dial peek (2-10, default: 6)
    lookahead_count: int = Field(
        default=6,
        ge=2,
        le=10,
        validation_alias=AliasChoices(
            "GCAL_GLANCE_LOOKAHEAD_COUNT",
            "GCAL_LOOKAHEAD_COUNT",
            "CALSYNC_LOOKAHEAD_COUNT",
            "lookahead_count",
        ),
    )

    @field_validator("upcoming_alert_sequence_minutes", mode="before")
    @classmethod
    def _parse_sequence(cls, v: object) -> object:
        """Allow GCAL_GLANCE_UPCOMING_ALERT_SEQUENCE_MINUTES='15,5,2,0' from env."""
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v


_CONFIG: Final[AppConfig] = AppConfig()


def get_config() -> AppConfig:
    return _CONFIG


def set_config(**cli_overrides: Any) -> None:
    global _CONFIG
    _CONFIG = AppConfig(**cli_overrides)  # type: ignore[misc] # only allowed here


# User-configurable event label/color mappings (structured — not env-driven)
LABEL_ICON_MAPPINGS: Final[dict[str, LabelMapping]] = {
    "meeting": {"color": "#4285F4FF", "icon": "video", "label": "MEETING"},
    "dnd": {"color": "#EA4335FF", "icon": "focus", "label": "DO NOT DISTURB"},
    "focus": {"color": "#A142F4FF", "icon": "focus", "label": "DEEP WORK"},
    "break": {"color": "#34A853FF", "icon": "coffee", "label": "ON BREAK"},
    "coffee": {"color": "#34A853FF", "icon": "coffee", "label": "ON BREAK"},
    "lunch": {"color": "#34A853FF", "icon": "coffee", "label": "LUNCH"},
    "travel": {"color": "#FBBC05FF", "icon": "travel", "label": "TRAVEL"},
    "fitness": {"color": "#34A853FF", "icon": "fitness", "label": "FITNESS"},
    "celebrate": {"color": "#EA4335FF", "icon": "celebrate", "label": "EVENT"},
    "overtime": {"color": "#EA4335FF", "icon": "overtime", "label": "OVERTIME"},
    "calendar": {"color": "#4285F4FF", "icon": "calendar", "label": "CALENDAR"},
}

# fmt: off
# Common UTF-8 Emoji to 5x5 Micro-Glyph Mapping Table
EMOJI_GLYPH_MAPPINGS: Final[dict[str, GlyphType]] = {
    # Transit & Travel
    "🚌": "travel", "🚍": "travel", "🚎": "travel", "🚐": "travel", "🚑": "travel",
    "🚒": "travel", "🚓": "travel", "🚕": "travel", "🚗": "travel", "🚙": "travel",
    "🚚": "travel", "🚛": "travel", "🚜": "travel", "🏎": "travel", "🏍": "travel",
    "🛵": "travel", "🚲": "travel", "🛴": "travel", "🚊": "travel", "🚝": "travel",
    "🚄": "travel", "🚅": "travel", "🚆": "travel", "🚇": "travel", "🚈": "travel",
    "🚉": "travel", "🚂": "travel", "✈": "travel", "🛫": "travel", "🛬": "travel",
    "🛩": "travel", "🚀": "travel", "🛸": "travel", "🚁": "travel", "🛶": "travel",
    "⛵": "travel", "🚤": "travel", "🛳": "travel", "🚢": "travel", "⚓": "travel",
    "🧳": "travel", "🗺": "travel",
    # Coffee, Food, Drinks, Break & Leisure
    "☕": "coffee", "🍵": "coffee", "🧋": "coffee", "🥤": "coffee", "🍺": "coffee",
    "🍻": "coffee", "🍷": "coffee", "🍸": "coffee", "🍹": "coffee", "🥂": "coffee",
    "🍽": "coffee", "🍴": "coffee", "🥄": "coffee", "🍕": "coffee", "🍔": "coffee",
    "🍟": "coffee", "🌭": "coffee", "🍿": "coffee", "🥪": "coffee", "🌮": "coffee",
    "🌯": "coffee", "🥗": "coffee", "🍜": "coffee", "🍲": "coffee", "🍛": "coffee",
    "🍣": "coffee", "🍱": "coffee", "🥟": "coffee", "🍦": "coffee", "🍧": "coffee",
    "🍨": "coffee", "🍩": "coffee", "🍪": "coffee", "🎂": "celebrate", "🍰": "coffee",
    "🍫": "coffee", "🍬": "coffee", "🍭": "coffee", "😎": "coffee", "🌴": "coffee",
    "🏖": "coffee", "☀️": "coffee", "🧘": "coffee", "💆": "coffee", "🛋": "coffee",
    "🎮": "coffee", "🎧": "coffee", "🎵": "coffee",
    # Focus, Deep Work, Coding, Reading, Thinking
    "🎯": "focus", "🧠": "focus", "💡": "focus", "💻": "focus", "🖥": "focus",
    "⌨": "focus", "🖱": "focus", "🖨": "focus", "📝": "focus", "✏": "focus",
    "🖊": "focus", "🖋": "focus", "📚": "focus", "📖": "focus", "📕": "focus",
    "📗": "focus", "📘": "focus", "📙": "focus", "📓": "focus", "📒": "focus",
    "🔬": "focus", "🔭": "focus", "🧪": "focus", "📐": "focus", "📏": "focus",
    "🛠": "focus", "🔧": "focus", "🔨": "focus", "⚙": "focus", "⚡": "focus",
    "🔥": "focus", "🔍": "focus", "🔎": "focus", "📊": "focus", "📈": "focus",
    # Video & Calls
    "📹": "video", "🎥": "video", "🎬": "video", "📺": "video", "📞": "video",
    "📱": "video", "🎙": "video", "🎤": "video", "💬": "video", "🗣": "video",
    "👥": "video", "👤": "video", "🤝": "video", "👔": "video",
    # Celebration & Social
    "🎉": "celebrate", "🎊": "celebrate", "🎈": "celebrate", "🥳": "celebrate",
    "🎁": "celebrate", "🍾": "celebrate", "✨": "celebrate", "⭐": "celebrate",
    "🌟": "celebrate", "🎆": "celebrate", "🎇": "celebrate",
    # Fitness & Health
    "🏃": "fitness", "🏋": "fitness", "🚴": "fitness", "🏊": "fitness", "⚽": "fitness",
    "🏀": "fitness", "🏈": "fitness", "⚾": "fitness", "🎾": "fitness", "🏐": "fitness",
    "🥊": "fitness", "🧗": "fitness", "🥋": "fitness", "❤️": "fitness", "🩺": "fitness",
    "💊": "fitness",
    # Calendar, Clock, Time
    "📅": "calendar", "🗓": "calendar", "📆": "calendar", "⏰": "calendar",
    "⏱": "calendar", "⏲": "calendar", "⏳": "overtime", "⌛": "overtime",
}
# fmt: on

# Display theme colours (#RRGGBBAA) - Multi-Tone Semantic Palette
COLOR_CLOCK = "#8AB4F8FF"  # Soft Ice Blue (calm wall time)
COLOR_CLOCK_ALERT = "#FDD663FF"  # Warm Amber (alert wall time)
COLOR_CLOCK_OVER = "#F28B82FF"  # Coral Red (overtime wall time)
COLOR_CLOCK_BREAK = "#81C995FF"  # Sage Green (break wall time)

COLOR_WHITE = "#FFFFFFFF"  # Crisp Chalk White (main titles & playhead needle)
COLOR_MUTED = "#9AA0A6FF"  # Slate Gray (subtext & metadata)
COLOR_SKY = "#AECBFAFF"  # Muted Sky Blue (runway metadata)

COLOR_BLUE = "#4285F4FF"  # Google Blue (meetings)
COLOR_BLUE_DARK = "#1A73E8FF"  # Inverted Blue Plate
COLOR_AMBER = "#FBBC05FF"  # Warning Amber (alerts & wrap-up)
COLOR_AMBER_DARK = "#7A4B00FF"  # Inverted Amber Plate
COLOR_RED = "#EA4335FF"  # Crimson Red (active calls & DND)
COLOR_RED_DARK = "#D93025FF"  # Inverted Red Plate
COLOR_RED_DIM = "#5C130CFF"  # Dim red pulse
COLOR_GREEN = "#34A853FF"  # Emerald Green (breaks & lunches)
COLOR_GREEN_DARK = "#1E8E3EFF"  # Inverted Green Plate
COLOR_PURPLE = "#A142F4FF"  # Royal Purple (deep focus)
COLOR_PURPLE_DARK = "#9334E6FF"  # Inverted Purple Plate

COLOR_RADAR_BG = "#141414FF"  # Deep Obsidian Base Rail (high contrast)
COLOR_TICK = "#303030FF"  # Dark Slate Milestone Pips

# Dynamic Category Marquee Soft-Tint Palette
GLYPH_TINT_COLORS: Final[dict[GlyphType, str]] = {
    "travel": "#FDD663FF",  # Soft Warm Amber
    "video": "#AECBFAFF",  # Soft Sky Blue
    "focus": "#D7AEFBFF",  # Soft Lavender
    "coffee": "#81C995FF",  # Soft Mint Green
    "fitness": "#81C995FF",  # Soft Mint Green
    "celebrate": "#F28B82FF",  # Soft Coral
    "overtime": "#F28B82FF",  # Soft Coral Red
    "calendar": "#AECBFAFF",  # Soft Sky Blue
}


def resolve_glyph_tint_color(glyph_type: GlyphType) -> str:
    """Resolve a soft illuminated text color for the marquee stream."""
    return GLYPH_TINT_COLORS.get(glyph_type, COLOR_SKY)


# --- Phase 2 Tactile Controls & LED Halo Settings ---------------------------

PEEK_INACTIVITY_TIMEOUT_SECONDS: Final[int] = 6
SYNC_ACK_FLASH_DURATION_SECONDS: Final[float] = 0.5

# Top-surface LED Notification Halo Colors (#RRGGBBAA)
LED_COLOR_ALERT: Final[str] = "#FBBC05FF"  # Amber pulse during alert banner
LED_COLOR_MEETING: Final[str] = "#4285F4FF"  # Solid Blue during active meeting
LED_COLOR_DND: Final[str] = "#EA4335FF"  # Solid Red during DND
LED_COLOR_SYNC_ACK: Final[str] = "#34A853FF"  # Green flash on manual sync


# --- BUSY Bar HTTP API (Async) ----------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="GCal Glance: Google Calendar for BUSY Bar (Aero Horizon)"
    )
    cfg = get_config()
    p.add_argument("--host", default=cfg.host)
    p.add_argument(
        "--ical-url",
        default=cfg.ical_url,
        help="Google Calendar secret iCal feed URL (or set GCAL_GLANCE_ICAL_URL)",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        default=cfg.demo,
        help=(
            "use fake events for testing without a calendar feed (or set"
            " GCAL_GLANCE_DEMO=true)"
        ),
    )
    p.add_argument(
        "--lookahead-count",
        type=int,
        default=cfg.lookahead_count,
        help=(
            "Number of upcoming events to cycle in idle marquee & dial peek"
            " (2-10, default: 6)"
        ),
    )
    p.add_argument(
        "--radar-window-minutes",
        type=int,
        default=cfg.radar_window_minutes,
        help=(
            "Duration of rolling proximity radar window in minutes (60-1440,"
            " default: 360 = 5 min/px)"
        ),
    )
    p.add_argument(
        "--ignore-all-day",
        action="store_true",
        default=cfg.ignore_all_day,
        help="Ignore all-day calendar events (or set GCAL_GLANCE_IGNORE_ALL_DAY=true)",
    )
    args, _ = p.parse_known_args()
    set_config(
        host=args.host,
        ical_url=args.ical_url,
        demo=args.demo,
        lookahead_count=args.lookahead_count,
        radar_window_minutes=args.radar_window_minutes,
        ignore_all_day=args.ignore_all_day,
    )
    return args


ARGS: Final[argparse.Namespace] = _parse_args()
BASE: Final[str] = "http://" + get_config().host.replace("http://", "").rstrip("/")


async def draw(
    client: httpx.AsyncClient, elements: list[DisplayElement], **extra: Any
) -> None:
    extra = {k: v for k, v in extra.items() if v is not None}
    body: dict[str, Any] = {"application_name": APP, "elements": elements, **extra}
    resp = await client.post(f"{BASE}/api/display/draw", json=body, timeout=5.0)
    if resp.status_code == 400:
        print(f"[draw] Bad payload: {json.dumps(body, indent=2)}", file=sys.stderr)
    resp.raise_for_status()


# --- Data Models ------------------------------------------------------------


@dataclass
class CalendarEvent:
    """A single calendar event with start/end times and metadata."""

    uid: str  # Unique event identifier (from iCal UID)
    summary: str  # Event title / summary
    start: datetime  # Start time (timezone-aware)
    end: datetime  # End time (timezone-aware)
    color: str = ""  # Google Calendar color label (if available)
    description: str = ""  # Event description body
    location: str = ""  # Room, meeting URL, or location


@dataclass
class AlertState:
    """Tracks a currently-active alert banner on screen."""

    event: CalendarEvent  # The event this alert is for
    checkpoint_minutes: int  # Which alert milestone triggered (e.g. 15, 5, 2, 0)
    expires_at: datetime  # Wall-clock time when this banner should dismiss

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


# Fired checkpoint tracker: set of (event_uid, checkpoint_minutes)
fired_checkpoints: set[tuple[str, int]] = set()

# Currently displayed alert banner (or None)
active_alert: AlertState | None = None


@dataclass
class AgendaPeekState:
    """Active Agenda Peek state for interactive rotary dial scrubbing."""

    queue: list[CalendarEvent]
    selected_index: int  # 0-indexed
    expires_at: datetime  # Inactivity expiration wall-clock time

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


# Currently active agenda peek overlay (or None)
active_peek: AgendaPeekState | None = None

# Manual sync feedback flash tracker & async trigger event
manual_sync_flash_until: datetime | None = None
_manual_sync_event: asyncio.Event = asyncio.Event()

# --- Calendar Fetcher -------------------------------------------------------


def _unfold_ical(text_content: str) -> list[str]:
    """Unfold lines in iCal according to RFC 5545."""
    lines: list[str] = []
    for line in text_content.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape_ical(text_val: str) -> str:
    """Unescape special characters in iCal text fields."""
    return (
        text_val.replace(r"\n", "\n")
        .replace(r"\,", ",")
        .replace(r"\;", ";")
        .replace(r"\\", "\\")
    )


def _parse_ical_datetime(
    val: str, params: str = "", as_utc: bool = True
) -> datetime | None:
    """Parse an iCal DTSTART/DTEND string into a timezone-aware datetime."""
    val = val.strip()
    is_all_day = "VALUE=DATE" in params or len(val) == 8
    if is_all_day:
        if get_config().ignore_all_day:
            return None
        try:
            # All-day event YYYYMMDD -> start of day in local timezone (or UTC)
            dt = datetime.strptime(val, "%Y%m%d")
            local_dt = dt.astimezone()
            return local_dt if not as_utc else local_dt.astimezone(timezone.utc)
        except Exception:
            return None
    try:
        if val.endswith("Z"):
            dt = datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt if not as_utc else dt.astimezone(timezone.utc)
        if "T" in val:
            dt = datetime.strptime(val, "%Y%m%dT%H%M%S")
            tzid_match = re.search(r"TZID=([^;:]+)", params, re.IGNORECASE)
            if tzid_match and ZoneInfo is not None:
                tz_name = tzid_match.group(1).strip()
                try:
                    tz_dt = dt.replace(tzinfo=ZoneInfo(tz_name))
                    return tz_dt if not as_utc else tz_dt.astimezone(timezone.utc)
                except Exception:
                    pass
            # Fallback to local system timezone -> UTC
            local_dt = dt.astimezone()
            return local_dt if not as_utc else local_dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None


_DAY_NAMES: Final[dict[str, int]] = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


def _get_nth_weekday_of_month(
    year: int, month: int, weekday: int, n: int
) -> date | None:
    """Find nth weekday of month (e.g. 3rd Friday: n=3; last Friday: n=-1)."""
    cal = calendar.Calendar(firstweekday=0)
    month_days = [
        d
        for d in cal.itermonthdates(year, month)
        if d.month == month and d.weekday() == weekday
    ]
    if not month_days:
        return None
    if n > 0 and n <= len(month_days):
        return month_days[n - 1]
    elif n < 0 and abs(n) <= len(month_days):
        return month_days[n]
    return None


def _parse_byday(byday_str: str) -> list[tuple[int | None, int]]:
    """Parse BYDAY string (e.g. '3FR', 'MO,TU,WE') into (pos, weekday_int) list."""
    results: list[tuple[int | None, int]] = []
    for item in byday_str.split(","):
        item = item.strip().upper()
        m = re.match(r"^([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)$", item)
        if m:
            pos = int(m.group(1)) if m.group(1) else None
            wd = _DAY_NAMES[m.group(2)]
            results.append((pos, wd))
    return results


def _expand_rrule(
    dtstart: datetime,
    rrule_str: str,
    window_start: datetime,
    window_end: datetime,
    max_occurrences: int = 100,
) -> list[datetime]:
    """Pure stdlib RFC 5545 recurrence expansion preserving timezone across DST."""
    props: dict[str, str] = {}
    for part in rrule_str.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            props[k.strip().upper()] = v.strip()

    freq = props.get("FREQ", "DAILY").upper()
    interval = max(1, int(props.get("INTERVAL", "1")))
    count = int(props["COUNT"]) if "COUNT" in props else None

    until_dt: datetime | None = None
    if "UNTIL" in props:
        u_val = props["UNTIL"]
        if len(u_val) == 8:
            u_y, u_m, u_d = int(u_val[:4]), int(u_val[4:6]), int(u_val[6:8])
            until_dt = datetime(u_y, u_m, u_d, 23, 59, 59, tzinfo=timezone.utc)
        elif "T" in u_val:
            u_clean = u_val.rstrip("Z")
            u_y, u_m, u_d = int(u_clean[:4]), int(u_clean[4:6]), int(u_clean[6:8])
            hh, mm = int(u_clean[9:11]), int(u_clean[11:13])
            ss = int(u_clean[13:15]) if len(u_clean) >= 15 else 0
            until_dt = datetime(u_y, u_m, u_d, hh, mm, ss, tzinfo=timezone.utc)

    byday = _parse_byday(props["BYDAY"]) if "BYDAY" in props else []
    bymonthday = (
        [int(x) for x in props["BYMONTHDAY"].split(",") if x.isdigit()]
        if "BYMONTHDAY" in props
        else []
    )

    tz = dtstart.tzinfo or timezone.utc
    start_date = dtstart.date()
    results: list[datetime] = []
    seen_count = 0

    def check_and_add(cand_date: date) -> bool:
        nonlocal seen_count
        if cand_date < start_date:
            return True
        try:
            cand_dt = datetime.combine(cand_date, dtstart.time(), tzinfo=tz)
        except Exception:
            return True
        cand_utc = cand_dt.astimezone(timezone.utc)
        if until_dt is not None and cand_utc > until_dt:
            return False
        seen_count += 1
        if count is not None and seen_count > count:
            return False
        if cand_utc >= window_start:
            if cand_utc <= window_end:
                results.append(cand_utc)
            elif cand_utc > window_end:
                return False
        return len(results) < max_occurrences

    if freq == "DAILY":
        curr = start_date
        win_start_local = window_start.astimezone(tz).date()
        if count is None and curr < win_start_local:
            days_behind = (win_start_local - curr).days
            steps = days_behind // interval
            curr += timedelta(days=steps * interval)
        while curr <= window_end.astimezone(tz).date() + timedelta(days=1):
            if not check_and_add(curr):
                break
            curr += timedelta(days=interval)

    elif freq == "WEEKLY":
        curr_week_start = start_date - timedelta(days=start_date.weekday())
        win_start_local = window_start.astimezone(tz).date()
        if count is None and curr_week_start < win_start_local - timedelta(days=7):
            weeks_behind = (win_start_local - curr_week_start).days // 7
            steps = weeks_behind // interval
            curr_week_start += timedelta(weeks=steps * interval)

        target_days = [wd for (_, wd) in byday] if byday else [dtstart.weekday()]
        target_days.sort()
        stop = False
        while not stop:
            for wd in target_days:
                cand_date = curr_week_start + timedelta(days=wd)
                if cand_date < start_date:
                    continue
                if not check_and_add(cand_date):
                    stop = True
                    break
            curr_week_start += timedelta(weeks=interval)
            if curr_week_start > window_end.astimezone(tz).date() + timedelta(days=7):
                break

    elif freq == "MONTHLY":
        curr_year = start_date.year
        curr_month = start_date.month
        win_start_local = window_start.astimezone(tz).date()
        if count is None:
            months_diff = (win_start_local.year - curr_year) * 12 + (
                win_start_local.month - curr_month
            )
            if months_diff > 1:
                steps = months_diff // interval
                total_months = (curr_year * 12 + curr_month - 1) + steps * interval
                curr_year = total_months // 12
                curr_month = (total_months % 12) + 1

        stop = False
        while not stop:
            if byday:
                for pos, wd in byday:
                    if pos is not None:
                        d_pos = _get_nth_weekday_of_month(
                            curr_year, curr_month, wd, pos
                        )
                        if d_pos and not check_and_add(d_pos):
                            stop = True
                            break
                    else:
                        cal = calendar.Calendar(firstweekday=0)
                        for d_day in cal.itermonthdates(curr_year, curr_month):
                            if d_day.month == curr_month and d_day.weekday() == wd:
                                if not check_and_add(d_day):
                                    stop = True
                                    break
            elif bymonthday:
                for bmd in bymonthday:
                    try:
                        d_bmd = date(curr_year, curr_month, bmd)
                        if not check_and_add(d_bmd):
                            stop = True
                            break
                    except ValueError:
                        pass
            else:
                try:
                    d_simple = date(curr_year, curr_month, start_date.day)
                    if not check_and_add(d_simple):
                        stop = True
                        break
                except ValueError:
                    pass
            total_m = (curr_year * 12 + curr_month - 1) + interval
            curr_year = total_m // 12
            curr_month = (total_m % 12) + 1
            if date(curr_year, curr_month, 1) > window_end.astimezone(
                tz
            ).date() + timedelta(days=32):
                break

    elif freq == "YEARLY":
        curr_year = start_date.year
        win_start_local = window_start.astimezone(tz).date()
        if count is None and curr_year < win_start_local.year - 1:
            years_diff = win_start_local.year - curr_year
            steps = years_diff // interval
            curr_year += steps * interval
        while True:
            try:
                d_yr = date(curr_year, start_date.month, start_date.day)
                if not check_and_add(d_yr):
                    break
            except ValueError:
                pass
            curr_year += interval
            if curr_year > window_end.astimezone(tz).date().year + 1:
                break

    results.sort()
    return results


async def fetch_events(
    client: httpx.AsyncClient, ical_url: AnyHttpUrl | str
) -> list[CalendarEvent] | None:
    """Fetch an iCal .ics URL and return future CalendarEvents sorted by start time."""
    try:
        resp = await client.get(
            str(ical_url),
            headers={"User-Agent": "BUSYBar-CalendarSync/1.0"},
            timeout=15.0,
        )
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        print(
            f"[fetch] warning: failed to fetch ical feed: {e}",
            file=sys.stderr,
            flush=True,
        )
        return None

    try:
        lines = _unfold_ical(content)
        raw_events: list[dict[str, Any]] = []
        in_vevent = False
        event_data: dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(days=60)

        for line in lines:
            line_str = line.strip()
            if line_str == "BEGIN:VEVENT":
                in_vevent = True
                event_data = {"EXDATES": []}
            elif line_str == "END:VEVENT":
                if in_vevent:
                    in_vevent = False
                    raw_events.append(dict(event_data))
            elif in_vevent:
                if ":" in line:
                    key_part, val = line.split(":", 1)
                    if ";" in key_part:
                        key, params = key_part.split(";", 1)
                    else:
                        key, params = key_part, ""
                    key_upper = key.upper()
                    if key_upper == "EXDATE":
                        event_data.setdefault("EXDATES", []).append((val, params))
                    elif key_upper == "RECURRENCE-ID":
                        event_data["RECURRENCE_ID_VAL"] = val
                        event_data["RECURRENCE_ID_PARAMS"] = params
                    elif key_upper in (
                        "UID",
                        "SUMMARY",
                        "DESCRIPTION",
                        "LOCATION",
                        "COLOR",
                        "X-APPLE-CALENDAR-COLOR",
                        "RRULE",
                        "STATUS",
                    ):
                        event_data[key_upper] = val
                    elif key_upper == "DTSTART":
                        event_data["DTSTART_VAL"] = val
                        event_data["DTSTART_PARAMS"] = params
                    elif key_upper == "DTEND":
                        event_data["DTEND_VAL"] = val
                        event_data["DTEND_PARAMS"] = params

        # 1. Collect overridden recurrence instances to suppress base RRULE occurrences
        overridden_recs: set[tuple[str, datetime]] = set()
        for ed in raw_events:
            uid = ed.get("UID", "")
            rec_val = ed.get("RECURRENCE_ID_VAL")
            rec_params = ed.get("RECURRENCE_ID_PARAMS", "")
            if rec_val and uid:
                rec_dt = _parse_ical_datetime(rec_val, rec_params, as_utc=True)
                if rec_dt is not None:
                    overridden_recs.add((uid, rec_dt))

        events: list[CalendarEvent] = []
        for ed in raw_events:
            uid = ed.get("UID", "")
            status = ed.get("STATUS", "").upper()
            if status == "CANCELLED":
                continue

            summary = _unescape_ical(ed.get("SUMMARY", "(No title)"))
            description = _unescape_ical(ed.get("DESCRIPTION", ""))
            location = _unescape_ical(ed.get("LOCATION", ""))
            color = ed.get("COLOR", "") or ed.get("X-APPLE-CALENDAR-COLOR", "")

            dtstart_val = ed.get("DTSTART_VAL", "")
            dtstart_params = ed.get("DTSTART_PARAMS", "")
            dtend_val = ed.get("DTEND_VAL", "")
            dtend_params = ed.get("DTEND_PARAMS", "")

            start_dt_raw = _parse_ical_datetime(
                dtstart_val, dtstart_params, as_utc=False
            )
            if start_dt_raw is None:
                continue
            end_dt_raw = (
                _parse_ical_datetime(dtend_val, dtend_params, as_utc=False)
                if dtend_val
                else None
            )
            is_all_day_evt = (
                "VALUE=DATE" in dtstart_params or len(dtstart_val.strip()) == 8
            )
            if end_dt_raw is None:
                end_dt_raw = start_dt_raw + (
                    timedelta(days=1) if is_all_day_evt else timedelta(hours=1)
                )
            duration = max(timedelta(seconds=0), end_dt_raw - start_dt_raw)

            start_dt = start_dt_raw.astimezone(timezone.utc)
            end_dt = end_dt_raw.astimezone(timezone.utc)

            rrule_str = ed.get("RRULE")
            if rrule_str:
                exdates: set[datetime] = set()
                for ex_val, ex_params in ed.get("EXDATES", []):
                    for part in ex_val.split(","):
                        ex_dt = _parse_ical_datetime(
                            part.strip(), ex_params, as_utc=True
                        )
                        if ex_dt is not None:
                            exdates.add(ex_dt)

                try:
                    for occ_start in _expand_rrule(
                        start_dt_raw,
                        rrule_str,
                        now - timedelta(hours=1),
                        window_end,
                    ):
                        if occ_start in exdates or (uid, occ_start) in overridden_recs:
                            continue
                        occ_end = occ_start + duration
                        if occ_end > now:
                            events.append(
                                CalendarEvent(
                                    uid=f"{uid}_{occ_start.isoformat()}",
                                    summary=summary,
                                    start=occ_start,
                                    end=occ_end,
                                    color=color,
                                    description=description,
                                    location=location,
                                )
                            )
                except Exception:
                    if end_dt > now:
                        events.append(
                            CalendarEvent(
                                uid=uid,
                                summary=summary,
                                start=start_dt,
                                end=end_dt,
                                color=color,
                                description=description,
                                location=location,
                            )
                        )
            else:
                if end_dt > now:
                    events.append(
                        CalendarEvent(
                            uid=uid,
                            summary=summary,
                            start=start_dt,
                            end=end_dt,
                            color=color,
                            description=description,
                            location=location,
                        )
                    )

        events.sort(key=lambda e: e.start)
        return events
    except Exception as e:
        print(
            f"[fetch] warning: error parsing ical feed: {e}",
            file=sys.stderr,
            flush=True,
        )
        return None


def demo_events() -> list[CalendarEvent]:
    """Return realistic fake events for testing Aero Horizon states."""
    now = datetime.now(timezone.utc)
    return [
        CalendarEvent(
            "demo-1",
            "Sprint Planning",
            now + timedelta(minutes=4),
            now + timedelta(minutes=34),
            location="Room 3B",
        ),
        CalendarEvent(
            "demo-2",
            "[focus] Deep Work / Architecture",
            now + timedelta(minutes=36),
            now + timedelta(minutes=90),
            location="Desk",
        ),
        CalendarEvent(
            "demo-3",
            "[coffee] 1-on-1 Sync",
            now + timedelta(minutes=105),
            now + timedelta(minutes=135),
            location="Cafeteria",
        ),
    ]


# --- Invariant Element & Helper Functions ------------------------------------


def make_rect(
    elem_id: str,
    x: int,
    y: int,
    width: int,
    height: int,
    fill: str,
    border_width: int = 0,
    border_color: str | None = None,
) -> RectangleElement:
    """Create a fully-specified rectangle with fill and explicit border width."""
    elem: RectangleElement = {
        "id": elem_id,
        "type": "rectangle",
        "x": x,
        "y": y,
        "width": max(1, width),
        "height": max(1, height),
        "fill": "none" if fill.endswith("00") else "solid",
        "fill_colors": [fill],
        "border_width": border_width,
    }
    if border_color:
        elem["border_color"] = border_color
    return elem


def park_rect(elem_id: str) -> RectangleElement:
    """Park an inactive rectangle safely off-screen."""
    return make_rect(elem_id, -100, -100, 1, 1, "#00000000")


def park_text(elem_id: str, text: str = "") -> TextElement:
    """Park an inactive text element safely off-screen."""
    return {
        "id": elem_id,
        "type": "text",
        "x": -100,
        "y": -100,
        "font": "tiny",
        "color": "#00000000",
        "text": text,
    }


def fmt_12h(dt: datetime, include_ampm: bool = True) -> str:
    """Format a timezone-aware datetime in 12-hour format without leading zero."""
    local_dt = dt.astimezone()
    hour = local_dt.hour % 12
    if hour == 0:
        hour = 12
    ampm = "AM" if local_dt.hour < 12 else "PM"
    if include_ampm:
        return f"{hour}:{local_dt.minute:02d} {ampm}"
    return f"{hour}:{local_dt.minute:02d}"


def fmt_compact_12h(dt: datetime) -> str:
    """Format datetime into compact 12h representation for status pills.

    Examples: '5:51A', '10A', '1:30P'.
    """
    local_dt = dt.astimezone()
    hour = local_dt.hour % 12
    if hour == 0:
        hour = 12
    ampm = "A" if local_dt.hour < 12 else "P"
    if local_dt.minute == 0:
        return f"{hour}{ampm}"
    return f"{hour}:{local_dt.minute:02d}{ampm}"


def fmt_countdown(total_seconds: float) -> str:
    """Format seconds into compact mm:ss or XhXXm."""
    sec = max(0, int(total_seconds))
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"{m:02d}:{s:02d}"
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m"


def fmt_remaining(delta: timedelta) -> str:
    """Format a timedelta as a compact remaining-time string."""
    total_minutes = max(0, int(delta.total_seconds()) // 60)
    if total_minutes >= 60:
        h, m = divmod(total_minutes, 60)
        return f"{h}h{m:02d}m"
    return f"{total_minutes}m"


def resolve_label(event: CalendarEvent) -> str:
    """Resolve an event to a display label using LABEL_ICON_MAPPINGS."""
    title = event.summary.lower()
    for key, mapping in LABEL_ICON_MAPPINGS.items():
        if f"[{key}]" in title:
            return mapping["label"]
    for mapping in LABEL_ICON_MAPPINGS.values():
        if event.color and event.color.upper().startswith(mapping["color"][:7].upper()):
            return mapping["label"]
    return "IN MEETING"


def clean_location(location: str, description: str = "") -> str:
    """Sanitize and extract a clean room name or meeting platform label."""
    raw = location.strip() if location else ""
    desc = description.strip() if description else ""

    # If location field is populated
    if raw:
        if "http://" in raw or "https://" in raw:
            parts = re.split(r"https?://\S+", raw)
            clean_part = parts[0].strip(" -·,;/")
            if clean_part:
                return clean_part[:30]
            # Location only contained a URL
            raw_lower = raw.lower()
            if "meet.google.com" in raw_lower:
                return "Google Meet"
            if "zoom.us" in raw_lower or "zoomgov.com" in raw_lower:
                return "Zoom"
            if "teams.microsoft.com" in raw_lower or "teams.live.com" in raw_lower:
                return "Teams"
            if "webex.com" in raw_lower:
                return "Webex"
            if "slack.com" in raw_lower:
                return "Slack Huddle"
            return "Video Link"
        return raw[:30]

    # If location is empty, check description for platform links
    if desc:
        desc_lower = desc.lower()
        if "meet.google.com" in desc_lower:
            return "Google Meet"
        if "zoom.us" in desc_lower or "zoomgov.com" in desc_lower:
            return "Zoom"
        if "teams.microsoft.com" in desc_lower or "teams.live.com" in desc_lower:
            return "Teams"
        if "webex.com" in desc_lower:
            return "Webex"
        if "slack.com" in desc_lower:
            return "Slack Huddle"

    return ""


def sanitize_display_text(text: str) -> str:
    """Sanitize text to printable ASCII to prevent missing glyph boxes on hardware."""
    if not text:
        return ""
    # Map common unicode punctuation/symbols to clean ASCII equivalents
    text = (
        text.replace("\u00b7", " - ")  # Middle dot
        .replace("\u2022", " - ")  # Bullet
        .replace("\u2014", " - ")  # Em dash
        .replace("\u2013", " - ")  # En dash
        .replace("\u201c", '"')  # Left double quote
        .replace("\u201d", '"')  # Right double quote
        .replace("\u2018", "'")  # Left single quote
        .replace("\u2019", "'")  # Right single quote
        .replace("\u2026", "...")  # Horizontal ellipsis
    )
    # Strip any remaining non-ASCII characters (e.g. emojis, symbols)
    cleaned = "".join(c for c in text if 32 <= ord(c) <= 126)
    # Normalize multiple whitespace
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Normalize multiple dashes
    cleaned = re.sub(r"(\s+-\s+)+", " - ", cleaned)
    return cleaned.strip(" -")


def format_idle_badge(now: datetime, upcoming_events: list[CalendarEvent]) -> str:
    """Format a compact temporal bookmark pill for Tier 1 top-right."""
    if not upcoming_events:
        return "FREE"
    next_evt = upcoming_events[0]
    now_local = now.astimezone()
    next_start_local = next_evt.start.astimezone()
    compact_time = fmt_compact_12h(next_evt.start)

    delta_days = (next_start_local.date() - now_local.date()).days

    if delta_days <= 0:
        return f"NEXT {compact_time}"
    elif delta_days == 1:
        return f"TMRW {compact_time}"
    elif delta_days < 7:
        day_name = next_start_local.strftime("%a").upper()
        return f"{day_name} {compact_time}"
    else:
        day_name = next_start_local.strftime("%a").upper()
        return (
            f"{day_name} {next_start_local.month}/{next_start_local.day} {compact_time}"
        )


def format_idle_marquee(
    now: datetime,
    upcoming_events: list[CalendarEvent],
    lookahead_count: int,
) -> str:
    """Build a rich marquee stream with configurable multi-event lookahead."""
    if not upcoming_events:
        return "Schedule clear"

    now_local = now.astimezone()
    next_evt = upcoming_events[0]
    next_start_local = next_evt.start.astimezone()

    first_summary = sanitize_display_text(next_evt.summary) or "Event"
    first_loc = sanitize_display_text(
        clean_location(next_evt.location, next_evt.description)
    )
    first_loc_part = f" - {first_loc}" if first_loc else ""

    # If first event is today, include start time (e.g. '5:51 PM - Travel')
    # If on a future day, its start time and day are in the pill [TMRW 5:51A],
    # so we start directly with the event title and details!
    if next_start_local.date() == now_local.date():
        time_str = fmt_12h(next_evt.start, include_ampm=True)
        parts = [f"{time_str} - {first_summary}{first_loc_part}"]
    else:
        parts = [f"{first_summary}{first_loc_part}"]

    # Downstream lookahead stream (up to lookahead_count items)
    prev_date = next_start_local.date()
    for evt in upcoming_events[1 : max(1, lookahead_count)]:
        evt_local = evt.start.astimezone()
        evt_summary = sanitize_display_text(evt.summary) or "Event"
        evt_loc = sanitize_display_text(clean_location(evt.location, evt.description))
        evt_loc_part = f" ({evt_loc})" if evt_loc else ""

        if evt_local.date() == prev_date:
            time_str = fmt_12h(evt.start, include_ampm=True)
            parts.append(f"[THEN {time_str}] {evt_summary}{evt_loc_part}")
        else:
            delta_days = (evt_local.date() - now_local.date()).days
            time_str = fmt_12h(evt.start, include_ampm=True)
            day_name = evt_local.strftime("%a")
            if delta_days < 7:
                parts.append(
                    f"[THEN {day_name} {time_str}] {evt_summary}{evt_loc_part}"
                )
            else:
                date_str = f"{evt_local.month}/{evt_local.day}"
                tag = f"[THEN {day_name} {date_str} {time_str}]"
                parts.append(f"{tag} {evt_summary}{evt_loc_part}")
            prev_date = evt_local.date()

    return " - ".join(parts)


def build_peek_queue(
    now: datetime,
    events: list[CalendarEvent],
    lookahead_count: int,
) -> list[CalendarEvent]:
    """Compose the lookahead peek queue for rotary dial navigation."""
    if not events:
        return []
    now_local = now.astimezone()
    today_date = now_local.date()

    today_events = [e for e in events if e.start.astimezone().date() == today_date]
    today_events.sort(key=lambda e: e.start)

    future_events = [e for e in events if e.start.astimezone().date() > today_date]
    future_events.sort(key=lambda e: e.start)

    if today_events:
        return list(today_events) + future_events[: max(1, lookahead_count)]
    return future_events[: max(1, lookahead_count)]


def format_peek_bookmark(
    now: datetime,
    event: CalendarEvent,
    index: int,
    total: int,
) -> str:
    """Format the index bookmark pill for dial peek (e.g. 1/4, TMRW 2/4, WED 3/4)."""
    now_local = now.astimezone()
    evt_start_local = event.start.astimezone()
    delta_days = (evt_start_local.date() - now_local.date()).days

    if delta_days <= 0:
        return f"{index}/{total}"
    elif delta_days == 1:
        return f"TMRW {index}/{total}"
    elif delta_days < 7:
        day_name = evt_start_local.strftime("%a").upper()
        return f"{day_name} {index}/{total}"
    else:
        day_name = evt_start_local.strftime("%a").upper()
        return (
            f"{day_name} {evt_start_local.month}/{evt_start_local.day} {index}/{total}"
        )


def format_relative_delta(now: datetime, event: CalendarEvent) -> str:
    """Format relative delta badge for peek view (e.g. 'in 42m', 'LIVE')."""
    if event.start <= now < event.end:
        return "LIVE"
    if now >= event.end:
        ended_min = max(0, int((now - event.end).total_seconds() / 60.0))
        if ended_min < 60:
            return f"ended {ended_min}m"
        ended_hrs = ended_min // 60
        return f"ended {ended_hrs}h"

    delta_sec = (event.start - now).total_seconds()
    delta_min = max(0, int(delta_sec / 60.0))
    if delta_min < 60:
        return f"in {delta_min}m"
    delta_hrs = delta_min // 60
    if delta_hrs < 24:
        return f"in {delta_hrs}h"
    delta_days = (event.start.astimezone().date() - now.astimezone().date()).days
    return f"in {delta_days}d"


def resolve_glyph_type(event: CalendarEvent, is_overtime: bool = False) -> GlyphType:
    """Determine the 5x5 procedural micro-glyph type for an event."""
    if is_overtime:
        return "overtime"

    # 1. UTF-8 Emoji scanning across summary and description
    content = f"{event.summary} {event.description}"
    for emoji_char, glyph in EMOJI_GLYPH_MAPPINGS.items():
        if emoji_char in content:
            return glyph

    # 2. Tag brackets (e.g. [focus], [video], [travel])
    title = event.summary.lower()
    for key, mapping in LABEL_ICON_MAPPINGS.items():
        if f"[{key}]" in title:
            return mapping["icon"]

    # 3. Keyword semantic analysis
    if re.search(
        r"\b(travel|flight|transit|airport|train|bus|drive|commute|trip|hotel|reservation)\b",
        title,
    ):
        return "travel"
    if re.search(
        r"\b(run|running|gym|workout|fitness|exercise|walk|swim|cycle|biking)\b",
        title,
    ):
        return "fitness"
    if re.search(
        r"\b(party|birthday|celebration|anniversary|happy hour|ceremony)\b",
        title,
    ):
        return "celebrate"
    if re.search(r"\b(coffee|break|lunch|hydrate|tea|decompress|relax)\b", title):
        return "coffee"
    if re.search(
        r"\b(focus|deep\s+work|dnd|study|coding|code|deepwork|reading)\b",
        title,
    ):
        return "focus"
    if re.search(
        r"\b(zoom|meet|teams|sync|standup|call|meeting|1:1|1-on-1)\b",
        title,
    ):
        return "video"

    # 4. Location meeting URLs
    loc_desc = f"{event.location} {event.description}".lower()
    if any(
        k in loc_desc
        for k in ("meet.google.com", "zoom.us", "teams.microsoft.com", "webex.com")
    ):
        return "video"

    # 5. Calendar color hints
    if event.color:
        c = event.color.upper()
        if c.startswith("#34A853"):
            return "coffee"
        if c.startswith("#A142F4"):
            return "focus"
        if c.startswith("#FBBC05"):
            return "travel"

    return "calendar"


def make_glyph_elements(glyph_type: GlyphType, color: str) -> list[RectangleElement]:
    """Generate 3 procedural rectangle primitives for a 5x5 micro-glyph."""
    if glyph_type == "video":
        # 5x5 Camera Body with Right Lens
        return [
            make_rect("glyph_0", 0, 8, 4, 5, color),
            make_rect("glyph_1", 1, 9, 2, 3, "#000000FF"),
            make_rect("glyph_2", 4, 9, 1, 3, color),
        ]
    elif glyph_type == "coffee":
        # 5x5 Coffee Mug with Handle and Steam Plume
        return [
            make_rect("glyph_0", 0, 9, 4, 4, color),
            make_rect("glyph_1", 1, 8, 2, 1, color),
            make_rect("glyph_2", 4, 9, 1, 2, color),
        ]
    elif glyph_type == "focus":
        # 5x5 Studio Headphones (Overhead arch + Cushioned earcups)
        return [
            make_rect("glyph_0", 0, 10, 5, 3, color),
            make_rect("glyph_1", 1, 8, 3, 2, color),
            make_rect("glyph_2", 2, 9, 1, 4, "#000000FF"),
        ]
    elif glyph_type == "travel":
        # 5x5 Airplane (Fuselage + 5px Wingspan + 3px Tail Stabilizer)
        return [
            make_rect("glyph_0", 2, 8, 1, 5, color),
            make_rect("glyph_1", 0, 10, 5, 1, color),
            make_rect("glyph_2", 1, 12, 3, 1, color),
        ]
    elif glyph_type == "fitness":
        # 5x5 Dumbbell / Activity profile
        return [
            make_rect("glyph_0", 0, 9, 5, 3, color),
            make_rect("glyph_1", 1, 8, 3, 5, color),
            make_rect("glyph_2", 1, 10, 3, 2, "#000000FF"),
        ]
    elif glyph_type == "celebrate":
        # 5x5 Sparkle / Celebration star
        return [
            make_rect("glyph_0", 2, 8, 1, 5, color),
            make_rect("glyph_1", 0, 10, 5, 1, color),
            make_rect("glyph_2", 1, 9, 3, 3, color),
        ]
    elif glyph_type == "overtime":
        # 5x5 Hourglass Vessel
        return [
            make_rect("glyph_0", 0, 8, 5, 5, color),
            make_rect("glyph_1", 1, 9, 3, 3, "#000000FF"),
            make_rect("glyph_2", 2, 10, 1, 1, color),
        ]
    else:
        # Default 5x5 Calendar Frame with Header Cutout
        return [
            make_rect("glyph_0", 0, 8, 5, 5, color),
            make_rect("glyph_1", 1, 9, 3, 1, "#000000FF"),
            make_rect("glyph_2", 2, 11, 1, 1, COLOR_WHITE),
        ]


# --- Exact Font Measurement Engine (Firmware True) ---------------------------

# fmt: off
TINY_FONT_ADVANCES: Final[dict[str, int]] = {
    "0": 4, "1": 4, "2": 4, "3": 4, "4": 4, "5": 4, "6": 4, "7": 4, "8": 4, "9": 4,
    " ": 3, "!": 2, '"': 4, "#": 6, "$": 4, "%": 5, "&": 5, "'": 2, "(": 3, ")": 3,
    "*": 4, "+": 4, ",": 2, "-": 4, ".": 2, "/": 5, ":": 2, ";": 2, "<": 4, "=": 4,
    ">": 4, "?": 4, "@": 4, "[": 3, "\\": 5, "]": 3, "^": 4, "_": 4, "`": 3, "{": 4,
    "|": 2, "}": 4, "~": 5,
    "A": 4, "B": 4, "C": 4, "D": 4, "E": 4, "F": 4, "G": 4, "H": 4, "I": 4, "J": 4,
    "K": 5, "L": 4, "M": 6, "N": 5, "O": 5, "P": 4, "Q": 5, "R": 4, "S": 4, "T": 4,
    "U": 5, "V": 6, "W": 6, "X": 4, "Y": 6, "Z": 5,
    "a": 4, "b": 4, "c": 4, "d": 4, "e": 4, "f": 3, "g": 3, "h": 4, "i": 2, "j": 2,
    "k": 4, "l": 2, "m": 6, "n": 4, "o": 4, "p": 4, "q": 4, "r": 3, "s": 4, "t": 4,
    "u": 4, "v": 4, "w": 6, "x": 4, "y": 4, "z": 4,
}

TINY_FONT_WIDTHS: Final[dict[str, int]] = {
    "0": 3, "1": 3, "2": 3, "3": 3, "4": 3, "5": 3, "6": 3, "7": 3, "8": 3, "9": 3,
    " ": 0, "!": 1, '"': 3, "#": 5, "$": 3, "%": 4, "&": 4, "'": 1, "(": 2, ")": 2,
    "*": 3, "+": 3, ",": 1, "-": 3, ".": 1, "/": 4, ":": 1, ";": 1, "<": 3, "=": 3,
    ">": 3, "?": 3, "@": 3, "[": 2, "\\": 4, "]": 2, "^": 3, "_": 3, "`": 2, "{": 3,
    "|": 1, "}": 3, "~": 4,
    "A": 3, "B": 3, "C": 3, "D": 3, "E": 3, "F": 3, "G": 3, "H": 3, "I": 3, "J": 3,
    "K": 4, "L": 3, "M": 5, "N": 4, "O": 4, "P": 3, "Q": 4, "R": 3, "S": 3, "T": 3,
    "U": 4, "V": 5, "W": 5, "X": 3, "Y": 5, "Z": 4,
    "a": 3, "b": 3, "c": 3, "d": 3, "e": 3, "f": 2, "g": 2, "h": 3, "i": 1, "j": 1,
    "k": 3, "l": 1, "m": 5, "n": 3, "o": 3, "p": 3, "q": 3, "r": 2, "s": 3, "t": 3,
    "u": 3, "v": 3, "w": 5, "x": 3, "y": 3, "z": 3,
}
# fmt: on


def measure_tiny_text(text: str) -> int:
    """Measure exact visual pixel width (lit bounds) rendered by BUSY Bar tiny font."""
    pen = 0
    max_right = 0
    for ch in text:
        w = TINY_FONT_WIDTHS.get(ch, 3)
        adv = TINY_FONT_ADVANCES.get(ch, 4)
        max_right = max(max_right, pen + w)
        pen += adv
    return max(1, max_right)


def measure_badge_width(text: str, left_pad: int = 1, right_pad: int = 1) -> int:
    """Calculate exact width of the background plate so text NEVER overflows."""
    return measure_tiny_text(text) + left_pad + right_pad


def make_pill(
    pill_id: str,
    text: str,
    x: int = 0,
    y: int = 0,
    bg_color: str = COLOR_BLUE_DARK,
    text_color: str = COLOR_WHITE,
    font: FontType = "tiny",
    margin_x: int = 1,
    margin_y: int = 0,
    height: int = 7,
    align: AlignType = "top_left",
) -> tuple[RectangleElement, TextElement]:
    """Generate an encapsulated status pill component (bg rectangle + text).

    Automatically computes the exact rendered pixel width of `text` based on
    firmware font glyph metrics and wraps it with `margin_x` on both sides.
    Returns (bg_rectangle_element, text_element).
    """
    text_w = measure_tiny_text(text)
    plate_w = text_w + (margin_x * 2)

    if align == "top_right":
        rect_x = x - plate_w + 1
    else:
        rect_x = x

    text_x = rect_x + margin_x
    rect_elem = make_rect(f"{pill_id}_bg", rect_x, y, plate_w, height, bg_color)
    text_elem: TextElement = {
        "id": f"{pill_id}_txt",
        "type": "text",
        "x": text_x,
        "y": y + margin_y,
        "font": font,
        "color": text_color,
        "text": text,
        "align": "top_left",
    }
    return rect_elem, text_elem


# --- Aero Horizon Render Functions ------------------------------------------


def render_aero_idle(
    now: datetime, upcoming_events: list[CalendarEvent]
) -> tuple[list[DisplayElement], DisplayExtra]:
    """Render Aero Horizon State 1: Idle Focus Runway with Rolling 3h Radar."""
    next_evt = upcoming_events[0] if upcoming_events else None
    clock_str = fmt_12h(now, include_ampm=True)

    if next_evt:
        now_local = now.astimezone()
        next_start_local = next_evt.start.astimezone()
        if next_start_local.date() == now_local.date():
            free_min = max(0, int((next_evt.start - now).total_seconds() / 60.0))
            runway_str = f"{free_min}m Focus Runway"
        elif next_start_local.date() == now_local.date() + timedelta(days=1):
            runway_str = "Rest of Day Free"
        else:
            runway_str = "Schedule Clear"
        next_pill = format_idle_badge(now, upcoming_events)
        lookahead = get_config().lookahead_count
        title_str = format_idle_marquee(now, upcoming_events, lookahead_count=lookahead)
        glyph_type = resolve_glyph_type(next_evt)
        badge_bg = COLOR_BLUE_DARK
        title_color = resolve_glyph_tint_color(glyph_type)
    else:
        runway_str = "Schedule Clear"
        next_pill = "FREE"
        title_str = "Schedule clear"
        glyph_type = "calendar"
        badge_bg = "#253342FF"
        title_color = COLOR_SKY

    badge_bg_elem, badge_txt_elem = make_pill(
        "badge", next_pill, x=71, y=0, bg_color=badge_bg, align="top_right"
    )

    elements: list[DisplayElement] = [
        # Tier 1: Clean 2-Point Telemetry (Left Clock + Right Status Badge)
        {
            "id": "clock_txt",
            "type": "text",
            "x": 0,
            "y": 0,
            "font": "small",
            "color": COLOR_CLOCK,
            "text": clock_str,
        },
        park_text("mid_txt", text=runway_str),
        badge_bg_elem,
        badge_txt_elem,
        park_text("tele_txt"),
    ]

    # Tier 2: Micro-glyph + Marquee Stream
    elements.extend(make_glyph_elements(glyph_type, badge_bg))
    elements.append(
        {
            "id": "title_txt",
            "type": "text",
            "x": 6,
            "y": 8,
            "width": 66,
            "font": "tiny",
            "color": title_color,
            "text": title_str,
            "scroll_rate": 500,
            "scroll_start_delay": 1200,
        }
    )

    # Tier 3: Rolling 3-Hour Proximity Runway
    elements.extend(
        [
            make_rect("radar_bg", 0, 14, 72, 2, COLOR_RADAR_BG),
            park_rect("tick_25"),
            park_rect("tick_50"),
            park_rect("tick_75"),
            make_rect("bar_fill", 0, 14, 2, 2, COLOR_CLOCK),  # Origin Beacon (NOW)
            park_rect("playhead"),
            park_rect("tight_pip"),
        ]
    )

    # Render upcoming meeting blocks in the rolling runway window
    radar_win_min = get_config().radar_window_minutes
    for i in range(3):
        blk_id = f"radar_blk_{i}"
        if i < len(upcoming_events):
            evt = upcoming_events[i]
            offset_start_sec = (evt.start - now).total_seconds()
            offset_end_sec = (evt.end - now).total_seconds()
            window_sec = radar_win_min * 60.0

            if 0 <= offset_start_sec < window_sec:
                x_start = min(
                    71, max(0, int((offset_start_sec / window_sec) * GRID_WIDTH))
                )
                x_end = min(
                    72,
                    max(
                        x_start + 1,
                        int((offset_end_sec / window_sec) * GRID_WIDTH),
                    ),
                )
                width = max(1, x_end - x_start)
                evt_type = resolve_glyph_type(evt)
                mapping = LABEL_ICON_MAPPINGS.get(evt_type)
                evt_color = mapping["color"] if mapping else COLOR_BLUE
                elements.append(make_rect(blk_id, x_start, 14, width, 2, evt_color))
            else:
                elements.append(park_rect(blk_id))
        else:
            elements.append(park_rect(blk_id))

    extra: DisplayExtra = {"priority": 30, "led_notification_color": None}
    return elements, extra


def render_aero_alert(
    now: datetime, alert: AlertState, upcoming_events: list[CalendarEvent]
) -> tuple[list[DisplayElement], DisplayExtra]:
    """Render Aero Horizon State 2: Approaching Milestone Alert."""
    delta_sec = (alert.event.start - now).total_seconds()
    cd_str = f"{fmt_countdown(delta_sec)} left"

    if alert.checkpoint_minutes == 0:
        badge_str = "STARTING"
    else:
        badge_str = f"IN {alert.checkpoint_minutes} MIN"

    start_str = f"Starts at {fmt_12h(alert.event.start, False)}"
    loc = sanitize_display_text(
        clean_location(alert.event.location, alert.event.description)
    )
    loc_part = f" - {loc}" if loc else ""
    start_fmt = fmt_12h(alert.event.start, True)
    summary = sanitize_display_text(alert.event.summary) or "Event"
    title_str = f"{summary}{loc_part} - [STARTS {start_fmt}]"
    glyph_type = resolve_glyph_type(alert.event)
    title_color = resolve_glyph_tint_color(glyph_type)

    checkpoint_total_sec = max(60.0, float(alert.checkpoint_minutes * 60))
    approach_pct = max(
        0.0, min(1.0, 1.0 - (max(0.0, delta_sec) / checkpoint_total_sec))
    )
    fill_w = max(2, int(approach_pct * GRID_WIDTH))

    badge_bg_elem, badge_txt_elem = make_pill(
        "badge", badge_str, x=0, y=0, bg_color=COLOR_AMBER, text_color="#000000FF"
    )

    elements: list[DisplayElement] = [
        # Tier 1: Alert Banner (Left Amber Plate + Right Urgency Countdown)
        park_text("clock_txt"),
        badge_bg_elem,
        badge_txt_elem,
        park_text("mid_txt", text=start_str),
        {
            "id": "tele_txt",
            "type": "text",
            "x": 71,
            "y": 0,
            "font": "small",
            "align": "top_right",
            "color": COLOR_CLOCK_ALERT,
            "text": cd_str,
        },
    ]

    # Tier 2: Micro-glyph + Marquee
    elements.extend(make_glyph_elements(glyph_type, COLOR_AMBER))
    elements.append(
        {
            "id": "title_txt",
            "type": "text",
            "x": 6,
            "y": 8,
            "width": 66,
            "font": "tiny",
            "color": title_color,
            "text": title_str,
            "scroll_rate": 500,
            "scroll_start_delay": 1200,
        }
    )

    # Tier 3: Approach Rail
    elements.extend(
        [
            make_rect("radar_bg", 0, 14, 72, 2, COLOR_RADAR_BG),
            park_rect("tick_25"),
            park_rect("tick_50"),
            park_rect("tick_75"),
            make_rect("bar_fill", 0, 14, fill_w, 2, COLOR_AMBER),
            make_rect("playhead", min(71, fill_w), 14, 1, 2, COLOR_WHITE),
            park_rect("tight_pip"),
            park_rect("radar_blk_0"),
            park_rect("radar_blk_1"),
            park_rect("radar_blk_2"),
        ]
    )

    extra: DisplayExtra = {"priority": 45, "led_notification_color": COLOR_AMBER}
    return elements, extra


def render_aero_active(
    now: datetime,
    event: CalendarEvent,
    upcoming_events: list[CalendarEvent],
    active_alert: AlertState | None = None,
) -> tuple[list[DisplayElement], DisplayExtra]:
    """Render Aero Horizon Active Meeting lifecycle phases (States 3, 4, 5, 6, 7)."""
    elapsed_sec = (now - event.start).total_seconds()
    total_sec = max(1.0, (event.end - event.start).total_seconds())
    rem_sec = total_sec - elapsed_sec
    is_overtime = rem_sec < 0
    is_wrapup = 0 <= rem_sec <= 180

    # Downstream buffer calculations
    next_evt = next((e for e in upcoming_events if e.start >= event.end), None)
    buffer_min = (
        ((next_evt.start - event.end).total_seconds() / 60.0) if next_evt else 999.0
    )
    is_tight_turn = buffer_min < 5.0

    # Progress bar calculation
    progress_ratio = max(0.0, min(1.0, elapsed_sec / total_sec))
    fill_w = (
        GRID_WIDTH if is_overtime else min(GRID_WIDTH, int(progress_ratio * GRID_WIDTH))
    )
    playhead_x = min(71, fill_w)

    evt_type = resolve_glyph_type(event)
    mapping = LABEL_ICON_MAPPINGS.get(evt_type)
    evt_color = mapping["color"] if mapping else COLOR_RED
    title_color = resolve_glyph_tint_color(evt_type)

    summary = sanitize_display_text(event.summary) or "Event"
    loc = sanitize_display_text(clean_location(event.location, event.description))
    loc_part = f" - {loc}" if loc else ""

    # -------------------------------------------------------------------------
    # STATE 7: Interrupted Alert Overlay (Horizon & Progress Preserved)
    # -------------------------------------------------------------------------
    if active_alert and not active_alert.is_expired(now):
        badge_bg_col = COLOR_AMBER
        badge_txt_col = "#000000FF"
        badge_txt_val = (
            "STARTING"
            if active_alert.checkpoint_minutes == 0
            else f"IN {active_alert.checkpoint_minutes} MIN"
        )
        tele_txt_val = f"{max(1, round(rem_sec / 60.0))}m on call"
        tele_col = COLOR_RED
        alert_loc = sanitize_display_text(
            clean_location(active_alert.event.location, active_alert.event.description)
        )
        alert_loc_part = f" - {alert_loc}" if alert_loc else ""
        alert_summary = sanitize_display_text(active_alert.event.summary) or "Event"
        title_str = (
            f"[NEXT] {alert_summary} starts at"
            f" {fmt_12h(active_alert.event.start, True)}{alert_loc_part}"
        )
        glyph_type = resolve_glyph_type(active_alert.event)
        title_color = resolve_glyph_tint_color(glyph_type)
        bar_col = evt_color
        led_color = COLOR_AMBER
        priority = 60

        badge_bg_elem, badge_txt_elem = make_pill(
            "badge",
            badge_txt_val,
            x=0,
            y=0,
            bg_color=badge_bg_col,
            text_color=badge_txt_col,
        )

        elements: list[DisplayElement] = [
            park_text("clock_txt"),
            badge_bg_elem,
            badge_txt_elem,
            park_text("mid_txt", text=f"Next: {alert_summary[:10]}"),
            {
                "id": "tele_txt",
                "type": "text",
                "x": 71,
                "y": 0,
                "font": "small",
                "align": "top_right",
                "color": tele_col,
                "text": tele_txt_val,
            },
        ]

    # -------------------------------------------------------------------------
    # STATE 6: Dedicated Overtime Phase
    # -------------------------------------------------------------------------
    elif is_overtime:
        overtime_min = int(abs(rem_sec) // 60)
        badge_bg_col = COLOR_RED_DARK
        badge_txt_col = COLOR_WHITE
        badge_txt_val = f"+{overtime_min:02d}m OVER"
        tele_txt_val = f"End: {fmt_12h(event.end, False)}"
        tele_col = COLOR_CLOCK_OVER
        bar_col = COLOR_RED if int(now.timestamp()) % 2 == 0 else COLOR_RED_DIM
        next_gap = (
            int(max(0, (next_evt.start - now).total_seconds() / 60.0))
            if next_evt
            else 0
        )
        if next_evt:
            next_sum = sanitize_display_text(next_evt.summary) or "Event"
            title_str = (
                f"{summary} Overrun (+{overtime_min}m) - Next in"
                f" {next_gap}m: {next_sum}"
            )
        else:
            title_str = (
                f"{summary} Overrun (+{overtime_min}m) - Scheduled end was"
                f" {fmt_12h(event.end, True)}"
            )
        glyph_type = "overtime"
        led_color = COLOR_RED
        priority = 60

        badge_bg_elem, badge_txt_elem = make_pill(
            "badge",
            badge_txt_val,
            x=0,
            y=0,
            bg_color=badge_bg_col,
            text_color=badge_txt_col,
        )

        elements = [
            park_text("clock_txt", text=fmt_12h(now, False)),
            badge_bg_elem,
            badge_txt_elem,
            park_text("mid_txt"),
            {
                "id": "tele_txt",
                "type": "text",
                "x": 71,
                "y": 0,
                "font": "small",
                "align": "top_right",
                "color": tele_col,
                "text": tele_txt_val,
            },
        ]

    # -------------------------------------------------------------------------
    # STATE 5: Active Meeting Phase 3 (Wrap-Up Cue - Last 3 Minutes)
    # -------------------------------------------------------------------------
    elif is_wrapup:
        badge_bg_col = COLOR_AMBER
        badge_txt_col = "#000000FF"
        badge_txt_val = "WRAP UP"
        tele_txt_val = f"{int(rem_sec // 60):02d}:{int(rem_sec % 60):02d} rem"
        tele_col = COLOR_AMBER
        bar_col = COLOR_AMBER
        if is_tight_turn and next_evt:
            next_loc = sanitize_display_text(
                clean_location(next_evt.location, next_evt.description)
            )
            next_loc_part = f" - {next_loc}" if next_loc else ""
            next_sum = sanitize_display_text(next_evt.summary) or "Event"
            title_str = (
                f"[TIGHT TURN] {next_sum} starts in {int(buffer_min)}m!{next_loc_part}"
            )
        elif next_evt:
            next_start_fmt = fmt_12h(next_evt.start, True)
            next_sum = sanitize_display_text(next_evt.summary) or "Event"
            title_str = f"Wrap up soon - [NEXT {next_start_fmt}] {next_sum}"
        else:
            title_str = f"Wrap up soon - [ENDS {fmt_12h(event.end, True)}] {summary}"
        glyph_type = resolve_glyph_type(event)
        title_color = COLOR_AMBER
        led_color = COLOR_AMBER
        priority = 60

        badge_bg_elem, badge_txt_elem = make_pill(
            "badge",
            badge_txt_val,
            x=0,
            y=0,
            bg_color=badge_bg_col,
            text_color=badge_txt_col,
        )

        elements = [
            park_text("clock_txt", text=fmt_12h(now, False)),
            badge_bg_elem,
            badge_txt_elem,
            park_text("mid_txt"),
            {
                "id": "tele_txt",
                "type": "text",
                "x": 71,
                "y": 0,
                "font": "small",
                "align": "top_right",
                "color": tele_col,
                "text": tele_txt_val,
            },
        ]

    # -------------------------------------------------------------------------
    # STATE 3: Active Meeting Phase 1 (First 0..15 Seconds: Title Verification)
    # -------------------------------------------------------------------------
    elif elapsed_sec <= 15:
        badge_bg_col = COLOR_RED_DARK
        badge_txt_col = COLOR_WHITE
        badge_txt_val = "LIVE CALL"
        tele_txt_val = f"{round(total_sec / 60.0)}m call"
        tele_col = COLOR_RED
        bar_col = evt_color
        title_str = f"{summary}{loc_part}"
        glyph_type = resolve_glyph_type(event)
        led_color = COLOR_RED
        priority = 60

        badge_bg_elem, badge_txt_elem = make_pill(
            "badge",
            badge_txt_val,
            x=0,
            y=0,
            bg_color=badge_bg_col,
            text_color=badge_txt_col,
        )

        elements = [
            park_text("clock_txt", text=fmt_12h(event.start, False)),
            badge_bg_elem,
            badge_txt_elem,
            park_text("mid_txt"),
            {
                "id": "tele_txt",
                "type": "text",
                "x": 71,
                "y": 0,
                "font": "small",
                "align": "top_right",
                "color": tele_col,
                "text": tele_txt_val,
            },
        ]

    # -------------------------------------------------------------------------
    # STATE 4: Active Meeting Phase 2 (Underway: Focus Flight Deck & Lookahead)
    # -------------------------------------------------------------------------
    else:
        if evt_type == "focus":
            badge_bg_col = "#7B1FA2FF"
            badge_txt_val = "FOCUS"
            tele_col = COLOR_PURPLE
        elif evt_type == "coffee":
            badge_bg_col = COLOR_GREEN_DARK
            badge_txt_val = "BREAK"
            tele_col = COLOR_CLOCK_BREAK
        else:
            badge_bg_col = COLOR_RED_DARK
            badge_txt_val = "LIVE"
            tele_col = COLOR_RED
        badge_txt_col = COLOR_WHITE
        tele_txt_val = f"{max(1, round(rem_sec / 60.0))}m left"
        bar_col = evt_color
        if next_evt:
            next_loc = sanitize_display_text(
                clean_location(next_evt.location, next_evt.description)
            )
            next_loc_str = f" ({next_loc})" if next_loc else ""
            next_start_fmt = fmt_12h(next_evt.start, True)
            next_sum = sanitize_display_text(next_evt.summary) or "Event"
            title_str = (
                f"{summary}{loc_part} - [NEXT {next_start_fmt}] "
                f"{next_sum}{next_loc_str} (in {int(buffer_min)}m)"
            )
        else:
            title_str = f"{summary}{loc_part}"
        glyph_type = resolve_glyph_type(event)
        title_color = resolve_glyph_tint_color(glyph_type)
        led_color = evt_color
        priority = 60

        badge_bg_elem, badge_txt_elem = make_pill(
            "badge",
            badge_txt_val,
            x=0,
            y=0,
            bg_color=badge_bg_col,
            text_color=badge_txt_col,
        )

        elements = [
            park_text("clock_txt", text=fmt_12h(now, False)),
            badge_bg_elem,
            badge_txt_elem,
            park_text("mid_txt"),
            {
                "id": "tele_txt",
                "type": "text",
                "x": 71,
                "y": 0,
                "font": "small",
                "align": "top_right",
                "color": tele_col,
                "text": tele_txt_val,
            },
        ]

    # Tier 2: Micro-glyph + Marquee Stream
    elements.extend(make_glyph_elements(glyph_type, badge_bg_col))
    elements.append(
        {
            "id": "title_txt",
            "type": "text",
            "x": 6,
            "y": 8,
            "width": 66,
            "font": "tiny",
            "color": title_color,
            "text": title_str,
            "scroll_rate": 500,
            "scroll_start_delay": 1200,
        }
    )

    # Tier 3: Precision Progress Horizon (25%, 50%, 75% Ticks & Live Playhead)
    elements.extend(
        [
            make_rect("radar_bg", 0, 14, 72, 2, COLOR_RADAR_BG),
            make_rect("tick_25", 18, 14, 1, 2, COLOR_TICK),
            make_rect("tick_50", 36, 14, 1, 2, COLOR_TICK),
            make_rect("tick_75", 54, 14, 1, 2, COLOR_TICK),
            make_rect("bar_fill", 0, 14, fill_w, 2, bar_col),
            make_rect("playhead", playhead_x, 14, 1, 2, COLOR_WHITE),
            make_rect(
                "tight_pip",
                70 if (is_tight_turn and (is_wrapup or is_overtime)) else -100,
                14 if (is_tight_turn and (is_wrapup or is_overtime)) else -100,
                2 if (is_tight_turn and (is_wrapup or is_overtime)) else 0,
                2 if (is_tight_turn and (is_wrapup or is_overtime)) else 0,
                COLOR_AMBER,
            ),
            park_rect("radar_blk_0"),
            park_rect("radar_blk_1"),
            park_rect("radar_blk_2"),
        ]
    )

    extra: DisplayExtra = {"priority": priority, "led_notification_color": led_color}
    return elements, extra


def render_aero_breather(
    now: datetime, next_event: CalendarEvent
) -> tuple[list[DisplayElement], DisplayExtra]:
    """Render Aero Horizon State 8: Post-Meeting Breather / Buffer Window."""
    free_sec = (next_event.start - now).total_seconds()
    free_min = max(0, int(free_sec / 60.0))
    cd_str = fmt_countdown(free_sec)

    badge_str = f"{free_min}m BREAK"
    loc = sanitize_display_text(
        clean_location(next_event.location, next_event.description)
    )
    loc_part = f" - {loc}" if loc else ""
    next_sum = sanitize_display_text(next_event.summary) or "Event"
    title_str = (
        "Breather Window - Hydrate & Stretch - [NEXT"
        f" {fmt_12h(next_event.start, True)}] {next_sum}{loc_part}"
    )

    # Draining bar calculation (drains toward next meeting)
    drain_w = max(2, min(72, int((free_sec / (20 * 60.0)) * GRID_WIDTH)))

    badge_bg_elem, badge_txt_elem = make_pill(
        "badge", badge_str, x=0, y=0, bg_color=COLOR_GREEN_DARK, text_color=COLOR_WHITE
    )

    elements: list[DisplayElement] = [
        # Tier 1: Breather Banner (Left Green Plate + Right Countdown)
        park_text("clock_txt", text=fmt_12h(now, False)),
        badge_bg_elem,
        badge_txt_elem,
        park_text("mid_txt"),
        {
            "id": "tele_txt",
            "type": "text",
            "x": 71,
            "y": 0,
            "font": "small",
            "align": "top_right",
            "color": COLOR_CLOCK_BREAK,
            "text": cd_str,
        },
    ]

    # Tier 2: Micro-glyph + Marquee
    elements.extend(make_glyph_elements("coffee", COLOR_GREEN))
    elements.append(
        {
            "id": "title_txt",
            "type": "text",
            "x": 7,
            "y": 8,
            "width": 65,
            "font": "tiny",
            "color": COLOR_CLOCK_BREAK,
            "text": title_str,
            "scroll_rate": 500,
            "scroll_start_delay": 1200,
        }
    )

    # Tier 3: Breather Drain Horizon
    elements.extend(
        [
            make_rect("radar_bg", 0, 14, 72, 2, COLOR_RADAR_BG),
            park_rect("tick_25"),
            park_rect("tick_50"),
            park_rect("tick_75"),
            make_rect("bar_fill", 0, 14, drain_w, 2, COLOR_GREEN),
            make_rect("playhead", min(71, drain_w), 14, 1, 2, COLOR_WHITE),
            park_rect("tight_pip"),
            park_rect("radar_blk_0"),
            park_rect("radar_blk_1"),
            park_rect("radar_blk_2"),
        ]
    )

    extra: DisplayExtra = {"priority": 30, "led_notification_color": COLOR_GREEN}
    return elements, extra


# --- Aero Horizon State: Interactive Agenda Peek (Phase 2) ------------------


def render_aero_peek(
    now: datetime,
    peek: AgendaPeekState,
) -> tuple[list[DisplayElement], DisplayExtra]:
    """Render Aero Horizon State: Interactive Agenda Peek (Rotary Dial Navigation)."""
    if not peek.queue:
        return render_aero_idle(now, [])

    idx = max(0, min(len(peek.queue) - 1, peek.selected_index))
    evt = peek.queue[idx]
    total = len(peek.queue)
    curr_1idx = idx + 1

    pill_str = format_peek_bookmark(now, evt, curr_1idx, total)
    start_str = fmt_12h(evt.start, include_ampm=False)
    end_str = fmt_12h(evt.end, include_ampm=True)
    time_range_str = f"{start_str}-{end_str}"
    delta_str = format_relative_delta(now, evt)

    # Dynamic badge background
    if delta_str == "LIVE":
        badge_bg = COLOR_RED_DARK
        badge_color = COLOR_WHITE
        led_color: str | None = LED_COLOR_MEETING
    else:
        badge_bg = "#253342FF"
        badge_color = COLOR_SKY
        led_color = None

    badge_bg_elem, badge_txt_elem = make_pill(
        "badge",
        f"[{delta_str}]",
        x=71,
        y=0,
        bg_color=badge_bg,
        text_color=badge_color,
        align="top_right",
    )

    glyph_type = resolve_glyph_type(evt)
    title_color = resolve_glyph_tint_color(glyph_type)
    summary_str = sanitize_display_text(evt.summary) or "Event"
    loc_str = sanitize_display_text(clean_location(evt.location, evt.description))
    full_title = f"{summary_str} - {loc_str}" if loc_str else summary_str

    elements: list[DisplayElement] = [
        # Tier 1: Telemetry (Bookmark Index + Time Range on left/mid, Delta on right)
        {
            "id": "clock_txt",
            "type": "text",
            "x": 0,
            "y": 0,
            "font": "tiny",
            "color": COLOR_CLOCK,
            "text": f"[{pill_str}] {time_range_str}",
        },
        park_text("mid_txt"),
        badge_bg_elem,
        badge_txt_elem,
        park_text("tele_txt"),
    ]

    # Tier 2: 5x5 Micro-glyph + Title Marquee
    elements.extend(make_glyph_elements(glyph_type, badge_bg))
    elements.append(
        {
            "id": "title_txt",
            "type": "text",
            "x": 6,
            "y": 8,
            "width": 66,
            "font": "tiny",
            "color": title_color,
            "text": full_title,
            "scroll_rate": 500,
            "scroll_start_delay": 1200,
        }
    )

    # Tier 3: Proportional Stepper Rail
    elements.extend(
        [
            make_rect("radar_bg", 0, 14, 72, 2, COLOR_RADAR_BG),
            park_rect("tick_25"),
            park_rect("tick_50"),
            park_rect("tick_75"),
            park_rect("bar_fill"),
            park_rect("playhead"),
            park_rect("tight_pip"),
            park_rect("radar_blk_0"),
            park_rect("radar_blk_1"),
            park_rect("radar_blk_2"),
        ]
    )

    # Dynamic pips for total queue length
    for k in range(total):
        pip_id = f"stepper_{k}"
        if total > 1:
            x_pip = int(k * 66 / (total - 1)) + 3
        else:
            x_pip = 35

        if k == idx:
            elements.append(make_rect(pip_id, max(0, x_pip - 1), 14, 3, 2, COLOR_WHITE))
        else:
            elements.append(make_rect(pip_id, x_pip, 14, 1, 2, "#404040FF"))

    extra: DisplayExtra = {
        "priority": 50,
        "led_notification_color": led_color,
    }
    return elements, extra


# --- FSM Engine -------------------------------------------------------------


def evaluate_display_with_hardware(
    now: datetime, events: list[CalendarEvent]
) -> tuple[list[DisplayElement], DisplayExtra]:
    """Evaluate time against events, alerts, and tactile hardware state.

    Returns (elements, extra).
    """
    global active_alert, active_peek, fired_checkpoints, manual_sync_flash_until
    cfg = get_config()

    # 1. Prune fired checkpoints for events that have ended
    ended_uids = {e.uid for e in events if e.end <= now}
    fired_checkpoints = {
        (uid, cp) for (uid, cp) in fired_checkpoints if uid not in ended_uids
    }

    # 2. Check for active alert banner expiration
    if active_alert is not None and active_alert.is_expired(now):
        active_alert = None

    # 2.5 Check for active peek overlay expiration
    if active_peek is not None and active_peek.is_expired(now):
        active_peek = None

    # 3. If Agenda Peek is active, render peek display
    if active_peek is not None and not active_peek.is_expired(now):
        elements, extra = render_aero_peek(now, active_peek)
        if manual_sync_flash_until is not None and now < manual_sync_flash_until:
            extra["led_notification_color"] = LED_COLOR_SYNC_ACK
        return elements, extra

    # 4. Identify active event or overtime event
    current_event = None
    for evt in events:
        if evt.start <= now < evt.end:
            current_event = evt
            break
        elif evt.end <= now < evt.end + timedelta(minutes=15):
            # Overtime candidate: only if no new event has started
            if not any(e.start <= now < e.end for e in events):
                current_event = evt
                break

    upcoming_events = [e for e in events if e.start > now and e != current_event]
    upcoming_events.sort(key=lambda e: e.start)

    # 5. Check for upcoming alert triggers
    checkpoints = sorted(cfg.upcoming_alert_sequence_minutes, reverse=True)
    candidate_upcoming = [e for e in events if e.end > now and e != current_event]
    candidate_upcoming.sort(key=lambda e: e.start)

    for evt in candidate_upcoming:
        delta_minutes = (evt.start - now).total_seconds() / 60.0

        eligible_cps = [cp for cp in checkpoints if delta_minutes <= cp]
        if eligible_cps:
            target_cp = min(eligible_cps)

            if evt.start <= now and target_cp != 0:
                continue

            if (
                target_cp == 0
                and (now - evt.start).total_seconds()
                >= cfg.alert_banner_duration_seconds
            ):
                fired_checkpoints.add((evt.uid, 0))
                continue

            for cp in checkpoints:
                if cp > target_cp:
                    fired_checkpoints.add((evt.uid, cp))

            if (evt.uid, target_cp) not in fired_checkpoints:
                fired_checkpoints.add((evt.uid, target_cp))
                active_alert = AlertState(
                    event=evt,
                    checkpoint_minutes=target_cp,
                    expires_at=now
                    + timedelta(seconds=cfg.alert_banner_duration_seconds),
                )
                break
        break

    # 6. Render based on FSM State
    if current_event:
        elements, extra = render_aero_active(
            now, current_event, upcoming_events, active_alert
        )
    elif active_alert and not active_alert.is_expired(now):
        elements, extra = render_aero_alert(now, active_alert, upcoming_events)
    else:
        elements, extra = render_aero_idle(now, upcoming_events)

    if manual_sync_flash_until is not None and now < manual_sync_flash_until:
        extra["led_notification_color"] = LED_COLOR_SYNC_ACK

    return elements, extra


def evaluate_display(
    now: datetime, events: list[CalendarEvent]
) -> list[DisplayElement]:
    """Compatibility wrapper returning elements list."""
    elements, _ = evaluate_display_with_hardware(now, events)
    return elements


# --- Hardware Input Stream & Protobuf Wire Decoder --------------------------

BTN_OK: Final[int] = 0
BTN_BACK: Final[int] = 1
BTN_START: Final[int] = 2

ACTION_PRESS: Final[int] = 0
ACTION_RELEASE: Final[int] = 1


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint from buf at pos."""
    value = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


def _iter_proto_fields(buf: bytes) -> Generator[tuple[int, int, Any], None, None]:
    """Yield (field_number, wire_type, value) for a protobuf buffer."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
            yield field_no, wire, value
        elif wire == 1:
            if pos + 8 > len(buf):
                return
            yield field_no, wire, buf[pos : pos + 8]
            pos += 8
        elif wire == 2:
            n, pos = _read_varint(buf, pos)
            end = pos + n
            if end > len(buf):
                return
            yield field_no, wire, buf[pos:end]
            pos = end
        elif wire == 5:
            if pos + 4 > len(buf):
                return
            yield field_no, wire, buf[pos : pos + 4]
            pos += 4
        else:
            return


def decode_input_events(frame: bytes) -> list[tuple[str, Any]]:
    """Decode binary status frame -> [('button', (id, act)), ('encoder', delta)]."""
    events: list[tuple[str, Any]] = []
    for field_no, wire, update in _iter_proto_fields(frame):
        if field_no != 2 or wire != 2:  # State.updates (StateUpdate)
            continue
        for uf, uw, uv in _iter_proto_fields(update):
            if uf != 11 or uw != 2:  # StateUpdate.input (InputEvent)
                continue
            for ef, ew, ev in _iter_proto_fields(uv):
                if ef == 1 and ew == 2:  # InputEvent.button_event
                    button = action = 0
                    for bf, bw, bv in _iter_proto_fields(ev):
                        if bw == 0 and bf == 1:
                            button = int(bv)
                        elif bw == 0 and bf == 2:
                            action = int(bv)
                    events.append(("button", (button, action)))
                elif ef == 2 and ew == 2:  # InputEvent.encoder_event
                    delta = 0
                    for enc_f, enc_w, enc_v in _iter_proto_fields(ev):
                        if enc_f == 1 and enc_w == 0:
                            val = int(enc_v)
                            if val > 0x7FFFFFFF:
                                val -= 0x100000000
                            delta = val
                    if delta != 0:
                        events.append(("encoder", delta))
    return events


def apply_hardware_input(
    now: datetime,
    event_type: str,
    value: Any,
    events: list[CalendarEvent],
) -> bool:
    """Process hardware input event. Returns True if display state changed."""
    global active_alert, active_peek, manual_sync_flash_until
    cfg = get_config()
    state_changed = False

    if event_type == "button":
        btn_id, action = value
        if action == ACTION_PRESS:
            # 1. Early alert dismissal (START or BACK)
            if btn_id in (BTN_START, BTN_BACK) and active_alert is not None:
                active_alert = None
                state_changed = True

            # 2. Exit peek view (BACK)
            if btn_id == BTN_BACK and active_peek is not None:
                active_peek = None
                state_changed = True

            # 3. Manual instant sync (OK)
            if btn_id == BTN_OK:
                manual_sync_flash_until = now + timedelta(
                    seconds=SYNC_ACK_FLASH_DURATION_SECONDS
                )
                _manual_sync_event.set()
                state_changed = True

    elif event_type == "encoder":
        delta = int(value)
        if delta != 0:
            timeout = timedelta(seconds=PEEK_INACTIVITY_TIMEOUT_SECONDS)
            if active_peek is None or active_peek.is_expired(now):
                queue = build_peek_queue(
                    now, events, lookahead_count=cfg.lookahead_count
                )
                if queue:
                    # Find initial index (active event or first upcoming event)
                    start_idx = 0
                    for idx, evt in enumerate(queue):
                        if evt.start <= now < evt.end:
                            start_idx = idx
                            break
                        elif evt.start > now:
                            start_idx = idx
                            break

                    target_idx = max(0, min(len(queue) - 1, start_idx))
                    active_peek = AgendaPeekState(
                        queue=queue,
                        selected_index=target_idx,
                        expires_at=now + timeout,
                    )
                    state_changed = True
            else:
                new_idx = max(
                    0,
                    min(len(active_peek.queue) - 1, active_peek.selected_index + delta),
                )
                active_peek.selected_index = new_idx
                active_peek.expires_at = now + timeout
                state_changed = True

    return state_changed


class InputListener:
    """Stream button/encoder events off a background asyncio task or busylib."""

    def __init__(self, host: str, token: str | None = None) -> None:
        self.host = host
        self.token = token
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Connect to WebSocket and stream input events with reconnection backoff."""
        # Try busylib AsyncBusyBar first if available
        try:
            from busylib import AsyncBusyBar  # type: ignore[import-untyped]

            kwargs: dict[str, Any] = {"token": self.token} if self.token else {}
            bb = AsyncBusyBar(self.host, **kwargs)
            try:
                async for msg in bb.stream_status_ws():
                    if self._stop.is_set():
                        break
                    if not isinstance(msg, dict):
                        continue
                    for upd in msg.get("updates", []):
                        inp = upd.get("input")
                        if not inp:
                            continue
                        if "button_event" in inp:
                            be = inp["button_event"]
                            btn_name = str(be.get("button", "OK")).upper()
                            btn_id = (
                                BTN_START
                                if btn_name == "START"
                                else BTN_BACK
                                if btn_name == "BACK"
                                else BTN_OK
                            )
                            action_name = str(be.get("action", "PRESS")).upper()
                            action_id = (
                                ACTION_RELEASE
                                if action_name == "RELEASE"
                                else ACTION_PRESS
                            )
                            self.queue.put_nowait(("button", (btn_id, action_id)))
                        if "encoder_event" in inp:
                            delta = int(inp["encoder_event"].get("delta", 0))
                            if delta != 0:
                                self.queue.put_nowait(("encoder", delta))
                return
            except Exception:
                pass
            finally:
                try:
                    await bb.aclose()
                except Exception:
                    pass
        except ImportError:
            pass

        # Raw websockets client fallback
        try:
            import websockets
        except ImportError:
            print(
                "[input] neither busylib nor websockets available; hardware controls"
                " disabled",
                flush=True,
            )
            return

        raw_host = self.host.rstrip("/")
        if "://" not in raw_host:
            raw_host = "http://" + raw_host
        import urllib.parse

        parsed = urllib.parse.urlparse(raw_host)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/") + "/api/status/ws"
        url = urllib.parse.urlunparse((scheme, parsed.netloc, path, "", "", ""))

        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, open_timeout=5, ping_interval=20
                ) as ws:
                    await ws.send(json.dumps({"enable": True}))
                    backoff = 1.0
                    print(f"[input] connected to {url}", flush=True)
                    async for msg in ws:
                        if self._stop.is_set():
                            break
                        if isinstance(msg, (bytes, bytearray)):
                            for evt in decode_input_events(bytes(msg)):
                                self.queue.put_nowait(evt)
            except asyncio.CancelledError:
                break
            except Exception:
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 1.5)


# --- Main Loop (AsyncIO) ----------------------------------------------------

_events: list[CalendarEvent] = []


def _update_events(new_events: list[CalendarEvent]) -> None:
    global _events
    _events = list(new_events)


def _get_events() -> list[CalendarEvent]:
    return list(_events)


async def _sync_worker(client: httpx.AsyncClient, ical_url: AnyHttpUrl) -> None:
    """Background coroutine: periodically fetch calendar and update shared store."""
    while True:
        try:
            events = await fetch_events(client, ical_url)
            if events is not None:
                _update_events(events)
                print(f"[sync] fetched {len(events)} event(s)", flush=True)
        except Exception as e:
            print(f"[sync] error: {e}", file=sys.stderr, flush=True)

        poll_interval = get_config().calendar_poll_interval_seconds
        try:
            await asyncio.wait_for(_manual_sync_event.wait(), timeout=poll_interval)
            _manual_sync_event.clear()
            print("[sync] manual sync requested via OK button", flush=True)
        except asyncio.TimeoutError:
            pass


_last_payload_str: str | None = None


async def _display_loop(
    client: httpx.AsyncClient, input_listener: InputListener | None = None
) -> None:
    """Display loop coroutine processing inputs and 1 Hz refreshes."""
    global _last_payload_str
    while True:
        now = datetime.now(timezone.utc)
        events = _get_events()

        if input_listener is not None:
            while not input_listener.queue.empty():
                evt_type, val = input_listener.queue.get_nowait()
                apply_hardware_input(now, evt_type, val, events)

        elements, extra = evaluate_display_with_hardware(now, events)

        import json

        payload_str = json.dumps({"elements": elements, "extra": extra}, sort_keys=True)

        if payload_str != _last_payload_str:
            try:
                await draw(client, elements, **extra)
                _last_payload_str = payload_str
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    try:
                        await client.delete(f"{BASE}/api/display/draw", timeout=2.0)
                        await draw(client, elements, **extra)
                        _last_payload_str = payload_str
                    except Exception:
                        pass
                else:
                    print(
                        f"[draw] HTTP {e.response.status_code}: {e.response.text}",
                        file=sys.stderr,
                    )
            except httpx.HTTPError as e:
                print(f"[draw] network error: {e}", file=sys.stderr, flush=True)

        sleep_duration = (
            0.1
            if (active_peek or (input_listener and not input_listener.queue.empty()))
            else 0.5
        )
        await asyncio.sleep(sleep_duration)


async def async_main() -> None:
    cfg = get_config()
    input_listener = InputListener(cfg.host)
    input_task = asyncio.create_task(input_listener.run())

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            if cfg.demo:
                _update_events(demo_events())
                print(
                    f"{APP} → {BASE}  [Aero Horizon demo mode]  (Ctrl-C to stop)",
                    flush=True,
                )
                await _display_loop(client, input_listener)
            elif cfg.ical_url:
                print(f"{APP} → {BASE}  (Ctrl-C to stop)", flush=True)
                try:
                    initial_events = await fetch_events(client, cfg.ical_url)
                    if initial_events is not None:
                        _update_events(initial_events)
                except Exception as e:
                    print(
                        f"[sync] initial fetch failed: {e}", file=sys.stderr, flush=True
                    )

                await asyncio.gather(
                    _display_loop(client, input_listener),
                    _sync_worker(client, cfg.ical_url),
                )
            else:
                print(
                    f"{APP} → {BASE}  [no calendar source, use --ical-url, --demo, or"
                    " GCAL_GLANCE_ICAL_URL]",
                    flush=True,
                )
                print("Showing Aero Horizon idle focus runway display.", flush=True)
                await _display_loop(client, input_listener)
        finally:
            input_listener.stop()
            input_task.cancel()
            try:
                await input_task
            except asyncio.CancelledError:
                pass


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
