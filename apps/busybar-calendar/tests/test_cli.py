import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from busylib import types
from busylib.exceptions import BusyBarAPIError

from busybar_calendar import cli, connect, diagnose
from busybar_calendar import config as config_module
from busybar_calendar.config import CONFIG_ENV, Config, ConfigError, find
from busybar_calendar.events import CalendarRef, Event

SOURCES = {"sources": [{"name": "Feed", "type": "ics", "url": "https://x"}]}


def test_find_prefers_flag_then_env_then_cwd_then_config_dir(tmp_path, monkeypatch):
    home = tmp_path / "xdg"
    (home / "busybar-calendar").mkdir(parents=True)
    in_home = home / "busybar-calendar" / "config.toml"
    in_home.write_text("")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert find() == in_home
    (tmp_path / "config.toml").write_text("")
    assert find() == Path("config.toml")
    monkeypatch.setenv(CONFIG_ENV, "/elsewhere.toml")
    assert find() == Path("/elsewhere.toml")
    assert find("given.toml") == Path("given.toml")


def test_find_says_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="busybar-calendar init"):
        find()


class FakeBar:
    def __init__(self, refuse=None, mode="disabled"):
        self.refuse, self.mode = refuse, mode

    async def version(self):
        return types.VersionInfo(api_semver="27.5.0")

    async def status_firmware(self):
        if self.refuse:
            raise BusyBarAPIError("Forbidden", status_code=self.refuse)
        return types.StatusFirmware(version="1.2.4")

    async def access(self):
        return types.HttpAccessInfo(mode=self.mode, key_valid=False)

    async def aclose(self):
        pass


class FakeSource:
    name = "Feed"

    async def fetch(self, start, end):
        return []

    async def aclose(self):
        pass


async def check(monkeypatch, capsys, bar, data=None):
    config = Config.model_validate(data or SOURCES)
    monkeypatch.setattr(connect, "make_bar", lambda _config: bar)
    monkeypatch.setattr(connect, "make_sources", lambda _config: [FakeSource()])
    code = await diagnose.check(config)
    return code, capsys.readouterr().out


async def test_check_all_good(monkeypatch, capsys):
    code, out = await check(monkeypatch, capsys, FakeBar())
    assert code == 0
    assert "firmware 1.2.4, API 27.5.0, token accepted" in out
    assert "Feed: no events in the next 7 days" in out
    assert "All good." in out


async def test_check_explains_a_refused_lan_call(monkeypatch, capsys):
    lan = {**SOURCES, "bar": {"address": "192.168.1.50", "token": "x" * 43}}
    code, out = await check(monkeypatch, capsys, FakeBar(refuse=403), lan)
    assert code == 1
    assert "HTTP API over Wi-Fi is disabled" in out

    code, out = await check(monkeypatch, capsys, FakeBar(refuse=403, mode="key"), lan)
    assert "43 characters" in out and "cloud token" in out


def test_a_missing_config_says_how_to_make_one(tmp_path):
    from busybar_calendar.config import load

    with pytest.raises(ConfigError, match="busybar-calendar init"):
        load(tmp_path / "nope.toml")


async def test_status_without_a_config(monkeypatch, capsys):
    from busybar_calendar import service

    monkeypatch.setattr(service, "status", lambda: service.Status(False, False))
    monkeypatch.setattr(service, "recent_log", lambda state: [])
    assert await diagnose.status(None) == 0
    out = capsys.readouterr().out
    assert "not installed" in out and "busybar-calendar install" in out
    assert "busybar-calendar init" in out


async def test_status_shows_now_and_next(monkeypatch, capsys, tmp_path):
    from datetime import UTC, datetime, timedelta

    from busybar_calendar import service
    from busybar_calendar.events import CalendarRef, Event

    log = tmp_path / "busybar-calendar.log"
    log.write_text("16:40:19 reminder: Design review\n")
    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=4321, log=log))
    now = datetime.now(UTC)
    ref = CalendarRef("Feed", "Feed", 0)
    running = Event(
        "a", "Design review", now - timedelta(minutes=5), now + timedelta(minutes=25), ref
    )
    later = Event("b", "Retro", now + timedelta(hours=2), now + timedelta(hours=3), ref)

    class Source:
        name = "Feed"

        async def fetch(self, start, end):
            return [running, later]

        async def aclose(self):
            pass

    monkeypatch.setattr(connect, "make_sources", lambda config: [Source()])
    monkeypatch.setattr(connect, "make_bar", lambda config: FakeBar())
    assert await diagnose.status(Config.model_validate(SOURCES)) == 0
    out = capsys.readouterr().out
    assert "running, pid 4321" in out
    assert "reminder: Design review" in out  # the log tail
    assert "now status: Design review" in out
    assert "next reminder: Retro" in out


def test_version(capsys):
    from busybar_calendar import __version__

    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out.strip() == f"busybar-calendar {__version__}"
    assert __version__[0].isdigit()  # read from the installed package


async def test_check_as_json(monkeypatch, capsys):
    import argparse

    config = Config.model_validate(SOURCES)
    monkeypatch.setattr(connect, "make_bar", lambda _config: FakeBar())
    monkeypatch.setattr(connect, "make_sources", lambda _config: [FakeSource()])
    code = await cli.cmd_check(config, argparse.Namespace(json=True))  # through the CLI
    result = json.loads(capsys.readouterr().out)  # nothing else on stdout
    assert code == 0
    assert result["ok"] is True
    bar, calendar = result["findings"]
    assert (bar["area"], bar["firmware"], bar["api"]) == ("bar", "1.2.4", "27.5.0")
    assert (calendar["area"], calendar["source"], calendar["events"]) == ("calendar", "Feed", 0)


async def test_status_as_json(monkeypatch, capsys):
    from datetime import UTC, datetime, timedelta

    from busybar_calendar import service
    from busybar_calendar.events import CalendarRef, Event

    monkeypatch.setattr(
        service, "status", lambda: service.Status(True, False, detail="last exit code 1")
    )
    monkeypatch.setattr(service, "recent_log", lambda state: ["Feed: 401, check the credentials"])
    monkeypatch.setattr(service, "installed_python", lambda: "/opt/venv/bin/python")
    now = datetime.now(UTC)
    later = Event(
        "b",
        "Retro",
        now + timedelta(hours=2),
        now + timedelta(hours=3),
        CalendarRef("Feed", "Feed", 0),
        "tentative",
    )

    class Source:
        name = "Feed"

        async def fetch(self, start, end):
            return [later]

        async def aclose(self):
            pass

    monkeypatch.setattr(connect, "make_sources", lambda config: [Source()])
    monkeypatch.setattr(connect, "make_bar", lambda config: FakeBar())
    await diagnose.status(Config.model_validate(SOURCES), as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["service"] == {
        "installed": True,
        "running": False,
        "pid": None,
        "detail": "last exit code 1",
        "log": None,
        "recent_log": ["Feed: 401, check the credentials"],
        "version": None,
        "python": "/opt/venv/bin/python",
        "auto_update": False,
    }
    assert data["calendar"]["now"] is None
    nxt = data["calendar"]["next"]
    assert (nxt["kind"], nxt["title"], nxt["response"]) == ("reminder", "Retro", "tentative")
    assert datetime.fromisoformat(nxt["at"]) < datetime.fromisoformat(nxt["start"])


async def test_check_tells_a_refused_token_from_an_unreachable_bar(monkeypatch, capsys):
    class RefusingCloud(FakeBar):
        async def version(self):  # the cloud wants a valid token for every call
            raise BusyBarAPIError("Forbidden", status_code=403)

    code, out = await check(monkeypatch, capsys, RefusingCloud())
    assert code == 1
    assert "refused (403)" in out and "cloud token is wrong" in out
    assert "unreachable" not in out

    class Offline(FakeBar):
        async def version(self):
            raise ConnectionError("no route to host")

    code, out = await check(monkeypatch, capsys, Offline())
    assert "unreachable (no route to host)" in out


async def test_check_names_the_calendars_a_typo_could_have_meant(monkeypatch, capsys):
    from zoneinfo import ZoneInfo

    from busybar_calendar import sources
    from busybar_calendar.caldav import RemoteCalendar

    data = {
        "sources": [{"name": "Work", "type": "caldav", "url": "https://x", "include": ["Wrok"]}]
    }
    config = Config.model_validate(data)
    item = sources.CalDAVSource(config.sources[0], 0, ZoneInfo("UTC"), True)
    found = [RemoteCalendar("https://x/1/", "Team"), RemoteCalendar("https://x/2/", "Work")]

    async def calendars():
        return found

    async def fetch(start, end):
        raise AssertionError("no calendar is used, so nothing should be fetched")

    item.calendars, item.fetch = calendars, fetch
    monkeypatch.setattr(connect, "make_bar", lambda _config: FakeBar())
    monkeypatch.setattr(connect, "make_sources", lambda _config: [item])
    assert await diagnose.check(config) == 1
    out = capsys.readouterr().out
    assert "no calendar matches 'Wrok'; there are: Team, Work" in out
    assert "no events" not in out  # no false all-clear for a source that reads nothing
    await item.aclose()


def test_restart(monkeypatch, capsys):
    from busybar_calendar import service

    monkeypatch.setattr(service, "restart", lambda: True)
    assert cli.main(["restart"]) == 0
    assert "Restarted" in capsys.readouterr().out
    monkeypatch.setattr(service, "restart", lambda: False)
    assert cli.main(["restart"]) == 1
    assert "Not installed" in capsys.readouterr().out


def test_bare_command_suggests_first_steps(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    monkeypatch.delenv("BUSYBAR_CALENDAR_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "No config yet" in out and "busybar-calendar init" in out


def test_bare_command_shows_status_once_configured(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[[sources]]\nname = "Feed"\ntype = "ics"\nurl = "https://x"\n')
    seen = []

    async def fake_status(loaded, as_json=False, **where):
        seen.append(loaded.sources[0].name)
        return 0

    monkeypatch.setattr(diagnose, "status", fake_status)
    assert cli.main(["-c", str(config)]) == 0
    assert seen == ["Feed"]
    assert "--help lists every command" in capsys.readouterr().out


def test_a_failing_launchctl_is_one_line_not_a_traceback(tmp_path, monkeypatch, capsys):
    import subprocess

    from busybar_calendar import service

    config = tmp_path / "config.toml"
    config.write_text('[[sources]]\nname = "Feed"\ntype = "ics"\nurl = "https://x"\n')
    monkeypatch.setattr(service, "_home", lambda: tmp_path)

    def refuse(plan):
        raise subprocess.CalledProcessError(5, ["launchctl", "bootstrap", "gui/501", "x.plist"])

    monkeypatch.setattr(service, "install", refuse)
    assert cli.main(["-c", str(config), "install", "--force"]) == 1
    err = capsys.readouterr().err
    assert "launchctl bootstrap gui/501 x.plist failed (exit 5)" in err


def _config_file(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return str(path)


def test_config_mistakes_are_one_line_each(tmp_path, capsys):
    path = _config_file(
        tmp_path,
        '[bar]\nadress = "x"\n[sync]\ntimezone = "Europe/Kiyv"\n'
        '[[sources]]\nname = "Feed"\ntype = "ics"\nurl = "https://x"\n',
    )
    assert cli.main(["-c", path, "upcoming"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert "busybar-calendar: bar.adress: Extra inputs are not permitted" in err
    assert any(line.startswith("busybar-calendar: sync.timezone: 'Europe/Kiyv'") for line in err)
    assert not any("pydantic.dev" in line for line in err)


def test_an_unreachable_server_is_one_line(tmp_path, monkeypatch, capsys):
    import httpx

    path = _config_file(
        tmp_path,
        '[[sources]]\nname = "Gone"\ntype = "caldav"\nurl = "https://dav.invalid/"\n',
    )

    async def unreachable(self):
        request = httpx.Request("PROPFIND", "https://dav.invalid/")
        raise httpx.ConnectError("name or service not known", request=request)

    from busybar_calendar import sources

    monkeypatch.setattr(sources.CalDAVSource, "calendars", unreachable)
    assert cli.main(["-c", path, "calendars"]) == 1
    assert capsys.readouterr().err.strip() == (
        "busybar-calendar: cannot reach https://dav.invalid/: name or service not known"
    )


async def test_status_says_when_the_service_runs_another_version(monkeypatch, capsys, tmp_path):
    from busybar_calendar import __version__, service

    log = tmp_path / "busybar-calendar.log"
    log.write_text(
        "2026-10-01 INFO busybar_calendar: busybar-calendar 0.0.9 started: 1 calendar source(s)\n"
        "2026-10-12 INFO busybar_calendar: busybar-calendar 0.1.0 started: 1 calendar source(s)\n"
        "2026-10-12 INFO busybar_calendar.runner: cleared\n"
    )
    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=7, log=log))
    monkeypatch.setattr(service, "installed_python", lambda: sys.executable)  # this install
    assert service.running_version(service.Status(True, True, log=log)) == "0.1.0"  # the last
    await diagnose.status(None)
    out = capsys.readouterr().out
    assert "Service   running 0.1.0, pid 7" in out
    assert f"that is 0.1.0, but this is {__version__}" in out and "restart` runs this one" in out


async def test_status_names_the_install_the_service_runs_from(monkeypatch, capsys, tmp_path):
    from busybar_calendar import __version__, service

    log = tmp_path / "busybar-calendar.log"
    log.write_text("x busybar-calendar 0.1.0 started: 1 calendar source(s)\n")
    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=7, log=log))
    # A checkout run with `uv run`, while the service runs the `uv tool` install:
    # a restart would start 0.1.0 again, so it must not be offered as the fix.
    monkeypatch.setattr(service, "installed_python", lambda: "/opt/tools/old/bin/python")
    await diagnose.status(None)
    out = capsys.readouterr().out
    assert f"that is 0.1.0, from /opt/tools/old, not this {__version__}" in out
    assert "upgrade that one, then `busybar-calendar restart`" in out
    assert "runs this one" not in out


def test_upgrade_command_only_for_a_uv_tool_from_git(tmp_path):
    tool = tmp_path / "tools" / "busybar-calendar"
    (tool / "bin").mkdir(parents=True)
    python = str(tool / "bin" / "python")
    assert diagnose.upgrade_command(python) is None  # not a uv tool
    receipt = tool / "uv-receipt.toml"
    receipt.write_text(
        '[tool]\nrequirements = [{ name = "busybar-calendar", '
        'git = "https://github.com/kurok/busybar-calendar" }]\n'
    )
    assert diagnose.upgrade_command(python) == "uv tool upgrade busybar-calendar"
    # From a wheel file, `uv tool upgrade` would reinstall the same version.
    receipt.write_text(
        '[tool]\nrequirements = [{ name = "busybar-calendar", path = "/d/x-0.3.0.whl" }]\n'
    )
    assert diagnose.upgrade_command(python) is None
    receipt.write_text("not toml [")
    assert diagnose.upgrade_command(python) is None


async def test_status_quiet_when_versions_match(monkeypatch, capsys, tmp_path):
    from busybar_calendar import __version__, service

    log = tmp_path / "busybar-calendar.log"
    log.write_text(f"x busybar-calendar {__version__} started: 1 calendar source(s)\n")
    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=7, log=log))
    await diagnose.status(None)
    out = capsys.readouterr().out
    assert f"running {__version__}, pid 7" in out and "but this is" not in out


async def test_status_says_when_the_bar_does_not_answer(monkeypatch, capsys):
    import asyncio

    from busybar_calendar import service

    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=7))
    monkeypatch.setattr(service, "recent_log", lambda state: [])
    monkeypatch.setattr(connect, "make_sources", lambda config: [FakeSource()])

    class Hung(FakeBar):
        async def version(self):
            await asyncio.sleep(60)  # a bar whose HTTP API hangs

    monkeypatch.setattr(connect, "make_bar", lambda config: Hung())
    monkeypatch.setattr(diagnose, "BAR_TIMEOUT", 0.05)
    health = await diagnose.bar_health(Config.model_validate(SOURCES), timeout=0.05)
    assert health == {"where": "BUSY Cloud", "answering": False, "detail": "no answer"}

    data = {
        "version": "x",
        "service": {
            "installed": True,
            "running": True,
            "pid": 7,
            "detail": None,
            "log": None,
            "recent_log": [],
            "version": None,
        },
        "config": {"path": None, "problems": []},
        "bar": health,
        "calendar": None,
    }
    diagnose.print_status(data)
    out = capsys.readouterr().out
    assert "Bar       BUSY Cloud: NOT answering (no answer)" in out
    assert "unplug it and plug it back in" in out


async def test_bar_health_cases(monkeypatch):
    config = Config.model_validate(SOURCES)
    monkeypatch.setattr(connect, "make_bar", lambda config: FakeBar())
    assert (await diagnose.bar_health(config))["answering"] is True

    class Refusing(FakeBar):
        async def version(self):
            raise BusyBarAPIError("Forbidden", status_code=403)

    monkeypatch.setattr(connect, "make_bar", lambda config: Refusing())
    health = await diagnose.bar_health(config)
    assert health["answering"] is True and "refused (403)" in health["detail"]

    def no_bar(config):
        raise ConfigError("give bar.address, or a bar-scope cloud token as bar.token")

    monkeypatch.setattr(connect, "make_bar", no_bar)
    assert (await diagnose.bar_health(config))["answering"] is False


WATCHED = """\
[bar]
address = "10.0.4.20"

[[sources]]
name = "Work"
type = "ics"
url = "https://example.com/work.ics"
"""


async def test_the_service_follows_config_edits(tmp_path, caplog):
    from busybar_calendar.runner import Runner

    now = datetime.now(UTC)
    path = tmp_path / "config.toml"
    path.write_text(WATCHED)
    config = config_module.load(path)
    runner = Runner(config, bar=None, sources=connect.make_sources(config))
    watch = cli.ConfigWatch(path, runner)
    try:
        await watch()  # unchanged: nothing to do
        assert runner.config is config

        path.write_text(
            WATCHED + '[[sources]]\nname = "Home"\ntype = "ics"\nurl = "https://example.com/h.ics"'
            '\n\n[[titles]]\nmatch = "Lunch*"\nstatus.show = "lunch"\n'
        )
        await watch()
        assert [s.name for s in runner.sources] == ["Work", "Home"]
        assert (
            runner.settings_for(Event("u", "Lunch", now, now, CalendarRef("Work", "Work", 0)))[
                1
            ].show
            == "lunch"
        )

        applied = runner.config
        path.write_text(WATCHED + '[[titles]]\nmatch = "Lunch"\nstatus.show = "partying"\n')
        await watch()
        assert runner.config is applied  # a broken edit is not applied...
        assert "keeps the config it has" in caplog.text  # ...and the log says why
        assert "titles.0.status: show:" in caplog.text

        path.write_text(WATCHED.replace("10.0.4.20", "192.168.1.50"))
        await watch()
        assert runner.config.bar.address == "10.0.4.20"  # [bar] waits for a restart
        assert "restart" in caplog.text
        assert [s.name for s in runner.sources] == ["Work"]  # the rest is applied

        path.unlink()
        await watch()
        assert [s.name for s in runner.sources] == ["Work"]
    finally:
        await connect.close_all(runner.sources)


def test_status_reports_a_config_that_does_not_load(tmp_path, monkeypatch, capsys):
    from busybar_calendar import service

    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=7))
    monkeypatch.setattr(service, "recent_log", lambda state: [])
    monkeypatch.setattr(service, "running_version", lambda state: None)
    monkeypatch.setattr(service, "installed_python", lambda: None)
    path = tmp_path / "config.toml"
    path.write_text(WATCHED + '[[titles]]\nmatch = "Lunch"\nstatus.show = "partying"\n')

    assert cli.main(["-c", str(path), "status"]) == 1
    out = capsys.readouterr().out
    assert "Service   running, pid 7" in out  # the service part still shows
    assert f"Config    does not load: {path}" in out
    assert "the running service keeps the config it last loaded" in out
    assert "titles.0.status: show: show must be" in out
    assert "no config yet" not in out

    assert cli.main(["-c", str(path), "status", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["config"]["path"] == str(path)
    assert data["config"]["problems"][0].startswith("titles.0.status: show:")
    assert data["service"]["running"] is True

    assert cli.main(["-c", str(path)]) == 1  # the bare command too, not first steps
    assert "does not load" in capsys.readouterr().out


def test_status_with_no_config_file_still_says_how_to_make_one(tmp_path, monkeypatch, capsys):
    from busybar_calendar import service

    monkeypatch.setattr(service, "status", lambda: service.Status(False, False))
    monkeypatch.setattr(service, "recent_log", lambda state: [])
    assert cli.main(["-c", str(tmp_path / "none.toml"), "status"]) == 0
    assert "no config yet" in capsys.readouterr().out
    assert cli.main(["-c", str(tmp_path / "none.toml"), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["config"] == {"path": None, "problems": []}


def test_upcoming_notes_say_what_the_bar_does_and_why():
    config = Config.model_validate(
        {
            **SOURCES,
            "defaults": {"status": {"tentative": False}},
            "titles": [
                {"match": "Focus*", "reminder": {"enabled": False}, "status": {"enabled": False}},
                {"match": ["Lunch", "Обід*"], "status": {"show": "lunch"}},
                {"match": "*", "reminder": {"minutes_before": 2}},
            ],
        }
    )
    now = datetime.now(UTC)
    ref = CalendarRef("Feed", "Feed", 0)

    def notes(title, response="accepted"):
        return cli.upcoming_notes(config, Event("u", title, now, now, ref, response))

    assert notes("Sync") == "reminder 2m, status name, titles '*'"
    # Tentative, with status.tentative off: the reminder only, and the note says why.
    assert notes("Sync", "tentative") == "reminder 2m, tentative, titles '*'"
    assert notes("обід з командою") == "reminder 2m, status lunch, titles 'Обід*' '*'"
    config.titles.pop()  # without the catch-all, Focus time keeps nothing
    assert notes("Focus time") == "hidden, titles 'Focus*'"


def test_run_auto_update_updates_before_reading_the_config(tmp_path, monkeypatch):
    from busybar_calendar import update

    order = []

    class Restarted(Exception):
        pass

    def restart():
        order.append("restart")
        raise Restarted  # os.execv does not return

    monkeypatch.setattr(update, "restart_into_update", restart)
    monkeypatch.setattr(update, "update", lambda: order.append("update") or True)
    monkeypatch.setenv(update.UPDATED_ENV, "")
    command = ["-c", str(tmp_path / "missing.toml"), "run", "--auto-update"]
    with pytest.raises(Restarted):  # even a config this version cannot read waits
        cli.main(command)
    assert order == ["update", "restart"]

    order.clear()
    monkeypatch.setenv(update.UPDATED_ENV, "1")  # the process the update restarted into
    assert cli.main(command) == 1  # straight on to the config, which is missing
    assert order == []

    monkeypatch.setenv(update.UPDATED_ENV, "")
    monkeypatch.setattr(update, "update", lambda: order.append("update") or False)
    assert cli.main(command) == 1  # nothing new: on with the version it has
    assert order == ["update"]
    assert cli.main(command[:-1]) == 1  # plain `run` does not update
    assert order == ["update"]


def test_status_with_a_self_updating_service(monkeypatch, capsys):
    from busybar_calendar import service

    monkeypatch.setattr(service, "status", lambda: service.Status(True, True, pid=7))
    monkeypatch.setattr(service, "recent_log", lambda state: [])
    monkeypatch.setattr(service, "running_version", lambda state: "0.3.1")
    monkeypatch.setattr(service, "installed_python", lambda: "/opt/tools/old/bin/python")
    monkeypatch.setattr(service, "auto_updates", lambda: True)
    data = asyncio.run(diagnose.status_data(None))
    assert data["service"]["auto_update"] is True
    diagnose.print_status(data)
    out = capsys.readouterr().out
    assert "running 0.3.1, pid 7, updates itself at each start" in out
    assert "`busybar-calendar restart` updates it" in out
    assert "uv tool upgrade" not in out
