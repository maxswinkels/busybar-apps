"""The usage API source, against fakes: no sign-in is read and nothing goes out."""

import json
import logging
import subprocess

import httpx2
import pytest

from busybar_cclimits import limits, render, usage_api
from busybar_cclimits.config import DisplayConfig
from busybar_cclimits.usage_api import ApiError, Poller, Token, TokenError

NOW = 1_800_000_000.0
SECRET = "sk-test-not-a-real-token"


def blob(access=SECRET, expires_ms=(NOW + 3600) * 1000):
    return json.dumps({"claudeAiOauth": {"accessToken": access, "expiresAt": expires_ms}})


def test_parse_credentials_and_the_token_never_shows():
    token = usage_api.parse_credentials(blob())
    assert token.access == SECRET and token.expires_at == NOW + 3600
    assert not token.expired(NOW) and token.expired(NOW + 3600)
    assert SECRET not in repr(token) and SECRET not in str(token)
    with pytest.raises(TokenError):
        usage_api.parse_credentials("{}")
    with pytest.raises(TokenError):
        usage_api.parse_credentials(blob(access=""))


def test_read_token_from_the_keychain_then_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    calls = []

    def keychain(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=blob(), stderr="")

    assert usage_api.read_token(platform="darwin", run=keychain).access == SECRET
    assert calls == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]

    def missing(args, **kwargs):
        return subprocess.CompletedProcess(args, 44, stdout="", stderr="not found")

    (tmp_path / ".credentials.json").write_text(blob(access="from-file"))
    assert usage_api.read_token(platform="darwin", run=missing).access == "from-file"
    assert usage_api.read_token(platform="linux", run=keychain).access == "from-file"


def test_no_sign_in_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with pytest.raises(TokenError, match="log in"):
        usage_api.read_token(platform="linux")


def test_to_rate_limits():
    answer = {
        "five_hour": {"utilization": 23.0, "resets_at": "2027-01-15T08:00:00.123456+00:00"},
        "seven_day": {"utilization": 41, "resets_at": "2027-01-17T08:00:00Z"},
        "seven_day_opus": {"utilization": 0.0, "resets_at": None},
        "seven_day_sonnet": None,
        "seven_day_oauth_apps": {"utilization": True},
        "note": "not a window",
    }
    found = usage_api.to_rate_limits(answer)
    assert found == {
        "five_hour": {"used_percentage": 23.0, "resets_at": 1800000000},
        "seven_day": {"used_percentage": 41.0, "resets_at": 1800172800},
        "seven_day_opus": {"used_percentage": 0.0, "resets_at": None},
    }
    assert usage_api.to_rate_limits([]) == {}


def test_a_model_limit_comes_from_the_scoped_limits():
    """The answer's shape as seen in October 2026, with made-up numbers."""
    reset = "2027-01-17T08:00:00.5734+00:00"
    answer = {
        "five_hour": {"utilization": 23.0, "resets_at": reset, "used_dollars": None},
        "seven_day": {"utilization": 41.0, "resets_at": reset},
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": True, "monthly_limit": 20000, "utilization": 12.5},
        "spend": {"percent": 12, "severity": "normal"},
        "limits": [
            {"kind": "session", "percent": 23, "resets_at": reset, "scope": None},
            {"kind": "weekly_all", "percent": 41, "resets_at": reset, "scope": None},
            {
                "kind": "weekly_scoped",
                "percent": 6,
                "resets_at": reset,
                "scope": {"model": {"display_name": "Fable", "id": None}, "surface": None},
            },
            {"kind": "weekly_scoped", "percent": 3, "scope": {"model": {"display_name": "?"}}},
            "not an entry",
        ],
        "amber_gauge": None,
    }
    found = usage_api.to_rate_limits(answer)
    assert set(found) == {"five_hour", "seven_day", "extra_usage", "seven_day_fable"}
    assert found["seven_day_fable"] == {"used_percentage": 6.0, "resets_at": 1800172800}
    assert found["extra_usage"] == {"used_percentage": 12.5, "resets_at": None}
    assert found["five_hour"]["used_percentage"] == 23.0  # the top level, not `limits`


class Response:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.seen = response, error, []

    async def get(self, url, headers):
        self.seen.append((url, headers))
        if self.error:
            raise self.error
        return self.response


async def test_fetch_sends_the_sign_in_and_reads_the_answer():
    client = Client(Response(200, {"five_hour": {"utilization": 5}}))
    assert await usage_api.fetch(Token(SECRET, None), client) == {"five_hour": {"utilization": 5}}
    ((url, headers),) = client.seen
    assert url == usage_api.USAGE_URL
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["anthropic-beta"] == usage_api.BETA
    assert headers["User-Agent"].startswith("busybar-cclimits/")


@pytest.mark.parametrize(
    ("client", "status"),
    [
        (Client(Response(429, {})), 429),
        (Client(Response(401, {})), 401),
        (Client(Response(200, ValueError("not json"))), None),
        (Client(Response(200, ["a list"])), None),
        (Client(error=httpx2.ConnectError("down")), None),
    ],
)
async def test_fetch_failures_are_api_errors_without_the_token(client, status):
    with pytest.raises(ApiError) as caught:
        await usage_api.fetch(Token(SECRET, None), client)
    assert caught.value.status == status
    assert SECRET not in str(caught.value)


def poller(tmp_path, token=None, answers=None, every=120):
    answers = list(answers or [])
    asked = []

    async def get(token):
        asked.append(token)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def read():
        if isinstance(token, Exception):
            raise token
        return token or Token(SECRET, NOW + 3600)

    return Poller(tmp_path / "limits.json", every, token=read, get=get), asked


async def test_poller_stores_the_limits_without_marking_the_status_line_seen(tmp_path):
    answer = {"five_hour": {"utilization": 42, "resets_at": "2027-01-15T08:00:00Z"}}
    p, asked = poller(tmp_path, answers=[answer, answer])
    await p(NOW)
    await p(NOW + 60)  # not due yet
    assert len(asked) == 1
    feed = limits.read(p.path)
    assert feed.windows["five_hour"].used == 42
    assert (feed.updated_at, feed.seen_at) == (NOW, None)
    await p(NOW + 120)
    assert len(asked) == 2


async def test_poller_backs_off_from_429_but_not_from_a_refused_sign_in(tmp_path, caplog):
    p, asked = poller(
        tmp_path, answers=[ApiError("x", 429), ApiError("x", 429), ApiError("x", 401), {}]
    )
    await p(NOW)
    assert len(asked) == 1 and p._next == NOW + 120
    await p(NOW + 120)
    assert p._next == NOW + 120 + 240  # doubled
    await p(NOW + 360)
    assert p._next == NOW + 360 + 120  # a 401 waits for a renewed sign-in, no back-off
    assert not p.path.exists()
    assert "refused" in caplog.text


async def test_poller_waits_for_an_expired_sign_in_and_says_so_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    p, asked = poller(tmp_path, token=Token(SECRET, NOW - 1))
    await p(NOW)
    await p(NOW + 120)
    assert asked == []
    assert caplog.text.count("has expired") == 1


async def test_poller_reports_a_missing_sign_in(tmp_path, caplog):
    p, asked = poller(tmp_path, token=TokenError("no Claude Code sign-in found"))
    await p(NOW)
    assert asked == [] and "no Claude Code sign-in" in caplog.text


def test_changed_at_marks_usage_moving_for_with_claude(tmp_path):
    path = tmp_path / "limits.json"
    limits.record_limits(path, {"five_hour": {"used_percentage": 10, "resets_at": 9e9}}, NOW)
    first = limits.read(path)
    assert first.changed_at is None and first.seen_at is None
    limits.record_limits(path, {"five_hour": {"used_percentage": 12, "resets_at": 9e9}}, NOW + 300)
    moved = limits.read(path)
    assert moved.changed_at == NOW + 300 and moved.active_at() == NOW + 300

    shy = DisplayConfig(show="with_claude", idle_minutes=15, keep_above=80)
    assert render.visible(moved, NOW + 300 + 15 * 60, shy)
    assert not render.visible(moved, NOW + 300 + 15 * 60 + 1, shy)
