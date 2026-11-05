"""
Command line: the commands, their options, and which code each one runs.

`statusline` runs inside Claude Code's status line, every few seconds, so it
imports nothing beyond the standard library: the rest is imported by the
commands that need it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, limits

PROG = "busybar-cclimits"
logger = logging.getLogger("busybar_cclimits")

FIRST_STEPS = f"""\
{PROG} shows Claude Code's usage limits on a BUSY Bar: a bar for the 5-hour
window with a countdown to its reset, and one for the week.

  {PROG} init --cloud    write the config (or --host 10.0.4.20 for USB)
  {PROG} usage           check that Anthropic answers with your limits
  {PROG} test            draw a sample on the bar
  {PROG} install         run in the background from now on

{PROG} status tells you where you stand; --help lists every command."""


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not verbose:
        for name in ("busylib", "httpx", "httpx2"):
            logging.getLogger(name).setLevel(logging.WARNING)
    # Its bounds check ignores anchors; and it nags about every released firmware.
    logging.getLogger("busylib.client.display").setLevel(logging.ERROR)
    logging.getLogger("busylib.client.firmware").setLevel(logging.ERROR)
    # It logs every refused call; the runner reports those itself.
    logging.getLogger("busylib.client.base").setLevel(logging.CRITICAL)


def _ago(then: float | None, now: float) -> str:
    return "never" if then is None else f"{limits.duration(max(0.0, now - then))} ago"


NAMES = {"five_hour": "5-hour", "seven_day": "weekly", "spend_limit": "spend"}


def _name(window: str) -> str:
    return NAMES.get(window) or window.replace("seven_day_", "weekly ").replace("_", " ")


def _when(at: int) -> str:
    return time.strftime("%a %H:%M", time.localtime(at))


def describe_feed(
    feed: limits.Feed | None, path: Path, now: float, source: str = "api"
) -> list[str]:
    if feed is None:
        how = (
            "the service writes it as it asks for the limits; `usage` asks once"
            if source == "api"
            else f"run `{PROG} hook install`, then use Claude Code"
        )
        return [f"Feed:    none yet at {path} - {how}"]
    seen = f", status line last ran {_ago(feed.seen_at, now)}" if feed.seen_at else ""
    lines = [f"Feed:    {path}", f"         limits updated {_ago(feed.updated_at, now)}{seen}"]
    if not feed.windows:
        lines.append(
            "         Claude Code sends no rate limits: they come for claude.ai subscriptions "
            "(and gateway spend limits), after a session's first response"
        )
    for name, window in feed.at(now).items():
        text = f"         {_name(name):<13} {window.used:5.1f}%"
        if window.resets_at is not None:
            text += (
                f"  resets in {limits.duration(window.left(now) or 0)} ({_when(window.resets_at)})"
            )
        elif feed.windows[name].resets_at is None:  # never had one: usage credits, say
            text += "  no reset time given"
        else:
            text += "  reset - a new window starts with the next request"
        if window.used_usd is not None and window.limit_usd is not None:
            text += f"  ${window.used_usd:,.2f} of ${window.limit_usd:,.0f}"
        lines.append(text)
    return lines


def status_report(config_path: Path | None, as_json: bool) -> int:
    from . import config as config_module
    from . import hook, service

    now = time.time()
    problems: list[str] = []
    config = None
    if config_path is not None and config_path.is_file():
        try:
            config = config_module.load(config_path)
        except Exception as error:
            problems = describe(error)
    feed_path = config.feed.path if config is not None else limits.default_feed()
    source = config.source if config is not None else config_module.SourceConfig()
    feed = limits.read(feed_path)
    hooked, command = hook.installed()
    state = service.status()
    if as_json:
        report: dict[str, Any] = {
            "config": str(config_path) if config_path else None,
            "config_problems": problems,
            "source": source.kind,
            "feed": str(feed_path),
            "updated_at": feed.updated_at if feed else None,
            "seen_at": feed.seen_at if feed else None,
            "limits": {
                name: {"used_percentage": w.used, "resets_at": w.resets_at}
                for name, w in (feed.at(now).items() if feed else ())
            },
            "hook": {
                "installed": hooked,
                "command": command,
                "settings": str(hook.settings_path()),
            },
            "service": {"installed": state.installed, "running": state.running, "pid": state.pid},
        }
        print(json.dumps(report, indent=2))
        return 0
    lines = describe_feed(feed, feed_path, now, source.kind)
    if source.kind == "api":
        lines.append(
            f"Source:  Anthropic's usage endpoint every {source.poll_seconds}s, "
            "with Claude Code's sign-in"
        )
    settings = hook.settings_path()
    if hooked:
        lines.append(f"Hook:    in {settings}")
    elif source.kind == "statusline":
        lines.append(f"Hook:    not installed - `{PROG} hook install`")
    if config_path is None:
        lines.append(f"Config:  none - `{PROG} init --cloud` or `{PROG} init --host ADDRESS`")
    else:
        lines.append(f"Config:  {config_path}")
        lines.extend(f"         {problem}" for problem in problems)
    if not state.installed:
        lines.append(f"Service: not installed - `{PROG} install` once `{PROG} test` works")
    elif state.running:
        version = service.running_version(state)
        lines.append(f"Service: running (pid {state.pid}){f', {version}' if version else ''}")
    else:
        lines.append(
            f"Service: installed, not running{f' ({state.detail})' if state.detail else ''}"
        )
        lines.extend(f"         {line}" for line in service.recent_log(state))
    print("\n".join(lines))
    return 1 if problems else 0


INIT_TEMPLATE = """\
# {prog}: Claude Code's usage limits on a BUSY Bar.
# Every setting, with its default: config.example.toml in the repository.

[bar]
{bar}
"""


def init_config(path: Path, args: argparse.Namespace) -> int:
    if path.exists() and not args.force:
        print(f"{PROG}: {path} exists; --force replaces it", file=sys.stderr)
        return 1
    if bool(args.host) == bool(args.cloud):
        print(
            f"{PROG}: give --host ADDRESS (LAN or USB) or --cloud (through BUSY Cloud)",
            file=sys.stderr,
        )
        return 2
    lines = [f'address = "{args.host}"'] if args.host else []
    if args.keychain:
        lines.append(
            f'token_command = ["security", "find-generic-password", "-s", "{args.keychain}", "-w"]'
        )
    elif args.token_env:
        lines.append(f'token_env = "{args.token_env}"')
    elif args.cloud:
        print(
            f"{PROG}: --cloud needs the token: --keychain SERVICE or --token-env VAR",
            file=sys.stderr,
        )
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(INIT_TEMPLATE.format(prog=PROG, bar="\n".join(lines)))
    path.chmod(0o600)
    print(f"Wrote {path}. Next: `{PROG} test` draws a sample on the bar.")
    return 0


def cmd_hook(args: argparse.Namespace, program: list[str] | None = None) -> int:
    from . import hook

    path = Path(args.settings).expanduser() if args.settings else hook.settings_path()
    settings = hook.load(path)
    change = (
        hook.plan_install(settings, program or hook.program())
        if args.action == "install"
        else hook.plan_uninstall(settings)
    )
    if change.before == change.after and change.settings == settings:
        print(f"{path}: {change.note}")
        return 0
    print(f"{path}: {change.note}")
    print(f"  status line before: {change.before or '(none)'}")
    print(f"  status line after:  {change.after or '(none)'}")
    if args.dry_run:
        print("(dry run: nothing written)")
        return 0
    backup = hook.save(path, change.settings)
    if backup is not None:
        print(f"  the old file is at {backup}")
    print("Claude Code picks the change up by itself; the limits arrive after its next response.")
    return 0


async def cmd_usage(as_json: bool) -> int:
    """Ask the usage endpoint once and show what it answers."""
    from . import usage_api

    token = usage_api.read_token()
    now = time.time()
    if token.expired(now):
        print("The Claude Code sign-in has expired; run `claude` once to renew it.")
        return 1
    data = await usage_api.fetch(token)
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    found = usage_api.to_rate_limits(data)
    if not found:
        print("The answer holds no limits this understands; `usage --json` shows it.")
        return 1
    for name, raw in found.items():
        resets = raw["resets_at"]
        when = (
            f"resets in {limits.duration(max(0, resets - now))} ({_when(resets)})"
            if resets
            else "no reset time (not started, or monthly)"
        )
        print(f"{name:<22} {raw['used_percentage']:5.1f}%  {when}")
    print("\nA [[display.rows]] window can be any of these names.")
    return 0


async def cmd_run(config: Any) -> int:
    from . import connect
    from .runner import Runner

    poller = None
    if config.source.kind == "api":
        from . import usage_api

        poller = usage_api.Poller(config.feed.path, config.source.poll_seconds)
    bar = connect.make_bar(config)
    stop = connect.stop_on_signals()
    # `status` reads this line to tell which version the service runs.
    logger.info(
        "%s %s started: %s, feed %s, bar %s",
        PROG,
        __version__,
        "usage API" if poller else "status line",
        config.feed.path,
        config.bar.address or "via BUSY Cloud",
    )
    try:
        await Runner(config, bar, poller=poller).run(stop)
    finally:
        await bar.aclose()
    return 0


async def cmd_demo(config: Any, seconds: float, steps: list[tuple[float, float]]) -> int:
    """Draw sample limits, one step after another, for `seconds` in all."""
    import asyncio

    from . import connect, render
    from .samples import Sample

    bar = connect.make_bar(config)
    stop = connect.stop_on_signals()
    each = seconds / len(steps)
    try:
        await render.clear(bar, config.bar.application_name)
        for five, week in steps:
            now = time.time()
            drawn = render.elements(Sample("", five, week=week).feed(now), now, config.display)
            until = now + each + 5
            await render.send(
                bar, render.drawing(drawn, until, config.bar.application_name, config.bar.priority)
            )
            try:
                await asyncio.wait_for(stop.wait(), each)
                break
            except TimeoutError:
                pass
    finally:
        await render.clear(bar, config.bar.application_name)
        await bar.aclose()
    return 0


async def cmd_snapshot(config: Any, names: list[str], out: Path, hold: float) -> int:
    """Draw each sample and save what the bar's front display then shows, as a 72x16 PNG."""
    import asyncio

    from busylib.exceptions import BusyBarAPIError

    from . import connect, render
    from .samples import SAMPLES

    bar = connect.make_bar(config)
    app, priority = config.bar.application_name, config.bar.priority
    out.mkdir(parents=True, exist_ok=True)
    try:
        for name in names:
            sample = SAMPLES[name]
            now = time.time()
            # Each layout has ids of its own: clear, or the last one's would stay.
            await render.clear(bar, app)
            drawn = render.elements(sample.feed(now), now, sample.display_config(config.display))
            try:
                await render.send(bar, render.drawing(drawn, now + hold + 30, app, priority))
            except BusyBarAPIError as error:
                if error.status_code == 409:
                    print(f"{name}: the bar shows something with a higher priority; skipped")
                    continue
                raise
            await asyncio.sleep(hold)
            path = out / f"{name}.png"
            path.write_bytes((await bar.frame("front")).to_png())
            print(f"{name}: {path}")
    finally:
        await render.clear(bar, app)
        await bar.aclose()
    return 0


def describe(error: BaseException) -> list[str]:
    """An expected failure in the user's terms."""
    from pydantic import ValidationError

    if isinstance(error, ValidationError):
        return [
            f"{'.'.join(str(part) for part in problem['loc']) or 'config'}: "
            f"{problem['msg'].removeprefix('Value error, ')}"
            for problem in error.errors()
        ]
    from busylib.exceptions import BusyBarAPIError

    if isinstance(error, BusyBarAPIError) and error.status_code == 409:
        return [
            "the bar shows something with a higher priority than this app's "
            "- a BUSY session or a calendar event; try again once it ends"
        ]
    return [str(error) or type(error).__name__]


def load_config(given: str | None) -> tuple[Any, Path | None]:
    from . import config as config_module

    path = config_module.find(given)
    if path is None:
        raise config_module.ConfigError(
            f"no config yet: `{PROG} init --cloud --keychain SERVICE` "
            f"or `{PROG} init --host ADDRESS`"
        )
    return config_module.load(path), path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "-c",
        "--config",
        help="TOML configuration (default: $BUSYBAR_CCLIMITS_CONFIG, then "
        "~/.config/busybar-cclimits/config.toml)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    line = commands.add_parser(
        "statusline", help="Claude Code's status line command: store the limits, print a line"
    )
    line.add_argument("--then", metavar="COMMAND", help="your own status line command, run after")
    line.add_argument(
        "--feed", help="the feed file (default: ~/.cache/busybar-cclimits/limits.json)"
    )

    hook = commands.add_parser("hook", help="add the status line command to Claude Code's settings")
    hook.add_argument("action", choices=["install", "uninstall"])
    hook.add_argument("--settings", help="the settings file (default: ~/.claude/settings.json)")
    hook.add_argument("--dry-run", action="store_true", help="show the change, write nothing")

    init = commands.add_parser("init", help="write the config")
    init.add_argument("--host", help="the bar's address on your network, or 10.0.4.20 over USB")
    init.add_argument("--cloud", action="store_true", help="reach the bar through BUSY Cloud")
    init.add_argument(
        "--keychain", metavar="SERVICE", help="read the token from this macOS Keychain item"
    )
    init.add_argument(
        "--token-env", metavar="VAR", help="read the token from this environment variable"
    )
    init.add_argument("--force", action="store_true", help="replace an existing config")

    usage = commands.add_parser(
        "usage", help="ask Anthropic's usage endpoint once, with Claude Code's sign-in"
    )
    usage.add_argument("--json", action="store_true", help="the answer as it came")

    status = commands.add_parser("status", help="the limits, the hook and the service")
    status.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="keep the bar in step with the limits")
    run.add_argument(
        "--auto-update",
        action="store_true",
        help="first update a uv tool installed from git to its latest version",
    )
    commands.add_parser("test", help="draw sample limits on the bar for 20 seconds")
    demo = commands.add_parser("demo", help="draw sample limits filling up")
    demo.add_argument("--seconds", type=float, default=28)

    snapshot = commands.add_parser(
        "snapshot", help="draw sample states and save what the bar shows, as PNGs"
    )
    snapshot.add_argument("sample", nargs="*", help="which samples (default: all)")
    snapshot.add_argument("--out", default="snapshots", help="folder for the PNGs")
    snapshot.add_argument("--hold", type=float, default=2.0, help="seconds before each capture")

    install = commands.add_parser("install", help="run in the background (launchd / systemd)")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument(
        "--auto-update",
        action="store_true",
        help="update to the latest version at every start (uv tool installed from git)",
    )
    commands.add_parser("restart", help="restart the background service")
    uninstall = commands.add_parser("uninstall", help="stop and remove the background service")
    uninstall.add_argument("--dry-run", action="store_true")
    return parser


def cmd_install(config_path: Path, args: argparse.Namespace) -> int:
    from . import service

    plan = service.install_plan(config_path, auto_update=args.auto_update)
    print(f"{'Would write' if args.dry_run else 'Writing'} {plan.path}")
    for command in plan.commands:
        print(f"  {' '.join(command)}")
    if args.dry_run:
        print(plan.content.decode())
        return 0
    service.install(plan)
    print(f"Installed. Log: {plan.log}" if plan.log else "Installed.")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import service

    plan = service.uninstall_plan()
    if args.dry_run:
        print(f"Would stop the service and remove {plan.path}")
        return 0
    print("Removed." if service.uninstall(plan) else "Nothing to remove: not installed.")
    return 0


def main(argv: list[str] | None = None, program: list[str] | None = None) -> int:
    """`program`: how the status line should start this, when not the installed command."""
    args = _parser().parse_args(argv)
    if args.command == "statusline":  # first, and nothing else imported
        from . import feed

        path = Path(args.feed).expanduser() if args.feed else limits.default_feed()
        return feed.run(args.then, path)

    setup_logging(args.verbose)
    try:
        import asyncio

        from . import config as config_module

        if args.command is None:
            path = config_module.find(args.config)
            if path is None and limits.read(limits.default_feed()) is None:
                print(FIRST_STEPS)
                return 0
            return status_report(path, as_json=False)
        if args.command == "usage":
            return asyncio.run(cmd_usage(args.json))
        if args.command == "status":
            return status_report(config_module.find(args.config), args.json)
        if args.command == "hook":
            return cmd_hook(args, program)
        if args.command == "init":
            target = Path(args.config).expanduser() if args.config else config_module.default_path()
            return init_config(target, args)
        if args.command == "uninstall":
            return cmd_uninstall(args)
        if args.command == "restart":
            from . import service

            restarted = service.restart()
            print("Restarted." if restarted else "Not installed.")
            return 0 if restarted else 1
        if args.command == "run" and args.auto_update:
            from . import update

            if not os.environ.get(update.UPDATED_ENV) and update.update():
                update.restart_into_update()
        config, path = load_config(args.config)
        if args.command == "install":
            assert path is not None
            return cmd_install(path, args)
        if args.command == "run":
            return asyncio.run(cmd_run(config))
        if args.command == "test":
            print("Drawing sample limits for 20 seconds (5-hour 42%, weekly 18%).")
            return asyncio.run(cmd_demo(config, 20, [(42, 18)]))
        if args.command == "demo":
            from .samples import DEMO_STEPS

            return asyncio.run(cmd_demo(config, args.seconds, DEMO_STEPS))
        if args.command == "snapshot":
            from .samples import SAMPLES

            names = args.sample or list(SAMPLES)
            unknown = sorted(set(names) - set(SAMPLES))
            if unknown:
                raise ValueError(f"no sample {', '.join(unknown)}; there are {', '.join(SAMPLES)}")
            return asyncio.run(cmd_snapshot(config, names, Path(args.out), args.hold))
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        if args.verbose:
            raise
        for line in describe(error):
            print(f"{PROG}: {line}", file=sys.stderr)
        return 1
    return 0
