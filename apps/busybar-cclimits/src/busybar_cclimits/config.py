"""
Configuration, read from a TOML file; every setting has a default, so the
file can be as short as the way to reach the bar.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .limits import default_feed

CONFIG_ENV = "BUSYBAR_CCLIMITS_CONFIG"
USB_HOST = "10.0.4.20"


class ConfigError(Exception):
    pass


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "busybar-cclimits" / "config.toml"


def find(given: str | None = None) -> Path | None:
    """--config, else $BUSYBAR_CCLIMITS_CONFIG, else the default path if it exists."""
    if given:
        return Path(given).expanduser()
    if found := os.environ.get(CONFIG_ENV):
        return Path(found).expanduser()
    path = default_path()
    return path if path.is_file() else None


def _color(value: str) -> str:
    if not re.fullmatch(r"#[0-9A-Fa-f]{8}", value):
        raise ValueError(f"{value!r} is not a #RRGGBBAA colour")
    return value.upper()


Color = Annotated[str, AfterValidator(_color)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Secret(_Strict):
    """
    A credential given inline, by environment variable, or by a command run
    without a shell - for example the macOS keychain:
    ["security", "find-generic-password", "-s", "busybar", "-w"].
    """

    value: str | None = None
    env: str | None = None
    command: list[str] | None = None

    def resolve(self) -> str | None:
        if self.value is not None:
            return self.value
        if self.env is not None:
            found = os.environ.get(self.env)
            if found is None:
                raise ConfigError(f"environment variable {self.env} is not set")
            return found
        if self.command:
            done = subprocess.run(self.command, capture_output=True, text=True, check=False)
            if done.returncode != 0:
                raise ConfigError(f"{self.command[0]} exited with {done.returncode}")
            return done.stdout.strip()
        return None


def _fold_secret(data: dict[str, Any], key: str) -> dict[str, Any]:
    """`key`, `key_env` and `key_command` become one Secret."""
    secret = {
        "value": data.pop(key, None),
        "env": data.pop(f"{key}_env", None),
        "command": data.pop(f"{key}_command", None),
    }
    if sum(value is not None for value in secret.values()) > 1:
        raise ValueError(f"give only one of {key}, {key}_env, {key}_command")
    data[key] = secret
    return data


class BarConfig(_Strict):
    address: str | None = Field(
        default=None,
        description="The bar on your network (`ip[:port]`; `10.0.4.20` over USB). Leave it "
        "out to reach the bar through BUSY Cloud.",
    )
    token: Secret = Field(
        default_factory=Secret,
        description="With an address, the bar's HTTP API password; without, a BUSY Cloud "
        'token with the "BUSY Bar" access scope. As `token`, `token_env` or `token_command`.',
    )
    priority: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Draw priority. 50 is above the bar's own apps (10) and gives way to a "
        "BUSY session (90) and calendar reminders (91).",
    )
    application_name: str = Field(default="busybar-cclimits", min_length=1, max_length=32)

    @model_validator(mode="before")
    @classmethod
    def _token(cls, data: Any) -> Any:
        return _fold_secret(dict(data), "token") if isinstance(data, dict) else data


# The windows with names of their own: `extra_usage` is the month's usage
# credits, from the usage API. A model's own limit - Fable, Opus, Sonnet - is
# `seven_day_<model>` or `five_hour_<model>`.
KNOWN_WINDOWS = ("five_hour", "seven_day", "spend_limit", "extra_usage")
DEFAULT_LABELS = {
    "five_hour": "5H",
    "seven_day": "WK",
    "spend_limit": "$",
    "extra_usage": "CR",
    "seven_day_opus": "OP",
    "seven_day_sonnet": "SN",
    "seven_day_fable": "FB",
}
_PER_MODEL = re.compile(r"(five_hour|seven_day)_[a-z][a-z0-9_]{0,30}")


def _window(value: str) -> str:
    if value in KNOWN_WINDOWS or _PER_MODEL.fullmatch(value):
        return value
    raise ValueError(
        f"{value!r} is not a limit: five_hour, seven_day, spend_limit, extra_usage, or a "
        "model's seven_day_<model> such as seven_day_fable"
    )


Window = Annotated[str, AfterValidator(_window)]


class Row(_Strict):
    window: Window
    label: str | None = Field(
        default=None,
        max_length=2,
        description="Up to 2 characters; 5H, WK, $, OP, SN, FB by default, else the model's "
        "first two letters.",
    )
    text: Literal["time", "percent", "none"] = Field(
        default="time",
        description="Beside the bar: the time to the reset (a live countdown once under 10 "
        "hours), the percentage used, or nothing and a longer bar.",
    )

    @property
    def shown_label(self) -> str:
        if self.label is not None:
            return self.label
        return DEFAULT_LABELS.get(self.window) or self.window.split("_")[-1][:2].upper()


def _default_rows() -> list[Row]:
    return [Row(window="five_hour", text="time"), Row(window="seven_day", text="percent")]


class Colors(_Strict):
    label: Color = "#D97757FF"
    track: Color = "#333333FF"
    ok: Color = "#00C853FF"
    warn: Color = "#FFC800FF"
    critical: Color = "#FF2A00FF"
    text: Color = "#FFFFFFFF"
    idle: Color = "#5A5A5AFF"


class DisplayConfig(_Strict):
    show: Literal["always", "with_claude"] = Field(
        default="always",
        description="`always` while there are limits to show; `with_claude` only while "
        "Claude Code has updated them in the last `idle_minutes`, or a limit is past "
        "`keep_above`, so the bar has its own screens back in between.",
    )
    idle_minutes: int = Field(default=15, ge=1)
    keep_above: float = Field(default=80, ge=0, le=100)
    rows: list[Row] = Field(
        default_factory=_default_rows,
        min_length=1,
        max_length=3,
        description="One to three rows; three are drawn in the 4-pixel tiny font.",
    )
    warn_at: float = Field(default=75, ge=0, le=100, description="Bars turn yellow here.")
    critical_at: float = Field(default=90, ge=0, le=100, description="And red here.")
    refresh_seconds: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="How often the drawing is sent again even when nothing changed: it "
        "takes the screen back after a calendar reminder or a BUSY session.",
    )
    colors: Colors = Field(default_factory=Colors)

    @model_validator(mode="after")
    def _thresholds(self) -> DisplayConfig:
        if self.warn_at > self.critical_at:
            raise ValueError("warn_at must not be above critical_at")
        return self


class SourceConfig(_Strict):
    kind: Literal["api", "statusline"] = Field(
        default="api",
        description="`api`: ask Anthropic's usage endpoint with Claude Code's own sign-in, "
        "read and never changed - the 5-hour and weekly windows and any model's own. "
        "`statusline`: only what Claude Code hands its status line (`hook install`).",
    )
    poll_seconds: int = Field(
        default=120,
        ge=30,
        le=3600,
        description="With `api`, how often to ask; a refused or failed request backs off.",
    )


class FeedConfig(_Strict):
    path: Path = Field(
        default_factory=default_feed,
        description="Where the limits are kept, by the usage API poller or the status line hook.",
    )


class Config(_Strict):
    bar: BarConfig = Field(default_factory=BarConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    feed: FeedConfig = Field(default_factory=FeedConfig)


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text())
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    config = Config.model_validate(data)
    config.feed.path = config.feed.path.expanduser()
    return config


__all__ = ["Config", "ConfigError", "ValidationError", "find", "load"]
