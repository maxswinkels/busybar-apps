"""
What the bar should show at a given moment, and when that next changes.

Each event can put up two screens: a reminder from `minutes_before` until it
starts, then its status until it ends. When several want the bar at once:

1. the event you are surest to attend wins: accepted, then tentative, then
   not answered yet;
2. between those, the newest event, as in the Google Calendar integration -
   so a reminder for your next meeting replaces the status of the one still
   running;
3. between events starting together, the calendar configured last.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .config import ReminderSettings, StatusSettings
from .events import RESPONSE_RANK, Event

SettingsFor = Callable[[Event], tuple[ReminderSettings, StatusSettings]]


@dataclass(frozen=True)
class Screen:
    kind: Literal["reminder", "status"]
    event: Event
    reminder: ReminderSettings
    status: StatusSettings

    @property
    def until(self) -> datetime:
        return self.event.start if self.kind == "reminder" else self.event.end

    @property
    def key(self) -> tuple:
        """Equal keys draw the same thing, so nothing needs redrawing."""
        return (
            self.kind,
            self.event.key,
            self.event.title,
            self.event.end,
            self.event.response,
            self.reminder,
            self.status,
        )


def shows_status(event: Event, status: StatusSettings) -> bool:
    """Whether the event puts its status up while it is on."""
    return status.enabled and (status.tentative or event.response != "tentative")


def _reminder_from(event: Event, reminder: ReminderSettings) -> datetime:
    return event.start - timedelta(minutes=reminder.minutes_before)


def choose(now: datetime, events: Iterable[Event], settings_for: SettingsFor) -> Screen | None:
    best: Screen | None = None
    best_rank: tuple | None = None
    for event in events:
        reminder, status = settings_for(event)
        if shows_status(event, status) and event.start <= now < event.end:
            screen = Screen("status", event, reminder, status)
        elif reminder.enabled and _reminder_from(event, reminder) <= now < event.start:
            screen = Screen("reminder", event, reminder, status)
        else:
            continue
        rank = (RESPONSE_RANK[event.response], event.start, event.calendar.order, event.key)
        if best_rank is None or rank > best_rank:
            best, best_rank = screen, rank
    return best


def next_change(
    now: datetime, events: Iterable[Event], settings_for: SettingsFor
) -> datetime | None:
    """The first moment after `now` at which `choose` could answer differently."""
    upcoming = []
    for event in events:
        reminder, _ = settings_for(event)
        for moment in (_reminder_from(event, reminder), event.start, event.end):
            if moment > now:
                upcoming.append(moment)
    return min(upcoming, default=None)


def upcoming(
    now: datetime, events: Iterable[Event], settings_for: SettingsFor, horizon: int = 500
) -> tuple[datetime, Screen] | None:
    """The next moment a screen opens or gives way to another, and that screen."""
    events = list(events)
    current = choose(now, events, settings_for)
    moment: datetime | None = now
    for _ in range(horizon):
        moment = next_change(moment, events, settings_for)
        if moment is None:
            return None
        screen = choose(moment, events, settings_for)
        if screen is not None and (current is None or screen.key != current.key):
            return moment, screen
    return None
