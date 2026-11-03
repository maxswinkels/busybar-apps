"""
The configuration reference, generated from the models in config.py.

    python -m busybar_calendar.reference > docs/configuration.md

A test compares docs/configuration.md with this output, so the reference
cannot fall behind the code.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from .config import (
    BarConfig,
    DisplayConfig,
    ReminderSettings,
    Secret,
    SourceConfig,
    StatusSettings,
    SyncConfig,
    TitleRule,
)

INTRO = """\
# Configuration reference

The config is a TOML file. `busybar-calendar init` writes one; it is found
in `~/.config/busybar-calendar/config.toml`, `./config.toml`, or wherever
`$BUSYBAR_CALENDAR_CONFIG` or `-c` points. Only `[[sources]]` is required;
everything else has the default shown.

Secrets - `token` and `password` - take one of three forms. Prefer the last
two, which keep the secret out of the file:

```toml
password = "..."                                  # inline
password_env = "CALDAV_PASSWORD"                  # an environment variable
password_command = ["security", "find-generic-password", "-s", "my-calendar", "-w"]
```

The command is run without a shell, and its output is the secret.

*Generated from the code by `python -m busybar_calendar.reference`; edit the
descriptions in `config.py`, not this file.*
"""

TITLES = """\
## `[[titles]]`

Settings for the events whose title matches, from whichever calendar: a
rule's `reminder` and `status` go on top of the calendar's, and a later rule
on top of an earlier one. To keep some events off the bar, and show lunch as
the bar's own status:

```toml
[[titles]]
match = ["Focus time", "Commute*"]
reminder.enabled = false
status.enabled = false

[[titles]]
match = "Lunch*"
reminder.enabled = false
status.show = "lunch"
```

`busybar-calendar upcoming` shows what each event ends up with.

"""


def _default(field: FieldInfo) -> str:
    if field.is_required():
        return "**required**"
    if field.default_factory is not None:
        made = field.default_factory()
        if isinstance(made, Secret):
            return "none"
        return "`[]`" if isinstance(made, list) else "`{}`"
    value = field.default
    if value is None:
        return "none"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, str):
        return f'`"{value}"`'
    return f"`{value}`"


def _key(name: str, field: FieldInfo) -> str:
    if field.annotation is Secret or field.annotation == "Secret":
        return f"`{name}` / `{name}_env` / `{name}_command`"
    return f"`{name}`"


def table(model: type[BaseModel], skip: tuple[str, ...] = ()) -> str:
    rows = ["| Key | Default | |", "| --- | --- | --- |"]
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        text = (field.description or "").replace("|", "\\|")
        rows.append(f"| {_key(name, field)} | {_default(field)} | {text} |")
    return "\n".join(rows)


def markdown() -> str:
    sections = [
        INTRO,
        "## `[bar]`\n\nHow to reach the bar.\n\n" + table(BarConfig),
        "## `[sync]`\n\nHow the calendars are read.\n\n" + table(SyncConfig),
        "## `[display]`\n\nWhat the bar shows.\n\n" + table(DisplayConfig),
        "## `[defaults.reminder]`\n\nThe countdown before an event: the settings the bar's "
        "Google Calendar integration has per calendar. They layer: `[defaults.reminder]`, "
        "then a source's `[sources.reminder]`, then one calendar's "
        '`[sources.calendars."<name or id>".reminder]`, then the `[[titles]]` rules that '
        "match the event's title.\n\n" + table(ReminderSettings),
        "## `[defaults.status]`\n\nWhat the bar shows while an event is on. It layers the "
        "same way: `[defaults.status]`, `[sources.status]`, "
        '`[sources.calendars."<name or id>".status]`, `[[titles]]`.\n\n' + table(StatusSettings),
        "## `[[sources]]`\n\nOne CalDAV account or one ICS link each; at least one.\n\n"
        + table(SourceConfig, skip=("reminder", "status")),
        TITLES + table(TitleRule, skip=("reminder", "status")),
    ]
    return "\n\n".join(section.rstrip() for section in sections) + "\n"


if __name__ == "__main__":
    print(markdown(), end="")
