import plistlib
from pathlib import Path

import pytest

from busybar_calendar import service


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def ran(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs.get("check", False)))
        gone = command[:2] == ["launchctl", "print"]  # 113: no such service
        return service.subprocess.CompletedProcess(command, 113 if gone else 0, "", "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    return calls


def test_launchd_agent(home):
    plan = service.install_plan(Path("cfg/config.toml"), "darwin")
    assert plan.path == home / "Library/LaunchAgents/app.busybar.calendar.plist"
    agent = plistlib.loads(plan.content)
    assert agent["ProgramArguments"][1:] == ["-m", "busybar_calendar", "run"]
    # An absolute config path, so the agent does not depend on a working directory.
    assert Path(agent["EnvironmentVariables"]["BUSYBAR_CALENDAR_CONFIG"]).is_absolute()
    assert agent["RunAtLoad"] and agent["KeepAlive"]
    assert agent["ThrottleInterval"] == service.RESTART_AFTER
    assert plan.commands[0][:2] == ["launchctl", "bootstrap"]


def test_systemd_unit(home):
    config = (home / "cal.toml").resolve()
    plan = service.install_plan(config, "linux")
    assert plan.path == home / ".config/systemd/user/busybar-calendar.service"
    unit = plan.content.decode()
    assert f"Environment=BUSYBAR_CALENDAR_CONFIG={config}" in unit
    assert "-m busybar_calendar run" in unit
    assert f"RestartSec={service.RESTART_AFTER}" in unit
    assert plan.commands[-1] == ["systemctl", "--user", "enable", "--now", service.UNIT]


def test_other_platforms_are_refused():
    with pytest.raises(OSError, match="macOS and Linux"):
        service.install_plan(Path("c.toml"), "win32")


def test_install_writes_then_loads(home, ran):
    plan = service.install_plan(Path("c.toml"), "darwin")
    service.install(plan)
    assert plan.path.read_bytes() == plan.content
    assert plan.log.parent.is_dir()
    # Unloading an older agent may fail; loading the new one must not.
    assert ran == [
        (list(plan.cleanup[0]), False),
        (list(plan.gone), False),  # gone already: no wait
        (plan.commands[0], True),
    ]


def test_a_reinstall_waits_for_the_old_agent_to_be_gone(home, monkeypatch):
    calls, naps = [], []
    still_there = iter([0, 0, 113])  # launchctl print: loaded, loaded, gone

    def fake_run(command, **kwargs):
        calls.append(command[1])
        code = next(still_there) if command[1] == "print" else 0
        return service.subprocess.CompletedProcess(command, code, "", "")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    monkeypatch.setattr(service.time, "sleep", naps.append)
    service.install(service.install_plan(Path("c.toml"), "darwin"))
    # Bootstrapping before the old agent is gone fails with exit 5.
    assert calls == ["bootout", "print", "print", "print", "bootstrap"]
    assert len(naps) == 2


def test_the_wait_for_the_old_agent_is_bounded(home, monkeypatch):
    clock = iter(range(100))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        return service.subprocess.CompletedProcess(command, 0, "", "")  # never gone

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    service.install(service.install_plan(Path("c.toml"), "darwin"))
    assert calls[-1] == "bootstrap"  # tried anyway after GONE_WITHIN, to say what is wrong
    assert calls.count("print") <= service.GONE_WITHIN + 2


def test_uninstall(home, ran):
    plan = service.uninstall_plan("linux")
    assert service.uninstall(plan) is False  # nothing installed
    plan.path.parent.mkdir(parents=True)
    plan.path.write_text("[Unit]\n")
    assert service.uninstall(plan) is True
    assert not plan.path.exists()
    assert [c for c, _ in ran] == [list(plan.cleanup[0]), plan.commands[0]]


def test_systemd_quotes_paths_with_spaces():
    unit = service.systemd_unit(["/opt/my tools/python", "-m", "x"], Path("/a b/c.toml"))
    text = unit.decode()
    assert 'Environment="BUSYBAR_CALENDAR_CONFIG=/a b/c.toml"' in text
    assert 'ExecStart="/opt/my tools/python" -m x' in text


def test_installed_python_reads_the_service_file(home):
    for platform in ("darwin", "linux"):
        assert service.installed_python(platform) is None  # not installed
        plan = service.install_plan(Path("c.toml"), platform)
        plan.path.parent.mkdir(parents=True)
        command = ["/opt/my tools/bin/python", "-m", "busybar_calendar", "run"]
        plan.path.write_bytes(
            service.launchd_plist(command, Path("/c.toml"), Path("/l.log"))
            if platform == "darwin"
            else service.systemd_unit(command, Path("/c.toml"))
        )
        assert service.installed_python(platform) == "/opt/my tools/bin/python"
        plan.path.write_text("garbage")
        assert service.installed_python(platform) is None


# Shapes taken from `launchctl print` on macOS: the job's fields one tab deep,
# nested blocks (which repeat `state`) deeper.
RUNNING = """gui/501/app.busybar.calendar = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/app.busybar.calendar.plist
\tstate = running
\tprogram = /usr/bin/python3
\truns = 1
\tpid = 20464
\tlast exit code = (never exited)
\tendpoints = {
\t\tstate = active
\t}
}
"""
STOPPED = (
    RUNNING.replace("state = running", "state = not running")
    .replace("\tpid = 20464\n", "")
    .replace("(never exited)", "1")
)


def test_parse_launchctl_takes_only_the_jobs_own_fields():
    fields = service.parse_launchctl(RUNNING)
    assert (fields["state"], fields["pid"]) == ("running", "20464")


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (Done(0, RUNNING), (True, 20464, "")),
        (Done(0, STOPPED), (False, None, "last exit code 1")),
        (Done(113, ""), (False, None, "not loaded")),
    ],
)
def test_status_on_macos(home, monkeypatch, output, expected):
    agent = home / "Library/LaunchAgents/app.busybar.calendar.plist"
    agent.parent.mkdir(parents=True)
    agent.write_text("<plist/>")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: output)
    state = service.status("darwin")
    assert state.installed
    assert (state.running, state.pid, state.detail) == expected
    assert state.log == home / "Library/Logs/busybar-calendar.log"


def test_status_not_installed(home, monkeypatch):
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k: Done(113))
    state = service.status("darwin")
    assert (state.installed, state.running, state.detail) == (False, False, "")


def test_status_on_linux(home, monkeypatch):
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **k: Done(0, "ActiveState=failed\nMainPID=0\nExecMainStatus=1\n"),
    )
    state = service.status("linux")
    assert (state.running, state.pid, state.detail) == (False, None, "last exit code 1")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *a, **k: Done(0, "ActiveState=active\nMainPID=4321\nExecMainStatus=0\n"),
    )
    assert service.status("linux").pid == 4321


def test_recent_log_reads_the_tail(tmp_path):
    log = tmp_path / "busybar-calendar.log"
    log.write_text("\n".join(f"line {n}" for n in range(20)))
    state = service.Status(True, True, log=log)
    assert service.recent_log(state, 3) == ["line 17", "line 18", "line 19"]
    assert service.recent_log(service.Status(True, True, log=tmp_path / "none"), 3) == []


def test_restart_only_an_installed_service(home, ran):
    assert service.restart("darwin") is False
    assert ran == []
    agent = home / "Library/LaunchAgents/app.busybar.calendar.plist"
    agent.parent.mkdir(parents=True)
    agent.write_text("<plist/>")
    assert service.restart("darwin") is True
    # Unloaded and loaded again: `kickstart -k` waits out the 5-minute throttle.
    assert [command[:2] for command, _ in ran] == [
        ["launchctl", "bootout"],
        ["launchctl", "print"],  # gone: load it
        ["launchctl", "bootstrap"],
    ]
    assert ran[-1][0][-1] == str(agent)


@pytest.fixture
def uv_from_git(monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: "/opt/uv/bin/uv")
    monkeypatch.setattr(service.update, "from_git", lambda python: True)


def test_a_self_updating_launchd_agent(home, uv_from_git):
    plan = service.install_plan(Path("c.toml"), "darwin", auto_update=True)
    agent = plistlib.loads(plan.content)
    assert agent["ProgramArguments"][1:] == ["-m", "busybar_calendar", "run", "--auto-update"]
    env = agent["EnvironmentVariables"]
    # launchd's own PATH has neither uv nor its folder; git and `security` are in /usr/bin.
    assert env["PATH"].split(":")[:2] == ["/opt/uv/bin", "/usr/local/bin"]
    assert "/usr/bin" in env["PATH"].split(":")
    assert Path(env["BUSYBAR_CALENDAR_CONFIG"]).is_absolute()
    assert agent["RunAtLoad"] and agent["KeepAlive"]
    assert not service.auto_updates("darwin")  # not installed yet
    plan.path.parent.mkdir(parents=True)
    plan.path.write_bytes(plan.content)
    assert service.auto_updates("darwin")


def test_a_self_updating_systemd_unit(home, uv_from_git):
    plan = service.install_plan(Path("c.toml"), "linux", auto_update=True)
    unit = plan.content.decode()
    assert "-m busybar_calendar run --auto-update" in unit
    assert "Environment=PATH=/opt/uv/bin:/usr/local/bin:/usr/bin:" in unit
    plan.path.parent.mkdir(parents=True)
    plan.path.write_bytes(plan.content)
    assert service.auto_updates("linux")


def test_auto_update_needs_uv_and_an_install_from_git(home, monkeypatch):
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    with pytest.raises(OSError, match="no uv on PATH"):
        service.install_plan(Path("c.toml"), "darwin", auto_update=True)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/opt/uv/bin/uv")
    monkeypatch.setattr(service.update, "from_git", lambda python: False)
    with pytest.raises(OSError, match="installed from git"):
        service.install_plan(Path("c.toml"), "darwin", auto_update=True)
    # Without it, neither is needed, and the agent is as before.
    agent = plistlib.loads(service.install_plan(Path("c.toml"), "darwin").content)
    assert "PATH" not in agent["EnvironmentVariables"]
    assert agent["ProgramArguments"][-1] == "run"
