"""
Configured sources, each turned into a list of events for a time window.

A CalDAV source is one account with several calendars; an ICS source is a
single published feed and counts as one calendar named after the source.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, tzinfo
from fnmatch import fnmatchcase

import httpx

from . import events
from .caldav import CalDAVAuthError, CalDAVClient, CalDAVError, RemoteCalendar
from .config import SourceConfig

logger = logging.getLogger(__name__)

USER_AGENT = "busybar-calendar"
# An ICS feed is the whole calendar, history included; refuse the absurd.
MAX_FEED_BYTES = 32 * 1024 * 1024
# Calendars are listed again after this many refreshes, to pick up new ones.
REDISCOVER_EVERY = 12


# Answers that mean the source is configured wrong - credentials, URL - so
# asking again will not help; anything else (timeouts, 5xx) is retried.
FATAL_STATUS = frozenset({401, 403, 404, 405, 410})


class FatalSourceError(Exception):
    """A source that cannot work as configured. The service stops on one."""


def matches(calendar: RemoteCalendar, pattern: str) -> bool:
    """
    Whether a filter entry names this calendar.

    An entry is a calendar's display name, its id (the last segment of its
    URL) or its URL, and may use the wildcards * and ?: "Office - Room *".
    """
    values = (calendar.name, calendar.id, calendar.url, calendar.url.rstrip("/"))
    return any(fnmatchcase(value, pattern) for value in values)


def select(
    calendars: Sequence[RemoteCalendar], include: Sequence[str], exclude: Sequence[str]
) -> tuple[list[RemoteCalendar], list[str]]:
    """
    The calendars to use, in overlap order, and the entries that matched nothing.

    With `include`, calendars come in the order their entries are listed, so
    the one listed last wins when two events start together; without it, all
    of them in the server's order. `exclude` then removes calendars.
    """
    if include:
        chosen: list[RemoteCalendar] = []
        for pattern in include:
            chosen += [c for c in calendars if c not in chosen and matches(c, pattern)]
    else:
        chosen = list(calendars)
    chosen = [c for c in chosen if not any(matches(c, pattern) for pattern in exclude)]
    unmatched = [
        pattern
        for pattern in (*include, *exclude)
        if not any(matches(calendar, pattern) for calendar in calendars)
    ]
    return chosen, unmatched


def http_client(source: SourceConfig) -> httpx.AsyncClient:
    auth: httpx.Auth | None = None
    password = source.password.resolve()
    if source.username is not None and password is not None:
        auth = (
            httpx.DigestAuth(source.username, password)
            if source.auth == "digest"
            else httpx.BasicAuth(source.username, password)
        )
    return httpx.AsyncClient(
        auth=auth,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": USER_AGENT},
    )


class Source:
    def __init__(self, config: SourceConfig, index: int, floating_tz: tzinfo, skip_free: bool):
        self.config = config
        self.name = config.name
        self._index = index
        self._floating_tz = floating_tz
        self._skip_free = skip_free

    def _ref(self, calendar: str, position: int, calendar_id: str = "") -> events.CalendarRef:
        return events.CalendarRef(self.name, calendar, self._index * 1000 + position, calendar_id)

    def _parse(self, data, ref, start, end) -> list[events.Event]:
        return events.parse(
            data,
            ref,
            start,
            end,
            floating_tz=self._floating_tz,
            skip_free=self._skip_free,
            emails=self.config.emails,
        )

    async def fetch(self, start: datetime, end: datetime) -> list[events.Event]:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class ICSSource(Source):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http = http_client(self.config)
        self._etag: str | None = None
        self._body: bytes | None = None

    async def fetch(self, start: datetime, end: datetime) -> list[events.Event]:
        headers = {"If-None-Match": self._etag} if self._etag and self._body else {}
        response = await self._http.get(self.config.url, headers=headers)
        if response.status_code in FATAL_STATUS:
            raise FatalSourceError(
                f"{self.name}: {self.config.url} answered {response.status_code}"
            )
        if response.status_code != 304:
            response.raise_for_status()
            if len(response.content) > MAX_FEED_BYTES:
                raise ValueError(f"{self.name}: feed is over {MAX_FEED_BYTES} bytes")
            self._body = response.content
            self._etag = response.headers.get("ETag")
        assert self._body is not None
        return self._parse(self._body, self._ref(self.name, 0), start, end)

    async def aclose(self) -> None:
        await self._http.aclose()


class CalDAVSource(Source):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http = http_client(self.config)
        self._client = CalDAVClient(self._http, self.config.url)
        self._calendars: list[RemoteCalendar] = []
        self._since_listed = 0
        self._unmatched: list[str] = []

    async def calendars(self) -> list[RemoteCalendar]:
        """Every event calendar on the account, before `include` and `exclude`."""
        return await self._client.discover()

    def _keep_known(self, error: Exception) -> None:
        """Listing the calendars failed for now: go on with the ones known, if any."""
        if not self._calendars:
            raise error
        logger.warning(
            "%s: could not list the calendars (%s); going on with the ones it knows",
            self.name,
            error,
        )

    async def _selected(self) -> list[tuple[int, RemoteCalendar]]:
        if not self._calendars or self._since_listed >= REDISCOVER_EVERY:
            try:
                self._calendars = await self.calendars()
                self._since_listed = 0
            except CalDAVError as error:
                # No status: discovery found no calendar where it was pointed.
                if error.status is None or error.status in FATAL_STATUS:
                    raise FatalSourceError(f"{self.name}: {error}") from error
                self._keep_known(error)
            except httpx.HTTPError as error:
                self._keep_known(error)
        self._since_listed += 1
        selected, unmatched = select(self._calendars, self.config.include, self.config.exclude)
        if unmatched != self._unmatched:
            if unmatched:
                logger.warning("%s: no calendar matches %s", self.name, ", ".join(unmatched))
            self._unmatched = unmatched
        return list(enumerate(selected))

    async def fetch(self, start: datetime, end: datetime) -> list[events.Event]:
        selected = await self._selected()

        async def one(position: int, calendar: RemoteCalendar) -> list[events.Event]:
            try:
                documents = await self._client.events(calendar, start, end)
            except CalDAVAuthError as error:
                raise FatalSourceError(f"{self.name}: {error}") from error
            ref = self._ref(calendar.name, position, calendar.id)
            return self._parse(documents, ref, start, end)

        results = await asyncio.gather(*(one(i, c) for i, c in selected))
        return [event for result in results for event in result]

    async def aclose(self) -> None:
        await self._http.aclose()


def build(config: SourceConfig, index: int, floating_tz: tzinfo, skip_free: bool) -> Source:
    kind = CalDAVSource if config.type == "caldav" else ICSSource
    return kind(config, index, floating_tz, skip_free)
