"""CalDAV discovery and queries against a fake server shaped like iCloud's."""

from datetime import UTC, datetime

import httpx
import pytest

from busybar_calendar.caldav import CalDAVAuthError, CalDAVClient, CalDAVError, RemoteCalendar

EVENT = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Sync &amp; plan\r\n"
    "DTSTART:20261013T090000Z\r\nDTEND:20261013T093000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
)


def multistatus(*responses: str) -> str:
    body = "".join(f"<d:response>{r}</d:response>" for r in responses)
    return (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
        f'xmlns:c="urn:ietf:params:xml:ns:caldav">{body}</d:multistatus>'
    )


def ok(href: str, props: str, missing: str = "") -> str:
    out = f"<d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
    out += "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    if missing:
        out += f"<d:propstat><d:prop>{missing}</d:prop>"
        out += "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
    return out


class FakeServer:
    """caldav.example redirects /.well-known; the account lives on p01-caldav.example."""

    def __init__(self, status: int = 207) -> None:
        self.status = status
        self.reports: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if self.status != 207:
            return httpx.Response(self.status)
        if url == "https://caldav.example/":
            return httpx.Response(405)
        if url == "https://caldav.example/.well-known/caldav":
            # A 302, as PrivateEmail and many others send: an HTTP client that
            # turns it into a GET would get this server's 405 below.
            return httpx.Response(302, headers={"Location": "/dav/"})
        if url == "https://caldav.example/dav/" and request.method != "PROPFIND":
            return httpx.Response(405)
        if url == "https://caldav.example/dav/":
            return httpx.Response(
                207,
                text=multistatus(
                    ok(
                        "/dav/",
                        "<d:current-user-principal><d:href>https://p01-caldav.example/123/principal/"
                        "</d:href></d:current-user-principal>",
                    )
                ),
            )
        if url == "https://p01-caldav.example/123/principal/":
            return httpx.Response(
                207,
                text=multistatus(
                    ok(
                        "/123/principal/",
                        "<c:calendar-home-set><d:href>/123/calendars/</d:href></c:calendar-home-set>",
                    )
                ),
            )
        if url == "https://p01-caldav.example/123/calendars/" and request.method == "PROPFIND":
            return httpx.Response(
                207,
                text=multistatus(
                    ok("/123/calendars/", "<d:resourcetype><d:collection/></d:resourcetype>"),
                    ok(
                        "/123/calendars/work/",
                        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                        "<d:displayname>Work</d:displayname><c:supported-calendar-component-set>"
                        '<c:comp name="VEVENT"/></c:supported-calendar-component-set>',
                    ),
                    ok(
                        "/123/calendars/tasks/",
                        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                        "<d:displayname>Reminders</d:displayname><c:supported-calendar-component-set>"
                        '<c:comp name="VTODO"/></c:supported-calendar-component-set>',
                    ),
                    ok(
                        "/123/calendars/home/",
                        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>",
                        missing="<d:displayname/>",
                    ),
                ),
            )
        if request.method == "REPORT" and url == "https://p01-caldav.example/123/calendars/work/":
            assert request.headers["Depth"] == "1"
            self.reports.append(request.content.decode())
            return httpx.Response(
                207,
                text=multistatus(
                    ok("/123/calendars/work/x.ics", f"<c:calendar-data>{EVENT}</c:calendar-data>"),
                    ok("/123/calendars/work/empty.ics", "<c:calendar-data/>"),
                ),
            )
        if url == "https://caldav.example/direct/" and request.method == "PROPFIND":
            return httpx.Response(
                207,
                text=multistatus(
                    ok(
                        "/direct/",
                        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                        "<d:displayname>Direct</d:displayname>",
                    )
                ),
            )
        return httpx.Response(404)


def client(server: FakeServer, url: str) -> CalDAVClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)
    return CalDAVClient(http, url)


async def test_discovers_through_well_known_and_another_host():
    calendars = await client(FakeServer(), "https://caldav.example/").discover()
    assert calendars == [
        RemoteCalendar("https://p01-caldav.example/123/calendars/work/", "Work"),
        RemoteCalendar(
            "https://p01-caldav.example/123/calendars/home/",
            "https://p01-caldav.example/123/calendars/home/",
        ),
    ]


async def test_direct_calendar_url():
    calendars = await client(FakeServer(), "https://caldav.example/direct/").discover()
    assert calendars == [RemoteCalendar("https://caldav.example/direct/", "Direct")]


async def test_report_with_time_range():
    server = FakeServer()
    work = RemoteCalendar("https://p01-caldav.example/123/calendars/work/", "Work")
    documents = await client(server, "https://caldav.example/").events(
        work, datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, 12, tzinfo=UTC)
    )
    assert len(documents) == 1
    assert "SUMMARY:Sync & plan" in documents[0]
    assert 'start="20261012T000000Z" end="20261013T120000Z"' in server.reports[0]


async def test_auth_failure_is_reported_not_retried_elsewhere():
    with pytest.raises(CalDAVAuthError):
        await client(FakeServer(status=401), "https://caldav.example/").discover()


async def test_nothing_to_discover():
    with pytest.raises(CalDAVError, match="current-user-principal"):
        await client(FakeServer(), "https://nowhere.example/").discover()


async def test_errors_carry_their_status():
    with pytest.raises(CalDAVAuthError) as caught:
        await client(FakeServer(status=401), "https://caldav.example/").discover()
    assert caught.value.status == 401
