import stat

from busybar_calendar import wizard
from busybar_calendar.caldav import CalDAVAuthError, RemoteCalendar
from busybar_calendar.config import load

FOUND = [
    RemoteCalendar("https://dav.example.com/cal/me/1001/", "Home"),
    RemoteCalendar("https://dav.example.com/cal/me/1002/", "Work"),
    RemoteCalendar("https://dav.example.com/cal/me/2001/", "Room 1 (6 people)"),
]


def script(*answers):
    pending = list(answers)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return pending.pop(0)

    ask.asked = asked
    return ask


class FakeStore:
    def __init__(self, keychain=True):
        self.saved = []
        self.keychain = keychain

    def save(self, service, account, env):
        self.saved.append((service, account))
        if self.keychain:
            return wizard.Secret(command=["security", "find-generic-password", "-s", service, "-w"])
        return wizard.Secret(env=env)


def test_cloud_and_an_icloud_account(tmp_path):
    path = tmp_path / "busybar-calendar" / "config.toml"
    # cloud (default), CalDAV (default), username, the guessed provider, two calendars
    ask = script("", "", "me@icloud.com", "", "2, 1")
    store = FakeStore()
    assert wizard.run(path, ask=ask, store=store, discover=lambda source: FOUND) == 0

    assert ask.asked[3] == "Choose 1-5 [1]: "  # iCloud, guessed from the address
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    config = load(path)
    assert config.bar.address is None
    assert config.bar.token.command[-2:] == ["busybar-calendar-bar", "-w"]
    [source] = config.sources
    assert (source.name, source.url, source.username) == (
        "iCloud",
        "https://caldav.icloud.com",
        "me@icloud.com",
    )
    assert source.include == ["Home", "Work"]  # in the server's order
    assert store.saved == [
        ("busybar-calendar-bar", "busybar-calendar"),
        ("busybar-calendar-me@icloud.com", "me@icloud.com"),  # one entry per account
    ]


def test_usb_and_an_ics_link(tmp_path):
    path = tmp_path / "config.toml"
    ask = script("3", "2", "webcal://example.com/team.ics")
    assert wizard.run(path, ask=ask, store=FakeStore(), discover=None) == 0
    config = load(path)
    assert config.bar.address == "10.0.4.20"
    assert config.bar.token.resolve() is None
    assert (config.sources[0].type, config.sources[0].url) == (
        "ics",
        "https://example.com/team.ics",
    )


def test_wifi_without_a_keychain_and_a_failed_sign_in(tmp_path, capsys):
    path = tmp_path / "config.toml"

    def refuse(source):
        raise CalDAVAuthError("PROPFIND https://dav.example.com: 401, check the credentials", 401)

    ask = script("2", "192.168.1.50", "1", "me@example.com", "", "https://dav.example.com")
    assert wizard.run(path, ask=ask, store=FakeStore(keychain=False), discover=refuse) == 0
    out = capsys.readouterr().out
    assert "Could not sign in" in out
    text = path.read_text()
    assert 'token_env = "BUSYBAR_TOKEN"' in text
    assert 'password_env = "CALDAV_PASSWORD"' in text
    assert "include" not in text
    assert load(path).sources[0].name == "example.com"


def test_all_calendars_when_none_are_picked(tmp_path):
    path = tmp_path / "config.toml"
    ask = script("3", "1", "me@example.com", "", "https://dav.example.com", "")
    wizard.run(path, ask=ask, store=FakeStore(), discover=lambda source: FOUND)
    assert load(path).sources[0].include == []


def test_bad_answers_are_asked_again(tmp_path):
    path = tmp_path / "config.toml"
    ask = script("9", "x", "3", "2", "https://example.com/a.ics")
    assert wizard.run(path, ask=ask, store=FakeStore(), discover=None) == 0
    assert len(ask.asked) == 5


def test_an_existing_config_is_kept_unless_confirmed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("keep me")
    assert wizard.run(path, ask=script("n"), store=FakeStore(), discover=None) == 1
    assert path.read_text() == "keep me"
    ask = script("y", "3", "2", "https://example.com/a.ics")
    assert wizard.run(path, ask=ask, store=FakeStore(), discover=None) == 0
    assert "keep me" not in path.read_text()


EXISTING = """# my config, with my own comments
[bar]
address = "10.0.4.20"

[[sources]]
name = "iCloud"
type = "caldav"
url = "https://caldav.icloud.com"
username = "me@icloud.com"
password_env = "ICLOUD"

[sources.calendars.Work.status]
show = "meeting"
"""


def test_add_appends_a_source_and_keeps_the_rest(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXISTING)
    # A second iCloud account: the name must not clash with the first.
    ask = script("1", "partner@icloud.com", "", "1")
    store = FakeStore()
    assert wizard.add(path, ask=ask, store=store, discover=lambda source: FOUND) == 0
    text = path.read_text()
    assert text.startswith(EXISTING.rstrip("\n"))  # comments and layout untouched
    config = load(path)
    assert [s.name for s in config.sources] == ["iCloud", "iCloud 2"]
    assert config.sources[1].include == ["Home"]
    assert config.settings_for("iCloud", "Work")[1].show == "meeting"  # still applies
    assert store.saved == [("busybar-calendar-partner@icloud.com", "partner@icloud.com")]


def test_add_an_ics_link(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXISTING)
    assert (
        wizard.add(
            path, ask=script("2", "webcal://example.com/team.ics"), store=FakeStore(), discover=None
        )
        == 0
    )
    assert load(path).sources[1].url == "https://example.com/team.ics"


def test_add_needs_a_config(tmp_path):
    import pytest

    from busybar_calendar.config import ConfigError

    with pytest.raises(ConfigError, match="busybar-calendar init"):
        wizard.add(tmp_path / "none.toml", ask=script(), store=FakeStore(), discover=None)


def test_keychain_prompt_is_asked_again_then_gives_up_cleanly(monkeypatch, capsys):
    import subprocess

    import pytest

    from busybar_calendar.config import ConfigError

    codes = [1, 0]  # a mistyped retype, then right
    monkeypatch.setattr(wizard.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(
        wizard.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, codes.pop(0)),
    )
    secret = wizard.SecretStore("darwin").save("svc", "me", "ENV")
    assert secret.command[-2:] == ["svc", "-w"]
    assert "Once more" in capsys.readouterr().out

    monkeypatch.setattr(
        wizard.subprocess, "run", lambda command, **kw: subprocess.CompletedProcess(command, 1)
    )
    with pytest.raises(ConfigError, match="did not store the password"):
        wizard.SecretStore("darwin").save("svc", "me", "ENV")


def caldav_source(tmp_path, *answers):
    path = tmp_path / "config.toml"
    ask = script("3", "1", *answers)  # USB, CalDAV, then the answers given
    wizard.run(path, ask=ask, store=FakeStore(), discover=lambda source: FOUND)
    return load(path).sources[0], ask


def test_providers_fill_in_the_server(tmp_path):
    source, ask = caldav_source(tmp_path, "me@mydomain.com", "3", "")  # PrivateEmail
    assert (source.name, source.url) == ("PrivateEmail", "https://dav.privateemail.com")
    assert ask.asked[3] == "Choose 1-5 [5]: "  # an unknown domain: another server by default


def test_nextcloud_needs_only_the_host(tmp_path):
    source, _ = caldav_source(tmp_path, "me@example.com", "4", "cloud.example.com/", "")
    assert source.url == "https://cloud.example.com/remote.php/dav"
    assert source.name == "example.com"


def test_fastmail_is_guessed(tmp_path):
    source, ask = caldav_source(tmp_path, "me@fastmail.com", "", "")
    assert (source.name, source.url) == ("Fastmail", "https://caldav.fastmail.com")
    assert ask.asked[3] == "Choose 1-5 [2]: "
