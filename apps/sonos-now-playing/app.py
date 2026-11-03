#!/usr/bin/env python3
"""Display the currently playing Sonos track on a BUSY Bar.

    +--------------------+---------------------------+
    |  SONG TITLE HERE                                |   scrolling top line
    |  Artist Name                                    |   smaller bottom line
    +--------------------+---------------------------+

Polls the selected Sonos speaker every few seconds. Shows nothing when
paused/stopped. Auto-discovers speakers on the local network if no
--speaker-ip is given; use --speaker-name to select by room name.
If no speaker is specified, the first speaker alphabetically is used.

Requires busylib and soco:
    pip install busylib soco
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass

import soco
import soco.exceptions

from busylib import AsyncBusyBar
from busylib.exceptions import BusyBarError
from busylib.features import notify

LOG = logging.getLogger("sonos-now-playing")

APP_NAME = "sonos-now-playing"
PRIORITY = 50  # PRIORITY_DEFAULT


@dataclass
class TrackInfo:
    title: str
    artist: str
    is_playing: bool

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TrackInfo):
            return False
        return self.title == other.title and self.artist == other.artist and self.is_playing == other.is_playing


def find_speaker(ip: str | None, name: str | None) -> soco.SoCo | None:
    if ip:
        return soco.SoCo(ip)
    speakers = soco.discover(timeout=5)
    if not speakers:
        LOG.warning("no Sonos speakers found on the network")
        return None
    # Sort for stable selection across restarts
    sorted_speakers = sorted(speakers, key=lambda s: s.player_name.lower())
    LOG.info("available speakers: %s", ", ".join(
        f"{s.player_name} ({s.ip_address})" for s in sorted_speakers))
    if name:
        name_lower = name.lower()
        for s in sorted_speakers:
            if s.player_name.lower() == name_lower:
                return s
        LOG.warning("no speaker named %r; use one of the names above", name)
        return None
    # Pick the first coordinator alphabetically (avoids picking a grouped follower)
    coordinators = [s for s in sorted_speakers if s.is_coordinator]
    chosen = coordinators[0] if coordinators else sorted_speakers[0]
    LOG.info("auto-selected %r — use --speaker-name to pick a different one", chosen.player_name)
    return chosen


def get_track(speaker: soco.SoCo) -> TrackInfo | None:
    try:
        info = speaker.get_current_transport_info()
        state = info.get("current_transport_state", "STOPPED")
        is_playing = state == "PLAYING"

        track = speaker.get_current_track_info()
        title = (track.get("title") or "").strip()
        artist = (track.get("artist") or "").strip()

        if not title and not artist:
            return None
        return TrackInfo(title=title or "Unknown", artist=artist, is_playing=is_playing)
    except soco.exceptions.SoCoException as exc:
        LOG.warning("Sonos error: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        LOG.warning("unexpected error polling speaker: %s", exc)
        return None


async def draw_track(bar: AsyncBusyBar, track: TrackInfo) -> None:
    if not track.is_playing:
        with contextlib.suppress(BusyBarError):
            await bar.display_clear(application_name=APP_NAME)
        return

    line1 = track.title[:40]
    line2 = track.artist[:40] if track.artist else None

    try:
        if line2:
            await notify(
                bar,
                line1,
                line_2=line2,
                line_1_font="normal",
                line_2_font="tiny",
                line_1_color=[255, 255, 255],
                line_2_color=[0, 200, 80],
                priority=PRIORITY,
                application_name=APP_NAME,
                duration=0,
            )
        else:
            await notify(
                bar,
                line1,
                line_1_font="normal",
                line_1_color=[255, 255, 255],
                priority=PRIORITY,
                application_name=APP_NAME,
                duration=0,
            )
    except BusyBarError as exc:
        if "409" not in str(exc):
            LOG.warning("draw failed: %s", exc)


async def run(args: argparse.Namespace) -> None:
    speaker = find_speaker(args.speaker_ip, args.speaker_name)
    if speaker is None:
        LOG.error("no speaker available; exiting")
        sys.exit(1)

    LOG.info("monitoring %r (%s)", speaker.player_name, speaker.ip_address)

    async with AsyncBusyBar(args.host) as bar:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        last: TrackInfo | None = None

        while not stop.is_set():
            track = await loop.run_in_executor(None, get_track, speaker)

            if track != last:
                if track is not None:
                    await draw_track(bar, track)
                elif last is not None:
                    with contextlib.suppress(BusyBarError):
                        await bar.display_clear(application_name=APP_NAME)
                last = track

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=args.interval)

        LOG.info("stopping")
        with contextlib.suppress(BusyBarError):
            await bar.display_clear(application_name=APP_NAME)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="10.0.4.20",
                   help="busybar-manager proxy or BUSY Bar address (injected by the manager)")
    p.add_argument("--speaker-ip", metavar="IP",
                   help="Sonos speaker IP address (auto-discovers if omitted)")
    p.add_argument("--speaker-name", metavar="NAME",
                   help="Sonos room/speaker name to select (e.g. 'Living Room'); "
                        "defaults to first speaker alphabetically")
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds between polls (default 5)")
    p.add_argument("--once", action="store_true",
                   help="draw one frame and exit (useful for tests)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.once:
        speaker = find_speaker(args.speaker_ip, args.speaker_name)
        if speaker is None:
            return 1
        track = get_track(speaker)
        if track:
            print(f"{track.title} — {track.artist} ({'playing' if track.is_playing else 'paused'})")
        else:
            print("nothing playing")
        return 0

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
