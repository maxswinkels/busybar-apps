# Troubleshooting

Two commands answer most questions:

```sh
busybar-calendar check    # is the setup right? the bar, its token, every calendar
busybar-calendar status   # is the service running, what did it last log, what is on the bar?
```

The outputs below are the commands' own, from real failures.

## The bar refuses: `refused (403)`

`check` names the cause after the code.

**The bar's HTTP API is off over Wi-Fi.** It is off until you switch it on:

```
Bar
  FAIL 192.168.1.50: refused (403). The bar's HTTP API over Wi-Fi is disabled: enable it at http://10.0.4.20 (USB) -> Network -> HTTP API, or use a BUSY Cloud token without an address.
```

Connect the bar by USB, open `http://10.0.4.20`, then **Network -> HTTP API
-> Set password and enable**. Or leave `address` out of `[bar]` and give a
BUSY Cloud token instead: that needs nothing switched on.

**The cloud token is wrong, revoked, or lacks the scope:**

```
Bar
  FAIL BUSY Cloud: refused (403). The cloud token is wrong, revoked, or lacks the BUSY Bar access scope.
```

Create a new token in your BUSY account with **BUSY Bar** under **Access
scope**, and store it where `token_command` / `token_env` reads it.

**A cloud token given as the bar's password.** With `address` set, the token
must be the HTTP API password, 32 characters at most; `check` spots a cloud
token by its length (`The token is 43 characters ... Is it a cloud token?`).
Leave `address` out to use the cloud token as one.

## The bar is unreachable

```
  FAIL 192.168.1.50: unreachable (...)
```

The address is wrong, or the bar is off or on another network: the computer
must be on the bar's network for a LAN address (**Settings -> Wi-Fi -> your
network -> View IP Address** on the bar). Over USB the bar is always
`10.0.4.20`. BUSY Cloud works from anywhere.

## The bar stopped answering

`status` checks the bar too:

```
Bar       BUSY Cloud: NOT answering (no answer). Is it on and online? If it stays stuck, unplug it and plug it back in.
```

A bar that answers ping but not its HTTP API - not on the LAN, not through
BUSY Cloud - has a stuck HTTP server; the service cannot draw until it is
power-cycled, and carries on by itself once the bar is back. (Seen on
firmware 1.2.4 after a draw request with a title of thousands of
characters; the service now caps titles at 120.)

## A calendar refuses the password

```
Calendars
  FAIL iCloud: PROPFIND https://caldav.icloud.com/: 401, check the credentials
```

iCloud and Fastmail need an **app password**, not the account's own. Store
the new one under the same Keychain entry, then restart the service, which
reads passwords only when it starts:

```sh
security add-generic-password -U -s busybar-calendar-me@icloud.com -a me@icloud.com -w
busybar-calendar restart
```

A running service stops on a refused password rather than asking again and
again, and `status` shows it:

```
Service   stopped (last exit code 1); the log says why
          log /Users/me/Library/Logs/busybar-calendar.log
            busybar-calendar: iCloud: PROPFIND https://caldav.icloud.com/: 401, check the credentials
```

It is started again five minutes later, so fixing the password is enough;
`busybar-calendar restart` does not wait.

## `no calendar matches`

```
Calendars
  FAIL Work: signed in; 0 of 2 calendars used: none
  FAIL Work: no calendar matches 'Wrok'; there are: Team, Work
```

An `include` or `exclude` entry names no calendar. Names are exact and
case-sensitive; `busybar-calendar calendars` lists them with their ids, and
an id keeps working when a calendar is renamed.

## `Config does not load`

```
Config    does not load: /Users/me/.config/busybar-calendar/config.toml
          the running service keeps the config it last loaded
            titles.0.status: show: show must be 'name' or one of back_soon, booked, ...
```

An edit broke the config. The service picks up edits by itself within a
minute, but not this one: it carries on with the config it last loaded,
and its log has the same lines. Each says where the problem is -
`titles.0` is the first `[[titles]]` rule, `sources.1` the second source -
and once the file is fixed, the service takes it at its next wake.

## Nothing on the bar during a meeting

`busybar-calendar status` says what the service thinks is on the bar:

- **`nothing on the bar now`**: the event is not one it shows. `busybar-calendar
  upcoming` lists every event it will show, with your response. Events are
  left out when they are all-day or 24 hours or longer, cancelled, marked
  free (`skip_free`), declined, in a calendar that `include` / `exclude`
  leaves out, or tentative with `status.tentative = false`.
- **`now status: ...` but the bar shows something else**: something above
  `bar.priority` (91) holds the screen, such as a menu you opened on the bar.
  The calendar comes back when it lets go; the log shows `bar refused the
  update (...)` meanwhile.

## A reminder came late, or not at all

Calendars are read every `poll_minutes` (5): an event added two minutes
before it starts can miss its reminder. ICS links are only as fresh as their
publisher makes them; Outlook and Google can take hours to republish.

## Getting more detail

```sh
busybar-calendar -v run                    # the service in the foreground, with debug logs
busybar-calendar check --json              # the findings as data, to attach to a report
busybar-calendar status --json
```
