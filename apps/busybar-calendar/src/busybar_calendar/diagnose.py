"""`check` and `status`: is the setup right, is the service running, what is on the bar."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from busylib.client import AsyncBusyBar
from busylib.exceptions import BusyBarAPIError

from . import __version__, connect, schedule, service, sources, update
from .config import Config, ConfigError
from .events import Event

LAN_TOKEN_LIMIT = 32  # the firmware refuses longer X-API-Token values


# report(ok, message, **fields): one finding of `check`, printed or collected as JSON.
Report = Callable[..., None]


async def _check_bar(config: Config, report: Report) -> None:
    where = config.bar.address or "BUSY Cloud"
    bar = connect.make_bar(config)
    try:
        try:
            version = await bar.version()
            firmware = await bar.status_firmware()
        except BusyBarAPIError as error:  # it answered: the token, not the network
            if error.status_code not in (401, 403):
                report(False, f"{where}: {error}", area="bar", where=where)
                return
            report(
                False,
                f"{where}: refused ({error.status_code}). {await _access_hint(bar, config, error)}",
                area="bar",
                where=where,
                status=error.status_code,
            )
            return
        except Exception as error:
            report(False, f"{where}: unreachable ({error})", area="bar", where=where)
            return
        report(
            True,
            f"{where}: firmware {firmware.version}, API {version.api_semver}, token accepted",
            area="bar",
            where=where,
            firmware=firmware.version,
            api=version.api_semver,
        )
    finally:
        await bar.aclose()


async def _access_hint(bar: AsyncBusyBar, config: Config, error: BusyBarAPIError) -> str:
    """Why an authenticated call was refused, in the words of the fix."""
    if not config.bar.address:
        return "The cloud token is wrong, revoked, or lacks the BUSY Bar access scope."
    try:
        access = await bar.access()  # answered without a token
    except Exception:
        access = None
    if access is not None and access.mode == "disabled":
        return (
            "The bar's HTTP API over Wi-Fi is disabled: enable it at http://10.0.4.20 "
            "(USB) -> Network -> HTTP API, or use a BUSY Cloud token without an address."
        )
    token = config.bar.token.resolve()
    if not token:
        return "The bar wants its HTTP API password: set bar.token / token_env / token_command."
    if len(token) > LAN_TOKEN_LIMIT:
        return (
            f"The token is {len(token)} characters; the bar takes at most "
            f"{LAN_TOKEN_LIMIT}. Is it a cloud token? Then leave bar.address out."
        )
    return "The bar did not accept the password."


async def _check_source(item: sources.Source, report: Report, tz: tzinfo) -> None:
    now = datetime.now(UTC)
    where = {"area": "calendar", "source": item.name}
    try:
        if isinstance(item, sources.CalDAVSource):
            found = await item.calendars()
            used, unmatched = sources.select(found, item.config.include, item.config.exclude)
            names = ", ".join(c.name for c in used) or "none"
            report(
                bool(used),
                f"{item.name}: signed in; {len(used)} of {len(found)} calendars used: {names}",
                **where,
                calendars=[c.name for c in used],
                available=len(found),
            )
            available = ", ".join(c.name for c in found[:MAX_NAMES])
            more = f" and {len(found) - MAX_NAMES} more" if len(found) > MAX_NAMES else ""
            for pattern in unmatched:
                report(
                    False,
                    f"{item.name}: no calendar matches {pattern!r}; there are: {available}{more}",
                    **where,
                    unmatched=pattern,
                )
            if not used:
                return  # no calendar is read, so there are no events to report on
        with _quiet(sources.logger):  # its warnings repeat what is reported here
            events = await item.fetch(now - timedelta(hours=24), now + timedelta(days=7))
    except Exception as error:
        report(False, f"{item.name}: {error}", **where)
        return
    ahead = sorted((e for e in events if e.end > now), key=lambda e: e.start)
    if not ahead:
        report(True, f"{item.name}: no events in the next 7 days", **where, events=0)
        return
    first = ahead[0]
    report(
        True,
        f"{item.name}: {len(ahead)} events in the next 7 days; next "
        f"{first.start.astimezone(tz):%a %d %b %H:%M} {first.title}",
        **where,
        events=len(ahead),
        next=_event_json(first, tz),
    )


MAX_NAMES = 8


@contextlib.contextmanager
def _quiet(log: logging.Logger) -> Iterator[None]:
    level = log.level
    log.setLevel(logging.ERROR)
    try:
        yield
    finally:
        log.setLevel(level)


def _iso(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).isoformat(timespec="minutes")


def _event_json(event: Event, tz: tzinfo) -> dict:
    return {
        "title": event.title,
        "start": _iso(event.start, tz),
        "end": _iso(event.end, tz),
        "calendar": event.calendar.name,
        "response": event.response,
    }


async def check(config: Config, as_json: bool = False) -> int:
    """Check the setup end to end: the bar, its token, every calendar."""
    findings: list[dict] = []

    def report(good: bool, text: str, **fields: object) -> None:
        findings.append({"ok": good, "message": text, **fields})
        if not as_json:
            print(f"  {'ok ' if good else 'FAIL'} {text}")

    if not as_json:
        print(f"busybar-calendar {__version__}\nBar")
    await _check_bar(config, report)
    if not as_json:
        print("Calendars")
    items = connect.make_sources(config)
    try:
        for item in items:
            await _check_source(item, report, connect.floating_tz(config))
    finally:
        await connect.close_all(items)
    problems = sum(not finding["ok"] for finding in findings)
    if as_json:
        result = {"version": __version__, "ok": not problems, "findings": findings}
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("\nAll good." if not problems else f"\n{problems} problem(s) above.")
    return 0 if not problems else 1


def _when(moment: datetime, now: datetime, tz: tzinfo) -> str:
    minutes = max(0, int((moment - now).total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    delta = f"in {hours}h {minutes:02d}m" if hours else f"in {minutes}m"
    return f"{moment.astimezone(tz):%a %d %b %H:%M} ({delta})"


def _screen_json(screen: schedule.Screen, tz: tzinfo, **moment: str) -> dict:
    return {"kind": screen.kind, **_event_json(screen.event, tz), **moment}


BAR_TIMEOUT = 5.0  # seconds; a bar that takes longer is as good as not answering


async def bar_health(config: Config, timeout: float = BAR_TIMEOUT) -> dict:
    """Whether the bar answers at all: one light, read-only call, as `check` makes."""
    where = config.bar.address or "BUSY Cloud"
    try:
        bar = connect.make_bar(config)
    except ConfigError as error:  # no address and no token: nothing to ask
        return {"where": where, "answering": False, "detail": str(error)}
    try:
        await asyncio.wait_for(bar.version(), timeout)
        return {"where": where, "answering": True, "detail": None}
    except BusyBarAPIError as error:
        if error.status_code in (401, 403):  # it is there; the token is the problem
            return {
                "where": where,
                "answering": True,
                "detail": f"refused ({error.status_code}): `busybar-calendar check` says why",
            }
        return {"where": where, "answering": False, "detail": str(error)}
    except Exception as error:
        detail = (
            "no answer" if isinstance(error, TimeoutError) else str(error) or type(error).__name__
        )
        return {"where": where, "answering": False, "detail": detail}
    finally:
        with contextlib.suppress(Exception):
            await bar.aclose()


async def status_data(
    config: Config | None, path: Path | None = None, problems: Sequence[str] = ()
) -> dict:
    """
    What `status` reports, as data: the service, the config's problems if it
    does not load, and the calendar's screen now and next.
    """
    state = service.status()
    data: dict = {
        "version": __version__,
        "service": {
            "installed": state.installed,
            "running": state.running,
            "pid": state.pid,
            "detail": state.detail or None,
            "log": str(state.log) if state.log and state.log.exists() else None,
            "recent_log": service.recent_log(state),
            "version": service.running_version(state) if state.running else None,
            "python": service.installed_python() if state.installed else None,
            "auto_update": service.auto_updates() if state.installed else False,
        },
        "config": {"path": str(path) if path else None, "problems": list(problems)},
        "bar": None,
        "calendar": None,
    }
    if config is None:
        return data
    data["bar"] = await bar_health(config)
    now = datetime.now(UTC)
    runner = await connect.read_calendars(config, now)
    tz = connect.floating_tz(config)
    screen = schedule.choose(now, runner.events, runner.settings_for)
    following = schedule.upcoming(now, runner.events, runner.settings_for)
    data["calendar"] = {
        "lookahead_hours": config.sync.lookahead_hours,
        "now": _screen_json(screen, tz, until=_iso(screen.until, tz)) if screen else None,
        "next": _screen_json(following[1], tz, at=_iso(following[0], tz)) if following else None,
    }
    return data


def _environment(python: str) -> str:
    """The environment an interpreter belongs to (its bin/ folder's parent), home as ~."""
    folder = Path(python).parent.parent
    with contextlib.suppress(ValueError):
        return str(Path("~") / folder.relative_to(Path.home()))
    return str(folder)


def upgrade_command(python: str) -> str | None:
    """`uv tool upgrade` for a uv tool installed from git, the install it can move forward.

    A tool installed from a wheel file would reinstall that same file.
    """
    return " ".join(update.upgrade_command()) if update.from_git(python) else None


def print_status(data: dict) -> None:
    print(f"busybar-calendar {data['version']}")
    svc = data["service"]
    if svc["running"]:
        pid = f", pid {svc['pid']}" if svc["pid"] else ""
        running = svc.get("version")
        updating = ", updates itself at each start" if svc.get("auto_update") else ""
        print(f"Service   running{f' {running}' if running else ''}{pid}{updating}")
        if running and running != data["version"]:
            python = svc.get("python")
            if svc.get("auto_update"):
                print(
                    f"          that is {running}, not this {data['version']}: "
                    "`busybar-calendar restart` updates it"
                )
            elif python and python != sys.executable:
                # Another environment: restarting it starts the same old version again.
                print(
                    f"          that is {running}, from {_environment(python)}, "
                    f"not this {data['version']}:"
                )
                command = upgrade_command(python)
                how = f"`{command}`" if command else "upgrade that one"
                print(f"          {how}, then `busybar-calendar restart`")
            else:
                print(
                    f"          that is {running}, but this is {data['version']}: "
                    "`busybar-calendar restart` runs this one"
                )
    elif svc["installed"]:
        why = f" ({svc['detail']})" if svc["detail"] else ""
        print(f"Service   stopped{why}; the log says why")
    else:
        print("Service   not installed: `busybar-calendar install` runs it in the background")
    if svc["log"]:
        print(f"          log {svc['log']}")
    for line in svc["recent_log"]:
        print(f"            {line}")
    bar = data.get("bar")
    if bar is not None:
        if bar["answering"] and not bar["detail"]:
            print(f"Bar       {bar['where']}: answering")
        elif bar["answering"]:
            print(f"Bar       {bar['where']}: {bar['detail']}")
        else:
            print(
                f"Bar       {bar['where']}: NOT answering ({bar['detail']}). Is it on and "
                "online? If it stays stuck, unplug it and plug it back in."
            )
    problems = data["config"]["problems"]
    if problems:
        print(f"Config    does not load: {data['config']['path']}")
        if svc["running"]:
            print("          the running service keeps the config it last loaded")
        for line in problems:
            print(f"            {line}")
    calendar = data["calendar"]
    if calendar is None:
        if not problems:
            print("Calendar  no config yet: `busybar-calendar init` writes one")
        return
    now = datetime.now(UTC)
    if calendar["now"] is None:
        print("Calendar  nothing on the bar now")
    else:
        current = calendar["now"]
        until = datetime.fromisoformat(current["until"])
        print(
            f"Calendar  now {current['kind']}: {current['title']}, "
            f"until {_when(until, now, until.tzinfo)}"
        )
    if calendar["next"] is None:
        print(f"          nothing more in the next {calendar['lookahead_hours']} hours")
    else:
        nxt = calendar["next"]
        at = datetime.fromisoformat(nxt["at"])
        print(f"          next {nxt['kind']}: {nxt['title']}, at {_when(at, now, at.tzinfo)}")


async def status(
    config: Config | None,
    as_json: bool = False,
    *,
    path: Path | None = None,
    problems: Sequence[str] = (),
) -> int:
    """Is it running, what did it last do, and what is on the bar now and next.

    1 when the config does not load (`problems`), so a script notices.
    """
    data = await status_data(config, path, problems)
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_status(data)
    return 1 if problems else 0
