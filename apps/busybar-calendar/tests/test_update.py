import os
import subprocess
import sys

import pytest

from busybar_calendar import __version__, update


@pytest.fixture
def git_tool(monkeypatch):
    monkeypatch.setattr(update, "from_git", lambda python: True)
    monkeypatch.setattr(update.shutil, "which", lambda name: "/opt/uv/bin/uv")


def on_disk(monkeypatch, *versions):
    seen = iter(versions)
    monkeypatch.setattr(update, "installed", lambda: next(seen))


def answers(code=0, stderr=""):
    return lambda command, **kwargs: subprocess.CompletedProcess(command, code, "", stderr)


def test_an_update_that_brings_new_code(git_tool, monkeypatch, caplog):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(update.subprocess, "run", run)
    on_disk(monkeypatch, "0.3.1 (2713373)", "0.6.1 (0046d3c)")
    with caplog.at_level("INFO"):
        assert update.update() is True
    assert calls == [(["/opt/uv/bin/uv", "tool", "upgrade", "busybar-calendar"], update.TIMEOUT)]
    assert "0.3.1 (2713373) -> 0.6.1 (0046d3c)" in caplog.text


def test_new_commits_count_even_without_a_new_version(git_tool, monkeypatch):
    monkeypatch.setattr(update.subprocess, "run", answers())
    on_disk(monkeypatch, "0.6.1 (0046d3c)", "0.6.1 (9f8e7d6)")
    assert update.update() is True


def test_already_the_latest(git_tool, monkeypatch, caplog):
    monkeypatch.setattr(update.subprocess, "run", answers())
    on_disk(monkeypatch, "0.6.1 (0046d3c)", "0.6.1 (0046d3c)")
    with caplog.at_level("INFO"):
        assert update.update() is False
    assert "0.6.1 (0046d3c) is the latest" in caplog.text


def test_a_failed_update_starts_what_is_there(git_tool, monkeypatch, caplog):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    failing = (answers(2, "error: Failed to fetch\n  Caused by: dns error\n"), timeout, missing)
    for run in failing:  # no network yet at login, a hung fetch, uv gone
        monkeypatch.setattr(update.subprocess, "run", run)
        on_disk(monkeypatch, "0.6.1 (0046d3c)")
        assert update.update() is False
    assert caplog.text.count("staying on 0.6.1 (0046d3c)") == 3
    assert "Caused by: dns error" in caplog.text  # uv's last line says why


def test_only_a_uv_tool_from_git_updates_itself(monkeypatch, caplog):
    def never(command, **kwargs):
        raise AssertionError("must not run uv")

    monkeypatch.setattr(update.subprocess, "run", never)
    monkeypatch.setattr(update, "from_git", lambda python: False)
    assert update.update() is False
    assert "only a uv tool installed from git" in caplog.text
    monkeypatch.setattr(update, "from_git", lambda python: True)
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    assert update.update() is False
    assert "no uv on PATH" in caplog.text


def test_installed_reads_the_version_on_disk():
    assert update.installed().startswith(__version__)


def test_restart_into_update_reruns_the_same_command(monkeypatch):
    seen = []
    monkeypatch.setattr(update.os, "execv", lambda path, argv: seen.append((path, argv)))
    monkeypatch.setenv(update.UPDATED_ENV, "")
    argv = ["/x/bin/python", "-m", "busybar_calendar", "run", "--auto-update"]
    monkeypatch.setattr(update.sys, "orig_argv", argv)
    update.restart_into_update()
    assert seen == [(sys.executable, [sys.executable, *argv[1:]])]
    assert os.environ[update.UPDATED_ENV] == "1"  # the new process does not update again
