"""
Fixtures that run the real BUSY Bar emulator (github.com/maxswinkels/busybar-emulator).

The other tests draw on a fake bar that accepts anything. The emulator is a bar that
enforces the firmware's draw contract - element ids, #RRGGBBAA colours, priorities and
409s, the 100-element cap, firmware's merge-by-id - so these tests show the app's
drawings are ones a bar takes, and that the service keeps the screen right over a real
HTTP API.

They need a checkout of the emulator: BUSYBAR_EMULATOR names its directory, and `node`
runs `server.js` from it (no npm install; the server has no dependencies). Without it
they skip; with BUSYBAR_EMULATOR_REQUIRED=1, as in CI, a missing emulator fails instead.
Run them with `pytest tests/emulator` or leave them out with `-m "not emulator"`.

Nothing here reaches a real bar or reads anyone's Claude Code limits: the emulator
listens on loopback with a throwaway API password, the limits come from a feed file the
test writes, and a subprocess runs with a sealed environment (a fresh HOME and cache, no
BUSYBAR_* variables), because the app looks for the config and the feed of whoever runs
it in ~/.config and ~/.cache, and a test must not find yours.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx2
import pytest
from emulator_support import Emulator, Running, wait_for

EMULATOR_DIR = "BUSYBAR_EMULATOR"
REQUIRED = "BUSYBAR_EMULATOR_REQUIRED"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if HERE in item.path.parents:
            item.add_marker(pytest.mark.emulator)


def _unavailable(reason: str) -> None:
    if os.environ.get(REQUIRED):
        pytest.fail(f"{REQUIRED} is set, but {reason}", pytrace=False)
    pytest.skip(reason)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="session")
def emulator(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Emulator]:
    directory = os.environ.get(EMULATOR_DIR)
    if not directory:
        _unavailable(f"{EMULATOR_DIR} is not set to a busybar-emulator checkout")
    server = Path(directory) / "server.js"
    if not server.is_file():
        _unavailable(f"{server} does not exist")
    node = shutil.which("node")
    if node is None:
        _unavailable("node is not installed")
    port = _free_port()
    log = tmp_path_factory.mktemp("emulator") / "server.log"
    # The API password only matters to callers off loopback; nothing else may use this.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PORT": str(port),
        "BUSY_API_TOKEN": secrets.token_hex(8),
    }
    with log.open("wb") as out:
        process = subprocess.Popen(
            [node, "server.js"], cwd=directory, env=env, stdout=out, stderr=subprocess.STDOUT
        )
        try:
            emu = Emulator(port, log)

            def up() -> bool:
                if process.poll() is not None:
                    pytest.fail(f"the emulator exited:\n{log.read_text()}", pytrace=False)
                try:
                    return httpx2.get(f"http://{emu.host}/api/version", timeout=1).is_success
                except httpx2.HTTPError:
                    return False

            wait_for(up, "the emulator to answer", timeout=15)
            yield emu
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture(autouse=True)
def blank_display(emulator: Emulator) -> Emulator:
    """The emulator, with a blank display and no scenario left over from the last test."""
    emulator.reset()
    return emulator


@pytest.fixture
def sealed_env(tmp_path: Path) -> dict[str, str]:
    """An environment in which the app can find no one's config, feed or token."""
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PYTHONUNBUFFERED": "1",
    }


@pytest.fixture
def feed_path(tmp_path: Path) -> Path:
    return tmp_path / "limits.json"


@pytest.fixture
def write_config(tmp_path: Path, emulator: Emulator, feed_path: Path) -> Callable[..., Path]:
    """
    Write a config for a bar at the emulator and a feed in the test's directory.

    The source is the status line, so the service never looks for a Claude Code
    sign-in: on a Mac, `security` finds the login Keychain whatever HOME says.
    """

    def write(extra: str = "") -> Path:
        path = tmp_path / "config.toml"
        path.write_text(
            f'[bar]\naddress = "{emulator.host}"\n\n[source]\nkind = "statusline"\n\n'
            f'[feed]\npath = "{feed_path}"\n\n{extra}'
        )
        return path

    return write


@pytest.fixture
def start(tmp_path: Path, sealed_env: dict[str, str]) -> Iterator[Callable[..., Running]]:
    """Start `python <args>` sealed in; whatever is still running afterwards is stopped."""
    running: list[Running] = []
    work = tmp_path / "work"
    work.mkdir()

    def launch(*args: str) -> Running:
        output = tmp_path / f"run-{len(running)}.log"
        with output.open("wb") as out:
            process = subprocess.Popen(
                [sys.executable, *args],
                cwd=work,
                env=sealed_env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        running.append(Running(process, output))
        return running[-1]

    yield launch
    for item in running:
        with contextlib.suppress(Exception):
            item.process.kill()
            item.process.wait(timeout=5)
