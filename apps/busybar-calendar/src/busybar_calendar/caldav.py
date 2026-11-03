"""
A small, read-only CalDAV client: find the calendars, read events in a range.

Only the parts of RFC 4791 a reminder needs: discovery through
current-user-principal and calendar-home-set (RFC 5397, RFC 6764's
/.well-known/caldav as a fallback), and calendar-query REPORTs with a time
range. Recurrence is expanded by the caller, not the server.

Hrefs are joined against the URL that answered, which is what makes iCloud
work: its principal and calendars live on a per-account host, returned as
absolute URLs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree.ElementTree import Element

import httpx
from defusedxml.ElementTree import fromstring

logger = logging.getLogger(__name__)

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
APPLE = "http://apple.com/ns/ical/"
NS = {"d": DAV, "c": CALDAV, "a": APPLE}

_PROPFIND_ENTRY = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:current-user-principal/><d:resourcetype/><d:displayname/></d:prop>
</d:propfind>"""

_PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>"""

_PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>
  </d:prop>
</d:propfind>"""

_REPORT_RANGE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{start}" end="{end}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""


MAX_REDIRECTS = 5


class CalDAVError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CalDAVAuthError(CalDAVError):
    pass


@dataclass(frozen=True)
class RemoteCalendar:
    url: str
    name: str

    @property
    def id(self) -> str:
        """The calendar's own path segment: 4711 in .../calendars/me@example.com/4711/."""
        return unquote(urlsplit(self.url).path.rstrip("/").rsplit("/", 1)[-1])


@dataclass(frozen=True)
class _Response:
    href: str
    props: Element


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class CalDAVClient:
    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url

    async def _send(self, method: str, url: str, body: str, depth: str) -> httpx.Response:
        """
        Send a DAV request, following redirects with the method and body intact.

        httpx, like browsers, turns a 302 into a GET; a PROPFIND that becomes
        a GET is refused. /.well-known/caldav is usually a 302 (RFC 6764).
        """
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._http.request(
                method, url, content=body.encode(), headers=headers, follow_redirects=False
            )
            location = response.headers.get("Location")
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                return response
            url = urljoin(str(response.url), location)
        raise CalDAVError(f"{method} {url}: more than {MAX_REDIRECTS} redirects")

    async def _dav(self, method: str, url: str, body: str, depth: str) -> list[_Response]:
        response = await self._send(method, url, body, depth)
        code = response.status_code
        if code in (401, 403):
            raise CalDAVAuthError(f"{method} {url}: {code}, check the credentials", code)
        if code != 207:
            raise CalDAVError(f"{method} {url}: expected 207, got {code}", code)
        base = str(response.url)
        root = fromstring(response.content)
        results = []
        for item in root.findall("d:response", NS):
            href = item.findtext("d:href", default="", namespaces=NS).strip()
            for propstat in item.findall("d:propstat", NS):
                status = propstat.findtext("d:status", default="", namespaces=NS)
                prop = propstat.find("d:prop", NS)
                if " 200 " in f"{status} " and prop is not None:
                    results.append(_Response(urljoin(base, href), prop))
        return results

    async def _href(self, url: str, body: str, path: str) -> str | None:
        for response in await self._dav("PROPFIND", url, body, "0"):
            href = response.props.findtext(path, default="", namespaces=NS).strip()
            if href:
                return urljoin(response.href, href)
        return None

    async def _principal(self, url: str) -> tuple[str | None, list[_Response]]:
        try:
            entry = await self._dav("PROPFIND", url, _PROPFIND_ENTRY, "0")
        except CalDAVAuthError:
            raise
        except CalDAVError as error:
            logger.debug("%s", error)
            return None, []
        for response in entry:
            href = response.props.findtext(
                "d:current-user-principal/d:href", default="", namespaces=NS
            ).strip()
            if href:
                return urljoin(response.href, href), entry
        return None, entry

    async def discover(self) -> list[RemoteCalendar]:
        """The event calendars behind the configured URL."""
        principal, entry = await self._principal(self._url)
        # A calendar URL given directly is used as it is.
        for response in entry:
            if response.props.find("d:resourcetype/c:calendar", NS) is not None:
                name = response.props.findtext("d:displayname", default="", namespaces=NS)
                return [RemoteCalendar(response.href, name.strip() or response.href)]

        if principal is None:
            parts = urlsplit(self._url)
            well_known = f"{parts.scheme}://{parts.netloc}/.well-known/caldav"
            if self._url.rstrip("/") != well_known:
                principal, _ = await self._principal(well_known)
        if principal is None:
            raise CalDAVError(f"{self._url}: no current-user-principal; give a calendar URL")

        home = await self._href(principal, _PROPFIND_HOME, "c:calendar-home-set/d:href")
        if home is None:
            raise CalDAVError(f"{principal}: no calendar-home-set")

        calendars = []
        for response in await self._dav("PROPFIND", home, _PROPFIND_CALENDARS, "1"):
            if response.props.find("d:resourcetype/c:calendar", NS) is None:
                continue
            components = response.props.findall("c:supported-calendar-component-set/c:comp", NS)
            if components and not any(c.get("name") == "VEVENT" for c in components):
                continue  # a task list or a journal
            name = response.props.findtext("d:displayname", default="", namespaces=NS).strip()
            calendars.append(RemoteCalendar(response.href, name or response.href))
        return calendars

    async def events(self, calendar: RemoteCalendar, start: datetime, end: datetime) -> list[str]:
        """The iCalendar data of every event resource overlapping the range."""
        body = _REPORT_RANGE.format(start=_stamp(start), end=_stamp(end))
        documents = []
        for response in await self._dav("REPORT", calendar.url, body, "1"):
            data = response.props.findtext("c:calendar-data", default="", namespaces=NS)
            if data.strip():
                documents.append(data)
        return documents
