"""Helpers for the emulator tests: the emulator, a feed file, a running subprocess."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest


def wait_for(condition: Callable[[], object], what: str, timeout: float = 20.0) -> object:
    """Poll until `condition` returns something truthy, and return it."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            pytest.fail(f"gave up after {timeout:g}s waiting for {what}", pytrace=False)
        time.sleep(0.1)


class Emulator:
    """A running emulator, and the ways a test looks at what is on its display."""

    def __init__(self, port: int, log: Path) -> None:
        self.host = f"127.0.0.1:{port}"
        self.log = log
        self._url = f"http://{self.host}"

    def snapshot(self) -> dict:
        """The state the emulator's own web page gets: the frame, the API log, ..."""
        with httpx2.stream("GET", f"{self._url}/events", timeout=5) as stream:
            event = None
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "state":
                    return json.loads(line[5:])
        raise AssertionError("the emulator's event stream ended without a state")

    def frame(self) -> dict:
        """What is on the front display: {application_name, priority, elements, ...}."""
        return self.snapshot()["frame"]

    def element_ids(self) -> list[str]:
        return [element["id"] for element in self.frame()["elements"]]

    def mine(self, app: str = "busybar-cclimits") -> dict[str, dict] | None:
        """This app's elements on the display by id, or None when it is not this app's."""
        frame = self.frame()
        if frame.get("application_name") != app:
            return None
        return {element["id"]: element for element in frame["elements"]}

    def calls(self, since: float = 0.0) -> list[tuple[str, str, str]]:
        """(method, path, note) of the API calls since `since` (time.time()), oldest first."""
        log = [entry for entry in self.snapshot()["log"] if entry["t"] / 1000 >= since]
        return [(e["method"], e["path"], e["note"]) for e in reversed(log)]

    def post(self, path: str, **body) -> httpx2.Response:
        response = httpx2.post(f"{self._url}{path}", json=body, timeout=5)
        response.raise_for_status()
        return response

    def reset(self) -> None:
        httpx2.delete(f"{self._url}/api/display/draw", timeout=5).raise_for_status()
        self.post("/api/_scenario/reset")


def write_feed(path: Path, now: float, **windows: tuple[float, float]) -> None:
    """A feed as the status line writes it: window -> (percent used, seconds to reset)."""
    limits = {
        name: {"used_percentage": used, "resets_at": int(now + left)}
        for name, (used, left) in windows.items()
    }
    data = {"version": 1, "seen_at": now, "updated_at": now, "rate_limits": limits}
    path.write_text(json.dumps(data))


class Running:
    """A subprocess of the app, with its output kept for the failure message."""

    def __init__(self, process: subprocess.Popen, output: Path) -> None:
        self.process = process
        self.output = output

    def text(self) -> str:
        return self.output.read_text()

    def wait(self, timeout: float = 30) -> int:
        """Let a command that ends by itself finish, and return its exit code."""
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            pytest.fail(f"still running after {timeout:g}s:\n{self.text()}", pytrace=False)

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 15) -> int:
        """Signal it, as `launchctl` or Ctrl-C would, and wait for it to finish."""
        if self.process.poll() is None:
            self.process.send_signal(sig)
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            pytest.fail(f"did not stop on signal {sig}:\n{self.text()}", pytrace=False)
