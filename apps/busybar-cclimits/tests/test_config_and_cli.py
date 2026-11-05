import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from busybar_cclimits import cli, hook
from busybar_cclimits.config import Config, ConfigError, load

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def test_defaults():
    config = Config()
    assert config.bar.priority == 50 and config.bar.address is None
    assert [r.window for r in config.display.rows] == ["five_hour", "seven_day"]
    assert config.display.colors.label == "#D97757FF"


def test_load(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[bar]\naddress = "10.0.4.20"\ntoken_env = "T"\n'
        '[display]\nshow = "with_claude"\n'
        '[[display.rows]]\nwindow = "spend_limit"\ntext = "percent"\n'
        '[display.colors]\nok = "#00ff00ff"\n'
        '[feed]\npath = "~/x.json"\n'
    )
    config = load(path)
    assert config.bar.token.env == "T"
    assert config.display.rows[0].shown_label == "$"
    assert config.display.colors.ok == "#00FF00FF"
    assert config.feed.path == Path("~/x.json").expanduser()


@pytest.mark.parametrize(
    "data",
    [
        {"display": {"colors": {"ok": "#00FF00"}}},  # no alpha byte
        {"display": {"warn_at": 95, "critical_at": 90}},
        {"display": {"rows": []}},
        {"display": {"rows": [{"window": "five_hour", "label": "ABC"}]}},
        {"bar": {"token": "a", "token_env": "B"}},
        {"bar": {"priority": 0}},
        {"nope": 1},
    ],
)
def test_rejects(data):
    with pytest.raises(ValidationError):
        Config.model_validate(data)


def test_load_reports_toml_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[bar\n")
    with pytest.raises(ConfigError):
        load(path)


def test_init_writes_a_config(tmp_path, capsys):
    path = tmp_path / "c" / "config.toml"
    assert cli.main(["-c", str(path), "init", "--cloud", "--keychain", "busybar"]) == 0
    config = load(path)
    assert config.bar.address is None
    assert config.bar.token.command == ["security", "find-generic-password", "-s", "busybar", "-w"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert cli.main(["-c", str(path), "init", "--host", "10.0.4.20"]) == 1  # exists
    assert cli.main(["-c", str(path), "init", "--host", "10.0.4.20", "--force"]) == 0
    assert load(path).bar.address == "10.0.4.20"
    assert cli.main(["-c", str(path), "init", "--cloud", "--force"]) == 2  # no token


def test_hook_command(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text('{"statusLine": {"type": "command", "command": "bash s.sh"}}')
    assert cli.main(["hook", "install", "--settings", str(settings), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert hook.installed(settings) == (False, "bash s.sh")
    assert cli.main(["hook", "install", "--settings", str(settings)]) == 0
    assert hook.installed(settings)[0]
    assert cli.main(["hook", "uninstall", "--settings", str(settings)]) == 0
    assert hook.installed(settings) == (False, "bash s.sh")


def test_status_without_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("BUSYBAR_CCLIMITS_FEED", raising=False)
    assert cli.status_report(None, as_json=False) == 0
    out = capsys.readouterr().out
    assert "`usage` asks once" in out and "init" in out and "usage endpoint" in out


def test_app_defaults_to_usb(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("BUSYBAR_CCLIMITS_CONFIG", raising=False)
    monkeypatch.delenv("BUSYBAR_TOKEN", raising=False)
    config = app.build_config(app.parse_args([]))
    assert config.bar.address == "10.0.4.20" and config.bar.application_name == app.APP
    config = app.build_config(app.parse_args(["--host", "127.0.0.1:8080", "--feed", "~/f.json"]))
    assert config.bar.address == "127.0.0.1:8080"
    assert config.feed.path == Path("~/f.json").expanduser()
    with pytest.raises(ValueError):
        app.build_config(app.parse_args(["--cloud"]))


def test_example_config_is_the_defaults():
    example = load(Path(__file__).resolve().parents[1] / "config.example.toml")
    assert example == Config()


def test_describe_a_refused_draw():
    from busylib.exceptions import BusyBarAPIError

    (line,) = cli.describe(BusyBarAPIError("Not drawn due to low priority", status_code=409))
    assert "higher priority" in line


def test_per_model_windows():
    from busybar_cclimits.config import Row

    assert Row(window="seven_day_fable").shown_label == "FB"
    assert Row(window="seven_day_opus").shown_label == "OP"
    assert Row(window="seven_day_haiku").shown_label == "HA"
    for bad in ("five_hours", "fable", "seven_day_", "SEVEN_DAY"):
        with pytest.raises(ValidationError):
            Row(window=bad)
    with pytest.raises(ValidationError):
        Config.model_validate({"display": {"rows": [{"window": "five_hour"}] * 4}})


def test_status_tells_a_reset_from_no_reset_time():
    from busybar_cclimits.limits import Feed, Window

    now = 1_800_000_000.0
    feed = Feed(
        {
            "five_hour": Window(40, int(now) - 1),  # its window has passed
            "extra_usage": Window(12.5, None),  # usage credits: never a reset time
        },
        updated_at=now,
        seen_at=None,
    )
    lines = "\n".join(cli.describe_feed(feed, Path("limits.json"), now))
    assert "5-hour" in lines and "a new window starts" in lines
    assert "extra usage" in lines and "no reset time given" in lines
