"""Command line: the commands, their options, and which code each one runs."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import ValidationError

from . import __version__, connect, diagnose, playback, schedule, service, sources, update, wizard
from . import config as config_module
from .caldav import CalDAVError
from .config import Config, ConfigError
from .events import Event
from .runner import Runner

logger = logging.getLogger("busybar_calendar")


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class ConfigWatch:
    """
    Puts an edited config to work in a running service, within a minute.

    An edit that does not load leaves the service on the config it has, and
    says why in the log. `[bar]` is read only at start: a change there waits
    for `busybar-calendar restart`.
    """

    def __init__(self, path: Path, runner: Runner) -> None:
        self.path = path
        self.runner = runner
        self._stamp = _stamp(path)

    async def __call__(self) -> None:
        stamp = _stamp(self.path)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        running = self.runner.config
        try:
            config = config_module.load(self.path)
            items = connect.make_sources(config)
        except EXPECTED as error:
            logger.warning(
                "%s changed, but the service keeps the config it has: %s",
                self.path,
                "; ".join(describe(error)),
            )
            return
        if config.bar != running.bar:
            logger.warning("[bar] changed: `busybar-calendar restart` puts it to use")
            config = config.model_copy(update={"bar": running.bar})
        retired = self.runner.sources
        self.runner.reconfigure(config, items)
        await connect.close_all(retired)
        logger.info("%s changed: reloaded, %d calendar source(s)", self.path, len(items))


async def run_service(config: Config, path: Path | None = None) -> int:
    """
    Keep the bar in step with the calendars until a signal, or a source that
    cannot work; with the config's `path`, follow its edits too.
    """
    items = connect.make_sources(config)
    bar = connect.make_bar(config)
    stop = connect.stop_on_signals()
    # `status` reads this line to tell which version the service runs.
    logger.info(
        "busybar-calendar %s started: %d calendar source(s), bar %s",
        __version__,
        len(items),
        config.bar.address or "via BUSY Cloud",
    )
    runner = Runner(config, bar, items)
    try:
        await runner.run(stop, ConfigWatch(path, runner) if path is not None else None)
    except sources.FatalSourceError as error:
        print(f"busybar-calendar: {error}", file=sys.stderr)
        return 1
    finally:
        await connect.close_all(runner.sources)
        await bar.aclose()
    return 0


async def cmd_run(config: Config, args: argparse.Namespace) -> int:
    return await run_service(config, args.config_path)


async def cmd_check(config: Config, args: argparse.Namespace) -> int:
    return await diagnose.check(config, as_json=args.json)


async def cmd_replay(config: Config, args: argparse.Namespace) -> int:
    return await playback.replay(config, args.day, args.seconds, args.quiet)


async def cmd_demo(config: Config, args: argparse.Namespace) -> int:
    return await playback.demo(config, args)


async def cmd_install(config: Config, args: argparse.Namespace) -> int:
    """Run the service in the background from now on: at login, restarted if it stops."""
    plan = service.install_plan(args.config_path, auto_update=args.auto_update)
    if args.dry_run:
        print(f"Would write {plan.path}:\n")
        print(plan.content.decode())
        print("Would run:")
        for command in [*plan.cleanup, *plan.commands]:
            print("  " + shlex.join(command))
        return 0
    if not args.force:
        print("Checking the setup first.\n")
        if await diagnose.check(config):
            print("Fix the problems above, or install anyway with --force.")
            return 1
        print()
    service.install(plan)
    print(f"Installed {plan.path}.")
    print("It runs at login, comes back if it stops, and reads", args.config_path.resolve())
    if args.auto_update:
        print(f"At every start and restart it first runs `{' '.join(update.upgrade_command())}`.")
    if plan.log:
        print(f"Log: {plan.log}")
    print("Remove it with: busybar-calendar uninstall")
    return 0


def cmd_restart(_args: argparse.Namespace) -> int:
    """Restart the background service, after a password or the config changed."""
    if service.restart():
        print("Restarted. `busybar-calendar status` shows how it is doing.")
        return 0
    print("Not installed: `busybar-calendar install` runs it in the background.")
    return 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    plan = service.uninstall_plan()
    if args.dry_run:
        print(f"Would remove {plan.path} and run:")
        for command in [*plan.cleanup, *plan.commands]:
            print("  " + shlex.join(command))
        return 0
    if service.uninstall(plan):
        print(f"Stopped and removed {plan.path}.")
    else:
        print("Not installed.")
    return 0


async def cmd_calendars(config: Config, _args: argparse.Namespace) -> int:
    items = connect.make_sources(config)
    try:
        for item in items:
            if isinstance(item, sources.CalDAVSource):
                found = await item.calendars()
                used, unmatched = sources.select(found, item.config.include, item.config.exclude)
                print(f"{item.name} (caldav): {len(found)} calendars, {len(used)} used")
                width = max((len(calendar.id) for calendar in found), default=0)
                for calendar in found:
                    mark = "*" if calendar in used else " "
                    print(f"  {mark} {calendar.id:<{width}}  {calendar.name}")
                for pattern in unmatched:
                    print(f"  ! no calendar matches {pattern!r}")
            else:
                print(f"{item.name} (ics): one calendar, {item.config.url}")
    finally:
        await connect.close_all(items)
    print("\n* = used. Filter with include/exclude by id, name or URL; * and ? are wildcards.")
    return 0


def upcoming_notes(config: Config, event: Event) -> str:
    """What the bar does with an event, and why: its settings, your answer, its rules."""
    ref = event.calendar
    reminder, status = config.settings_for(ref.source, ref.name, ref.id, event.title)
    notes = []
    if reminder.enabled:
        notes.append(f"reminder {reminder.minutes_before}m")
    if schedule.shows_status(event, status):
        notes.append(f"status {status.show}")
    notes = notes or ["hidden"]
    if event.response != "accepted":
        notes.append(event.response)
    if patterns := config.title_patterns(event.title):
        notes.append("titles " + " ".join(map(repr, patterns)))
    return ", ".join(notes)


async def cmd_upcoming(config: Config, args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    runner = await connect.read_calendars(config, now)
    tz = connect.floating_tz(config)
    horizon = now + timedelta(hours=args.hours)
    shown = [e for e in runner.events if e.end > now and e.start < horizon]
    for event in sorted(shown, key=lambda e: e.start):
        start = event.start.astimezone(tz)
        end = event.end.astimezone(tz)
        print(
            f"{start:%a %d %b %H:%M}-{end:%H:%M}  [{event.calendar.source}/"
            f"{event.calendar.name}]  {event.title}  ({upcoming_notes(config, event)})"
        )
    screen = schedule.choose(now, runner.events, runner.settings_for)
    print(f"\nNow on the bar: {screen.kind + ': ' + screen.event.title if screen else 'nothing'}")
    return 0


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        logging.getLogger("busylib").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpx2").setLevel(logging.WARNING)
    # Its bounds check ignores anchors, so a bottom-anchored line at y=16 -
    # busylib's own calibrated two-line layout - is reported as off-screen.
    logging.getLogger("busylib.client.display").setLevel(logging.ERROR)
    # "Device API minor version is behind Busy Lib": busylib's own nag, on every
    # bar that runs released firmware; `check` prints both versions instead.
    logging.getLogger("busylib.client.firmware").setLevel(logging.ERROR)
    # It logs every refused call; this service reports those itself, with what to do.
    logging.getLogger("busylib.client.base").setLevel(logging.CRITICAL)


FIRST_STEPS = """\
busybar-calendar shows your CalDAV and ICS calendars on a BUSY Bar: a
reminder before each event, then a red BUSY while it runs.

No config yet. To set it up:

  busybar-calendar init       a few questions: how to reach the bar, which calendar
  busybar-calendar check      proves it works, or says what to fix
  busybar-calendar install    runs it in the background from now on

busybar-calendar --help lists every command."""


def _status(args: argparse.Namespace, as_json: bool = False) -> int | None:
    """
    `status`, which needs no config for its service part, and takes a broken
    one in its stride: a running service keeps the last config that loaded,
    so it is reported beside the problems. None when there is no config yet.
    """
    try:
        path = config_module.find(args.config)
    except ConfigError:
        path = None
    if path is None or not path.is_file():
        if as_json:
            return asyncio.run(diagnose.status(None, as_json=True))
        return None
    try:
        config, problems = config_module.load(path), []
    except EXPECTED as error:
        config, problems = None, describe(error)
    return asyncio.run(diagnose.status(config, as_json, path=path, problems=problems))


def _where_you_stand(args: argparse.Namespace) -> int:
    """`busybar-calendar` alone: first steps before a config exists, the status after."""
    code = _status(args)
    if code is None:
        print(FIRST_STEPS)
        return 0
    print("\nbusybar-calendar --help lists every command.")
    return code


# The failures a user can meet and fix: reported in a line or two, never a traceback.
EXPECTED = (
    ValidationError,
    ConfigError,
    CalDAVError,
    sources.FatalSourceError,
    httpx.HTTPError,
    subprocess.CalledProcessError,
    OSError,
    ValueError,
)


def describe(error: BaseException) -> list[str]:
    """An expected failure in the user's terms: what went wrong, where."""
    if isinstance(error, ValidationError):  # the config: one line per problem
        return [
            f"{'.'.join(str(part) for part in problem['loc']) or 'config'}: "
            f"{problem['msg'].removeprefix('Value error, ')}"
            for problem in error.errors()
        ]
    if isinstance(error, httpx.RequestError):
        return [f"cannot reach {error.request.url}: {error}"]
    if isinstance(error, subprocess.CalledProcessError):  # launchctl, systemctl
        return [f"{shlex.join(error.cmd)} failed (exit {error.returncode})"]
    return [str(error)]


def _update_first() -> None:
    """`run --auto-update`, before the config is read - a newer version may be what reads
    it - and once: not again in the process an update restarts into."""
    if not os.environ.get(update.UPDATED_ENV) and update.update():
        update.restart_into_update()


def _restart_after(code: int) -> int:
    """After the config changed: a running background service reads it only when it starts."""
    if code == 0 and service.restart():
        print("Restarted the background service, so it reads the new config.")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="busybar-calendar", description=__doc__)
    parser.add_argument(
        "-c",
        "--config",
        help="TOML configuration (default: $BUSYBAR_CALENDAR_CONFIG, ./config.toml, "
        "then ~/.config/busybar-calendar/config.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # No command: where you stand, and what to do next (see _where_you_stand).
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="keep the bar in step with the calendars")
    run.add_argument(
        "--auto-update",
        action="store_true",
        help="first update a uv tool installed from git to its latest version",
    )
    init = commands.add_parser("init", help="answer a few questions to write the config")
    init.add_argument("--force", action="store_true", help="replace an existing config")
    commands.add_parser("add", help="add another calendar to the config, a few questions")
    check = commands.add_parser("check", help="check the bar, its token and every calendar")
    check.add_argument("--json", action="store_true", help="the findings as JSON")
    status = commands.add_parser(
        "status", help="is it running, and what is on the bar now and next"
    )
    status.add_argument("--json", action="store_true", help="the same, as JSON")
    install = commands.add_parser(
        "install", help="run in the background from now on (launchd / systemd)"
    )
    install.add_argument("--dry-run", action="store_true", help="show what it would do")
    install.add_argument("--force", action="store_true", help="install even if check fails")
    install.add_argument(
        "--auto-update",
        action="store_true",
        help="update to the latest version at every start and restart (uv tool from git)",
    )
    commands.add_parser("restart", help="restart the background service, e.g. after a new password")
    uninstall = commands.add_parser("uninstall", help="stop and remove the background service")
    uninstall.add_argument("--dry-run", action="store_true", help="show what it would do")
    commands.add_parser("calendars", help="list the calendars each CalDAV source has")
    upcoming = commands.add_parser("upcoming", help="list the events the bar will show")
    upcoming.add_argument("--hours", type=int, default=24)
    demo = commands.add_parser("demo", help="draw a sample event on the bar")
    demo.add_argument("--title", default="Weekly sync with the team")
    demo.add_argument("--minutes", type=int, default=5, help="reminder: minutes to the start")
    demo.add_argument("--status", action="store_true", help="draw the in-event status instead")
    demo.add_argument("--show", help="status to draw: name, meeting, on_call, ...")
    demo.add_argument("--font", choices=["tiny", "small", "global"])
    demo.add_argument("--sound", action="store_true")
    demo.add_argument("--seconds", type=int, default=20, help="how long to leave it up")
    replay = commands.add_parser(
        "replay", help="play a day's events on the bar one after another, in short slots"
    )
    replay.add_argument("--day", default="tomorrow", help="today, tomorrow or YYYY-MM-DD")
    replay.add_argument("--seconds", type=int, default=60, help="slot per event")
    replay.add_argument("--quiet", action="store_true", help="no sounds")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    handlers = {
        "run": cmd_run,
        "check": cmd_check,
        "install": cmd_install,
        "calendars": cmd_calendars,
        "upcoming": cmd_upcoming,
        "demo": cmd_demo,
        "replay": cmd_replay,
    }
    try:
        if args.command == "uninstall":  # needs no config: there may be none left
            return cmd_uninstall(args)
        if args.command == "restart":
            return cmd_restart(args)
        if args.command is None:
            return _where_you_stand(args)
        if args.command == "status":
            code = _status(args, args.json)
            return asyncio.run(diagnose.status(None)) if code is None else code
        if args.command == "init":  # writes the config
            path = Path(
                args.config
                or os.environ.get(config_module.CONFIG_ENV)
                or config_module.search_paths()[-1]
            )
            return _restart_after(wizard.run(path, overwrite=args.force))
        if args.command == "add":  # appends to the config
            return _restart_after(wizard.add(config_module.find(args.config)))
        if args.command == "run" and args.auto_update:
            _update_first()
        args.config_path = config_module.find(args.config)
        config = config_module.load(args.config_path)
        return asyncio.run(handlers[args.command](config, args))
    except EXPECTED as error:
        for line in describe(error):
            print(f"busybar-calendar: {line}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
