"""The MusicBrainz catalog behind Shijhon's catalog interface.

MusicBrainz's model - artist, release group, release, medium, track, recording - is read
like this:

- an **artist** is an artist; its ID is the MusicBrainz ID without the dashes (Shijhon's
  IDs are letters, digits and dots);
- an **album** is a *release*, shown once for each *release group*: the group's
  representative release (``choose``). Its ID is that release's, so an album that was
  shown or written stays the same release whatever is added to its group later;
- a **song** is a *track*: a recording at its place on one release, with that release as
  its album. A recording found by a search, by an ISRC or as a top song is shown on the
  release ``choose.home`` picks among those it is on.

Three services are asked, each at its own pace of one request a second, one request at a
time (``pace``): MusicBrainz's web service (search, lookups, browse requests), the Cover Art
Archive (covers, by release group; the images themselves come from the Internet Archive,
one at a time) and ListenBrainz (an artist's top recordings). Every failure is a
``CatalogError`` with a short reason, never an address or a token. Caching and
coalescing are Shijhon's; the adapter only remembers what saves a request it knows the
answer of: artists' names from the answers it has read (an artist page does not start with
a request for the name it just showed), and release groups the Cover Art Archive has no
cover for.
The adapter's declaration for Shijhon (``shijhon.catalog.plugin``) is at the end.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, SecretStr
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import (
    CatalogArtist,
    CatalogRef,
    CatalogRelease,
    CatalogTrack,
    ReleaseKind,
)
from shijhon.catalog.plugin import Adapter, Context, Problem, Words, denied

from shijhon_catalog_musicbrainz.choose import (
    Candidate,
    format_rank,
    home,
    representative,
    status_rank,
)
from shijhon_catalog_musicbrainz.pace import Busy, Pace

log = logging.getLogger(__name__)

MUSICBRAINZ = "https://musicbrainz.org"
LISTENBRAINZ = "https://api.listenbrainz.org"
COVERS = "https://coverartarchive.org"
# The Cover Art Archive answers with a redirect to the Internet Archive, which holds the
# images (archive.org and its storage hosts); a redirect to anywhere else is not followed.
_IMAGE_HOST = "archive.org"
COVER_SIZES = (250, 500, 1200)  # the thumbnails the Cover Art Archive has
_COVER_HOPS = 4
# What Shijhon serves and keeps (it reads the type from the bytes; this is the answer's claim).
_RASTER = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_REDIRECTS = (301, 302, 303, 307, 308)
_MOVES = 2  # redirects of a MusicBrainz lookup that are followed (an item merged into another)
_WHOLE_PAGES = 3  # pages of the releases of the groups an album search shows

PAGE = 100  # MusicBrainz's largest page
# An artist's official releases are read whole up to this many pages; a longer discography
# is read from its official digital releases alone (``_long_discography``).
BROWSE_PAGES = 5
DIGITAL_PAGES = 10
GROUP_PAGES = 3
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_ARTWORK_BYTES = 10 * 1024 * 1024
ONE_A_SECOND = 1.0
DEFAULT_PAUSE = 5.0  # after a 503 or 429 that names no time
RETRY_WITHIN = 5.0  # a pause up to this long is waited out and the request sent once more
_NAMES = 5000  # artists' names remembered from answers
# A release group the Cover Art Archive has no front cover for is not asked about again
# for this long (many have none, and a missing cover is asked for at every view of it);
# nor is a size of a cover whose image the archive does not have.
NO_COVER_SECONDS = 24 * 3600.0
_NO_COVERS = 5000

# (No genres: in MusicBrainz they come from users' tags, which are not part of its CC0 core
# data.)
RELEASE_INC = "recordings+artist-credits+isrcs+release-groups+labels"
BROWSE_INC = "release-groups+media+artist-credits"
RELEASE_TYPES = "album|ep|single"
OFFICIAL_DIGITAL = (
    'status:official AND format:"digital media" AND primarytype:(album OR ep OR single)'
)

VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
# MusicBrainz's placeholders for "many" and "nobody known": they have no discography of
# their own to show.
_NO_DISCOGRAPHY = frozenset({VARIOUS_ARTISTS, "125ec42a-7229-4250-afc5-e057484327fe"})

_HEX = re.compile(r"[0-9a-f]{32}")
_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DATE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")
_ISRC = re.compile(r"[A-Z0-9]{12}")
_LUCENE = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')
_COVER = re.compile(re.escape(COVERS) + r"/release-group/(" + _MBID.pattern + r")/front-(\d{1,5})")
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")
_SERVER_PATH = re.compile(r"(/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*/?")
# What a redirected lookup may be redirected to: another item of the same kind.
_LOOKUP = re.compile(r"/ws/2/(artist|release|recording|release-group)/" + _MBID.pattern + "$")
# The services that are asked one request a second, whatever anybody sets.
_ONE_A_SECOND_HOSTS = ("musicbrainz.org", "api.listenbrainz.org", "coverartarchive.org")
_JSON = {"accept": "application/json"}

Credit = tuple[str, tuple[str, ...], tuple[CatalogRef, ...]]


# --- IDs and lenient reading ----------------------------------------------------------------


def mbid(item_id: str) -> str:
    """The MusicBrainz ID behind one of the adapter's IDs; "not found" for anything else."""
    if not _HEX.fullmatch(item_id):
        raise CatalogError("not_found", "not a catalog ID")
    i = item_id
    return f"{i[:8]}-{i[8:12]}-{i[12:16]}-{i[16:20]}-{i[20:]}"


def item_id(value: Any) -> str | None:
    """A MusicBrainz ID as the adapter hands it out (without its dashes); None for
    anything that is not one."""
    if isinstance(value, str) and _MBID.fullmatch(value.lower()):
        return value.lower().replace("-", "")
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value) if value == value and abs(value) < 2**53 else None


def _date(value: Any) -> str | None:
    return value if isinstance(value, str) and _DATE.fullmatch(value) else None


def _isrc(values: Any, wanted: str | None = None) -> str | None:
    """One ISRC of a recording: the one asked for when it has it, else its first in
    alphabetical order (a recording can have several; the choice is always the same)."""
    found = sorted(
        code
        for v in (values if isinstance(values, list) else [])
        if isinstance(v, str) and _ISRC.fullmatch(code := v.strip().upper())
    )
    if wanted is not None and wanted in found:
        return wanted
    return found[0] if found else None


def _kind(group: dict[str, Any]) -> ReleaseKind:
    """Shijhon's four kinds from MusicBrainz's types. A secondary type other than
    "Compilation" (live, soundtrack, remix, ...) has no kind of its own there."""
    primary = (_text(group.get("primary-type")) or "").lower()
    if primary == "single":
        return ReleaseKind.SINGLE
    if primary == "ep":
        return ReleaseKind.EP
    secondary = {s.lower() for s in group.get("secondary-types") or [] if isinstance(s, str)}
    return ReleaseKind.COMPILATION if "compilation" in secondary else ReleaseKind.ALBUM


def _group(release: dict[str, Any]) -> str | None:
    """The ID of a release's release group, as MusicBrainz writes it."""
    found = _text(_dict(release.get("release-group")).get("id"))
    return found.lower() if found is not None and item_id(found) is not None else None


def candidate(release: dict[str, Any]) -> Candidate | None:
    """What ``choose`` looks at, of a release as any answer gives it."""
    found = _text(release.get("id"))
    if found is None or item_id(found) is None:
        return None
    media = _dicts(release.get("media"))
    tracks = _int(release.get("track-count"))
    if tracks is None:
        tracks = sum(_int(m.get("track-count")) or 0 for m in media)
    group = _dict(release.get("release-group"))
    return Candidate(
        id=found.lower(),
        status=_text(release.get("status")),
        formats=tuple(_text(m.get("format")) for m in media),
        date=_date(release.get("date")),
        tracks=tracks,
        comment=_text(release.get("disambiguation")) or "",
        primary=_text(group.get("primary-type")),
        secondary=tuple(s for s in group.get("secondary-types") or [] if isinstance(s, str)),
    )


def lucene(term: str) -> str:
    """A search term as plain words for MusicBrainz's search: lower case (so that "AND",
    "OR" and "NOT" are words) with the query syntax's characters escaped."""
    return _LUCENE.sub(r"\\\1", " ".join(term.lower().split()))


def retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds a ``Retry-After`` header asks to wait (a number or a date); None without
    one that can be read."""
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)  # as long as it asks: never shortened


async def fetch(
    http: httpx.AsyncClient,
    url: str,
    *,
    what: str,
    cap: int,
    params: Mapping[str, str | int] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, httpx.Headers, bytes]:
    """(status, headers, body) of a GET, redirects not followed; the body is read only for
    200 and at most ``cap`` bytes. Every failure is a ``CatalogError`` with a short
    reason, never an address."""
    try:
        async with http.stream(
            "GET", url, params=params, headers=headers, follow_redirects=False
        ) as response:
            body = bytearray()
            if response.status_code == 200:
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > cap:
                        raise CatalogError("invalid", f"{what}: answer too large")
            return response.status_code, response.headers, bytes(body)
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError) as exc:
        reason = "not allowed by the network policy" if denied(exc) else type(exc).__name__
        raise CatalogError("unavailable", f"{what}: {reason}") from None


# --- one pace for each service, shared by every catalog in the process --------------------

_paces: dict[str, Pace] = {}
_registry = threading.Lock()


def shared_pace(host: str, interval: float) -> Pace:
    """The one pace toward ``host`` in this process: every catalog built for it takes its
    turns there, so two of them (a catalog rebuilt after its settings changed, say) never
    add up to more than the one allowance."""
    if host in _ONE_A_SECOND_HOSTS:
        interval = max(interval, ONE_A_SECOND)
    with _registry:
        pace = _paces.get(host)
        if pace is None:
            pace = _paces[host] = Pace(interval)
        pace.interval = interval
        return pace


def server_address(server: str) -> str | None:
    """``server`` as the adapter asks it: its host as the HTTP client sends it (lower case,
    an international name in its ASCII form, no trailing dot), no default port, a plain path
    or none, no trailing slash. None when it is not such an http(s) address. What comes
    back reads the same when given again, so the host that is checked (``official``) is
    the host that is asked."""
    try:
        url = httpx.URL(server.strip())
        port = url.port
        host = url.raw_host.decode("ascii").lower().rstrip(".")
        path = url.raw_path.decode("ascii")
    except Exception:
        return None
    literal = ":" in host  # an IPv6 address
    if url.scheme not in ("http", "https") or not (literal or _HOSTNAME.fullmatch(host)):
        return None
    if url.userinfo or url.query or url.fragment or port == 0:
        return None
    # Nothing in the path that an address could be read differently by: no encoding, no
    # query, no steps up.
    if not _SERVER_PATH.fullmatch(path) or ".." in path:
        return None
    place = (f"[{host}]" if literal else host) + (f":{port}" if port is not None else "")
    return f"{url.scheme}://{place}{path.rstrip('/')}"


def official(server: str) -> bool:
    """Whether ``server`` is MusicBrainz's own service (musicbrainz.org or a host under
    it), where one request a second is the rule whatever the settings say."""
    address = server_address(server)
    if address is None:
        return False
    host = _host(address)
    return host == "musicbrainz.org" or host.endswith(".musicbrainz.org")


def _host(address: str) -> str:
    """The host of an address ``server_address`` made, as it is sent."""
    return httpx.URL(address).raw_host.decode("ascii").lower().rstrip(".")


def _official_digital(release: dict[str, Any]) -> bool:
    """An official release on digital media alone: ``choose``'s first class. (MusicBrainz's
    search by format also finds a release with one digital medium among others.)"""
    found = candidate(release)
    return found is not None and status_rank(found.status) == 0 and format_rank(found.formats) == 0


class MusicBrainzCatalog:
    key = "musicbrainz"
    region = ""  # one catalog for the whole world

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        server: str = MUSICBRAINZ,
        mirror_requests_per_second: float = 1.0,
        covers: bool = True,
        top_songs: bool = True,
        listenbrainz_token: str | None = None,
        browse_pages: int = BROWSE_PAGES,
        page: int = PAGE,
    ) -> None:
        address = server_address(server)
        if address is None or server_address(address) != address:
            raise ValueError("the MusicBrainz server must be a plain http(s) address")
        agent = http.headers.get("user-agent", "")
        if not agent or agent.lower().startswith("python-httpx"):
            # MusicBrainz asks every client to name itself and a contact; Shijhon's client
            # (``Context.http``) does.
            raise ValueError("the HTTP client names no application in its User-Agent")
        self.http = http
        self.server = address
        self.covers = covers
        self.top = top_songs
        self._token = listenbrainz_token or None
        # MusicBrainz's own service: one request a second, whatever the settings say. Only
        # a mirror (another host) may be asked faster.
        interval = (
            ONE_A_SECOND if official(address) else 1.0 / max(0.01, mirror_requests_per_second)
        )
        # One pace for MusicBrainz's own service under all its names and ports, and one for
        # a mirror's host: always the process's shared one (there is no other way in).
        host = "musicbrainz.org" if official(address) else _host(address)
        self._paces = {
            "musicbrainz": shared_pace(host, interval),
            "listenbrainz": shared_pace("api.listenbrainz.org", ONE_A_SECOND),
            "covers": shared_pace("coverartarchive.org", ONE_A_SECOND),
            # The images' own host: one at a time, right after the redirect that led there.
            "images": shared_pace(_IMAGE_HOST, 0.0),
        }
        self.browse_pages = max(1, browse_pages)
        self.page = max(1, min(page, PAGE))
        self._names: OrderedDict[str, str] = OrderedDict()
        # Release groups without a cover, and sizes without an image ("<group>/<size>").
        self._no_cover: OrderedDict[str, float] = OrderedDict()
        # Requests sent, for tests ("images": the archive's hops of a cover).
        self.requests = {"musicbrainz": 0, "listenbrainz": 0, "covers": 0, "images": 0}

    async def aclose(self) -> None:
        await self.http.aclose()

    async def check(self) -> None:
        """One small request past any cache (the dashboard's "Check now")."""
        await self._mb(f"artist/{VARIOUS_ARTISTS}", what="check")

    def ref(self, item: str) -> CatalogRef:
        return CatalogRef(self.key, item)

    # --- requests ---------------------------------------------------------------------------

    async def _fetch(
        self,
        service: str,
        url: str,
        *,
        what: str,
        cap: int,
        params: Mapping[str, str | int] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, bytes]:
        """One request to ``service``, in its turn and with nothing else under way there. A
        503 or 429 pauses the service for everyone, for as long as it asks
        (``Retry-After``); after a short pause the request is sent once more, after a long
        one it fails as rate limited."""
        pace = self._paces[service]
        for attempt in (0, 1):
            try:
                async with pace.request():
                    self.requests[service] += 1
                    status, answer, body = await fetch(
                        self.http, url, what=what, cap=cap, params=params, headers=headers
                    )
                    asked = retry_after(answer)
                    wait = DEFAULT_PAUSE if asked is None else asked
                    if status in (429, 503):
                        pace.hold(wait)  # before the next request is let go
                    elif status in _REDIRECTS and asked:
                        pace.hold(asked)  # a redirect may say when to follow it
            except Busy as busy:
                if busy.elsewhere:
                    raise CatalogError(
                        "unavailable", f"{what}: the catalog is in use from another thread"
                    ) from None
                why = "the service asked to wait" if busy.paused else "too many requests waiting"
                raise CatalogError("rate_limited", f"{what}: {why}") from None
            if status not in (429, 503):
                return status, answer, body
            if attempt == 0 and wait <= RETRY_WITHIN:
                continue  # (it takes a new turn, after the pause)
            kind = "rate_limited" if status == 429 or asked is not None else "unavailable"
            raise CatalogError(kind, f"{what}: HTTP {status}")
        raise AssertionError("unreachable")

    @staticmethod
    def _document(status: int, body: bytes, *, what: str, by_id: bool) -> Any:
        """The JSON document of an answer. ``by_id``: the request named one item by its ID,
        so an ID the service refuses (MusicBrainz answers 400 for some it has never issued)
        is an item the catalog does not have."""
        if status == 200:
            try:
                return json.loads(body)
            except ValueError:
                raise CatalogError("invalid", f"{what}: answer is not JSON") from None
        if status == 404 or (status == 400 and by_id):
            raise CatalogError("not_found", "not in the catalog")
        if status in (401, 403):
            raise CatalogError("unauthorized", f"{what}: HTTP {status}")
        if status == 400:
            raise CatalogError("invalid", f"{what}: the request was refused")
        raise CatalogError("unavailable", f"{what}: HTTP {status}")

    def _moved(self, url: str, location: str | None) -> str | None:
        """Where a redirect of a MusicBrainz lookup leads, when that is another item's
        lookup at the same server (an item merged into another answers with a redirect to
        that one); None for anywhere else."""
        try:
            base = httpx.URL(self.server)
            target = httpx.URL(url).join((location or "").strip())
            path = target.raw_path.decode("ascii").split("?", 1)[0]
            prefix = base.raw_path.decode("ascii").rstrip("/")
        except Exception:
            return None
        same = (target.scheme, target.raw_host, target.port) == (
            base.scheme,
            base.raw_host,
            base.port,
        )
        new = _LOOKUP.match(path[len(prefix) :]) if path.startswith(prefix) else None
        old = _LOOKUP.search(url)
        kind = new is not None and old is not None and new.group(1) == old.group(1)
        if not location or not same or not kind or target.userinfo:
            return None
        return f"{self.server}{path[len(prefix) :]}"

    async def _mb(
        self,
        path: str,
        params: Mapping[str, str | int] | None = None,
        *,
        what: str,
        by_id: bool = True,
    ) -> dict[str, Any]:
        """A document of MusicBrainz's web service (``/ws/2/<path>``). A redirect within the
        web service is followed, each step a request in its own turn."""
        url = f"{self.server}/ws/2/{path}"
        query = {**(params or {}), "fmt": "json"}
        for _ in range(_MOVES + 1):
            status, headers, body = await self._fetch(
                "musicbrainz", url, what=what, cap=MAX_JSON_BYTES, params=query, headers=_JSON
            )
            if status not in _REDIRECTS:
                break
            moved = self._moved(url, headers.get("location"))
            if moved is None:
                raise CatalogError("unavailable", f"{what}: redirected elsewhere")
            url = moved  # asked with the same parameters
        data = self._document(status, body, what=what, by_id=by_id)
        if not isinstance(data, dict):
            raise CatalogError("invalid", f"{what}: unexpected answer")
        return data

    async def _found(self, entity: str, query: str, limit: int, offset: int = 0) -> dict[str, Any]:
        params: dict[str, str | int] = {"query": query, "limit": max(1, min(limit, PAGE))}
        if offset:
            params["offset"] = offset
        return await self._mb(entity, params, what="search", by_id=False)

    # --- the interface ----------------------------------------------------------------------

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        """Three requests: artists, albums (official digital releases, one for each release
        group: its representative among those that match) and songs (recordings, each on
        its release). When more releases matched than one page holds, up to three more: the
        official digital releases of the groups shown, page by page, so that each is shown
        with its representative release and not with whichever of its releases the page
        happened to hold (among up to ``_WHOLE_PAGES`` pages of them)."""
        words = lucene(term)
        if not words:
            return SearchResults()
        limit = max(1, min(limit, PAGE))
        artists = await self._found("artist", words, limit)
        releases = await self._found("release", f"({words}) AND {OFFICIAL_DIGITAL}", self.page)
        groups = self._by_group(r for r in _dicts(releases.get("releases")) if _official_digital(r))
        shown = list(groups)[:limit]
        if shown and (_int(releases.get("count")) or 0) > self.page:
            whole: list[dict[str, Any]] = []
            for number in range(_WHOLE_PAGES):
                found = await self._found(
                    "release",
                    f"rgid:({' OR '.join(shown)}) AND {OFFICIAL_DIGITAL}",
                    self.page,
                    number * self.page,
                )
                whole += _dicts(found.get("releases"))
                if (number + 1) * self.page >= (_int(found.get("count")) or 0):
                    break
            # (Added to what the first page held: nothing read is left out of the choice.)
            read = {str(r.get("id")) for rows in groups.values() for r in rows}
            more = (r for r in whole if _official_digital(r) and str(r.get("id")) not in read)
            for group, rows in self._by_group(more).items():
                if group in shown:
                    groups[group] += rows
        recordings = await self._found("recording", words, limit)
        return SearchResults(
            artists=tuple(
                artist
                for row in _dicts(artists.get("artists"))
                if (artist := self._artist(row)) is not None
            )[:limit],
            albums=self._albums({group: groups[group] for group in shown}),
            songs=self._songs(_dicts(recordings.get("recordings")))[:limit],
        )

    async def album(self, album_id: str) -> CatalogRelease:
        """One request: the release with its media, tracks, recordings and ISRCs."""
        wanted = mbid(album_id)
        data = await self._mb(f"release/{wanted}", {"inc": RELEASE_INC}, what="album")
        # (A release merged into another answers with that one: still the album asked for.)
        return self._release(data, self.ref(album_id))

    async def song(self, song_id: str) -> CatalogTrack:
        """One request: the release the track is on, and the track among its tracks."""
        wanted = mbid(song_id)
        data = await self._mb(
            "release", {"track": wanted, "inc": RELEASE_INC, "limit": 1}, what="song"
        )
        for row in _dicts(data.get("releases")):
            for track in self._release(row).tracks:
                if track.ref.id == song_id:
                    return track
        raise CatalogError("not_found", "not in the catalog")

    async def songs_by_isrc(self, isrc: str) -> tuple[CatalogTrack, ...]:
        """One request: the recordings with this ISRC, each on its release."""
        code = isrc.strip().upper()
        if not _ISRC.fullmatch(code):
            return ()
        data = await self._found("recording", f"isrc:{code}", PAGE)
        rows = [r for r in _dicts(data.get("recordings")) if _isrc(r.get("isrcs"), code) == code]
        return self._songs(rows, isrc=code)

    async def artist(self, artist_id: str) -> CatalogArtist:
        """No request for an artist an answer named before; else one."""
        wanted = mbid(artist_id)
        name = self._names.get(artist_id)
        if name is None:
            data = await self._mb(f"artist/{wanted}", what="artist")
            name = _text(data.get("name"))
            if name is None:
                raise CatalogError("invalid", "artist without a name")
            self._remember(artist_id, name)
        return CatalogArtist(self.ref(artist_id), name)

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        """The artist's official albums, EPs and singles, one for each release group, the
        newest first. One request for each hundred official releases, up to
        ``browse_pages``; a longer discography from its official digital releases."""
        wanted = mbid(artist_id)
        if wanted in _NO_DISCOGRAPHY:
            return ()
        first = await self._official_releases(wanted, 0)
        total = _int(first.get("release-count")) or 0
        rows = _dicts(first.get("releases"))
        if total > self.browse_pages * self.page:
            return await self._long_discography(wanted)
        for number in range(1, self.browse_pages):
            if number * self.page >= total:
                break
            following = await self._official_releases(wanted, number * self.page)
            rows += _dicts(following.get("releases"))
        return self._discography(rows, {})

    async def top_songs(self, artist_id: str, limit: int = 10) -> tuple[CatalogTrack, ...]:
        """Two requests: ListenBrainz's most listened recordings of the artist, then those
        recordings from MusicBrainz, each on its release. None without a ListenBrainz
        token (ListenBrainz answers this only to its users), or with top songs off."""
        wanted = mbid(artist_id)
        limit = max(0, min(limit, 25))
        if not self.top or self._token is None or limit == 0 or wanted in _NO_DISCOGRAPHY:
            return ()
        # (No redirect is followed: the token goes to ListenBrainz's own address alone.)
        status, _, body = await self._fetch(
            "listenbrainz",
            f"{LISTENBRAINZ}/1/popularity/top-recordings-for-artist/{wanted}",
            what="top songs",
            cap=MAX_JSON_BYTES,
            headers={**_JSON, "authorization": f"Token {self._token}"},
        )
        if status in (204, 404):  # no ranking for this artist
            return ()
        rows = self._document(status, body, what="top songs", by_id=False)
        ranked: list[str] = []
        for row in _dicts(rows):
            recording = _text(row.get("recording_mbid"))
            if recording and item_id(recording) and recording.lower() not in ranked:
                ranked.append(recording.lower())
        ranked = ranked[: limit + 5]  # a few more: not every recording has a release to show
        if not ranked:
            return ()
        data = await self._found("recording", f"rid:({' OR '.join(ranked)})", PAGE)
        found = {str(r.get("id")).lower(): r for r in _dicts(data.get("recordings"))}
        songs = self._songs([found[r] for r in ranked if r in found])
        return songs[:limit]

    async def artists_of(
        self, songs: tuple[str, ...], albums: tuple[str, ...]
    ) -> dict[str, tuple[CatalogRef, ...]]:
        """Nothing: every answer of MusicBrainz names its artists' items already."""
        return {}

    async def artwork(self, url: str) -> tuple[bytes, str]:
        """A release group's front cover from the Cover Art Archive, at its thumbnail size
        next above the one asked for (250, 500 or 1200 pixels)."""
        match = _COVER.fullmatch(url)
        if match is None:
            raise CatalogError("invalid", "not a catalog artwork URL")
        if not self.covers:
            raise CatalogError("not_found", "artwork: covers are off")
        group, wanted = match.group(1), int(match.group(2))
        size = next((s for s in COVER_SIZES if s >= wanted), COVER_SIZES[-1])
        now = time.monotonic()
        if max(self._no_cover.get(group, 0.0), self._no_cover.get(f"{group}/{size}", 0.0)) > now:
            raise CatalogError("not_found", "artwork: no cover")
        address = f"{COVERS}/release-group/{group}/front-{size}"
        status, headers, content = await self._fetch(
            "covers", address, what="artwork", cap=MAX_ARTWORK_BYTES
        )
        if status in (404, 410):  # the release group has no front cover
            self._missing(group)
            raise CatalogError("not_found", f"artwork: HTTP {status}")
        for _ in range(_COVER_HOPS):
            if status not in _REDIRECTS:
                break
            if asked := retry_after(headers):  # when to follow it: the image waits too
                self._paces["images"].hold(asked)
            address = self._image_address(address, headers.get("location"))
            status, headers, content = await self._fetch(
                "images", address, what="artwork", cap=MAX_ARTWORK_BYTES
            )
        if status in (404, 410):  # the archive has no image of this size
            self._missing(f"{group}/{size}")
            raise CatalogError("not_found", f"artwork: HTTP {status}")
        if status != 200:
            raise CatalogError("unavailable", f"artwork: HTTP {status}")
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in _RASTER:
            raise CatalogError("invalid", "artwork: not a JPEG, PNG, GIF or WebP image")
        return content, content_type

    @staticmethod
    def _image_address(address: str, location: str | None) -> str:
        """Where a cover's redirect leads: followed only to the Internet Archive, over
        TLS - never back to the Cover Art Archive (that would be a request out of turn)
        and never anywhere else."""
        try:
            target = httpx.URL(address).join((location or "").strip())
        except Exception:
            target = None
        if not location or target is None or target.scheme != "https" or target.userinfo:
            raise CatalogError("unavailable", "artwork: redirected elsewhere")
        host = target.host.rstrip(".")
        if host != _IMAGE_HOST and not host.endswith("." + _IMAGE_HOST):
            raise CatalogError("unavailable", "artwork: redirected elsewhere")
        return str(target.copy_with(fragment=None))

    def _missing(self, what: str) -> None:
        self._no_cover[what] = time.monotonic() + NO_COVER_SECONDS
        self._no_cover.move_to_end(what)
        while len(self._no_cover) > _NO_COVERS:
            self._no_cover.popitem(last=False)

    # --- an artist's releases -----------------------------------------------------------------

    async def _official_releases(self, artist: str, offset: int) -> dict[str, Any]:
        params: dict[str, str | int] = {
            "artist": artist,
            "status": "official",
            "type": RELEASE_TYPES,
            "inc": BROWSE_INC,
            "limit": self.page,
        }
        if offset:
            params["offset"] = offset
        return await self._mb("release", params, what="artist releases")

    async def _long_discography(self, artist: str) -> tuple[CatalogRelease, ...]:
        """A discography too long to read release by release: its official digital releases
        (the search knows them by format) and the release groups' first dates. Digital
        releases come first in ``choose``, so a group whose official digital releases were
        all read is shown with the same release as if every release had been read. A group
        without one is left out; with more digital releases than ``DIGITAL_PAGES`` hold,
        a group is chosen from those that were read (a thousand)."""
        rows: list[dict[str, Any]] = []
        for number in range(DIGITAL_PAGES):
            page = await self._found(
                "release", f"arid:{artist} AND {OFFICIAL_DIGITAL}", PAGE, number * PAGE
            )
            rows += _dicts(page.get("releases"))
            if (number + 1) * PAGE >= (_int(page.get("count")) or 0):
                break
        dates: dict[str, str] = {}
        for number in range(GROUP_PAGES):
            params: dict[str, str | int] = {
                "artist": artist,
                "type": RELEASE_TYPES,
                "release-group-status": "website-default",
                "limit": PAGE,
            }
            if number:
                params["offset"] = number * PAGE
            groups = await self._mb("release-group", params, what="artist releases")
            for group in _dicts(groups.get("release-groups")):
                date, found = _date(group.get("first-release-date")), _text(group.get("id"))
                if date and found:
                    dates[found.lower()] = date
            if (number + 1) * PAGE >= (_int(groups.get("release-group-count")) or 0):
                break
        return self._discography((r for r in rows if _official_digital(r)), dates)

    @staticmethod
    def _by_group(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Releases by their release group, the groups in the order they first appear."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            group = _group(row)
            if group is not None and candidate(row) is not None:
                groups.setdefault(group, []).append(row)
        return groups

    def _albums(
        self, groups: Mapping[str, list[dict[str, Any]]], dates: Mapping[str, str] | None = None
    ) -> tuple[CatalogRelease, ...]:
        """One album for each group: its representative among the releases given."""
        cards: list[CatalogRelease] = []
        for group, rows in groups.items():
            try:
                members = [(c, row) for row in rows if (c := candidate(row)) is not None]
                best = representative(c for c, _ in members)
                row = next(row for c, row in members if c is best)
                card = self._card(row, date=(dates or {}).get(group))
            except Exception as exc:
                _skipped("album", exc)
                continue
            if card is not None:
                cards.append(card)
        return tuple(cards)

    def _discography(
        self, rows: Iterable[dict[str, Any]], dates: Mapping[str, str]
    ) -> tuple[CatalogRelease, ...]:
        albums = self._albums(self._by_group(rows), dates)
        cards = sorted(albums, key=lambda c: (c.title.lower(), c.ref.id))
        return tuple(sorted(cards, key=lambda c: c.release_date or "", reverse=True))

    # --- reading (lenient: a malformed item is skipped, never the whole answer) ---------------

    def _remember(self, artist: str, name: str) -> None:
        self._names[artist] = name
        self._names.move_to_end(artist)
        while len(self._names) > _NAMES:
            self._names.popitem(last=False)

    def _artist(self, row: dict[str, Any]) -> CatalogArtist | None:
        found, name = item_id(row.get("id")), _text(row.get("name"))
        if found is None or name is None:
            return None
        self._remember(found, name)
        return CatalogArtist(self.ref(found), name)

    def _credit(self, credit: Any) -> Credit:
        """An artist credit: as displayed ("A feat. B"), its names as credited, and the
        artists' items."""
        display, names, refs = "", [], []
        for part in _dicts(credit):
            artist = _dict(part.get("artist"))
            name = _text(part.get("name")) or _text(artist.get("name"))
            if name is None:
                continue
            join = part.get("joinphrase")
            display += name + (join if isinstance(join, str) else "")
            names.append(name)
            found = item_id(artist.get("id"))
            if found is not None:
                refs.append(self.ref(found))
                self._remember(found, _text(artist.get("name")) or name)
        return display.strip(), tuple(names), tuple(refs)

    def _cover(self, group: dict[str, Any]) -> str | None:
        """The release group's front cover (the same for every release of it, in every
        view), with the size left open."""
        found = _text(group.get("id"))
        if not self.covers or found is None or item_id(found) is None:
            return None
        return f"{COVERS}/release-group/{found.lower()}/front-{{w}}"

    def _card(
        self, release: dict[str, Any], *, date: str | None = None, credit: Credit | None = None
    ) -> CatalogRelease | None:
        """A release without its tracks. Its date is the release group's first release -
        the album's year, whatever edition stands for it - where the answer has it (or
        ``date`` gives it), else the release's own."""
        found, title = item_id(release.get("id")), _text(release.get("title"))
        if found is None or title is None:
            return None
        group = _dict(release.get("release-group"))
        display, names, refs = self._credit(release.get("artist-credit"))
        if not names and credit is not None:
            display, names, refs = credit
        seen = candidate(release)
        labels = [
            _text(_dict(i.get("label")).get("name")) for i in _dicts(release.get("label-info"))
        ]
        barcode = _text(release.get("barcode"))
        return CatalogRelease(
            ref=self.ref(found),
            title=title,
            artist=display,
            kind=_kind(group),
            release_date=_date(group.get("first-release-date"))
            or date
            or _date(release.get("date")),
            label=next((name for name in labels if name), None),
            upc=barcode if barcode and barcode.isdigit() else None,
            track_count=(seen.tracks if seen is not None else 0) or None,
            artists=names,
            artist_refs=refs,
            artwork_template=self._cover(group),
        )

    def _release(self, data: dict[str, Any], ref: CatalogRef | None = None) -> CatalogRelease:
        """A release with its tracks, as a lookup gives it: each medium's tracks, a hidden
        track before the first one too (its "pregap", number 0). A video is left out, as
        are a disc's data tracks; a track without a length cannot be shown, and makes the
        release incomplete."""
        card = self._card(data)
        if card is None:
            raise CatalogError("invalid", "album without a title")
        card = replace(card, ref=ref or card.ref)
        tracks: list[CatalogTrack] = []
        skipped = False
        for medium in _dicts(data.get("media")):
            disc = _int(medium.get("position")) or 1
            rows = _dicts(medium.get("tracks"))
            if isinstance(medium.get("pregap"), dict):
                rows = [medium["pregap"], *rows]
            for row in rows:
                if _dict(row.get("recording")).get("video") is True:
                    continue
                try:
                    tracks.append(self._track(row, disc, card))
                except Exception as exc:
                    _skipped("track", exc)
                    skipped = True
        return replace(card, tracks=tuple(tracks), track_count=len(tracks), incomplete=skipped)

    def _track(self, row: dict[str, Any], disc: int, album: CatalogRelease) -> CatalogTrack:
        recording = _dict(row.get("recording"))
        found = item_id(row.get("id"))
        title = _text(row.get("title")) or _text(recording.get("title"))
        # The length this release states for the track, else the recording's.
        length = _int(row.get("length")) or _int(recording.get("length"))
        if found is None or title is None or length is None or length <= 0:
            raise ValueError("a track without an ID, a title or a length")
        # The credit as this release prints it, else the recording's, else the album's.
        display, names, refs = self._credit(row.get("artist-credit"))
        if not names:
            display, names, refs = self._credit(recording.get("artist-credit"))
        if not names:
            display, names, refs = album.artist, album.artists, album.artist_refs
        return CatalogTrack(
            ref=self.ref(found),
            title=title,
            artist=display,
            duration_ms=length,
            disc=disc,
            number=max(0, _int(row.get("position")) or 0),
            isrc=_isrc(recording.get("isrcs")),
            album=album.ref,
            artists=names,
            artist_refs=refs,
            album_title=album.title,
            release_date=album.release_date,
            artwork_template=album.artwork_template,
        )

    def _songs(
        self, recordings: list[dict[str, Any]], *, isrc: str | None = None
    ) -> tuple[CatalogTrack, ...]:
        songs: list[CatalogTrack] = []
        for recording in recordings:
            try:
                song = self._song(recording, isrc)
            except Exception as exc:
                _skipped("song", exc)
                continue
            if song is not None:
                songs.append(song)
        return tuple(songs)

    def _song(self, recording: dict[str, Any], isrc: str | None) -> CatalogTrack | None:
        """A recording a search found, as the track it is on its ``choose.home`` release
        (the answer lists its releases, and its track on each - with the track's title,
        length and place, but not its credit: the recording's is used)."""
        if recording.get("video") is True:
            return None
        placed: list[tuple[Candidate, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for release in _dicts(recording.get("releases")):
            found = candidate(release)
            for medium in _dicts(release.get("media")):
                rows = _dicts(medium.get("track"))
                if found is not None and rows and item_id(rows[0].get("id")) is not None:
                    placed.append((found, release, medium, rows[0]))
                    break
        if not placed:
            return None
        best = home(c for c, _, _, _ in placed)
        release, medium, row = next((r, m, t) for c, r, m, t in placed if c is best)
        credit = self._credit(recording.get("artist-credit"))
        album = self._card(release, credit=credit)
        track = item_id(row.get("id"))
        title = _text(row.get("title")) or _text(recording.get("title"))
        length = _int(row.get("length")) or _int(recording.get("length"))
        if album is None or track is None or title is None or length is None or length <= 0:
            return None
        offset = _int(medium.get("track-offset"))
        display, names, refs = credit
        if not names:
            display, names, refs = album.artist, album.artists, album.artist_refs
        return CatalogTrack(
            ref=self.ref(track),
            title=title,
            artist=display,
            duration_ms=length,
            disc=_int(medium.get("position")) or 1,
            number=offset + 1 if offset is not None and offset >= 0 else 0,
            isrc=_isrc(recording.get("isrcs"), isrc),
            album=album.ref,
            artists=names,
            artist_refs=refs,
            album_title=album.title,
            release_date=album.release_date,
            artwork_template=album.artwork_template,
        )


def _skipped(what: str, exc: Exception) -> None:
    log.warning("catalog: skipped a malformed %s (%s)", what, type(exc).__name__)


# --- the adapter's declaration (shijhon.catalog.plugin) -----------------------------------


class Settings(BaseModel):
    """The adapter's own ``[catalog]`` settings. All have defaults: nothing is needed to
    use MusicBrainz itself."""

    # MusicBrainz's address; another one is a mirror of it.
    server: str = MUSICBRAINZ
    # Requests a second to a mirror. MusicBrainz's own service is asked one a second
    # whatever this says.
    mirror_requests_per_second: float = Field(default=1.0, gt=0, le=100)
    covers: bool = True  # covers from the Cover Art Archive
    top_songs: bool = True  # an artist's top songs from ListenBrainz (needs the token)
    # A ListenBrainz user token: ListenBrainz answers its popularity lists only with one.
    listenbrainz_token: SecretStr | None = None


def build(settings: Settings, context: Context) -> MusicBrainzCatalog:
    if server_address(settings.server) is None:
        raise ValueError("the MusicBrainz server must be a plain http(s) address")
    if official(settings.server) and settings.mirror_requests_per_second != 1.0:
        log.warning(
            "catalog: the mirror's request rate is not used for MusicBrainz's own service"
            " (one request a second)"
        )
    token = settings.listenbrainz_token
    # Shijhon's client: public addresses only, and its User-Agent with the project's address.
    return MusicBrainzCatalog(
        context.http(),
        server=settings.server,
        mirror_requests_per_second=settings.mirror_requests_per_second,
        covers=settings.covers,
        top_songs=settings.top_songs,
        listenbrainz_token=token.get_secret_value().strip() if token is not None else None,
    )


def problem(settings: Settings) -> Problem | None:
    if server_address(settings.server) is None:
        return Problem(
            "server",
            "Enter an address such as https://musicbrainz.org.",
            "must be an http(s) address, such as https://musicbrainz.org.",
        )
    return None


adapter = Adapter(
    label="MusicBrainz",
    build=build,
    settings=Settings,
    words={
        "server": Words(
            "Server",
            "MusicBrainz's address. Change it only to use a mirror of your own.",
            max_length=200,
        ),
        "covers": Words("Covers", "Album covers from the Cover Art Archive.", "albums"),
        "top_songs": Words(
            "Top songs",
            "An artist's most listened songs, from ListenBrainz. Needs the token below.",
            "albums",
        ),
        "listenbrainz_token": Words(
            "ListenBrainz token",
            "Your ListenBrainz user token, for top songs. Write-only: stored on the server"
            " and never shown. Leave empty to keep the current one.",
            "albums",
        ),
        "mirror_requests_per_second": Words(
            "Mirror request rate",
            "Requests to a mirror at most. MusicBrainz's own service is always asked one a"
            " second, whatever this says.",
            "advanced",
            unit="per s",
            spoken="requests per second",
        ),
    },
    problem=problem,
)
