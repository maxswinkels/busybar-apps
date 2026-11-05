import io
import json
import shlex
import sys

from busybar_cclimits import feed, hook, limits

NOW = 1_800_000_000.0
PROG = ["/home/me/.local/bin/busybar-cclimits"]


def stdin(obj) -> io.BytesIO:
    return io.BytesIO(json.dumps(obj).encode())


def test_own_line():
    status = {
        "rate_limits": {
            "five_hour": {"used_percentage": 42.4, "resets_at": NOW + 3600 + 17 * 60 + 30},
            "seven_day": {"used_percentage": 18, "resets_at": NOW + 3 * 86400 + 4 * 3600 + 5},
        }
    }
    assert feed.own_line(status, NOW) == "5h 42% (1h17) · week 18% (3d4h)"
    assert feed.own_line({"model": {"display_name": "Opus"}}, NOW) == "Opus"
    assert feed.own_line(None, NOW) == "Claude Code"


def test_run_stores_and_prints(tmp_path):
    path = tmp_path / "limits.json"
    out = io.StringIO()
    code = feed.run(None, path, stdin({"rate_limits": {"five_hour": {"used_percentage": 5}}}), out)
    assert code == 0 and out.getvalue() == "5h 5%\n"
    assert limits.read(path).windows["five_hour"].used == 5


def test_run_hands_the_same_input_to_your_command(tmp_path, capfd):
    script = "import sys; d = sys.stdin.read(); print(len(d)); sys.exit(3)"
    then = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    data = {"model": {"display_name": "Opus"}}
    code = feed.run(then, tmp_path / "limits.json", stdin(data))
    assert code == 3
    assert capfd.readouterr().out.strip() == str(len(json.dumps(data)))


def test_run_survives_bad_input_and_an_unwritable_feed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    out = io.StringIO()
    assert feed.run(None, blocker / "limits.json", io.BytesIO(b"{oops"), out) == 0
    assert out.getvalue() == "Claude Code\n"


def test_wrap_and_unwrap_round_trip():
    mine = "bash '/Users/me/my status.sh' | tr a b"
    command = hook.wrap(mine, PROG)
    assert hook.unwrap(command) == (True, mine)
    assert hook.unwrap(hook.wrap(None, PROG)) == (True, None)
    assert hook.unwrap(mine) == (False, None)
    assert hook.unwrap("python -m busybar_cclimits statusline") == (True, None)
    assert hook.unwrap("python3 /x/busybar-cclimits/app.py statusline --then ls") == (True, "ls")
    assert hook.unwrap("echo statusline") == (False, None)
    assert hook.unwrap("unbalanced 'quote") == (False, None)


def test_install_wraps_and_keeps_the_rest():
    settings = {
        "model": "opus",
        "statusLine": {"type": "command", "command": "bash ~/s.sh", "refreshInterval": 10},
    }
    change = hook.plan_install(settings, PROG)
    line = change.settings["statusLine"]
    assert line["refreshInterval"] == 10 and change.settings["model"] == "opus"
    assert hook.unwrap(line["command"]) == (True, "bash ~/s.sh")
    assert settings["statusLine"]["command"] == "bash ~/s.sh"  # not modified in place
    again = hook.plan_install(change.settings, PROG)
    assert again.note == "already installed" and again.settings == change.settings
    moved = hook.plan_install(change.settings, ["/opt/busybar-cclimits"])
    assert moved.note == "updated the installed hook"
    assert hook.unwrap(moved.after) == (True, "bash ~/s.sh")


def test_install_without_a_status_line_and_uninstall():
    change = hook.plan_install({}, PROG)
    assert change.note == "added a status line"
    assert change.settings == {"statusLine": {"type": "command", "command": hook.wrap(None, PROG)}}
    assert hook.plan_uninstall(change.settings).settings == {}


def test_uninstall_restores():
    installed = hook.plan_install({"statusLine": {"type": "command", "command": "x"}}, PROG)
    back = hook.plan_uninstall(installed.settings)
    assert back.settings == {"statusLine": {"type": "command", "command": "x"}}
    assert hook.plan_uninstall(back.settings).note == "not installed"


def test_save_backs_up(tmp_path):
    path = tmp_path / "settings.json"
    assert hook.save(path, {"a": 1}) is None
    backup = hook.save(path, {"a": 2})
    assert json.loads(backup.read_text()) == {"a": 1}
    assert hook.load(path) == {"a": 2}
    assert hook.installed(path) == (False, None)
