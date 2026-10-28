# Changelog

All notable changes to busybar-calendar. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.0] - 2026-10-20

### Added

- Events you marked tentative ("Maybe") are drawn in yellow: a yellow calendar
  and a yellow `BUSY`, in the reminder and during the event, so they read
  differently from the events you accepted. `[display] tentative_yellow = false`
  draws them in red like the rest. With `reminder_icon` / `event_icon` set to
  `animation` only the label turns yellow, as the bar's own animation has no
  yellow version.

### Changed

- `scripts/capture_screens.py` draws the tentative status for the README, and
  its docs now say that a capture takes a running service's screen down until
  the service redraws.
- The screen is redrawn when you change your answer to an event that is
  already up - marked "maybe" from your phone, say - at the next refresh,
  not only when the screen changes.
- CI tests Python 3.12, 3.14 and 3.15 (it was 3.11, 3.12 and 3.13), and its
  actions run on Node 24. `requires-python` stays `>=3.11`, which is no
  longer tested.

## [0.7.2] - 2026-10-13

### Fixed

- `restart` on macOS waited up to five minutes when the service had
  started recently: `launchctl kickstart -k` waits out the agent's restart
  throttle. It now unloads the agent and loads it again, which starts it
  at once - and also works for an agent that was not loaded. With
  `--auto-update`, that makes `restart` the quick way to update.

## [0.7.1] - 2026-10-13

### Fixed

- `install` over a running launch agent could fail with
  `launchctl bootstrap ... failed (exit 5)`, leaving the service stopped:
  `launchctl bootout` returns before the old agent is gone. `install` now
  waits for it (up to 10 seconds) before loading the new one.

## [0.7.0] - 2026-10-13

### Added

- `busybar-calendar install --auto-update`: the background service updates
  itself every time it starts - at login, on `restart`, after it stopped -
  with `uv tool upgrade busybar-calendar`, before it reads the config, then
  runs the new version at once. A failed update (no network yet at login)
  starts the version it has. It needs the one-line install from git, and
  `install` says so when it is not that. `status` shows
  `updates itself at each start`, and `--json` has `service.auto_update`.
- `run --auto-update`, what such a service runs.

## [0.6.1] - 2026-10-13

### Fixed

- A CalDAV account lists its calendars again every hour, to find new
  ones; a timeout or server error at that moment no longer drops the whole
  refresh. It goes on with the calendars it knows, says so in the log, and
  lists them again at the next refresh. A refused password still stops the
  service, as before.

## [0.6.0] - 2026-10-13

### Added

- `upcoming` names the `[[titles]]` patterns that apply to each event, so a
  rule that hides or changes one shows why: `(hidden, titles 'Focus*')`.

### Fixed

- `upcoming` no longer lists `status` for a tentative event when
  `status.tentative` is off; it shows only the reminder, as the bar does.

## [0.5.1] - 2026-10-13

### Fixed

- `status`, and `busybar-calendar` on its own, report a config that does
  not load - with its problems and the fact that a running service keeps
  the config it last loaded - instead of stopping at the first error and
  showing nothing about the service. `status` exits 1 then, and its
  `--json` has a `config` block with the path and the problems.

## [0.5.0] - 2026-10-13

### Added

- A running service follows edits to its config: they apply within a
  minute, with the calendars read again at once and the screen redrawn
  (without a second chime). An edit that does not load leaves the service
  on the config it has, and the log says what is wrong; a change under
  `[bar]` waits for `busybar-calendar restart`.

## [0.4.0] - 2026-10-13

### Added

- `[[titles]]` rules: reminder and status settings for the events whose
  title matches (`*` and `?` wildcards, any case), from any calendar, on top
  of the calendar's own - to keep `Focus time` off the bar, or show `Lunch*`
  as the bar's own lunch status. `upcoming` shows what each event ends up
  with, and `demo --title` draws with the rules for that title.

### Fixed

- A bad reminder or status value now says where it is
  (`sources.0.calendars.Team.reminder: minutes_before: ...`), not just which
  key.

## [0.3.2] - 2026-10-12

### Changed

- Install with one line, no clone needed:
  `uv tool install git+https://github.com/kurok/busybar-calendar`, and
  upgrade with `uv tool upgrade busybar-calendar`.

### Fixed

- `status` no longer offers `restart` as the way to a newer version when the
  service runs from another install (a checkout run with `uv run`, say): a
  restart would start the old version again. It names the install to upgrade,
  and the `uv tool upgrade` command when that install came from git.

## [0.3.1] - 2026-10-12

### Added

- `status` checks the bar with one light read-only call and says whether it
  answers, refuses the token, or does not answer at all - with what to do -
  so a stuck bar no longer looks like a healthy setup.

### Fixed

- Event titles are capped at 120 characters (a longer one only scrolls
  longer). A draw request with a title of thousands of characters left a
  bar's HTTP API hanging, on the LAN and through BUSY Cloud, until the bar
  was power-cycled.

## [0.3.0] - 2026-10-12

### Changed

- `init` and `add` ask for the calendar provider from a list - iCloud,
  Fastmail, PrivateEmail, Nextcloud or another server - guessed from your
  address, instead of for the server's URL; Nextcloud needs only its host.

### Added

- `busybar-calendar` on its own says where you stand: the first steps before
  there is a config, the status after, instead of a usage error.
- `status` shows the version the background service runs, read from the line
  it now logs at start, and says to `restart` when that is not the version
  installed; the setup guide has an "Upgrading" section.
- Tentative events in published feeds, which carry no attendees, now count as
  tentative: Outlook's busy status or `STATUS:TENTATIVE` decides there, for
  overlaps and for `status.tentative`.

### Fixed

- A Keychain / libsecret prompt that failed - a mistyped retype - ended
  `init` and `add` with a traceback; it is now asked again, up to three times,
  then reported in one line.
- A failing `launchctl` or `systemctl` ended `install` with a traceback; it is
  now one line naming the command and its exit code.
- The service remembered every screen it had chimed for; it now forgets
  them once their events are gone, so a long-running service does not grow.
- A mistyped `sync.timezone` ended every command in a traceback; it is now
  rejected when the config is read, with an example of a valid name.
- `calendars` and `replay` ended in a traceback when a calendar server could
  not be reached; they now say which address and why.
- A config mistake printed pydantic's whole report; it is now one line per
  problem, naming the key (`bar.adress: Extra inputs are not permitted`), in
  the CLI and in `app.py` alike.
- `app.py --minutes 0` (and other invalid reminder or status settings given
  as flags) was accepted and only failed at the first event; it now fails at
  start.

## [0.2.0] - 2026-10-12

### Added

- `busybar-calendar add`: another calendar source by the same questions as
  `init`, appended to the config with its comments and layout kept, under a
  name that does not clash with the others.
- `--json` for `status` and `check`: the service, the screen now and next,
  and each check's finding with its fields (firmware, calendars, next event),
  with times in ISO 8601.
- `busybar-calendar restart`, for the background service after a new
  password or config.
- [docs/troubleshooting.md](docs/troubleshooting.md): what `check` and
  `status` say for each common problem, from real failures, and the fix.

### Changed

- After `init` or `add`, an installed background service is restarted, so it
  reads the new config.
- `init` and `add` keep each account's password in a Keychain / libsecret
  entry of its own; two accounts on one domain no longer share one.

### Fixed

- `check` called a BUSY Cloud that refused the token "unreachable"; it now
  says the token is wrong, revoked or lacks the BUSY Bar scope.
- For a source whose `include` matched no calendar, `check` added a
  reassuring "no events in the next 7 days"; it now lists the calendars
  there are, and the source's own warnings no longer repeat in its output.

## [0.1.0] - 2026-10-12

The first release.

### Added

- Reminders and a BUSY status on the BUSY Bar from any CalDAV account
  (iCloud, Fastmail, Nextcloud, PrivateEmail, Radicale, ...) or ICS / webcal
  link, drawn through the bar's HTTP API on the LAN or through BUSY Cloud.
- The Google Calendar integration's per-calendar settings - reminder time,
  sound, show name, status during events - layered as defaults, per source
  and per calendar.
- A red calendar icon and a red BUSY beside the countdown during an event;
  the bar's own animations and statuses (`meeting`, `on_call`, ...) as
  options.
- Overlapping events settled by your response: accepted over tentative over
  not answered, then the newest event. `status.tentative` keeps tentative
  events off the busy status.
- Calendar filters: `include` / `exclude` by name, id or URL, with wildcards.
- Commands: `init` (a few questions write the config, passwords go to the
  Keychain or libsecret), `check` (the bar, its token and every calendar),
  `install` / `uninstall` (launchd or systemd), `status` (running? now and
  next), `calendars`, `upcoming`, `demo`, `replay`, `run`, `--version`.
- `app.py`, following the BUSY Bar community gallery's rules: `--host`,
  `--ics`, `--caldav`, `--demo`, `--test`, secrets from the environment.
- Docs with screenshots read back from a real bar, and a configuration
  reference generated from the code.

### Fixed

Found on a bar running firmware 1.2.4, and recorded in
[docs/how-it-works.md](docs/how-it-works.md):

- The countdown overlapped the title: its font sits 4px high in its box.
- The status lost its icon when it replaced a reminder as the reminder's
  `display_until` fired; drawings now outlive their screen by a minute.
- A draw merges by element id, so a new layout could land on top of the old
  one; a clear now comes first only when an element would be left over,
  and a reminder becomes its event without a blink.
- CalDAV discovery turned PROPFIND into GET on a 302 redirect.
- A 401 or 404 from a calendar was retried forever; it now stops the
  service with the reason.
- A refused draw (409) stopped the demo and replay.
- A Ctrl-C during the slow first start printed a traceback.

[Unreleased]: https://github.com/kurok/busybar-calendar/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/kurok/busybar-calendar/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/kurok/busybar-calendar/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/kurok/busybar-calendar/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/kurok/busybar-calendar/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/kurok/busybar-calendar/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/kurok/busybar-calendar/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/kurok/busybar-calendar/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/kurok/busybar-calendar/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/kurok/busybar-calendar/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/kurok/busybar-calendar/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/kurok/busybar-calendar/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kurok/busybar-calendar/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kurok/busybar-calendar/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kurok/busybar-calendar/releases/tag/v0.1.0
