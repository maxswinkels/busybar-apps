# Changelog

## [Unreleased]

## [0.3.0] - 2026-10-21

### Added

- The limits come from Anthropic's usage endpoint by default (`[source] kind
  = "api"`): the one behind Claude Code's own `/usage`, asked every two
  minutes (`poll_seconds`) with the sign-in Claude Code already has - read
  from the Keychain or `~/.claude/.credentials.json`, never refreshed or
  written back. Besides the 5-hour and weekly windows it reports a model's
  own weekly limit, such as `seven_day_fable` for Fable, and the month's
  usage credits as `extra_usage` (label CR). A rate-limited or failed request
  backs off; an expired sign-in waits for Claude Code to renew it.
- `busybar-cclimits usage [--json]` asks the endpoint once and lists what
  your account reports.

### Changed

- `kind = "statusline"` keeps the status line hook as the source, for those
  who want no credentials read.
- `show = "with_claude"` also counts the numbers moving as Claude Code at
  work, since the usage endpoint never runs the status line.
- `status` names the source, no longer fails on a per-model window, and
  tells a window whose reset has passed from one with no reset time.
- The README's screens are captures from a real bar. The previews script pads
  the countdown's hours as the firmware does (`01:42:07`).

## [0.2.0] - 2026-10-20

### Added

- Up to three rows. A third row switches every row to the bar's 4-pixel tiny
  font, with 2-pixel gaps between the bars; the countdown keeps its 5-pixel
  digits, and on the bottom row they end on the panel's last row.
- Rows for a model's own limit: `seven_day_<model>` or `five_hour_<model>`,
  such as `seven_day_fable`, `seven_day_opus` and `seven_day_sonnet`,
  labelled FB, OP and SN (else the model's first two letters). The status
  line does not report these windows, so such a row shows `--` until
  something writes them to the feed.
- `snapshot` draws sample states on the bar and saves what its front
  display then shows, as 72x16 PNGs; `scripts/render_previews.py` draws the
  same samples in the firmware's fonts without a bar.
- Previews in the README, and `preview.gif` for the BUSY Bar app gallery.
- CI runs the app against the BUSY Bar emulator too (`tests/emulator`): every
  sample drawing, the clear when the element ids change, giving way to a
  higher priority and taking the screen back, redraws in place, the service,
  `test` and `app.py`.

### Changed

- A draw the bar refuses because something with a higher priority holds the
  screen - a BUSY session or a calendar event - says so, instead of
  printing the bar's error.

## [0.1.0] - 2026-10-20

First release.

- The 5-hour and weekly windows as bars, green, yellow from 75% and red from
  90%, with a live countdown to the 5-hour reset and the weekly percentage.
- The limits come from Claude Code's own status line input: `statusline`
  stores them and runs your status line command after; `hook install` and
  `hook uninstall` put it in Claude Code's settings and take it out again.
- `[display]` settings: one or two rows of `five_hour`, `seven_day` or
  `spend_limit`, the text beside each bar, `show = "with_claude"`,
  thresholds and colours.
- Priority 50, re-sent every minute, with `display_until` on every element.
- `init`, `status`, `test`, `demo`, `run`, `install` (launchd / systemd,
  `--auto-update`), `restart`, `uninstall`; `app.py` for the gallery.

[Unreleased]: https://github.com/kurok/busybar-cclimits/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kurok/busybar-cclimits/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kurok/busybar-cclimits/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kurok/busybar-cclimits/releases/tag/v0.1.0
