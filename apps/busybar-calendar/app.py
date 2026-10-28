#!/usr/bin/env python3
"""CalDAV Calendar: meeting reminders and a red BUSY from any CalDAV or ICS calendar.

A countdown before each event, then the event's title with a countdown to its
end and a red BUSY while it runs; the bar is left alone between events.
CalDAV (iCloud, Fastmail, Nextcloud, PrivateEmail, Radicale, ...) or any ICS
link (Outlook "Publish a calendar", Google's secret iCal address).

    python app.py                                  # demo schedule, BUSY Bar over USB (10.0.4.20)
    python app.py --host 127.0.0.1:8080            # emulator or a Wi-Fi bar
    python app.py --ics https://example.com/work.ics
    python app.py --caldav https://caldav.icloud.com --user me@icloud.com --include Work
    python app.py --config config.toml             # every setting: see config.example.toml
                                                   # (without flags, the config that
                                                   # `busybar-calendar init` wrote is used)
    python app.py --test                           # draw one frame of an event and exit

Secrets come from the environment, never the command line or this file:

    CALDAV_PASSWORD   the CalDAV password (iCloud and Fastmail need an app password)
    BUSYBAR_TOKEN     the bar's HTTP API password, if its Wi-Fi API asks for one;
                      with --cloud, a BUSY Cloud token with the "BUSY Bar" scope

The calendar is polled every 5 minutes. A 401 or 404 from it stops the app
with an error rather than retrying. Drawings go in at priority 91, above a
running BUSY session (90), the way the bar's own calendar events do; between
events nothing is drawn, so the bar is never held.
"""

# First of all: Ctrl-C before the app is up ends it quietly. The first start
# after an install can take seconds while the system checks the newly installed
# libraries; once running, every mode handles SIGINT itself and clears the bar.
import signal
import sys

signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

import argparse
import asyncio
import os
from pathlib import Path

# The app's code lives in src/busybar_calendar, next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from busybar_calendar import cli, playback
from busybar_calendar.config import Config, ConfigError, find, load

APP = "busybar-calendar"
USB_HOST = "10.0.4.20"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", help=f"the bar: ip[:port] (default {USB_HOST}, USB)")
    p.add_argument("--cloud", action="store_true", help="reach the bar through BUSY Cloud")
    p.add_argument("--ics", action="append", default=[], metavar="URL", help="an ICS feed")
    p.add_argument("--caldav", metavar="URL", help="a CalDAV server or calendar URL")
    p.add_argument("--user", help="CalDAV username, usually the email address")
    p.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="NAME_OR_ID",
        help="use only these CalDAV calendars (* wildcards)",
    )
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="NAME_OR_ID",
        help="leave these CalDAV calendars out",
    )
    p.add_argument("--minutes", type=int, default=5, help="reminder before an event (5)")
    p.add_argument("--priority", type=int, default=91, help="draw priority (91)")
    p.add_argument("--quiet", action="store_true", help="no sounds")
    p.add_argument("--config", help="a TOML file with every setting")
    p.add_argument("--demo", action="store_true", help="loop a made-up schedule")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def config_file(args: argparse.Namespace) -> Path | None:
    """The config to use: --config, else - unless calendars are given as flags - the one
    `busybar-calendar init` wrote ($BUSYBAR_CALENDAR_CONFIG, ./config.toml or
    ~/.config/busybar-calendar/config.toml), else none."""
    if args.config:
        return Path(args.config)
    if args.ics or args.caldav:
        return None
    try:
        return find()
    except ConfigError:
        return None


def build_config(args: argparse.Namespace, path: Path | None) -> Config:
    if path is not None:
        config = load(path)
        if args.host:
            config.bar.address = args.host
        return config

    bar: dict = {"application_name": APP, "priority": args.priority}
    if not args.cloud:
        bar["address"] = args.host or USB_HOST
    if os.environ.get("BUSYBAR_TOKEN"):
        bar["token_env"] = "BUSYBAR_TOKEN"
    elif args.cloud:
        raise ConfigError("--cloud needs a BUSY Cloud token in BUSYBAR_TOKEN")

    sources = [
        {"name": f"ics-{n}" if len(args.ics) > 1 else "ics", "type": "ics", "url": url}
        for n, url in enumerate(args.ics, 1)
    ]
    if args.caldav:
        caldav = {
            "name": "caldav",
            "type": "caldav",
            "url": args.caldav,
            "include": args.include,
            "exclude": args.exclude,
        }
        if args.user:
            caldav |= {"username": args.user, "password_env": "CALDAV_PASSWORD"}
        sources.append(caldav)

    sound = not args.quiet
    # --minutes 0 fails here, not at the first event
    return Config.model_validate(
        {
            "bar": bar,
            "defaults": {
                "reminder": {"minutes_before": args.minutes, "sound": sound},
                "status": {"sound": sound},
            },
            "sources": sources,
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cli.setup_logging(args.verbose)
    try:
        path = config_file(args)
        config = build_config(args, path)
        if args.test:
            return asyncio.run(playback.draw_test_frame(config))
        if args.demo or not config.sources:
            if not args.demo:
                print(
                    "No calendar yet (--ics, --caldav, or a config from `busybar-calendar "
                    "init`): playing a demo schedule.",
                    file=sys.stderr,
                )
            return asyncio.run(playback.run_demo(config))
        where = config.bar.address or "BUSY Cloud"
        print(
            f"{APP} -> {where}{f', config {path}' if path else ''}  (Ctrl-C to stop)",
            file=sys.stderr,
        )
        return asyncio.run(cli.run_service(config))
    except cli.EXPECTED as error:
        for line in cli.describe(error):
            print(f"{APP}: {line}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
