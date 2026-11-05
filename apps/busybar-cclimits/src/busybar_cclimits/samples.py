"""Made-up limits for `test`, `demo`, `snapshot` and the previews: one per state worth seeing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import DisplayConfig
from .limits import Feed, Window

HOUR = 3600
DAY = 24 * HOUR


@dataclass(frozen=True)
class Sample:
    title: str
    five: float | None  # percent used; None: the window has reset
    five_left: float = 1 * HOUR + 42 * 60 + 10
    week: float = 18
    week_left: float = 3 * DAY + 4 * HOUR
    spend: float | None = None
    # Further windows: name -> (percent used, seconds to the reset).
    extra: dict[str, tuple[float, float]] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)

    def feed(self, now: float) -> Feed:
        windows = {
            "five_hour": Window(
                self.five or 0.0,
                int(now + self.five_left) if self.five is not None else int(now) - 1,
            ),
            "seven_day": Window(self.week, int(now + self.week_left)),
        }
        if self.spend is not None:
            windows["spend_limit"] = Window(
                self.spend, int(now + 9 * DAY), 314.12, 500.0, "monthly"
            )
        for name, (used, left) in self.extra.items():
            windows[name] = Window(used, int(now + left))
        return Feed(windows, updated_at=now, seen_at=now)

    def display_config(self, base: DisplayConfig | None = None) -> DisplayConfig:
        base = base or DisplayConfig()
        return DisplayConfig.model_validate(base.model_dump() | self.display)


SAMPLES: dict[str, Sample] = {
    "default": Sample("The default: 5-hour with a countdown, the week in percent", 42),
    "warn": Sample("Past 75%: yellow", 78, five_left=47 * 60 + 5, week=61),
    "critical": Sample("Past 90%: red", 96, five_left=12 * 60 + 40, week=91),
    "reset": Sample("The 5-hour window has reset", None, week=22),
    "week-time": Sample(
        "Both rows with the time to their reset",
        42,
        display={"rows": [{"window": "five_hour"}, {"window": "seven_day", "text": "time"}]},
    ),
    "percent": Sample(
        "Both rows in percent",
        42,
        display={
            "rows": [
                {"window": "five_hour", "text": "percent"},
                {"window": "seven_day", "text": "percent"},
            ]
        },
    ),
    "single": Sample(
        "One row: the 5-hour window alone",
        64,
        five_left=3 * HOUR + 5 * 60,
        display={"rows": [{"window": "five_hour"}]},
    ),
    "bars-only": Sample(
        "Bars only, no text",
        42,
        display={
            "rows": [
                {"window": "five_hour", "text": "none"},
                {"window": "seven_day", "text": "none"},
            ]
        },
    ),
    "spend": Sample(
        "A Claude apps gateway spend limit under the 5-hour window",
        42,
        spend=63,
        display={"rows": [{"window": "five_hour"}, {"window": "spend_limit", "text": "percent"}]},
    ),
    "fable": Sample(
        "Three rows: the 5-hour window, the week and Fable",
        42,
        extra={"seven_day_fable": (57, 2 * DAY + 6 * HOUR)},
        display={
            "rows": [
                {"window": "five_hour"},
                {"window": "seven_day", "text": "percent"},
                {"window": "seven_day_fable", "text": "percent"},
            ]
        },
    ),
    "fable-near": Sample(
        "Three rows, Fable nearly out and resetting within hours",
        61,
        five_left=3 * HOUR + 10 * 60,
        week=47,
        extra={"seven_day_fable": (93, 6 * HOUR + 12 * 60)},
        display={
            "rows": [
                {"window": "five_hour", "text": "percent"},
                {"window": "seven_day", "text": "percent"},
                {"window": "seven_day_fable"},
            ]
        },
    ),
    "models": Sample(
        "The week with Opus and Sonnet, which have limits of their own",
        42,
        week=38,
        extra={
            "seven_day_opus": (71, 2 * DAY + 3 * HOUR),
            "seven_day_sonnet": (24, 2 * DAY + 3 * HOUR),
        },
        display={
            "rows": [
                {"window": "seven_day", "text": "percent"},
                {"window": "seven_day_opus", "text": "percent"},
                {"window": "seven_day_sonnet", "text": "percent"},
            ]
        },
    ),
}

# `demo`: the default layout filling up, step by step.
DEMO_STEPS = [(8, 3), (27, 11), (42, 18), (61, 24), (78, 31), (93, 38), (100, 40)]
