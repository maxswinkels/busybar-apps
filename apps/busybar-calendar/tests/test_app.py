"""app.py, the community-gallery entrypoint at the repository root."""

import importlib.util
from pathlib import Path

import pytest

from busybar_calendar.config import CONFIG_ENV, ConfigError

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gallery_app", ROOT / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

ICS = '[[sources]]\nname = "Feed"\ntype = "ics"\nurl = "https://example.com/a.ics"\n'


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.delenv("BUSYBAR_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def config_for(argv):
    args = app.parse_args(argv)
    path = app.config_file(args)
    return path, app.build_config(args, path)


def test_gallery_defaults_without_a_config(no_config):
    path, config = config_for([])
    assert path is None
    assert config.bar.address == "10.0.4.20"  # the USB bar
    assert config.bar.application_name == "busybar-calendar"
    assert config.sources == []  # so main() plays the demo


def test_the_config_init_wrote_is_used(no_config, monkeypatch):
    written = no_config / "xdg" / "busybar-calendar" / "config.toml"
    written.parent.mkdir(parents=True)
    written.write_text(ICS)
    path, config = config_for(["--host", "192.168.1.50"])
    assert path == written
    assert config.sources[0].name == "Feed"
    assert config.bar.address == "192.168.1.50"  # --host still wins


def test_calendar_flags_win_over_a_config(no_config):
    (no_config / "config.toml").write_text(ICS)
    path, config = config_for(["--ics", "webcal://example.com/b.ics", "--minutes", "10"])
    assert path is None
    assert config.sources[0].url == "https://example.com/b.ics"
    assert config.settings_for("ics", "ics")[0].minutes_before == 10


def test_caldav_flags_read_the_password_from_the_environment(no_config):
    _, config = config_for(
        ["--caldav", "https://dav.example.com", "--user", "me@example.com", "--include", "Work"]
    )
    source = config.sources[0]
    assert (source.password.env, source.include) == ("CALDAV_PASSWORD", ["Work"])


def test_cloud_needs_a_token(no_config, monkeypatch):
    with pytest.raises(ConfigError, match="BUSYBAR_TOKEN"):
        config_for(["--cloud", "--ics", "https://example.com/a.ics"])
    monkeypatch.setenv("BUSYBAR_TOKEN", "t")
    _, config = config_for(["--cloud", "--ics", "https://example.com/a.ics"])
    assert config.bar.address is None and config.bar.token.resolve() == "t"


# Runs app.py, and sends SIGINT to itself the moment app.py imports its package:
# mid-import, before any of the app's own handling exists - whatever the
# machine's speed, unlike a timed signal.
INTERRUPT_MID_IMPORT = """
import builtins, os, runpy, signal, sys
sys.argv = [sys.argv[1], "--demo", "--host", "127.0.0.1:9"]
real_import = builtins.__import__
def interrupting_import(name, *args, **kwargs):
    if name == "busybar_calendar":
        builtins.__import__ = real_import
        os.kill(os.getpid(), signal.SIGINT)
    return real_import(name, *args, **kwargs)
builtins.__import__ = interrupting_import
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def _interrupt(command, after):
    import signal
    import subprocess
    import time

    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if after:
        time.sleep(after)
        proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=10)
    return proc.returncode, err


def test_ctrl_c_while_starting_ends_it_cleanly(no_config):
    """The gallery's checker sends SIGINT and expects exit 0 and no traceback, also
    when it lands mid-import, as on the slow first start after an install."""
    import sys

    code, err = _interrupt([sys.executable, "-c", INTERRUPT_MID_IMPORT, str(ROOT / "app.py")], 0)
    assert code == 0, err
    assert "Traceback" not in err


def test_ctrl_c_while_running_clears_and_ends_it_cleanly(no_config):
    import sys

    # The demo against a bar that is not there keeps trying until stopped.
    command = [sys.executable, str(ROOT / "app.py"), "--demo", "--host", "127.0.0.1:9"]
    code, err = _interrupt(command, 1.5)
    assert code == 0, err
    assert "Traceback" not in err


def test_bad_flags_fail_at_start(no_config):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="minutes_before"):
        config_for(["--ics", "https://example.com/a.ics", "--minutes", "0"])
