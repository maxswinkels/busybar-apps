#!/usr/bin/env python3
"""Spotify Now Playing: track, progress, and hardware playback controls.

    python app.py --spotify-client-id YOUR_CLIENT_ID
    python app.py --host 127.0.0.1:8080 --demo

Spotify setup:
  1. Add http://127.0.0.1:4381/callback to the Spotify app's redirect URIs.
  2. Pass --spotify-client-id or set SPOTIFY_CLIENT_ID.
  3. A browser opens once; the refresh token is stored in ~/.config.

Controls:
  Start/Pause       single: toggle, double: next, triple: previous
  Wheel press       next track
  Wheel rotation    volume down/up
  Emulator Back     previous track (the hardware Back button exits the app)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import queue
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


APP = "spotify-now-playing"
SPOTIFY_ACCOUNTS = "https://accounts.spotify.com"
SPOTIFY_API = "https://api.spotify.com/v1"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:4381/callback"
SCOPES = "user-read-playback-state user-read-currently-playing user-modify-playback-state"

GREEN = "#1ED760FF"
GREEN_DARK = "#0B3A20FF"
WHITE = "#FFFFFFFF"
MUTED = "#70917CFF"
RED = "#FF4D5AFF"
NETWORK_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError)
START_GESTURE_SECONDS = 0.36
ControlCommand = tuple[str, int]


class AppError(RuntimeError):
    """A concise, user-facing application error."""


class SpotifyError(AppError):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _decode_json_payload(raw: bytes) -> Any:
    """Decode JSON while tolerating Spotify's body-less/text-only 2xx replies."""
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return stripped.decode("utf-8", "replace")


@dataclass(frozen=True)
class Playback:
    title: str
    artist: str
    is_playing: bool
    progress_ms: int
    duration_ms: int
    volume: int | None
    device: str = ""
    fetched_at: float = 0.0
    active: bool = True

    def current_progress(self, now: float | None = None) -> int:
        value = self.progress_ms
        if self.is_playing:
            value += int(((now or time.monotonic()) - self.fetched_at) * 1000)
        return max(0, min(value, self.duration_ms))


def _clean_text(value: Any, fallback: str = "UNKNOWN") -> str:
    """Keep device text compact and safe for the bundled bitmap fonts."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = " ".join(text.upper().replace("&", "+").split())
    return text or fallback


def _json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    timeout: float = 8.0,
) -> tuple[int, Any]:
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, _decode_json_payload(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw)
            message = detail.get("error", detail)
            if isinstance(message, dict):
                message = message.get("message") or message.get("status") or message
        except (ValueError, AttributeError):
            message = raw or exc.reason
        retry = exc.headers.get("Retry-After")
        raise SpotifyError(
            exc.code,
            f"Spotify HTTP {exc.code}: {message}",
            float(retry) if retry else None,
        ) from exc


class SpotifyClient:
    """Small Spotify OAuth PKCE and Player API client using only stdlib."""

    def __init__(self, client_id: str, redirect_uri: str, token_file: Path):
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self.token: dict[str, Any] = self._load_token()
        if self.token.get("client_id") not in (None, self.client_id):
            self.token = {}

    def _load_token(self) -> dict[str, Any]:
        try:
            return json.loads(self.token_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save_token(self, payload: dict[str, Any]) -> None:
        previous_refresh = self.token.get("refresh_token")
        self.token.update(payload)
        if previous_refresh and not self.token.get("refresh_token"):
            self.token["refresh_token"] = previous_refresh
        self.token["client_id"] = self.client_id
        self.token["expires_at"] = time.time() + int(self.token.get("expires_in", 3600)) - 60
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token_file.write_text(json.dumps(self.token, indent=2) + "\n", encoding="utf-8")
        try:
            self.token_file.chmod(0o600)
        except OSError:
            pass

    def authorize(self) -> None:
        parsed = urllib.parse.urlparse(self.redirect_uri)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise AppError("redirect URI must be an http://127.0.0.1:<port>/... loopback URL")

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        auth_url = SPOTIFY_ACCOUNTS + "/authorize?" + urllib.parse.urlencode(params)
        result: dict[str, str] = {}

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("state", [""])[0] != state:
                    result["error"] = "OAuth state mismatch"
                elif "error" in query:
                    result["error"] = query["error"][0]
                elif "code" in query:
                    result["code"] = query["code"][0]
                else:
                    result["error"] = "Spotify returned no authorization code"
                ok = "code" in result
                body = (
                    "<html><body style='font:20px system-ui;background:#0b0d0c;color:white;padding:48px'>"
                    f"<h1>{'Connected' if ok else 'Could not connect'}</h1>"
                    f"<p>{'You can close this window.' if ok else result.get('error', '')}</p>"
                    "</body></html>"
                ).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", parsed.port), CallbackHandler)
        server.timeout = 180
        print("Open this URL to connect Spotify:\n" + auth_url)
        webbrowser.open(auth_url)
        server.handle_request()
        server.server_close()
        if "code" not in result:
            raise AppError("Spotify authorization failed: " + result.get("error", "timed out"))

        _, payload = _json_request(
            SPOTIFY_ACCOUNTS + "/api/token",
            method="POST",
            form={
                "grant_type": "authorization_code",
                "code": result["code"],
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": verifier,
            },
        )
        self._save_token(payload)

    def ensure_token(self) -> None:
        if not self.token.get("refresh_token"):
            self.authorize()
        elif time.time() >= float(self.token.get("expires_at", 0)):
            self.refresh()

    def refresh(self) -> None:
        refresh_token = self.token.get("refresh_token")
        if not refresh_token:
            return self.authorize()
        _, payload = _json_request(
            SPOTIFY_ACCOUNTS + "/api/token",
            method="POST",
            form={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
        )
        if "refresh_token" not in payload:
            payload["refresh_token"] = refresh_token
        self._save_token(payload)

    def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> Any:
        self.ensure_token()
        url = SPOTIFY_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            _, payload = _json_request(
                url,
                method=method,
                headers={"Authorization": "Bearer " + self.token["access_token"]},
                body=body,
            )
            return payload
        except SpotifyError as exc:
            if exc.status == 401 and retry_auth:
                self.refresh()
                return self._api(method, path, params=params, body=body, retry_auth=False)
            raise

    def playback(self) -> Playback:
        data = self._api("GET", "/me/player", params={"additional_types": "track,episode"})
        if not data or not data.get("item"):
            return Playback("NO ACTIVE PLAYER", "OPEN SPOTIFY", False, 0, 1, None, active=False)
        item = data["item"]
        artists = item.get("artists") or []
        if artists:
            artist = ", ".join(part.get("name", "") for part in artists)
        else:
            artist = (item.get("show") or {}).get("name") or item.get("publisher") or "SPOTIFY"
        device = data.get("device") or {}
        return Playback(
            title=_clean_text(item.get("name")),
            artist=_clean_text(artist, "SPOTIFY"),
            is_playing=bool(data.get("is_playing")),
            progress_ms=int(data.get("progress_ms") or 0),
            duration_ms=max(1, int(item.get("duration_ms") or 1)),
            volume=device.get("volume_percent"),
            device=_clean_text(device.get("name"), ""),
            fetched_at=time.monotonic(),
        )

    def toggle(self, state: Playback) -> None:
        self._api("PUT", "/me/player/pause" if state.is_playing else "/me/player/play")

    def next(self) -> None:
        self._api("POST", "/me/player/next")

    def previous(self) -> None:
        self._api("POST", "/me/player/previous")

    def set_volume(self, volume: int) -> None:
        self._api("PUT", "/me/player/volume", params={"volume_percent": max(0, min(100, volume))})


class DemoSpotify:
    """Deterministic playback model for emulator captures and controls."""

    TRACKS = (
        ("MIDNIGHT SERVICE", "THE NIGHT SHIFT", 214_000),
        ("LAST CALL LIGHTS", "NEON SOCIAL CLUB", 189_000),
        ("AFTER HOURS", "HOUSE SELECTOR", 241_000),
    )

    def __init__(self):
        self.index = 0
        self.playing = True
        self.progress = 73_000
        self.volume = 65
        self.anchor = time.monotonic()

    def playback(self) -> Playback:
        title, artist, duration = self.TRACKS[self.index]
        progress = self.progress + (int((time.monotonic() - self.anchor) * 1000) if self.playing else 0)
        if progress >= duration:
            self.next()
            return self.playback()
        return Playback(title, artist, self.playing, progress, duration, self.volume, "BAR SPEAKERS", time.monotonic())

    def _capture_progress(self) -> None:
        if self.playing:
            self.progress += int((time.monotonic() - self.anchor) * 1000)
        self.anchor = time.monotonic()

    def toggle(self, _state: Playback) -> None:
        self._capture_progress()
        self.playing = not self.playing

    def next(self) -> None:
        self.index = (self.index + 1) % len(self.TRACKS)
        self.progress = 0
        self.anchor = time.monotonic()

    def previous(self) -> None:
        self.index = (self.index - 1) % len(self.TRACKS)
        self.progress = 0
        self.anchor = time.monotonic()

    def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, volume))


class BusyBar:
    def __init__(self, host: str, token: str | None = None):
        if "://" not in host:
            host = "http://" + host
        self.base = host.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 6.0) -> Any:
        headers = {"X-API-Sem-Ver": "25.0.0"}
        if self.token:
            headers["X-API-Token"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def draw(self, elements: list[dict[str, Any]]) -> None:
        self.request("POST", "/api/display/draw", {
            "application_name": APP,
            "priority": 100,
            "elements": elements,
        })

    def clear(self) -> None:
        path = "/api/display/draw?" + urllib.parse.urlencode({"application_name": APP})
        self.request("DELETE", path)

    def clear_all(self) -> None:
        """Release a firmware screen that reclaimed priority after a button press."""
        self.request("DELETE", "/api/display/draw")

    def is_emulator(self) -> bool:
        try:
            self.request("GET", "/api/_scenario", timeout=1.5)
            return True
        except Exception:  # noqa: BLE001 - a 404 means real hardware
            return False


def _text(eid: str, value: str, x: int, y: int, font: str, color: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": eid,
        "type": "text",
        "text": value,
        "x": x,
        "y": y,
        "font": font,
        "color": color,
        "display": "front",
        **extra,
    }


def _rect(eid: str, x: int, y: int, width: int, height: int, color: str) -> dict[str, Any]:
    return {
        "id": eid,
        "type": "rectangle",
        "x": x,
        "y": y,
        "width": max(1, width),
        "height": max(1, height),
        "border_width": 0,
        "fill": "solid",
        "fill_colors": [color],
        "display": "front",
    }


def render_playback(state: Playback, feedback: str | None, phase: int) -> list[dict[str, Any]]:
    if not state.active:
        return [
            _rect("brand", 1, 2, 3, 3, GREEN),
            _text("title", state.title, 7, 0, "normal", WHITE, width=64, scroll_rate=0),
            _text("artist", state.artist, 7, 8, "small", GREEN, width=64, scroll_rate=0),
            _rect("rail", 0, 15, 72, 1, GREEN_DARK),
        ]

    elements: list[dict[str, Any]] = []
    if state.is_playing:
        heights = ((2, 5, 3), (4, 2, 5), (3, 5, 2))[phase % 3]
        for index, height in enumerate(heights):
            elements.append(_rect(f"eq{index}", 1 + index * 2, 7 - height, 1, height, GREEN))
    else:
        elements.extend((_rect("pause1", 1, 2, 1, 5, GREEN), _rect("pause2", 4, 2, 1, 5, GREEN)))

    elements.append(_text(
        "title",
        state.title,
        8,
        0,
        "normal",
        WHITE,
        width=63,
        # The firmware scrolls only when the rendered pixels exceed `width`.
        # Always enabling it avoids unreliable character-count estimates.
        scroll_rate=420,
        scroll_start_delay=700,
        scroll_repeat_delay=1200,
    ))
    subtitle = feedback or state.artist
    elements.append(_text(
        "artist",
        subtitle,
        8,
        8,
        "small",
        GREEN if feedback else MUTED,
        width=63,
        scroll_rate=300,
        scroll_start_delay=1100,
        scroll_repeat_delay=1600,
    ))
    elements.append(_rect("rail", 0, 15, 72, 1, GREEN_DARK))
    progress = int(72 * state.current_progress() / max(1, state.duration_ms))
    if progress:
        elements.append(_rect("progress", 0, 15, progress, 1, GREEN))
    return elements


def _emulator_events(bar: BusyBar, commands: queue.Queue[ControlCommand], stop: threading.Event) -> None:
    """Read the emulator-only SSE stream so its controls exercise the app."""
    mapping: dict[str, ControlCommand] = {
        "start": ("start", 0),
        "ok": ("next", 0),
        "back": ("previous", 0),
        "up": ("volume", 1),
        "down": ("volume", -1),
    }
    while not stop.is_set():
        try:
            request = urllib.request.Request(bar.base + "/events", headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(request, timeout=30) as response:
                event = ""
                for raw in response:
                    if stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:") and event == "input":
                        key = json.loads(line[5:].strip()).get("key")
                        if key in mapping:
                            commands.put(mapping[key])
        except Exception as exc:  # noqa: BLE001 - reconnect loop
            if not stop.is_set():
                print(f"emulator input stream reconnecting: {exc}")
                stop.wait(2)


def _commands_from_input_event(event: dict[str, Any]) -> list[ControlCommand]:
    """Translate input while restoring proto3 enum defaults omitted from JSON."""
    commands: list[ControlCommand] = []
    if "button_event" in event:
        button = event.get("button_event") or {}
        action = button.get("action", "PRESS")
        name = button.get("button", "OK")
        if action == "PRESS":
            if name == "START":
                commands.append(("start", 0))
            elif name == "OK":
                commands.append(("next", 0))
    encoder = event.get("encoder_event") or {}
    delta = int(encoder.get("delta") or 0)
    if delta:
        commands.append(("volume", max(-10, min(10, delta))))
    return commands


async def _hardware_event_loop(
    bar: BusyBar,
    commands: queue.Queue[ControlCommand],
    stop: threading.Event,
) -> None:
    try:
        from busylib import AsyncBusyBar
    except ImportError:
        print("hardware controls disabled: install requirements.txt (busylib)")
        return

    while not stop.is_set():
        try:
            async with AsyncBusyBar(addr=bar.base, token=bar.token) as client:
                async for message in client.stream_status_ws():
                    if stop.is_set():
                        return
                    if not isinstance(message, dict):
                        continue
                    for update in message.get("updates", []):
                        event = update.get("input") or {}
                        for command in _commands_from_input_event(event):
                            commands.put(command)
        except Exception as exc:  # noqa: BLE001 - reconnect on device/network loss
            if not stop.is_set():
                print(f"BUSY Bar input stream reconnecting: {exc}")
                await asyncio.sleep(2)


def _hardware_events(bar: BusyBar, commands: queue.Queue[ControlCommand], stop: threading.Event) -> None:
    asyncio.run(_hardware_event_loop(bar, commands, stop))


def _start_input_listener(
    bar: BusyBar,
    commands: queue.Queue[ControlCommand],
    stop: threading.Event,
) -> threading.Thread:
    target = _emulator_events if bar.is_emulator() else _hardware_events
    thread = threading.Thread(target=target, args=(bar, commands, stop), daemon=True, name="busybar-input")
    thread.start()
    return thread


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="10.0.4.20", help="BUSY Bar ip[:port] or emulator host")
    parser.add_argument("--busy-token", default=os.environ.get("BUSY_API_TOKEN"), help="BUSY Bar Wi-Fi API token")
    parser.add_argument("--spotify-client-id", default=os.environ.get("SPOTIFY_CLIENT_ID"), help="Spotify app client ID")
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI, help="Spotify allowlisted loopback redirect URI")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".config" / APP / "spotify-token.json",
        help="private Spotify token cache",
    )
    parser.add_argument("--demo", action="store_true", help="use sample playback without Spotify (emulator previews)")
    parser.add_argument("--once", action="store_true", help="draw one frame and exit (useful for tests)")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    bar = BusyBar(args.host, args.busy_token)
    if args.demo:
        spotify: SpotifyClient | DemoSpotify = DemoSpotify()
    else:
        if not args.spotify_client_id:
            raise AppError("set SPOTIFY_CLIENT_ID or pass --spotify-client-id (use --demo for the emulator)")
        spotify = SpotifyClient(args.spotify_client_id, args.redirect_uri, args.token_file)

    print(f"{APP} -> {bar.base} ({'demo' if args.demo else 'Spotify'})")
    state: Playback | None = None
    feedback: str | None = None
    feedback_until = 0.0
    next_poll = 0.0
    next_draw = 0.0
    backoff_until = 0.0
    phase = 0
    pending_volume: int | None = None
    pending_volume_at = 0.0
    pending_start_taps = 0
    pending_start_at = 0.0
    commands: queue.Queue[ControlCommand] = queue.Queue()
    stop = threading.Event()
    bar.clear_all()
    listener = _start_input_listener(bar, commands, stop)

    try:
        if isinstance(spotify, SpotifyClient) and not spotify.token.get("refresh_token"):
            try:
                bar.draw(render_playback(
                    Playback("CONNECT SPOTIFY", "CHECK YOUR BROWSER", False, 0, 1, None, active=False),
                    None,
                    0,
                ))
            except Exception as exc:  # noqa: BLE001 - OAuth can proceed without a display
                print(f"could not draw authorization prompt: {exc}")
            spotify.ensure_token()
        while True:
            now = time.monotonic()
            if now >= next_poll and now >= backoff_until:
                try:
                    state = spotify.playback()
                    next_poll = now + 2.0
                except SpotifyError as exc:
                    if exc.status == 429:
                        wait = exc.retry_after or 5.0
                        backoff_until = now + wait
                        next_poll = backoff_until
                        print(f"Spotify rate limited; retrying in {wait:g}s")
                    else:
                        raise
                except NETWORK_ERRORS as exc:
                    feedback = "SPOTIFY OFFLINE"
                    feedback_until = now + 4.5
                    backoff_until = now + 5.0
                    next_poll = backoff_until
                    if state is None:
                        state = Playback("SPOTIFY OFFLINE", "RETRYING", False, 0, 1, None, active=False)
                    print(f"Spotify connection failed; retrying: {getattr(exc, 'reason', exc)}")

            changed = False
            while state is not None:
                try:
                    command, value = commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    if command == "start":
                        pending_start_taps = min(3, pending_start_taps + 1)
                        pending_start_at = now if pending_start_taps == 3 else now + START_GESTURE_SECONDS
                        continue
                    if command == "toggle":
                        spotify.toggle(state)
                        state = replace(state, is_playing=not state.is_playing, progress_ms=state.current_progress(), fetched_at=now)
                        feedback = "PLAY" if state.is_playing else "PAUSE"
                    elif command == "next":
                        spotify.next()
                        feedback = "NEXT TRACK"
                    elif command == "previous":
                        spotify.previous()
                        feedback = "PREVIOUS TRACK"
                    elif command == "volume":
                        current_volume = pending_volume if pending_volume is not None else state.volume
                        volume = max(0, min(100, (current_volume if current_volume is not None else 50) + 5 * value))
                        pending_volume = volume
                        pending_volume_at = now + 0.14
                        state = replace(state, volume=volume)
                        feedback = f"VOLUME {volume}"
                    feedback_until = now + 1.0
                    if command != "volume":
                        next_poll = min(next_poll, now + 0.45)
                    changed = True
                except SpotifyError as exc:
                    feedback = "CONTROL UNAVAILABLE"
                    feedback_until = now + 1.5
                    print(exc)
                except NETWORK_ERRORS as exc:
                    feedback = "SPOTIFY OFFLINE"
                    feedback_until = now + 2.0
                    print(f"Spotify control failed: {getattr(exc, 'reason', exc)}")

            if state is not None and pending_start_taps and now >= pending_start_at:
                taps = pending_start_taps
                pending_start_taps = 0
                try:
                    if taps == 1:
                        spotify.toggle(state)
                        state = replace(
                            state,
                            is_playing=not state.is_playing,
                            progress_ms=state.current_progress(),
                            fetched_at=now,
                        )
                        feedback = "PLAY" if state.is_playing else "PAUSE"
                    elif taps == 2:
                        spotify.next()
                        feedback = "NEXT TRACK"
                    else:
                        spotify.previous()
                        feedback = "PREVIOUS TRACK"
                    feedback_until = now + 1.0
                    next_poll = min(next_poll, now + 0.45)
                    changed = True
                except SpotifyError as exc:
                    feedback = "CONTROL UNAVAILABLE"
                    feedback_until = now + 1.5
                    print(exc)
                except NETWORK_ERRORS as exc:
                    feedback = "SPOTIFY OFFLINE"
                    feedback_until = now + 2.0
                    print(f"Spotify control failed: {getattr(exc, 'reason', exc)}")

            if feedback and now >= feedback_until:
                feedback = None
                changed = True

            if state is not None and (changed or now >= next_draw):
                draw_delay = 0.8
                try:
                    bar.draw(render_playback(state, feedback, phase))
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
                    print("display reclaimed by firmware; taking it back")
                    try:
                        bar.clear_all()
                        draw_delay = 0.12
                    except NETWORK_ERRORS:
                        draw_delay = 2.0
                except NETWORK_ERRORS as exc:
                    print(f"BUSY Bar offline; retrying: {getattr(exc, 'reason', exc)}")
                    draw_delay = 2.0
                phase += 1
                next_draw = now + draw_delay
                if args.once:
                    return

            if pending_volume is not None and now >= pending_volume_at:
                target_volume = pending_volume
                pending_volume = None
                try:
                    spotify.set_volume(target_volume)
                    next_poll = min(next_poll, now + 0.55)
                except SpotifyError as exc:
                    feedback = "CONTROL UNAVAILABLE"
                    feedback_until = now + 1.5
                    print(exc)
                except NETWORK_ERRORS as exc:
                    feedback = "SPOTIFY OFFLINE"
                    feedback_until = now + 2.0
                    print(f"Spotify volume failed: {getattr(exc, 'reason', exc)}")
            time.sleep(0.08)
    finally:
        stop.set()
        if listener.is_alive():
            listener.join(timeout=0.25)
        try:
            bar.clear()
        except Exception:
            pass


def main() -> None:
    try:
        run(parse_args())
    except KeyboardInterrupt:
        print("\nstopped")
    except NETWORK_ERRORS as exc:
        sys.exit(f"cannot reach service: {getattr(exc, 'reason', exc)}")
    except AppError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
