#!/usr/bin/env python3
"""Bad Apple BUSY Bar

It has a screen? It can play Bad Apple. And yes, with audio, too!

    python3 app.py                        # BUSY Bar over USB (always 10.0.4.20)
    python3 app.py --host 127.0.0.1:8080  # emulator or a Wi-Fi bar
    python3 app.py --muted                # Play without audio (much faster upload)

Assets including the animation file and audio WAV will be downloaded automatically and cached for future runs.

See the full source code, including the assets and animation file generation scripts, at the main repository:
https://github.com/codynhanpham/BadApple/tree/main/busy-apple

"""

import argparse
import json
import signal
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

APP = "bad-apple"
DISPLAY_WIDTH = 72
DISPLAY_HEIGHT = 16
PRIORITY = 100
UPLOAD_TIMEOUTS = {"bad_apple_animation.anim": 30, "bad_apple_mono_s16_44100.wav": 60}

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets" / "bad_apple_h16px"
ANIMATION_FILE = ASSETS / "bad_apple_animation.anim"
AUDIO_FILE = ASSETS / "bad_apple_mono_s16_44100.wav"
REMOTE_ANIMATION_NAME = "bad_apple_animation.anim"
REMOTE_AUDIO_NAME = "bad_apple_mono_s16_44100.wav"

PREMADE_ASSETS_URLS = {
    ANIMATION_FILE: "https://raw.githubusercontent.com/codynhanpham/BadApple/refs/heads/main/busy-apple/assets/bad_apple_h16px/bad_apple_animation.anim",
    AUDIO_FILE: "https://raw.githubusercontent.com/codynhanpham/BadApple/refs/heads/main/busy-apple/assets/bad_apple_h16px/bad_apple_mono_s16_44100.wav",
}
ASSET_DOWNLOAD_TIMEOUT = 120


class BusyBarError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class BusyBarClient:
    def __init__(self, host: str) -> None:
        if "://" not in host:
            host = f"http://{host}"
        self.base_url = host.rstrip("/") + "/api"

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        timeout: float = 10,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read()
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
                message = detail.get("error", str(detail)) if isinstance(detail, dict) else str(detail)
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = error.reason or str(error)
            raise BusyBarError(error.code, message) from error
        except (URLError, TimeoutError, OSError) as error:
            raise BusyBarError(0, str(error)) from error

        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return data

    def draw(self, elements: list[dict[str, Any]]) -> None:
        payload = json.dumps({"application_name": APP, "priority": PRIORITY, "elements": elements}).encode()
        self.request("POST", "/display/draw", payload, timeout=5)

    def clear(self) -> None:
        query = urlencode({"application_name": APP})
        self.request("DELETE", f"/display/draw?{query}", timeout=5)

    def delete_assets(self) -> None:
        query = urlencode({"application_name": APP})
        self.request("DELETE", f"/assets/upload?{query}", timeout=5)

    def upload(self, filename: str, data: bytes, timeout: float) -> None:
        query = urlencode({"application_name": APP, "file": filename})
        self.request("POST", f"/assets/upload?{query}", data, "application/octet-stream", timeout)

    def play_audio(self, filename: str) -> None:
        payload = json.dumps({"application_name": APP, "path": filename}).encode()
        self.request("POST", "/audio/play", payload)

    def stop_audio(self) -> None:
        self.request("DELETE", "/audio/play", timeout=5)


def animation_duration(path: Path) -> float:
    data = path.read_bytes()
    if len(data) < 36 or data[:8] != b"bicycle0":
        raise ValueError(f"Invalid animation file: {path}")
    fps = data[12]
    display_ticks = struct.unpack_from("<I", data, 32)[0]
    if not fps or not display_ticks:
        raise ValueError(f"Invalid animation timing metadata: {path}")
    return display_ticks / fps


def ensure_local_assets(audio_enabled: bool) -> None:
    required_assets = [ANIMATION_FILE]
    if audio_enabled:
        required_assets.append(AUDIO_FILE)

    missing_assets = [path for path in required_assets if not path.is_file()]
    if not missing_assets:
        return

    print("Missing some required assets. Downloading them now.\nThey will be cached in ./assets/ next to this script file and reused in future runs.", flush=True)
    for path in missing_assets:
        url = PREMADE_ASSETS_URLS[path]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_name(f".{path.name}.download")
        print(f"Downloading {path.name}...", flush=True)
        try:
            with urlopen(url, timeout=ASSET_DOWNLOAD_TIMEOUT) as response:
                temporary_path.write_bytes(response.read())
            temporary_path.replace(path)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            temporary_path.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download {path.name}: {error}") from error


def background() -> dict[str, Any]:
    return {
        "id": "background", "type": "rectangle", "x": 0, "y": 0,
        "align": "top_left", "display": "front", "width": DISPLAY_WIDTH,
        "height": DISPLAY_HEIGHT, "fill": "solid", "fill_colors": ["#000000FF"],
        "border_width": 0, "border_color": "#00000000", "timeout": 0,
    }


def upload_screen(elapsed: float) -> list[dict[str, Any]]:
    cycle_width = 72
    bar_width = 55
    distance = elapsed * 38
    top = distance % cycle_width
    bottom = (25 - distance) % cycle_width
    elements = [background(), {
        "id": "upload-progress-label", "type": "text", "text": "LOADING ASSETS...",
        "font": "small", "color": "#FFFFFFFF", "x": 36, "y": 8,
        "align": "center", "display": "front", "timeout": 0,
    }]
    for identifier, y, phase in (("top", 0, top), ("bottom", 15, bottom)):
        for suffix, x in (("a", phase - cycle_width), ("b", phase)):
            elements.append({
                "id": f"upload-progress-{identifier}-{suffix}", "type": "rectangle",
                "x": x, "y": y, "align": "top_left", "display": "front",
                "width": bar_width, "height": 1, "fill": "solid",
                "fill_colors": ["#FFFFFFFF"], "border_width": 0,
                "border_color": "#00000000", "timeout": 0,
            })
    return elements


def is_temporary(error: BusyBarError) -> bool:
    return error.status in (0, 409) or error.status >= 500


def replace_terminal_line(message: str, *, end: str = "") -> None:
    print(f"\r\033[2K{message}", end=end, flush=True)


def install_signal_handlers(stop_event: threading.Event) -> None:
    def handle_signal(_signum: int, _frame: Any) -> None:
        if not stop_event.is_set():
            print("\nCtrl+C received. Stopping playback...", flush=True)
            stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)


def wait_until_drawable(client: BusyBarClient, stop_event: threading.Event) -> bool:
    started = time.monotonic()
    waited = False
    while not stop_event.is_set():
        try:
            client.draw([background()])
            if waited:
                replace_terminal_line("Display is now available.", end="\n")
            return waited
        except BusyBarError as error:
            if not is_temporary(error):
                raise
            waited = True
            elapsed = int(time.monotonic() - started)
            replace_terminal_line(f"Waiting for display to become available... ({elapsed}s elapsed)")
            stop_event.wait(1)
    return True


def wait_for_animation(stop_event: threading.Event, duration: float) -> None:
    deadline = time.monotonic() + duration
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        stop_event.wait(min(remaining, 0.1))


def upload_asset(
    client: BusyBarClient,
    filename: str,
    data: bytes,
    stop_event: threading.Event,
    upload_notice: list[bool],
    audio_enabled: bool,
) -> None:
    timeout = UPLOAD_TIMEOUTS[filename]
    while not stop_event.is_set():
        waited = wait_until_drawable(client, stop_event)
        if stop_event.is_set():
            return
        if not upload_notice[0] and not waited:
            message = "Uploading video assets to BUSY Bar"
            if audio_enabled:
                message += "\nThis may take a bit with audio..."
            print(message, flush=True)
            upload_notice[0] = True

        started = time.monotonic()
        progress_stop = threading.Event()

        def progress() -> None:
            while not progress_stop.wait(0.22) and not stop_event.is_set():
                try:
                    client.draw(upload_screen(time.monotonic() - started))
                except BusyBarError:
                    pass

        progress_thread = threading.Thread(target=progress, daemon=True)
        progress_thread.start()
        upload_done = threading.Event()
        upload_error: list[BaseException] = []

        def upload() -> None:
            try:
                client.upload(filename, data, timeout)
            except BaseException as error:
                upload_error.append(error)
            finally:
                upload_done.set()

        upload_thread = threading.Thread(target=upload, daemon=True)
        upload_thread.start()
        try:
            while not upload_done.wait(0.1):
                if stop_event.is_set():
                    return

            if upload_error:
                raise upload_error[0]
            return
        except BusyBarError as error:
            if not is_temporary(error):
                raise
            replace_terminal_line("Device became unavailable during upload; waiting to retry...")
        finally:
            progress_stop.set()
            progress_thread.join(timeout=1)


def main() -> int:
    module_doc = __doc__ or ""
    parser = argparse.ArgumentParser(
        description="Play Bad Apple on a BUSY Bar.",
        epilog=module_doc.partition("\n")[2].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="10.0.4.20", help="BUSY Bar host, including optional port")
    parser.add_argument("--muted", action="store_true", help="Skip audio upload and playback")
    args = parser.parse_args()

    client = BusyBarClient(args.host)
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    audio_started = False

    try:
        ensure_local_assets(not args.muted)
        animation_data = ANIMATION_FILE.read_bytes()
        upload_notice = [False]
        upload_asset(client, REMOTE_ANIMATION_NAME, animation_data, stop_event, upload_notice, not args.muted)
        if not args.muted and not stop_event.is_set():
            upload_asset(client, REMOTE_AUDIO_NAME, AUDIO_FILE.read_bytes(), stop_event, upload_notice, not args.muted)

        if stop_event.is_set():
            return 0

        if not args.muted:
            client.play_audio(REMOTE_AUDIO_NAME)
            audio_started = True
            print("Audio playback started.", flush=True)

        client.draw([background(), {
            "id": "bad-apple-animation", "type": "animation", "path": REMOTE_ANIMATION_NAME,
            "x": 0, "y": 0, "align": "top_left", "display": "front", "loop": False,
            "await_previous_end": False, "opacity": 100, "timeout": 0,
        }])
        print("Animation playback started.", flush=True)
        wait_for_animation(stop_event, animation_duration(ANIMATION_FILE))
        return 0
    except KeyboardInterrupt:
        stop_event.set()
        return 130
    finally:
        if audio_started:
            try:
                client.stop_audio()
            except BusyBarError as error:
                if error.status != 410:
                    print(f"Failed to stop audio: {error}", file=sys.stderr)
        try:
            client.clear()
            print("Display cleared.", flush=True)
        except BusyBarError as error:
            print(f"Failed to clear display: {error}", file=sys.stderr)
        try:
            client.delete_assets()
            print("Remote assets deleted.", flush=True)
        except BusyBarError as error:
            print(f"Failed to delete remote assets: {error}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

