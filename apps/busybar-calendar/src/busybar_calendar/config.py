"""
Configuration, read from a TOML file.

The reminder and status settings are the ones the Google Calendar
integration offers per calendar, so a calendar here behaves the way a Google
calendar does on the same bar. They layer: `[defaults]` applies everywhere, a
source can override it, and a single calendar inside a source can override
that again - which is also the account-level default that BUSY Cloud does not
have yet (busy-app/busybar-firmware#1083).
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

# The 72x16 statuses the firmware ships, under shared/animations/<name>_72x16.anim.
STOCK_STATUSES = (
    "back_soon",
    "booked",
    "chill_time",
    "coding",
    "dnd",
    "flow",
    "keep_out",
    "low_social_battery",
    "lunch",
    "meeting",
    "on_air",
    "on_call",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReminderSettings(_Strict):
    """A countdown shown before the event starts."""

    enabled: bool = Field(default=True, description="Show a reminder before events.")
    minutes_before: int = Field(
        default=5, ge=1, le=24 * 60, description="How long before the start it appears."
    )
    sound: bool = Field(default=True, description="Play the reminder sound as it appears.")
    show_name: bool = Field(
        default=True, description='Show the event\'s title; off, it says "Next event".'
    )


class StatusSettings(_Strict):
    """What the bar shows while the event is on."""

    enabled: bool = Field(default=True, description="Show the event while it is on.")
    show: str = Field(
        default="name",
        description="`name`: the red calendar, the title, a countdown and BUSY. Or one of "
        "the bar's own statuses: " + ", ".join(f"`{s}`" for s in STOCK_STATUSES) + ".",
    )
    sound: bool = Field(default=True, description="Play the event sound as it starts.")
    tentative: bool = Field(
        default=True,
        description="Events you marked tentative show as busy too. Off, they keep their "
        "reminder only.",
    )

    @field_validator("show")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value != "name" and value not in STOCK_STATUSES:
            raise ValueError(f"show must be 'name' or one of {', '.join(STOCK_STATUSES)}")
        return value


class Overrides(_Strict):
    """Partial reminder/status settings; unset fields fall through to the level above."""

    reminder: dict[str, Any] = Field(default_factory=dict)
    status: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reminder", "status")
    @classmethod
    def _valid(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        """Check the values here, where an error can say which section it is in."""
        model = ReminderSettings if info.field_name == "reminder" else StatusSettings
        try:
            model(**value)
        except ValidationError as error:
            problems = (
                f"{'.'.join(map(str, p['loc']))}: {p['msg'].removeprefix('Value error, ')}"
                for p in error.errors()
            )
            raise ValueError("; ".join(problems)) from None
        return value


def _merged(layers: Iterable[Overrides]) -> tuple[ReminderSettings, StatusSettings]:
    reminder: dict[str, Any] = {}
    status: dict[str, Any] = {}
    for layer in layers:
        reminder |= layer.reminder
        status |= layer.status
    return ReminderSettings(**reminder), StatusSettings(**status)


class TitleRule(Overrides):
    """Reminder/status overrides for the events whose title matches, from any calendar."""

    match: list[str] = Field(
        min_length=1,
        description="The titles it applies to: one or a list; `*` and `?` are wildcards, "
        "and case does not matter.",
    )

    @field_validator("match", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    def matching(self, title: str) -> str | None:
        """The first of `match` that names this title, if one does."""
        title = title.casefold()
        return next((p for p in self.match if fnmatchcase(title, p.casefold())), None)


class Secret(_Strict):
    """
    A credential given inline, by environment variable, or by a command.

    The command form is argv, run without a shell - for example the macOS
    keychain: ["security", "find-generic-password", "-s", "busybar", "-w"].
    """

    value: str | None = None
    env: str | None = None
    command: list[str] | None = None

    def resolve(self) -> str | None:
        if self.value is not None:
            return self.value
        if self.env is not None:
            found = os.environ.get(self.env)
            if found is None:
                raise ConfigError(f"environment variable {self.env} is not set")
            return found
        if self.command:
            result = subprocess.run(self.command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise ConfigError(f"{self.command[0]} exited with {result.returncode}")
            return result.stdout.strip()
        return None


def _secret(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Fold `key`, `key_env` and `key_command` into one Secret."""
    secret = {
        "value": data.pop(key, None),
        "env": data.pop(f"{key}_env", None),
        "command": data.pop(f"{key}_command", None),
    }
    given = [name for name, value in secret.items() if value is not None]
    if len(given) > 1:
        raise ValueError(f"give only one of {key}, {key}_env, {key}_command")
    data[key] = secret
    return data


class BarConfig(_Strict):
    address: str | None = Field(
        default=None,
        description="The bar on your network (`ip[:port]`; `10.0.4.20` over USB). Leave it "
        "out to reach the bar through BUSY Cloud.",
    )
    token: Secret = Field(
        default_factory=Secret,
        description="With an address, the bar's HTTP API password (32 characters at most); "
        'without, a BUSY Cloud token with the "BUSY Bar" access scope.',
    )
    priority: int = Field(
        default=91,
        ge=1,
        le=100,
        description="Draw priority: 91 is above a running BUSY session (90), like the bar's "
        "own calendar events.",
    )
    application_name: str = Field(
        default="busybar-calendar",
        max_length=32,
        description="The name drawings are made under; also the app's id in the community gallery.",
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_token(cls, data: Any) -> Any:
        return _secret(dict(data), "token") if isinstance(data, dict) else data


class DisplayConfig(_Strict):
    title_font: Literal["tiny", "small", "global"] = Field(
        default="small",
        description="`small` fits the title and countdown on two lines; `global` draws "
        "Cyrillic, CJK and accents, on one line beside the countdown.",
    )
    transliterate: bool = Field(
        default=True,
        description="Fold titles to ASCII for the bitmap fonts (`Планування` -> "
        "`Planuvannya`). Not used with `global`.",
    )
    reminder_sound: str = Field(
        default="shared/sounds/calendar_reminder_ends.snd",
        description="The stock sound a reminder plays.",
    )
    event_sound: str = Field(
        default="shared/sounds/calendar_event_starts.snd",
        description="The stock sound an event's start plays.",
    )
    reminder_icon: Literal["animation", "red_calendar"] = Field(
        default="red_calendar",
        description="The icon before an event: the red calendar, or the bar's own animation.",
    )
    event_icon: Literal["animation", "red_calendar"] = Field(
        default="red_calendar",
        description="The icon during an event: the red calendar, or the bar's own animation.",
    )
    busy_label: str = Field(
        default="BUSY",
        max_length=8,
        description='The word beside the countdown during an event; `""` for none. Not drawn '
        "with the `global` font.",
    )
    busy_color: str = Field(
        default="#FF0000FF", description="Its colour, `#RRGGBB` or `#RRGGBBAA`."
    )
    tentative_yellow: bool = Field(
        default=True,
        description="Draw events you marked tentative in yellow - the calendar icon and the "
        "label - so they differ from accepted ones. Off, they look the same. The icon is "
        "yellow only as the `red_calendar`; the bar's own animation has no yellow version.",
    )

    @field_validator("busy_label")
    @classmethod
    def _ascii_label(cls, value: str) -> str:
        if not all(" " <= c <= "~" for c in value):
            raise ValueError("busy_label must be printable ASCII")
        return value

    @field_validator("busy_color")
    @classmethod
    def _rgba(cls, value: str) -> str:
        digits = value.removeprefix("#")
        if len(digits) not in (6, 8) or any(c not in "0123456789abcdefABCDEF" for c in digits):
            raise ValueError("busy_color must be #RRGGBB or #RRGGBBAA")
        return f"#{digits.upper()}" + ("FF" if len(digits) == 6 else "")


class SyncConfig(_Strict):
    poll_minutes: int = Field(default=5, ge=1, description="How often calendars are read.")
    lookahead_hours: int = Field(
        default=24, ge=1, le=24 * 14, description="How far ahead events are read."
    )
    skip_free: bool = Field(
        default=True, description="Leave out events marked free (`TRANSP:TRANSPARENT`)."
    )
    timezone: str | None = Field(
        default=None,
        description="For event times that carry no timezone, e.g. `Europe/Kyiv`. Default: "
        "this machine's.",
    )

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError(
                    f"{value!r} is not a timezone name; use one like 'Europe/Kyiv'"
                ) from error
        return value


class SourceConfig(Overrides):
    name: str = Field(description="What logs and overrides call this source; unique.")
    type: Literal["caldav", "ics"] = Field(
        description="`caldav` for an account, `ics` for a published link (one calendar)."
    )
    url: str = Field(
        description="CalDAV: the server, or one calendar's URL. ICS: the link; `webcal://` works."
    )
    username: str | None = Field(default=None, description="Usually the email address.")
    password: Secret = Field(
        default_factory=Secret,
        description="iCloud and Fastmail need an app password.",
    )
    auth: Literal["basic", "digest"] = Field(
        default="basic", description="HTTP authentication; a few servers want `digest`."
    )
    emails: list[str] = Field(
        default_factory=list,
        description="Your attendee address(es), to read your response to invitations. "
        "Default: the username, when it is an email address.",
    )
    include: list[str] = Field(
        default_factory=list,
        description="CalDAV: only these calendars, by name, id or URL; `*` and `?` are "
        "wildcards. Default: all. The one listed last wins an overlap.",
    )
    exclude: list[str] = Field(
        default_factory=list, description="CalDAV: leave these calendars out, after include."
    )
    calendars: dict[str, Overrides] = Field(
        default_factory=dict,
        description="Per-calendar reminder and status overrides, keyed by name or id.",
    )

    @model_validator(mode="before")
    @classmethod
    def _fold_password(cls, data: Any) -> Any:
        return _secret(dict(data), "password") if isinstance(data, dict) else data

    @model_validator(mode="after")
    def _defaults(self) -> SourceConfig:
        if self.url.startswith("webcal://"):
            self.url = "https://" + self.url.removeprefix("webcal://")
        if not self.emails and self.username and "@" in self.username:
            self.emails = [self.username]
        if self.type == "ics" and (self.include or self.exclude or self.calendars):
            # An ICS feed is one calendar, named after its source.
            raise ValueError(
                f"source {self.name!r}: include/exclude/calendars apply to caldav only"
            )
        return self


class Config(_Strict):
    bar: BarConfig = Field(default_factory=BarConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    defaults: Overrides = Field(default_factory=Overrides)
    # Empty only for the gallery app's --demo and --test, which need no calendar.
    sources: list[SourceConfig] = Field(default_factory=list)
    titles: list[TitleRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Config:
        names = [source.name for source in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        return self

    def settings_for(
        self, source: str, calendar: str, calendar_id: str = "", title: str = ""
    ) -> tuple[ReminderSettings, StatusSettings]:
        """
        The effective settings of one event: defaults, then the source, then the
        calendar's overrides by name, then by id, then every `[[titles]]` rule
        that matches the title, in order.
        """
        layers: list[Overrides] = [self.defaults]
        src = next((s for s in self.sources if s.name == source), None)
        if src is not None:  # an event of no configured source (a demo) gets the defaults
            layers.append(src)
            layers += [src.calendars[k] for k in (calendar, calendar_id) if k in src.calendars]
        layers += [rule for rule in self.titles if rule.matching(title) is not None]
        return _merged(layers)

    def title_patterns(self, title: str) -> list[str]:
        """The `[[titles]]` patterns that name this title, one per rule that applies."""
        return [p for rule in self.titles if (p := rule.matching(title)) is not None]


class ConfigError(Exception):
    pass


CONFIG_ENV = "BUSYBAR_CALENDAR_CONFIG"


def search_paths() -> list[Path]:
    """Where a config is looked for when none is named: here, then the user's config dir."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return [Path("config.toml"), config_home / "busybar-calendar" / "config.toml"]


def find(explicit: str | Path | None = None) -> Path:
    """The config file: the one named, else $BUSYBAR_CALENDAR_CONFIG, else the first that exists."""
    if explicit:
        return Path(explicit)
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV])
    for path in search_paths():
        if path.is_file():
            return path
    tried = ", ".join(str(path) for path in search_paths())
    raise ConfigError(
        f"no config yet (looked for {tried}): run `busybar-calendar init` to write one, "
        f"or name one with -c or ${CONFIG_ENV}"
    )


def load(path: str | Path) -> Config:
    if not Path(path).is_file():
        raise ConfigError(f"{path}: no such config; `busybar-calendar init` writes one")
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    config = Config.model_validate(data)
    if not config.sources:
        raise ConfigError(f"{path}: no [[sources]]: add a CalDAV account or an ICS feed")
    return config
