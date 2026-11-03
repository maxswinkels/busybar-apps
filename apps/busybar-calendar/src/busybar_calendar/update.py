"""
Updating the install the service runs from, as it starts: `run --auto-update`.

Only a uv tool installed from git moves forward by itself: `uv tool upgrade`
fetches its branch again, while one installed from a wheel file would
reinstall that same file. The update runs before the config is read, so a
config that uses something newer gets the version that understands it.

After an update the process replaces itself with the same command, so the
new code runs at once; it does not update again in that second run.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import NoReturn

logger = logging.getLogger(__name__)

DIST = "busybar-calendar"
# At login the network can take a while to come up; past this, start as is.
TIMEOUT = 120
# Set for the process an update restarts into, so it does not update again.
UPDATED_ENV = "BUSYBAR_CALENDAR_UPDATED"


def upgrade_command(uv: str = "uv") -> list[str]:
    return [uv, "tool", "upgrade", DIST]


def from_git(python: str) -> bool:
    """Whether `python` is a uv tool installed from git, which `uv tool upgrade` moves forward."""
    receipt = Path(python).parent.parent / "uv-receipt.toml"
    with contextlib.suppress(OSError, ValueError, LookupError, TypeError):
        return "git" in tomllib.loads(receipt.read_text())["tool"]["requirements"][0]
    return False


def installed() -> str:
    """The version on disk now, with its git commit when it came from git; not
    necessarily the code this process runs."""
    importlib.invalidate_caches()
    try:
        dist = metadata.distribution(DIST)
    except metadata.PackageNotFoundError:
        return "unknown"
    commit = ""
    with contextlib.suppress(TypeError, ValueError, LookupError):  # no direct_url.json: None
        commit = json.loads(dist.read_text("direct_url.json"))["vcs_info"]["commit_id"]
    return f"{dist.version} ({commit[:7]})" if commit else dist.version


def update() -> bool:
    """`uv tool upgrade` the install this runs from; True when it now holds other code."""
    before = installed()
    if not from_git(sys.executable):
        logger.warning(
            "auto-update: only a uv tool installed from git updates itself; staying on %s",
            before,
        )
        return False
    uv = shutil.which("uv")
    if uv is None:
        logger.warning("auto-update: no uv on PATH; staying on %s", before)
        return False
    try:
        done = subprocess.run(
            upgrade_command(uv), capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("auto-update failed (%s); staying on %s", error, before)
        return False
    if done.returncode != 0:
        lines = done.stderr.strip().splitlines() or [f"exit {done.returncode}"]
        logger.warning("auto-update failed (%s); staying on %s", lines[-1], before)
        return False
    after = installed()
    if after == before:
        logger.info("auto-update: %s is the latest", after)
        return False
    logger.info("auto-update: %s -> %s", before, after)
    return True


def restart_into_update() -> NoReturn:
    """Replace this process with the same command, which now runs the updated code."""
    os.environ[UPDATED_ENV] = "1"
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
