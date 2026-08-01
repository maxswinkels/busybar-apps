#!/usr/bin/env python3
"""Home Assistant: browse and control lights from a BUSY Bar.

Run without Home Assistant credentials for a safe interactive demo:

    python app.py --host 127.0.0.1:8080

Connect to Home Assistant with environment variables:

    HA_URL=http://homeassistant.local:8123 HA_TOKEN=... python app.py

Optionally choose and order lights with ``HA_ENTITIES`` (comma-separated).
The BUSY HTTP access key can be supplied as ``BUSYBAR_TOKEN``.
"""

from __future__ import annotations

import argparse
import asyncio
import binascii
import os
import struct
import zlib
from dataclasses import dataclass
from typing import Any

import requests
from busylib import AsyncBusyBar, exceptions, types

APP = "home-assistant"
ACCENT = "#63E6BEFF"
WHITE = "#FFFFFFFF"
MUTED = "#A8B2C3FF"
OFF = "#495365FF"

COLOR_PRESETS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("MINT", (99, 230, 190)),
    ("AMBER", (255, 191, 71)),
    ("BLUE", (80, 150, 255)),
    ("ROSE", (255, 94, 147)),
    ("LIME", (159, 232, 112)),
    ("VIOLET", (167, 139, 250)),
)

LIGHT_BITMAP = (
    "................",
    "......####......",
    "....########....",
    "...##########...",
    "..############..",
    "..############..",
    "...##########...",
    "....########....",
    ".....######.....",
    "......####......",
    "......####......",
    ".....######.....",
    ".....######.....",
    "......####......",
    "................",
    "................",
)


@dataclass
class Light:
    """Small, mutable view of a Home Assistant light state."""

    entity_id: str
    name: str
    is_on: bool
    brightness: int
    rgb: tuple[int, int, int]
    temperature: int
    min_temperature: int
    max_temperature: int
    supports_rgb: bool
    supports_temperature: bool

    @property
    def controls(self) -> tuple[str, ...]:
        controls = ["brightness"]
        if self.supports_rgb:
            controls.append("color")
        if self.supports_temperature:
            controls.append("temperature")
        return tuple(controls)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
    )


def icon_png(size: int, color: tuple[int, int, int]) -> bytes:
    """Render the built-in transparent bulb bitmap as a tiny RGBA PNG."""
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        source_y = min(15, y * 16 // size)
        for x in range(size):
            source_x = min(15, x * 16 // size)
            alpha = 255 if LIGHT_BITMAP[source_y][source_x] == "#" else 0
            rows.extend((*color, alpha))
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="10.0.4.20", help="BUSY host or host:port")
    parser.add_argument("--busy-token", default=os.getenv("BUSYBAR_TOKEN", ""))
    parser.add_argument("--ha-url", default=os.getenv("HA_URL", ""))
    parser.add_argument("--ha-token", default=os.getenv("HA_TOKEN", ""))
    parser.add_argument("--entities", default=os.getenv("HA_ENTITIES", ""))
    parser.add_argument("--once", action="store_true", help="draw one frame and exit")
    parser.add_argument(
        "--screen",
        choices=("browse", "control"),
        default="browse",
        help="initial screen (useful for emulator previews)",
    )
    return parser.parse_args()


class HomeAssistant:
    """Minimal REST client; requests is kept off the asyncio event loop."""

    def __init__(self, url: str, token: str, entity_ids: list[str]) -> None:
        self.url = url.rstrip("/")
        self.entity_ids = entity_ids
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    @property
    def configured(self) -> bool:
        return bool(self.url and self.session.headers.get("Authorization") != "Bearer ")

    def get_lights(self) -> list[Light]:
        response = self.session.get(f"{self.url}/api/states", timeout=8)
        response.raise_for_status()
        states = response.json()
        wanted = set(self.entity_ids)
        lights = [
            state
            for state in states
            if str(state.get("entity_id", "")).startswith("light.")
            and (not wanted or state["entity_id"] in wanted)
        ]
        if wanted:
            order = {
                entity_id: index for index, entity_id in enumerate(self.entity_ids)
            }
            lights.sort(key=lambda state: order.get(state["entity_id"], len(order)))
        return [light_from_state(state) for state in lights[:12]]

    def service(self, service: str, data: dict[str, Any]) -> None:
        response = self.session.post(
            f"{self.url}/api/services/light/{service}", json=data, timeout=8
        )
        response.raise_for_status()


def light_from_state(state: dict[str, Any]) -> Light:
    attrs = state.get("attributes", {})
    modes = {str(mode) for mode in attrs.get("supported_color_modes", [])}
    raw_rgb = attrs.get("rgb_color") or COLOR_PRESETS[0][1]
    minimum = int(attrs.get("min_color_temp_kelvin") or 2000)
    maximum = int(attrs.get("max_color_temp_kelvin") or 6500)
    return Light(
        entity_id=state["entity_id"],
        name=str(attrs.get("friendly_name") or state["entity_id"].split(".", 1)[1]),
        is_on=state.get("state") == "on",
        brightness=round(int(attrs.get("brightness") or 0) * 100 / 255),
        rgb=tuple(int(channel) for channel in raw_rgb[:3]),
        temperature=int(attrs.get("color_temp_kelvin") or (minimum + maximum) // 2),
        min_temperature=minimum,
        max_temperature=maximum,
        supports_rgb=bool(modes & {"hs", "xy", "rgb", "rgbw", "rgbww"}),
        supports_temperature="color_temp" in modes,
    )


def demo_lights() -> list[Light]:
    return [
        Light(
            "light.studio",
            "Studio Lamp",
            True,
            72,
            COLOR_PRESETS[0][1],
            3200,
            2000,
            6500,
            True,
            True,
        ),
        Light(
            "light.desk",
            "Desk Light",
            True,
            45,
            COLOR_PRESETS[2][1],
            4000,
            2200,
            6500,
            True,
            True,
        ),
        Light(
            "light.lounge",
            "Lounge",
            False,
            0,
            COLOR_PRESETS[3][1],
            2700,
            2000,
            6500,
            True,
            True,
        ),
        Light(
            "light.bedroom",
            "Bedroom",
            True,
            28,
            COLOR_PRESETS[1][1],
            2400,
            2000,
            6500,
            False,
            True,
        ),
        Light(
            "light.hall",
            "Hall",
            False,
            0,
            COLOR_PRESETS[0][1],
            3500,
            2000,
            6500,
            False,
            True,
        ),
    ]


def nearest_color_index(rgb: tuple[int, int, int]) -> int:
    return min(
        range(len(COLOR_PRESETS)),
        key=lambda index: sum(
            (rgb[channel] - COLOR_PRESETS[index][1][channel]) ** 2
            for channel in range(3)
        ),
    )


def text_element(
    element_id: str,
    text: str,
    x: int,
    y: int,
    *,
    display: types.DisplayName = types.DisplayName.FRONT,
    font: str = "tiny",
    color: str = WHITE,
    **extra: Any,
) -> types.TextElement:
    return types.TextElement(
        id=element_id,
        display=display,
        x=x,
        y=y,
        text=text,
        font=font,
        color=color,
        **extra,
    )


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        entity_ids = [
            value.strip() for value in args.entities.split(",") if value.strip()
        ]
        self.ha = HomeAssistant(args.ha_url, args.ha_token, entity_ids)
        self.demo = not self.ha.configured
        self.lights = demo_lights()
        self.host = args.host
        self.client = AsyncBusyBar(args.host, token=args.busy_token or None)
        self.navigation = args.screen
        self.selected = 0
        self.control = 0
        self.pending: dict[str, tuple[str, dict[str, Any]]] = {}
        self.workers: dict[str, asyncio.Task[None]] = {}

    @property
    def light(self) -> Light:
        self.selected %= len(self.lights)
        return self.lights[self.selected]

    async def load(self) -> None:
        if not self.demo:
            self.lights = await asyncio.to_thread(self.ha.get_lights)
            if not self.lights:
                raise RuntimeError("No Home Assistant lights matched HA_ENTITIES")
        await self.upload_icons()

    async def upload_icons(self) -> None:
        assets = {
            "ha-light-active.png": icon_png(14, (99, 230, 190)),
            "ha-light-inactive.png": icon_png(10, (255, 255, 255)),
            "ha-light-control.png": icon_png(16, (255, 255, 255)),
            "ha-light-back.png": icon_png(40, (255, 255, 255)),
            "ha-blank.png": icon_png(1, (0, 0, 0)),
        }
        for filename, data in assets.items():
            await self.client.assets_upload(APP, filename, data)

    def browse_window(self) -> tuple[list[Light], int]:
        page_start = self.selected // 4 * 4
        return self.lights[page_start : page_start + 4], self.selected - page_start

    def control_value(self) -> str:
        control = self.light.controls[self.control % len(self.light.controls)]
        if control == "brightness":
            return f"{self.light.brightness}%"
        if control == "color":
            return COLOR_PRESETS[nearest_color_index(self.light.rgb)][0]
        return f"{self.light.temperature}K"

    def payload(self) -> types.DisplayElements:
        light = self.light
        if self.navigation == "browse":
            window, selected_slot = self.browse_window()
            images: list[types.DisplayElement] = []
            for slot in range(4):
                active = slot == selected_slot and slot < len(window)
                images.append(
                    types.ImageElement(
                        id=f"front_image_{slot}",
                        display=types.DisplayName.FRONT,
                        x=slot * 18 + (0 if active else 2),
                        y=0 if active else 2,
                        path=(
                            "ha-light-active.png"
                            if active
                            else "ha-light-inactive.png"
                            if slot < len(window)
                            else "ha-blank.png"
                        ),
                    )
                )
            mode = "DEMO" if self.demo else "LIVE"
            return types.DisplayElements(
                application_name=APP,
                priority=100,
                elements=[
                    *images,
                    types.ImageElement(
                        id="back_icon",
                        display=types.DisplayName.BACK,
                        x=8,
                        y=15,
                        path="ha-light-back.png",
                    ),
                    text_element(
                        "back_kicker",
                        f"HOME ASSISTANT  {self.selected + 1}/{len(self.lights)}",
                        58,
                        8,
                        display=types.DisplayName.BACK,
                        color=MUTED,
                    ),
                    text_element(
                        "back_name",
                        light.name,
                        58,
                        25,
                        display=types.DisplayName.BACK,
                        font="normal",
                        width=96,
                        scroll_rate=70,
                        scroll_start_delay=150,
                        scroll_repeat_delay=250,
                    ),
                    text_element(
                        "back_state",
                        f"LIGHT  ·  {'ON' if light.is_on else 'OFF'}  ·  {light.brightness}%",
                        58,
                        47,
                        display=types.DisplayName.BACK,
                        font="small",
                        color=WHITE if light.is_on else OFF,
                    ),
                    text_element(
                        "back_hint",
                        "DIAL: BROWSE   SELECT: CONTROLS   START: TOGGLE",
                        8,
                        68,
                        display=types.DisplayName.BACK,
                        color=MUTED,
                    ),
                    text_element(
                        "back_mode",
                        mode,
                        152,
                        8,
                        display=types.DisplayName.BACK,
                        color=MUTED,
                        align="top_right",
                    ),
                ],
            )

        controls = light.controls
        self.control %= len(controls)
        labels = {"brightness": "DIM", "color": "RGB", "temperature": "TEMP"}
        elements: list[types.DisplayElement] = [
            types.ImageElement(
                id="front_image_0",
                display=types.DisplayName.FRONT,
                x=0,
                y=0,
                path="ha-light-control.png",
            ),
            *[
                types.ImageElement(
                    id=f"front_image_{slot}",
                    display=types.DisplayName.FRONT,
                    x=0,
                    y=0,
                    path="ha-blank.png",
                )
                for slot in range(1, 4)
            ],
        ]
        for index in range(3):
            label = labels[controls[index]] if index < len(controls) else ""
            elements.append(
                text_element(
                    f"front_text_{index}",
                    label,
                    (18, 35, 52)[index],
                    0,
                    color=ACCENT if index == self.control else MUTED,
                )
            )
        elements.extend(
            [
                text_element(
                    "front_text_3",
                    self.control_value(),
                    19,
                    8,
                    font="small",
                    color=ACCENT if self.navigation == "edit" else WHITE,
                ),
                types.ImageElement(
                    id="back_icon",
                    display=types.DisplayName.BACK,
                    x=8,
                    y=15,
                    path="ha-light-back.png",
                ),
                text_element(
                    "back_kicker",
                    f"HOME ASSISTANT  {self.selected + 1}/{len(self.lights)}",
                    58,
                    8,
                    display=types.DisplayName.BACK,
                    color=MUTED,
                ),
                text_element(
                    "back_name",
                    light.name,
                    58,
                    25,
                    display=types.DisplayName.BACK,
                    font="normal",
                    width=96,
                    scroll_rate=70,
                    scroll_start_delay=150,
                    scroll_repeat_delay=250,
                ),
                text_element(
                    "back_state",
                    f"{labels[controls[self.control]]}  ·  {self.control_value()}",
                    58,
                    47,
                    display=types.DisplayName.BACK,
                    font="small",
                    color=ACCENT,
                ),
                text_element(
                    "back_hint",
                    "DIAL: CHANGE   SELECT: DONE   START: TOGGLE"
                    if self.navigation == "edit"
                    else "DIAL: PICK   SELECT: EDIT   START: TOGGLE",
                    8,
                    68,
                    display=types.DisplayName.BACK,
                    color=MUTED,
                ),
                text_element(
                    "back_mode",
                    self.navigation.upper(),
                    152,
                    8,
                    display=types.DisplayName.BACK,
                    color=MUTED,
                    align="top_right",
                ),
            ]
        )
        return types.DisplayElements(
            application_name=APP, priority=100, elements=elements
        )

    async def draw(self) -> None:
        await self.client.display_draw(self.payload(), sanitize_text=True)

    def queue_service(self, service: str, data: dict[str, Any]) -> None:
        if self.demo:
            return
        entity_id = data["entity_id"]
        self.pending[entity_id] = (service, data)
        worker = self.workers.get(entity_id)
        if not worker or worker.done():
            self.workers[entity_id] = asyncio.create_task(
                self.flush_services(entity_id)
            )

    async def flush_services(self, entity_id: str) -> None:
        while entity_id in self.pending:
            service, data = self.pending.pop(entity_id)
            try:
                await asyncio.to_thread(self.ha.service, service, data)
            except requests.RequestException as error:
                print(f"Home Assistant service failed: {error}")
            await asyncio.sleep(0.04)

    async def toggle(self) -> None:
        self.light.is_on = not self.light.is_on
        self.queue_service(
            "turn_on" if self.light.is_on else "turn_off",
            {"entity_id": self.light.entity_id},
        )
        await self.draw()

    async def adjust(self, delta: int) -> None:
        light = self.light
        control = light.controls[self.control]
        if control == "brightness":
            light.brightness = max(0, min(100, light.brightness + delta * 5))
            light.is_on = light.brightness > 0
            self.queue_service(
                "turn_on",
                {"entity_id": light.entity_id, "brightness_pct": light.brightness},
            )
        elif control == "color":
            index = (nearest_color_index(light.rgb) + delta) % len(COLOR_PRESETS)
            light.rgb = COLOR_PRESETS[index][1]
            light.is_on = True
            self.queue_service(
                "turn_on", {"entity_id": light.entity_id, "rgb_color": list(light.rgb)}
            )
        else:
            light.temperature = max(
                light.min_temperature,
                min(light.max_temperature, light.temperature + delta * 200),
            )
            light.is_on = True
            self.queue_service(
                "turn_on",
                {"entity_id": light.entity_id, "color_temp_kelvin": light.temperature},
            )
        await self.draw()

    async def handle(self, event: str, value: Any) -> None:
        if event == "encoder":
            delta = int(value)
            if self.navigation == "browse":
                self.selected = (self.selected + delta) % len(self.lights)
                self.control = 0
                await self.draw()
            elif self.navigation == "control":
                self.control = (self.control + delta) % len(self.light.controls)
                await self.draw()
            elif self.navigation == "edit":
                await self.adjust(delta)
            return

        button = str(value)
        if button == "start" and self.navigation != "inactive":
            await self.toggle()
        elif button == "ok":
            if self.navigation == "inactive":
                self.navigation = "browse"
            elif self.navigation == "browse":
                self.navigation = "control"
                self.control = 0
            elif self.navigation == "control":
                self.navigation = "edit"
            else:
                self.navigation = "control"
            await self.draw()
        elif button == "back":
            self.navigation = "inactive"
            await self.client.display_clear(application_name=APP)

    async def run(self, once: bool) -> None:
        await self.load()
        await self.draw()
        mode = "demo" if self.demo else "Home Assistant"
        print(f"{APP} → {mode} on {self.host}")
        if once:
            await self.client.aclose()
            return
        try:
            async for message in self.client.stream_status_ws():
                if not isinstance(message, dict):
                    continue
                for event, value in parse_events(message):
                    await self.handle(event, value)
        finally:
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
            if not once:
                try:
                    await self.client.display_clear(application_name=APP)
                except exceptions.BusyBarError:
                    pass
            await self.client.aclose()


def parse_events(message: dict[str, Any]) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for update in message.get("updates", []):
        input_update = update.get("input", {})
        if "button_event" in input_update:
            button = input_update.get("button_event") or {}
            if str(button.get("action", "")).lower() in ("", "press"):
                events.append(("button", str(button.get("button") or "ok").lower()))
        encoder = input_update.get("encoder_event")
        if encoder and int(encoder.get("delta", 0)):
            events.append(("encoder", int(encoder["delta"])))
    return events


async def main() -> None:
    args = parse_args()
    app = App(args)
    try:
        await app.run(args.once)
    except exceptions.BusyBarError as error:
        raise SystemExit(f"BUSY Bar error: {error}") from error
    except requests.RequestException as error:
        raise SystemExit(f"Home Assistant error: {error}") from error


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
