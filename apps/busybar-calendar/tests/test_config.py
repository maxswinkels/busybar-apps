import pytest
from pydantic import ValidationError

from busybar_calendar.config import Config, ConfigError, load


def base(**source):
    return {"sources": [{"name": "Work", "type": "caldav", "url": "https://dav.example", **source}]}


def test_settings_layer_defaults_source_calendar():
    config = Config.model_validate(
        {
            "defaults": {"reminder": {"minutes_before": 3, "sound": False}},
            "sources": [
                {
                    "name": "Work",
                    "type": "caldav",
                    "url": "https://dav.example",
                    "reminder": {"minutes_before": 10},
                    "calendars": {
                        "Team": {"status": {"show": "meeting"}, "reminder": {"sound": True}}
                    },
                }
            ],
        }
    )
    reminder, status = config.settings_for("Work", "Personal")
    assert (reminder.minutes_before, reminder.sound, status.show) == (10, False, "name")
    reminder, status = config.settings_for("Work", "Team")
    assert (reminder.minutes_before, reminder.sound, status.show) == (10, True, "meeting")


def test_rejects_unknown_status_and_typos():
    with pytest.raises(ValidationError, match="show must be"):
        Config.model_validate(base(status={"show": "partying"})).settings_for("Work", "x")
    with pytest.raises(ValidationError):
        Config.model_validate(base(urll="x"))


def test_load_validates_every_calendar(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[[sources]]\nname = "W"\ntype = "caldav"\nurl = "https://x"\n'
        "[sources.calendars.Team.reminder]\nminutes_before = 0\n"
    )
    with pytest.raises(ValidationError, match="minutes_before"):
        load(path)


def test_webcal_and_emails():
    config = Config.model_validate(
        {
            "sources": [
                {"name": "Feed", "type": "ics", "url": "webcal://cal.example/a.ics"},
                {
                    "name": "Mail",
                    "type": "caldav",
                    "url": "https://x",
                    "username": "me@example.com",
                },
            ]
        }
    )
    assert config.sources[0].url == "https://cal.example/a.ics"
    assert config.sources[1].emails == ["me@example.com"]


def test_ics_has_no_calendars():
    with pytest.raises(ValidationError, match="caldav only"):
        Config.model_validate(
            {"sources": [{"name": "Feed", "type": "ics", "url": "https://x", "include": ["a"]}]}
        )


def test_unique_source_names():
    with pytest.raises(ValidationError, match="unique"):
        Config.model_validate(
            {
                "sources": [
                    {"name": "A", "type": "ics", "url": "https://x"},
                    {"name": "A", "type": "ics", "url": "https://y"},
                ]
            }
        )


def test_secrets(monkeypatch):
    monkeypatch.setenv("APP_PW", "from-env")
    config = Config.model_validate(base(username="u", password_env="APP_PW"))
    assert config.sources[0].password.resolve() == "from-env"
    config = Config.model_validate(base(password_command=["echo", "from-cmd"]))
    assert config.sources[0].password.resolve() == "from-cmd"
    config = Config.model_validate({**base(), "bar": {"token": "inline"}})
    assert config.bar.token.resolve() == "inline"
    assert Config.model_validate(base()).bar.token.resolve() is None

    monkeypatch.delenv("APP_PW")
    config = Config.model_validate(base(password_env="APP_PW"))
    with pytest.raises(ConfigError, match="APP_PW"):
        config.sources[0].password.resolve()
    with pytest.raises(ValidationError, match="only one of"):
        Config.model_validate(base(password="a", password_env="APP_PW"))


def test_display_options_are_validated():
    from busybar_calendar.config import DisplayConfig

    assert DisplayConfig(busy_color="#ff0000").busy_color == "#FF0000FF"
    for bad in ({"busy_color": "red"}, {"busy_label": "ЗАЙНЯТО"}, {"event_icon": "blue"}):
        with pytest.raises(ValidationError):
            DisplayConfig(**bad)


def test_tentative_status_can_be_turned_off():
    config = Config.model_validate(base(status={"tentative": False}))
    _, status = config.settings_for("Work", "Work")
    assert status.tentative is False


def test_yellow_for_tentative_events_is_on_unless_turned_off(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[[sources]]\nname = "Work"\ntype = "ics"\nurl = "https://x"\n'
        "[display]\ntentative_yellow = false\n"
    )
    assert load(path).display.tentative_yellow is False
    assert Config.model_validate(base()).display.tentative_yellow is True


def test_a_config_file_needs_a_source(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[bar]\naddress = "10.0.4.20"\n')
    with pytest.raises(ConfigError, match="no"):
        load(path)


def test_events_of_no_source_get_the_defaults():
    config = Config.model_validate({"defaults": {"reminder": {"minutes_before": 2}}})
    reminder, status = config.settings_for("demo", "demo")
    assert (reminder.minutes_before, status.show) == (2, "name")


def test_timezone_is_checked_when_loading():
    with pytest.raises(ValidationError, match="not a timezone name"):
        Config.model_validate({**base(), "sync": {"timezone": "Europe/Kiyv"}})
    assert Config.model_validate({**base(), "sync": {"timezone": "Europe/Kyiv"}})


def test_title_rules_layer_over_the_calendar_in_order():
    config = Config.model_validate(
        {
            **base(calendars={"Team": {"status": {"show": "meeting"}}}),
            "titles": [
                {"match": "lunch*", "status": {"show": "lunch"}, "reminder": {"sound": False}},
                {"match": ["Focus time", "Commute ?"], "reminder": {"enabled": False}},
                {"match": "*", "reminder": {"minutes_before": 2}},
            ],
        }
    )
    reminder, status = config.settings_for("Work", "Team", title="Lunch with Alex")
    assert (status.show, reminder.sound, reminder.minutes_before) == ("lunch", False, 2)
    reminder, status = config.settings_for("Work", "Team", title="focus TIME")
    assert (reminder.enabled, status.show) == (False, "meeting")
    reminder, _ = config.settings_for("Work", "Team", title="Commute home")
    assert reminder.enabled is True  # `?` is one character
    _, status = config.settings_for("Work", "Team", title="Sync")
    assert status.show == "meeting"


def test_title_rules_need_a_match():
    with pytest.raises(ValidationError, match="match"):
        Config.model_validate({**base(), "titles": [{"reminder": {"enabled": False}}]})
    with pytest.raises(ValidationError, match="match"):
        Config.model_validate({**base(), "titles": [{"match": []}]})


def test_a_bad_setting_says_where_it_is(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        '[[sources]]\nname = "Work"\ntype = "ics"\nurl = "https://x"\n'
        '[[titles]]\nmatch = "Lunch"\nstatus.show = "partying"\n'
    )
    with pytest.raises(ValidationError) as caught:
        load(path)
    (problem,) = caught.value.errors()
    assert problem["loc"] == ("titles", 0, "status")
    assert "show: show must be" in problem["msg"]
    with pytest.raises(ValidationError) as caught:
        Config.model_validate(base(calendars={"Team": {"reminder": {"minutes_before": 0}}}))
    assert caught.value.errors()[0]["loc"] == ("sources", 0, "calendars", "Team", "reminder")
