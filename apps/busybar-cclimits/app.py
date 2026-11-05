#!/usr/bin/env python3
"""Claude Code Limits: the 5-hour and weekly usage limits as bars, with a countdown.

A bar for Claude Code's 5-hour window with a live countdown to its reset,
and one for the week with the percentage used, in green, then yellow from
75% and red from 90%. The numbers come from Anthropic's usage endpoint - the
one behind Claude Code's /usage - asked every two minutes with the sign-in
Claude Code already has, which is read and never changed. Or, with
--source statusline, from what Claude Code hands its status line, after
`python app.py --hook` wraps your status line command.

    python app.py                           # BUSY Bar over USB (10.0.4.20)
    python app.py --host 127.0.0.1:8080     # emulator or a Wi-Fi bar
    python app.py --config config.toml      # every setting: see config.example.toml
    python app.py --demo                    # sample limits filling up, no Claude Code needed
    python app.py --test                    # draw one frame and exit

Without a Claude Code sign-in (or, with --source statusline, without limits
from the status line yet) it plays the demo. Secrets come from the
environment, never the command line or this file:

    BUSYBAR_TOKEN   the bar's HTTP API password, if its Wi-Fi API asks for one;
                    with --cloud, a BUSY Cloud token with the "BUSY Bar" scope

Drawings go in at priority 50: above the bar's own apps (10), below a BUSY
session (90) and calendar reminders (91), and sent again every minute so
the limits come back once those end.
"""

# First of all: Ctrl-C before the app is up ends it quietly.
import signal
import sys

signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

import argparse
import asyncio
import os
import time
from pathlib import Path

# The app's code lives in src/busybar_cclimits, next to this file.
HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent / "src"))

from busybar_cclimits import cli

# The status line hook runs this file every few seconds: nothing heavy for it.
if sys.argv[1:2] == ["statusline"]:
    sys.exit(cli.main(sys.argv[1:]))

from busybar_cclimits import connect, limits, render, usage_api
from busybar_cclimits.samples import DEMO_STEPS, Sample
from busybar_cclimits.config import USB_HOST, Config, find, load

APP = "busybar-cclimits"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--host", default=None, help=f"the bar: ip[:port] (default {USB_HOST}, USB)")
    p.add_argument("--cloud", action="store_true", help="reach the bar through BUSY Cloud")
    p.add_argument("--config", help="a TOML file with every setting")
    p.add_argument(
        "--source",
        choices=["api", "statusline"],
        help="where the limits come from (default api: Anthropic's usage endpoint)",
    )
    p.add_argument("--feed", help="where the limits are kept (~/.cache/busybar-cclimits/...)")
    p.add_argument("--priority", type=int, default=50, help="draw priority (50)")
    p.add_argument("--hook", action="store_true", help="add the status line hook to Claude Code")
    p.add_argument("--demo", action="store_true", help="loop sample limits")
    p.add_argument("--test", action="store_true", help="draw one frame and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    path = Path(args.config) if args.config else find()
    if path is not None:
        config = load(path)
        if args.host:
            config.bar.address = args.host
    else:
        bar: dict = {"application_name": APP, "priority": args.priority}
        if not args.cloud:
            bar["address"] = args.host or USB_HOST
        if os.environ.get("BUSYBAR_TOKEN"):
            bar["token_env"] = "BUSYBAR_TOKEN"
        elif args.cloud:
            raise ValueError("--cloud needs a BUSY Cloud token in BUSYBAR_TOKEN")
        config = Config.model_validate({"bar": bar})
    if args.feed:
        config.feed.path = Path(args.feed).expanduser()
    if args.source:
        config.source.kind = args.source
    return config


def no_limits(config: Config) -> str | None:
    """Why there is nothing to show but the demo, or None."""
    if config.source.kind == "api":
        try:
            usage_api.read_token()
        except usage_api.TokenError as error:
            return str(error)
        return None
    if limits.read(config.feed.path) is None:
        return f"no limits from the status line yet ({config.feed.path}); `--hook` sets it up"
    return None


async def demo(config: Config, once: bool) -> int:
    """Sample limits filling up, over and over; with `once`, one frame and out."""
    bar = connect.make_bar(config)
    stop = connect.stop_on_signals()
    app, priority = config.bar.application_name, config.bar.priority
    try:
        await render.clear(bar, app)
        while not stop.is_set():
            for five, week in DEMO_STEPS:
                now = time.time()
                drawn = render.elements(Sample("", five, week=week).feed(now), now, config.display)
                await render.send(bar, render.drawing(drawn, now + 10, app, priority))
                if once:
                    return 0
                try:
                    await asyncio.wait_for(stop.wait(), 4)
                    return 0
                except TimeoutError:
                    pass
    finally:
        if not once:
            await render.clear(bar, app)
        await bar.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cli.setup_logging(args.verbose)
    try:
        if args.hook:
            return cli.main(["hook", "install"], program=[sys.executable, str(HERE)])
        config = build_config(args)
        if args.test:
            return asyncio.run(demo(config, once=True))
        why = None if args.demo else no_limits(config)
        if args.demo or why:
            if why:
                print(f"{APP}: {why}: playing a demo.", file=sys.stderr)
            return asyncio.run(demo(config, once=False))
        where = config.bar.address or "BUSY Cloud"
        print(f"{APP} -> {where}, feed {config.feed.path}  (Ctrl-C to stop)", file=sys.stderr)
        return asyncio.run(cli.cmd_run(config))
    except Exception as error:
        if args.verbose:
            raise
        for line in cli.describe(error):
            print(f"{APP}: {line}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
