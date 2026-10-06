"""Replay of the sanitized fixtures through an httpx mock transport.

The real adapter runs against it: MusicBrainz's lookups, browse requests and searches are
answered from ``tests/fixtures`` (lists cut to the ``limit`` and ``offset`` asked for), the
Cover Art Archive redirects to an image host as it really does, ListenBrainz answers only
with a token, and every request is counted with its time - so tests can say what the
adapter asked for, how often and how fast.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import httpx

from shijhon_catalog_musicbrainz import MusicBrainzCatalog
from shijhon_catalog_musicbrainz import catalog as adapter
from shijhon_catalog_musicbrainz.pace import Pace

FIXTURES = Path(__file__).resolve().parent / "fixtures"
IMAGE = b"\xff\xd8\xff\xe0" + b"\x00" * 64  # a JPEG by its first bytes: all that is checked
AGENT = "Shijhon-tests/0 (+https://example.invalid/shijhon)"
TOKEN = "listenbrainz-token-SECRET"
MISSING = "0" * 32  # an ID nothing is recorded under
_LISTS = ("artists", "releases", "recordings", "release-groups")
_NOT_FOUND = {"error": "Not Found", "help": "For usage, please see: the web service's guide"}


class Clock:
    """Time that passes only while somebody sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await anyio.lowlevel.checkpoint()


def fixture(name: str) -> dict[str, Any]:
    record: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return record


def short(mbid: str) -> str:
    """A MusicBrainz ID as the adapter hands it out."""
    return mbid.replace("-", "")


class Replay:
    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """``clock``, ``sleep``: real ones for a test in real time; else the replay's own
        time (``self.time``), which starts at 1000.0 and passes only in sleeps."""
        self.time = Clock()
        self.paces: dict[str, Pace] = {}  # this replay's, by host (``install``)
        records = [json.loads(p.read_text()) for p in sorted(FIXTURES.glob("*.json"))]
        self.lookups: dict[str, dict[str, Any]] = {}  # path -> record
        self.searches: dict[tuple[str, str], dict[str, Any]] = {}  # (path, query) -> record
        self.browses: dict[tuple[str, str], dict[str, Any]] = {}  # (path, artist) -> record
        self.tracks: dict[str, dict[str, Any]] = {}  # track ID -> its release's document
        self.rankings: dict[str, dict[str, Any]] = {}  # ListenBrainz path -> record
        for record in records:
            url = httpx.URL(record["url"])
            params = record["params"]
            if url.host == "api.listenbrainz.org":
                if record["status"] == 200:
                    self.rankings[url.path] = record
            elif url.host != "musicbrainz.org" or record["status"] != 200:
                continue
            elif "query" in params:
                self.searches[url.path, params["query"]] = record
            elif "artist" in params:
                self.browses[url.path, params["artist"]] = record
            elif "track" in params:
                for release in record["body"]["releases"]:
                    self._index_tracks(release)
            else:
                self.lookups[url.path] = record
                if url.path.startswith("/ws/2/release/"):
                    self._index_tracks(record["body"])
        self.clock = clock or self.time
        self.sleep = sleep or self.time.sleep
        # Seconds a request is on its way before the service sees it (a connection to
        # open): its time is noted when it arrives.
        self.on_the_way = 0.0
        self.answer_takes = 0.0  # seconds the service takes to answer
        self.log: list[str] = []  # what was asked, in order: "<service> <path>[ <what>]"
        self.times: dict[str, list[float]] = {
            "musicbrainz": [],
            "listenbrainz": [],
            "covers": [],
            "images": [],  # the archive's hops of a cover
        }
        self.agents: set[str] = set()  # every User-Agent seen
        self.authorized: dict[str, int] = {}  # host -> requests that carried an Authorization
        self.token = TOKEN
        # Answers to give first, per service: (status, headers) - a busy or failing service
        # (None: that request is answered as recorded).
        self.failing: dict[str, list[tuple[int, dict[str, str]] | None]] = {}
        self.no_cover: set[str] = set()  # release groups without a front cover
        self.cover_redirect: str | None = None  # where the archive redirects to instead
        self.image, self.image_type = IMAGE, "image/jpeg"  # what the archive answers with
        self.invalid: set[str] = set()  # IDs MusicBrainz calls invalid (400)
        # Items merged into others: a lookup of the old ID is redirected to the new one.
        self.merged: dict[str, str] = {}
        self.hosts = {"musicbrainz.org"}  # where MusicBrainz's web service answers

    def _index_tracks(self, release: dict[str, Any]) -> None:
        for medium in release.get("media", []):
            for track in medium.get("tracks", []):
                self.tracks[track["id"]] = release

    def count(self, service: str) -> int:
        return len(self.times[service])

    def asked(self, service: str) -> list[str]:
        return [line.split(" ", 1)[1] for line in self.log if line.startswith(service + " ")]

    def _note(self, service: str, request: httpx.Request, what: str = "") -> httpx.Response | None:
        self.times[service].append(self.clock())
        self.log.append(f"{service} {request.url.path}" + (f" {what}" if what else ""))
        failures = self.failing.get(service)
        failure = failures.pop(0) if failures else None
        if failure is None:
            return None
        status, headers = failure
        return httpx.Response(status, headers=headers, json={"error": "busy"})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.on_the_way:
            await self.sleep(self.on_the_way)
        answer = self._answer(request)
        if self.answer_takes:
            await self.sleep(self.answer_takes)
        return answer

    def _answer(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.agents.add(request.headers.get("user-agent", ""))
        if "authorization" in request.headers:
            self.authorized[url.host] = self.authorized.get(url.host, 0) + 1
        if url.host in self.hosts:
            return self._musicbrainz(request)
        if url.host == "api.listenbrainz.org":
            return self._listenbrainz(request)
        if url.host == "coverartarchive.org":
            return self._cover(request)
        if url.host == "archive.org" or url.host.endswith(".us.archive.org"):
            failed = self._note("images", request)
            if failed is not None:
                return failed
            if url.host != "archive.org":
                return httpx.Response(
                    200, content=self.image, headers={"content-type": self.image_type}
                )
            target = f"https://ia800000.us.archive.org{url.path}"
            if self.cover_redirect is not None:
                target = self.cover_redirect
            return httpx.Response(302, headers={"location": target} if target else {})
        self.log.append(f"elsewhere {url.host}")
        return httpx.Response(500)

    def _musicbrainz(self, request: httpx.Request) -> httpx.Response:
        url, params = request.url, request.url.params
        what = params.get("query") or params.get("artist") or params.get("track") or ""
        if params.get("offset"):
            what += f" offset={params['offset']}"
        failed = self._note("musicbrainz", request, what)
        if failed is not None:
            return failed
        if params.get("fmt") != "json":
            return httpx.Response(406)
        wanted = params.get("artist") or params.get("track") or url.path.rsplit("/", 1)[-1]
        if wanted in self.invalid:
            return httpx.Response(400, json={"error": "Invalid mbid."})
        if "query" in params:
            record = self.searches.get((url.path, params["query"]))
            kind = url.path.rsplit("/", 1)[-1] + "s"
            body = record["body"] if record else {"count": 0, "offset": 0, kind: []}
            return httpx.Response(200, json=_page(body, params))
        if "artist" in params:
            record = self.browses.get((url.path, params["artist"]))
            if record is None:
                return httpx.Response(404, json=_NOT_FOUND)
            return httpx.Response(200, json=_page(record["body"], params))
        if "track" in params:
            release = self.tracks.get(params["track"])
            if release is None:
                return httpx.Response(404, json=_NOT_FOUND)
            body = {"release-count": 1, "release-offset": 0, "releases": [release]}
            return httpx.Response(200, json=body)
        if wanted in self.merged:  # as MusicBrainz answers a merged item's ID
            moved = url.copy_with(path=url.path.replace(wanted, self.merged[wanted]))
            return httpx.Response(301, headers={"location": str(moved)})
        record = self.lookups.get(url.path)
        if record is None:
            return httpx.Response(404, json=_NOT_FOUND)
        return httpx.Response(200, json=record["body"])

    def _listenbrainz(self, request: httpx.Request) -> httpx.Response:
        failed = self._note("listenbrainz", request)
        if failed is not None:
            return failed
        if request.headers.get("authorization") != f"Token {self.token}":
            return httpx.Response(401, json=fixture("top-songs-no-token")["body"])
        record = self.rankings.get(request.url.path)
        return httpx.Response(200, json=record["body"] if record else [])

    def _cover(self, request: httpx.Request) -> httpx.Response:
        failed = self._note("covers", request)
        if failed is not None:
            return failed
        match = re.fullmatch(
            r"/release-group/([0-9a-f-]{36})/front-(250|500|1200)", request.url.path
        )
        if match is None or match.group(1) in self.no_cover:
            return httpx.Response(404, text="No cover art found")
        # As the archive answers (fixture "cover-redirect"): on to the image's own address.
        location = (
            f"https://archive.org/download/mbid-{match.group(1)}"
            f"/mbid-{match.group(1)}-10000000000_thumb{match.group(2)}.jpg"
        )
        return httpx.Response(307, headers={"location": location})

    def client(self, agent: str | None = AGENT) -> httpx.AsyncClient:
        headers = {"user-agent": agent} if agent else None
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), headers=headers)

    def pace(self, interval: float = 1.0) -> Pace:
        """A pace in the replay's own time: its waits take no real time."""
        return Pace(interval, clock=self.time, sleep=self.time.sleep)

    def install(self, server: str = adapter.MUSICBRAINZ) -> None:
        """The process's shared paces (``adapter.shared_pace``) as ones in the replay's own
        time, for the services a catalog of ``server`` asks: the adapter keeps its real
        pace - one request a second - and a test does not wait for it. (A catalog takes
        no pace of its own: the shared ones are the only ones there are.)"""
        address = adapter.server_address(server)
        hosts = ["api.listenbrainz.org", "coverartarchive.org", "archive.org"]
        if address is not None:  # (no address: the catalog will refuse it itself)
            official = adapter.official(address)
            hosts.append("musicbrainz.org" if official else adapter._host(address))
        for host in hosts:
            adapter._paces[host] = self.paces.setdefault(host, self.pace())

    def catalog(self, **kwargs: Any) -> MusicBrainzCatalog:
        """The adapter over the replay, at its real pace (one request a second; the images
        one at a time) in the replay's own time."""
        self.install(kwargs.get("server", adapter.MUSICBRAINZ))
        kwargs.setdefault("listenbrainz_token", TOKEN)
        return MusicBrainzCatalog(self.client(), **kwargs)


def _page(body: dict[str, Any], params: httpx.QueryParams) -> dict[str, Any]:
    """The answer's list from ``offset``, at most ``limit`` (MusicBrainz's own paging)."""
    offset = int(params.get("offset") or 0)
    limit = max(1, min(int(params.get("limit") or 25), 100))
    page = dict(body)
    for key in _LISTS:
        if key in page:
            page[key] = page[key][offset : offset + limit]
    for key in ("offset", "release-offset", "release-group-offset"):
        if key in page:
            page[key] = offset
    return page
