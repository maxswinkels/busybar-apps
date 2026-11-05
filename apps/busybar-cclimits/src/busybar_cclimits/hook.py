"""
Putting `busybar-cclimits statusline` into Claude Code's settings: `hook`.

The status line command in your user settings (~/.claude/settings.json, or
$CLAUDE_CONFIG_DIR/settings.json) is wrapped rather than replaced:

    <this program> statusline --then '<the command you had>'

so the status line looks exactly as before. Without a status line, the
command prints a short one of its own. `hook uninstall` restores the
command from the wrapped one; the file is backed up before each change.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

THEN = "--then"
BACKUP_SUFFIX = ".busybar-cclimits.bak"


def settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / "settings.json"


def program() -> list[str]:
    """How Claude Code should start this program: the installed command, or this Python."""
    found = shutil.which("busybar-cclimits")
    return [found] if found else [sys.executable, "-m", "busybar_cclimits"]


def _is_ours(argv: list[str]) -> bool:
    """`busybar-cclimits statusline`, `python -m busybar_cclimits statusline` or
    `python .../busybar-cclimits/app.py statusline`."""
    if "statusline" not in argv:
        return False
    head = argv[: argv.index("statusline")]
    return any("busybar-cclimits" in part or "busybar_cclimits" in part for part in head)


def unwrap(command: str) -> tuple[bool, str | None]:
    """(whether `command` is this program's, the command it wraps if any)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return False, None
    if not _is_ours(argv):
        return False, None
    rest = argv[argv.index("statusline") + 1 :]
    if THEN in rest and rest.index(THEN) + 1 < len(rest):
        return True, rest[rest.index(THEN) + 1]
    return True, None


def wrap(previous: str | None, prog: list[str]) -> str:
    command = shlex.join([*prog, "statusline"])
    return f"{command} {THEN} {shlex.quote(previous)}" if previous else command


@dataclass(frozen=True)
class Change:
    settings: dict[str, Any]  # the whole file after the change
    before: str | None  # the status line command before
    after: str | None  # and after
    note: str


def _status_line(settings: dict[str, Any]) -> dict[str, Any]:
    line = settings.get("statusLine")
    return dict(line) if isinstance(line, dict) else {}


def plan_install(settings: dict[str, Any], prog: list[str]) -> Change:
    line = _status_line(settings)
    current = line.get("command") if line.get("type") == "command" else None
    current = current if isinstance(current, str) and current.strip() else None
    ours, wrapped = unwrap(current) if current else (False, None)
    previous = wrapped if ours else current
    command = wrap(previous, prog)
    if ours and command == current:
        return Change(settings, current, current, "already installed")
    note = (
        "updated the installed hook"
        if ours
        else f"wrapped your status line: {previous}"
        if previous
        else "added a status line"
    )
    after = {**settings, "statusLine": {**line, "type": "command", "command": command}}
    return Change(after, current, command, note)


def plan_uninstall(settings: dict[str, Any]) -> Change:
    line = _status_line(settings)
    current = line.get("command") if isinstance(line.get("command"), str) else None
    ours, previous = unwrap(current) if current else (False, None)
    if not ours:
        return Change(settings, current, current, "not installed")
    after = dict(settings)
    if previous:
        after["statusLine"] = {**line, "command": previous}
        note = f"restored your status line: {previous}"
    else:
        del after["statusLine"]
        note = "removed the status line it had added"
    return Change(after, current, previous, note)


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def save(path: Path, settings: dict[str, Any]) -> Path | None:
    """Write the settings, keeping a copy of the old file; the copy's path, if any."""
    backup = None
    if path.exists():
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.busybar-cclimits.tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return backup


def installed(path: Path | None = None) -> tuple[bool, str | None]:
    """Whether the hook is in the settings, and the status line command there."""
    try:
        line = _status_line(load(path or settings_path()))
    except (OSError, ValueError):
        return False, None
    command = line.get("command") if isinstance(line.get("command"), str) else None
    return (unwrap(command)[0] if command else False), command
