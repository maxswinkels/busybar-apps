import json

import pytest

from busybar_cclimits import limits
from busybar_cclimits.limits import Feed, Window, duration, merge, record

NOW = 1_800_000_000.0


def status(**windows):
    return {"model": {"display_name": "Opus"}, "rate_limits": windows}


def test_window_parse_and_reset():
    w = Window.parse({"used_percentage": 23.5, "resets_at": NOW + 60})
    assert w == Window(23.5, int(NOW + 60))
    assert w.running and w.left(NOW) == 60
    assert w.at(NOW + 59) is w
    gone = w.at(NOW + 60)
    assert gone.used == 0 and not gone.running and gone.left(NOW) is None


@pytest.mark.parametrize(
    "raw",
    [None, {}, {"used_percentage": "12"}, {"used_percentage": True}, [], "x"],
)
def test_window_parse_rejects(raw):
    assert Window.parse(raw) is None


def test_spend_limit_keeps_dollars():
    raw = {"used_percentage": 63, "resets_at": NOW + 9, "used_usd": 314.12, "limit_usd": 500}
    w = Window.parse(raw | {"period": "monthly"})
    assert (w.used_usd, w.limit_usd, w.period) == (314.12, 500.0, "monthly")
    assert w.at(NOW + 10).used_usd == 0.0


def test_merge_keeps_windows_claude_code_dropped():
    first = merge({}, status(five_hour={"used_percentage": 50, "resets_at": NOW + 5}), NOW)
    later = merge(first, status(seven_day={"used_percentage": 9, "resets_at": NOW + 99}), NOW + 10)
    feed = Feed.parse(later)
    assert set(feed.windows) == {"five_hour", "seven_day"}
    assert feed.at(NOW + 10)["five_hour"].used == 0  # it reset at NOW + 5


def test_merge_spend_dollars_survive_a_percentage_only_update():
    full = {"used_percentage": 60, "resets_at": 1, "used_usd": 300.0, "limit_usd": 500}
    first = merge({}, status(spend_limit=full), NOW)
    second = merge(first, status(spend_limit={"used_percentage": 61, "resets_at": 1}), NOW)
    assert second["rate_limits"]["spend_limit"] == full | {"used_percentage": 61}
    # A new period starts clean.
    third = merge(second, status(spend_limit={"used_percentage": 1, "resets_at": 2}), NOW)
    assert third["rate_limits"]["spend_limit"] == {"used_percentage": 1, "resets_at": 2}


def test_merge_without_limits_only_marks_seen():
    first = merge({}, status(five_hour={"used_percentage": 5, "resets_at": 9}), NOW)
    second = merge(first, {"model": {}}, NOW + 3)
    assert second["rate_limits"] == first["rate_limits"]
    assert second["updated_at"] == NOW and second["seen_at"] == NOW + 3


def test_merge_keeps_only_known_fields():
    raw = {"used_percentage": 5, "resets_at": 9, "secret": "x"}
    assert merge({}, status(five_hour=raw), NOW)["rate_limits"]["five_hour"] == {
        "used_percentage": 5,
        "resets_at": 9,
    }


def test_record_throttles_unchanged_input(tmp_path):
    path = tmp_path / "f" / "limits.json"
    same = status(five_hour={"used_percentage": 5, "resets_at": 9})
    assert record(path, same, NOW)
    assert not record(path, same, NOW + 5)
    assert record(path, status(five_hour={"used_percentage": 6, "resets_at": 9}), NOW + 6)
    assert record(path, same, NOW + 6 + limits.SEEN_EVERY)
    assert json.loads(path.read_text())["seen_at"] == NOW + 6 + limits.SEEN_EVERY
    assert list(path.parent.iterdir()) == [path]  # no temporary files left


def test_read_tolerates_garbage(tmp_path):
    path = tmp_path / "limits.json"
    assert limits.read(path) is None
    path.write_text("{not json")
    assert limits.read(path) is None
    path.write_text('{"rate_limits": {"five_hour": {"used_percentage": "x"}}, "seen_at": 1}')
    assert limits.read(path) == Feed({}, None, 1.0)


def test_default_feed(monkeypatch, tmp_path):
    monkeypatch.delenv(limits.FEED_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert limits.default_feed() == tmp_path / "busybar-cclimits" / "limits.json"
    monkeypatch.setenv(limits.FEED_ENV, str(tmp_path / "x.json"))
    assert limits.default_feed() == tmp_path / "x.json"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0m"),
        (59, "0m"),
        (42 * 60, "42m"),
        (3600 + 5 * 60, "1h05"),
        (9 * 3600 + 59 * 60, "9h59"),
        (21 * 3600 + 30 * 60, "21h"),
        (86400, "1d"),
        (3 * 86400 + 4 * 3600 + 59 * 60, "3d4h"),
    ],
)
def test_duration(seconds, text):
    assert duration(seconds) == text


def test_any_window_name_is_kept():
    stored = merge(
        {}, status(seven_day_fable={"used_percentage": 57, "resets_at": NOW + 9}, odd=3), NOW
    )
    assert set(stored["rate_limits"]) == {"seven_day_fable"}
    assert Feed.parse(stored).windows["seven_day_fable"].used == 57
