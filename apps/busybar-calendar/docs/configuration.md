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

## `[bar]`

How to reach the bar.

| Key | Default | |
| --- | --- | --- |
| `address` | none | The bar on your network (`ip[:port]`; `10.0.4.20` over USB). Leave it out to reach the bar through BUSY Cloud. |
| `token` / `token_env` / `token_command` | none | With an address, the bar's HTTP API password (32 characters at most); without, a BUSY Cloud token with the "BUSY Bar" access scope. |
| `priority` | `91` | Draw priority: 91 is above a running BUSY session (90), like the bar's own calendar events. |
| `application_name` | `"busybar-calendar"` | The name drawings are made under; also the app's id in the community gallery. |

## `[sync]`

How the calendars are read.

| Key | Default | |
| --- | --- | --- |
| `poll_minutes` | `5` | How often calendars are read. |
| `lookahead_hours` | `24` | How far ahead events are read. |
| `skip_free` | `true` | Leave out events marked free (`TRANSP:TRANSPARENT`). |
| `timezone` | none | For event times that carry no timezone, e.g. `Europe/Kyiv`. Default: this machine's. |

## `[display]`

What the bar shows.

| Key | Default | |
| --- | --- | --- |
| `title_font` | `"small"` | `small` fits the title and countdown on two lines; `global` draws Cyrillic, CJK and accents, on one line beside the countdown. |
| `transliterate` | `true` | Fold titles to ASCII for the bitmap fonts (`Планування` -> `Planuvannya`). Not used with `global`. |
| `reminder_sound` | `"shared/sounds/calendar_reminder_ends.snd"` | The stock sound a reminder plays. |
| `event_sound` | `"shared/sounds/calendar_event_starts.snd"` | The stock sound an event's start plays. |
| `reminder_icon` | `"red_calendar"` | The icon before an event: the red calendar, or the bar's own animation. |
| `event_icon` | `"red_calendar"` | The icon during an event: the red calendar, or the bar's own animation. |
| `busy_label` | `"BUSY"` | The word beside the countdown during an event; `""` for none. Not drawn with the `global` font. |
| `busy_color` | `"#FF0000FF"` | Its colour, `#RRGGBB` or `#RRGGBBAA`. |
| `tentative_yellow` | `true` | Draw events you marked tentative in yellow - the calendar icon and the label - so they differ from accepted ones. Off, they look the same. The icon is yellow only as the `red_calendar`; the bar's own animation has no yellow version. |

## `[defaults.reminder]`

The countdown before an event: the settings the bar's Google Calendar integration has per calendar. They layer: `[defaults.reminder]`, then a source's `[sources.reminder]`, then one calendar's `[sources.calendars."<name or id>".reminder]`, then the `[[titles]]` rules that match the event's title.

| Key | Default | |
| --- | --- | --- |
| `enabled` | `true` | Show a reminder before events. |
| `minutes_before` | `5` | How long before the start it appears. |
| `sound` | `true` | Play the reminder sound as it appears. |
| `show_name` | `true` | Show the event's title; off, it says "Next event". |

## `[defaults.status]`

What the bar shows while an event is on. It layers the same way: `[defaults.status]`, `[sources.status]`, `[sources.calendars."<name or id>".status]`, `[[titles]]`.

| Key | Default | |
| --- | --- | --- |
| `enabled` | `true` | Show the event while it is on. |
| `show` | `"name"` | `name`: the red calendar, the title, a countdown and BUSY. Or one of the bar's own statuses: `back_soon`, `booked`, `chill_time`, `coding`, `dnd`, `flow`, `keep_out`, `low_social_battery`, `lunch`, `meeting`, `on_air`, `on_call`. |
| `sound` | `true` | Play the event sound as it starts. |
| `tentative` | `true` | Events you marked tentative show as busy too. Off, they keep their reminder only. |

## `[[sources]]`

One CalDAV account or one ICS link each; at least one.

| Key | Default | |
| --- | --- | --- |
| `name` | **required** | What logs and overrides call this source; unique. |
| `type` | **required** | `caldav` for an account, `ics` for a published link (one calendar). |
| `url` | **required** | CalDAV: the server, or one calendar's URL. ICS: the link; `webcal://` works. |
| `username` | none | Usually the email address. |
| `password` / `password_env` / `password_command` | none | iCloud and Fastmail need an app password. |
| `auth` | `"basic"` | HTTP authentication; a few servers want `digest`. |
| `emails` | `[]` | Your attendee address(es), to read your response to invitations. Default: the username, when it is an email address. |
| `include` | `[]` | CalDAV: only these calendars, by name, id or URL; `*` and `?` are wildcards. Default: all. The one listed last wins an overlap. |
| `exclude` | `[]` | CalDAV: leave these calendars out, after include. |
| `calendars` | `{}` | Per-calendar reminder and status overrides, keyed by name or id. |

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

| Key | Default | |
| --- | --- | --- |
| `match` | **required** | The titles it applies to: one or a list; `*` and `?` are wildcards, and case does not matter. |
