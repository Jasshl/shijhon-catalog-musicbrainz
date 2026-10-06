"""The adapter over the replayed fixtures: what each view answers, and what it costs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from itertools import pairwise
from typing import Any

import anyio
import httpx
import pytest
from shijhon.catalog.base import CatalogError
from shijhon.catalog.model import ReleaseKind, artwork_url

from shijhon_catalog_musicbrainz import MusicBrainzCatalog
from shijhon_catalog_musicbrainz import catalog as catalog_module
from shijhon_catalog_musicbrainz.catalog import (
    COVERS,
    VARIOUS_ARTISTS,
    candidate,
    item_id,
    lucene,
    mbid,
    official,
    retry_after,
    server_address,
    shared_pace,
)
from shijhon_catalog_musicbrainz.choose import representative
from shijhon_catalog_musicbrainz.pace import Pace
from tests.replay import AGENT, MISSING, Clock, Replay, fixture, short

pytestmark = pytest.mark.anyio

TERM = str(fixture("search-artists")["params"]["query"])
ALBUM = fixture("album")["body"]
ARTIST = short(fixture("artist")["body"]["id"])
LONG = short(fixture("long-releases")["params"]["artist"])


def groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        found.setdefault(row["release-group"]["id"], []).append(row)
    return found


def chosen(rows: list[dict[str, Any]]) -> str:
    """The representative of one group's releases, as the adapter's IDs."""
    candidates = [c for row in rows if (c := candidate(row)) is not None]
    return short(representative(candidates).id)


# --- IDs ------------------------------------------------------------------------------------------


def test_ids_are_musicbrainz_s_without_the_dashes() -> None:
    assert item_id("0A1B2C3D-4e5f-4a6b-8c7d-9e0f1a2b3c4d") == "0a1b2c3d4e5f4a6b8c7d9e0f1a2b3c4d"
    assert mbid("0a1b2c3d4e5f4a6b8c7d9e0f1a2b3c4d") == "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
    assert item_id("not-an-id") is None and item_id(None) is None
    for wrong in (
        "0",
        "",
        "0A1B2C3D4E5F4A6B8C7D9E0F1A2B3C4D",
        "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    ):
        with pytest.raises(CatalogError) as refused:
            mbid(wrong)
        assert refused.value.kind == "not_found"


async def test_an_id_that_cannot_be_one_is_not_found_without_a_request() -> None:
    replay = Replay()
    catalog = replay.catalog()
    for call in (
        catalog.album,
        catalog.song,
        catalog.artist,
        catalog.artist_releases,
        catalog.top_songs,
    ):
        with pytest.raises(CatalogError) as refused:
            await call("../../artist/x")
        assert refused.value.kind == "not_found"
    assert replay.log == []


# --- search ---------------------------------------------------------------------------------------


async def test_a_search_is_three_requests() -> None:
    replay = Replay()
    catalog = replay.catalog()
    results = await catalog.search(TERM.upper(), 25)
    assert replay.asked("musicbrainz") == [
        f"/ws/2/artist {TERM}",
        f'/ws/2/release ({TERM}) AND status:official AND format:"digital media"'
        " AND primarytype:(album OR ep OR single)",
        f"/ws/2/recording {TERM}",
    ]
    assert results.artists[0].name.lower() == TERM and results.artists[0].ref.id == ARTIST
    assert results.artists[0].artwork_template is None  # MusicBrainz has no artist images
    assert len(results.albums) > 5 and len(results.songs) > 10
    assert all(a.ref.catalog == "musicbrainz" and not a.tracks for a in results.albums)
    assert all(a.artist and a.artist_refs and a.track_count for a in results.albums)
    assert {a.kind for a in results.albums} >= {
        ReleaseKind.ALBUM,
        ReleaseKind.SINGLE,
        ReleaseKind.EP,
    }
    for song in results.songs:
        assert song.album is not None and song.album_title and song.artist_refs
        assert song.duration_ms > 0 and song.disc >= 1 and song.number >= 1
        assert not song.explicit and not song.clean  # MusicBrainz has no such flag


async def test_a_search_shows_one_album_for_each_release_group() -> None:
    replay = Replay()
    results = await replay.catalog().search(TERM, 100)
    recorded = groups(fixture("search-albums")["body"]["releases"])
    assert any(len(rows) > 1 for rows in recorded.values())  # groups with several releases
    assert [a.ref.id for a in results.albums] == [chosen(rows) for rows in recorded.values()]


async def test_a_search_that_matches_more_than_a_page_reads_its_groups_whole() -> None:
    """More releases match than the page read holds (here: eight of 23): the groups shown
    are asked for their official digital releases, so each is shown with its
    representative - the same release as on the artist's page - and not with whichever of
    its releases the page held."""
    replay = Replay()
    catalog = replay.catalog(page=8)
    results = await catalog.search(TERM, 25)
    whole = fixture("search-albums-whole")
    assert replay.asked("musicbrainz") == [
        f"/ws/2/artist {TERM}",
        f"/ws/2/release {fixture('search-albums')['params']['query']}",
        f"/ws/2/release {whole['params']['query']}",
        f"/ws/2/release {whole['params']['query']} offset=8",  # ... page by page
        f"/ws/2/recording {TERM}",
    ]
    assert whole["params"]["query"].startswith("rgid:(")
    read = groups(fixture("search-albums")["body"]["releases"][:8])
    complete = groups(whole["body"]["releases"])
    assert list(read) == [a_group for a_group in read if a_group in complete]
    assert any(len(complete[g]) > len(read[g]) for g in read)  # the page had cut groups
    assert [a.ref.id for a in results.albums] == [chosen(complete[g]) for g in read]
    page = {r.ref.id for r in await replay.catalog().artist_releases(ARTIST)}
    mine = [a for a in results.albums if a.artist_refs[0].id == ARTIST]
    assert len(mine) >= 3 and all(a.ref.id in page for a in mine)
    # ... which the cut page alone would not have given.
    cut = [chosen(rows) for rows in read.values()]
    assert cut != [a.ref.id for a in results.albums]


async def test_what_the_first_page_held_stays_in_the_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The groups' own releases are read on for a page only here - eight of their ten,
    fewer than there are: what the first page held is still chosen from. No group is shown
    with a later release than the first page offered."""
    monkeypatch.setattr(catalog_module, "_WHOLE_PAGES", 1)
    replay = Replay()
    results = await replay.catalog(page=8).search(TERM, 25)
    assert len(replay.asked("musicbrainz")) == 4
    first = groups(fixture("search-albums")["body"]["releases"][:8])
    reread = fixture("search-albums-whole")["body"]["releases"][:8]
    assert {r["id"] for rows in first.values() for r in rows} - {r["id"] for r in reread}
    every = {
        short(r["id"]): r
        for name in ("search-albums", "search-albums-whole")
        for r in fixture(name)["body"]["releases"]
    }
    assert len(first) == len(results.albums) == 5
    for album, rows in zip(results.albums, first.values(), strict=True):
        assert every[album.ref.id]["release-group"]["id"] == rows[0]["release-group"]["id"]
        assert every[album.ref.id]["date"] <= min(row["date"] for row in rows)


async def test_an_album_search_shows_releases_on_digital_media_alone() -> None:
    """MusicBrainz's search by format also finds a release with a digital medium among
    others; that is no digital release in the rule's sense."""
    replay = Replay()
    hits = replay.searches["/ws/2/release", fixture("search-albums")["params"]["query"]]["body"]
    whole = await replay.catalog().search(TERM, 100)
    mixed = hits["releases"][0]["release-group"]["id"]
    for release in hits["releases"]:
        if release["release-group"]["id"] == mixed:
            release["media"] = [*release["media"], {"format": "CD", "track-count": 3}]
    results = await replay.catalog().search(TERM, 100)
    assert len(results.albums) == len(whole.albums) - 1
    assert [a.ref for a in results.albums] == [a.ref for a in whole.albums[1:]]


async def test_a_search_keeps_to_the_limit() -> None:
    results = await Replay().catalog().search(TERM, 3)
    assert (len(results.artists), len(results.albums), len(results.songs)) == (2, 3, 3)


async def test_a_song_found_is_on_the_release_its_album_is_shown_with() -> None:
    """The same album ID in a search's songs, in its albums and on the artist's page."""
    replay = Replay()
    catalog = replay.catalog()
    results = await catalog.search(TERM, 25)
    page = {r.ref.id for r in await catalog.artist_releases(ARTIST)}
    albums = {a.ref.id for a in results.albums}
    album = short(ALBUM["id"])
    assert album in page and album in albums
    on_album = [s for s in results.songs if s.album is not None and s.album.id == album]
    assert on_album
    tracks = {t.ref: t for t in (await catalog.album(album)).tracks}
    for song in on_album:  # the same track, with the same title, length and place
        listed = tracks[song.ref]
        assert (song.title, song.duration_ms, song.disc, song.number, song.isrc) == (
            listed.title, listed.duration_ms, listed.disc, listed.number, listed.isrc,
        )  # fmt: skip
        assert (song.artist, song.artist_refs) == (listed.artist, listed.artist_refs)


def test_search_terms_are_plain_words() -> None:
    assert lucene("  Amber/Fable   Quiet Harbor ") == r"amber\/fable quiet harbor"
    assert lucene('status:official OR "x" AND (y) [z] ~1 *?!') == (
        r"status\:official or \"x\" and \(y\) \[z\] \~1 \*\?\!"
    )
    assert lucene("a && b || c \\ d - e + f ^ g {h}") == (
        r"a \&& b \|| c \\ d \- e \+ f \^ g \{h\}"
    )
    assert lucene("   ") == ""


async def test_an_empty_search_asks_nothing() -> None:
    replay = Replay()
    results = await replay.catalog().search("   ")
    assert results.artists == results.albums == results.songs == () and replay.log == []


async def test_a_search_nothing_matches() -> None:
    results = await Replay().catalog().search("nothing like this is recorded")
    assert results.artists == results.albums == results.songs == ()


# --- an album, a song -----------------------------------------------------------------------------


async def test_an_album_is_one_request_with_its_full_track_list() -> None:
    replay = Replay()
    album = await replay.catalog().album(short(ALBUM["id"]))
    assert replay.asked("musicbrainz") == [f"/ws/2/release/{ALBUM['id']}"]
    recorded = ALBUM["media"][0]["tracks"]
    assert album.ref.id == short(ALBUM["id"]) and album.title == ALBUM["title"]
    assert album.kind is ReleaseKind.ALBUM and not album.incomplete
    assert album.track_count == len(album.tracks) == len(recorded) == 10
    # The album's year is its release group's first release.
    assert album.release_date == ALBUM["release-group"]["first-release-date"]
    assert album.label and album.upc == ALBUM["barcode"]
    assert album.genres == ()  # not read: users' tags are no CC0 core data
    assert album.artist_refs[0].id == ARTIST
    assert album.artwork_template == (
        f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-{{w}}"
    )
    for track, row in zip(album.tracks, recorded, strict=True):
        assert track.ref.id == short(row["id"]) and track.album == album.ref
        assert (track.title, track.duration_ms) == (row["title"], row["length"])  # exact
        assert (track.disc, track.number) == (1, row["position"])
        assert track.isrc == row["recording"]["isrcs"][0]
        assert track.artist_refs and track.album_title == album.title
        assert track.artwork_template == album.artwork_template
        assert not track.explicit and not track.clean


async def test_a_song_is_one_request_and_the_same_as_in_its_album() -> None:
    replay = Replay()
    catalog = replay.catalog()
    album = await catalog.album(short(ALBUM["id"]))
    for track in (album.tracks[0], album.tracks[-1]):
        before = replay.count("musicbrainz")
        assert await catalog.song(track.ref.id) == track
        assert replay.count("musicbrainz") == before + 1
    assert replay.asked("musicbrainz")[-1] == f"/ws/2/release {mbid(album.tracks[-1].ref.id)}"


async def test_songs_by_isrc_is_one_request() -> None:
    replay = Replay()
    catalog = replay.catalog()
    album = await catalog.album(short(ALBUM["id"]))
    first = album.tracks[0]
    assert first.isrc is not None
    found = await catalog.songs_by_isrc(first.isrc.lower())
    assert replay.asked("musicbrainz")[-1] == f"/ws/2/recording isrc:{first.isrc}"
    assert [t.ref for t in found] == [first.ref] and found[0].isrc == first.isrc
    assert found[0].album == album.ref  # among all its releases: the album's own
    before = replay.count("musicbrainz")
    assert await catalog.songs_by_isrc("ZZZZZ0000000") == ()
    assert replay.count("musicbrainz") == before + 1
    assert await catalog.songs_by_isrc("not an isrc") == ()  # nothing to ask
    assert replay.count("musicbrainz") == before + 1


async def test_a_merged_release_still_answers_under_its_old_id() -> None:
    """MusicBrainz redirects the ID of an item merged into another: followed, in the
    request's next turn, and the album keeps the ID it was asked for."""
    replay = Replay()
    catalog, _ = clocked(replay)
    old = "0f0e0d0c-0b0a-4908-8706-050403020100"
    replay.merged[old] = ALBUM["id"]
    album = await catalog.album(short(old))
    assert album.ref.id == short(old) and len(album.tracks) == 10
    assert all(track.album == album.ref for track in album.tracks)
    assert replay.asked("musicbrainz") == [f"/ws/2/release/{old}", f"/ws/2/release/{ALBUM['id']}"]
    assert replay.times["musicbrainz"] == [1000.0, 1001.0]  # each step in its own turn


async def test_a_redirect_out_of_the_web_service_is_not_followed() -> None:
    for elsewhere in (
        "https://example.invalid/ws/2/release/x",
        "http://musicbrainz.org/ws/2/release/x",  # another scheme
        "https://musicbrainz.org:8443/ws/2/release/x",
        "https://musicbrainz.org/elsewhere/x",
        "https://user@musicbrainz.org/ws/2/release/x",
        f"https://musicbrainz.org/ws/2/%2e%2e/%2e%2e/doc/{ALBUM['id']}",  # steps up, encoded
        f"https://musicbrainz.org/ws/2/release/../../doc/{ALBUM['id']}",
        f"https://musicbrainz.org/ws/2/release/{ALBUM['id']}/extra",
        "https://musicbrainz.org/ws/2/release?artist=x",  # no lookup of one item
        "https://[bad",
        "",
    ):
        replay = Replay()
        replay.failing["musicbrainz"] = [(301, {"location": elsewhere} if elsewhere else {})]
        with pytest.raises(CatalogError) as moved:
            await replay.catalog().album(short(ALBUM["id"]))
        assert moved.value.kind == "unavailable" and "://" not in str(moved.value)
        assert replay.count("musicbrainz") == 1 and len(replay.log) == 1
    replay = Replay()  # ... and a loop of redirects ends
    replay.failing["musicbrainz"] = [
        (301, {"location": f"https://musicbrainz.org/ws/2/release/{ALBUM['id']}"})
    ] * 5
    with pytest.raises(CatalogError) as looping:
        await replay.catalog().album(short(ALBUM["id"]))
    assert looping.value.kind == "unavailable" and replay.count("musicbrainz") == 3


async def test_a_track_s_own_credit_and_a_hidden_first_track() -> None:
    replay = Replay()
    body = replay.lookups[f"/ws/2/release/{ALBUM['id']}"]["body"]
    medium = body["media"][0]
    guest = {"id": "0a0b0c0d-0e0f-4a0b-8c0d-0e0f0a0b0c0d", "name": "Quiet Guest"}
    medium["tracks"][1]["artist-credit"] = [
        {**medium["tracks"][1]["artist-credit"][0], "joinphrase": " feat. "},
        {"name": "The Guest", "joinphrase": "", "artist": guest},
    ]
    hidden = {
        "id": "1a1b1c1d-1e1f-4a1b-8c1d-1e1f1a1b1c1d",
        "position": 0,
        "number": "0",
        "title": "Hidden Intro",
        "length": 61000,
        "recording": {"id": "2a2b2c2d-2e2f-4a2b-8c2d-2e2f2a2b2c2d", "title": "Hidden Intro"},
    }
    medium["pregap"] = hidden
    medium["data-tracks"] = [{**hidden, "id": "3a3b3c3d-3e3f-4a3b-8c3d-3e3f3a3b3c3d"}]
    catalog = replay.catalog()
    album = await catalog.album(short(ALBUM["id"]))
    assert [t.number for t in album.tracks] == list(range(11)) and not album.incomplete
    first, second, third = album.tracks[:3]
    assert (first.title, first.number, first.duration_ms) == ("Hidden Intro", 0, 61000)
    assert first.artist == album.artist  # no credit of its own: the album's
    # The credit as this release prints it, not the recording's.
    assert third.artist.endswith(" feat. The Guest") and third.artists[1] == "The Guest"
    assert third.artist_refs[1].id == short(guest["id"]) and second.artist == album.artist
    assert (await catalog.artist(short(guest["id"]))).name == "Quiet Guest"
    replay.tracks[hidden["id"]] = body
    assert await catalog.song(short(hidden["id"])) == first


async def test_a_track_without_a_length_and_a_video() -> None:
    replay = Replay()
    body = replay.lookups[f"/ws/2/release/{ALBUM['id']}"]["body"]
    tracks = body["media"][0]["tracks"]
    tracks[1]["length"] = tracks[1]["recording"]["length"] = None
    tracks[2]["recording"]["video"] = True
    tracks[3]["length"] = None  # the recording's length then
    album = await replay.catalog().album(short(ALBUM["id"]))
    assert album.incomplete and len(album.tracks) == album.track_count == 8
    assert [t.number for t in album.tracks] == [1, 4, 5, 6, 7, 8, 9, 10]
    assert album.tracks[1].duration_ms == tracks[3]["recording"]["length"]


# --- an artist ------------------------------------------------------------------------------------


async def test_an_artist_named_by_an_answer_costs_no_request() -> None:
    replay = Replay()
    catalog = replay.catalog()
    artist = await catalog.artist(ARTIST)
    assert artist.name == fixture("artist")["body"]["name"] and artist.artwork_template is None
    assert replay.asked("musicbrainz") == [f"/ws/2/artist/{mbid(ARTIST)}"]
    assert await catalog.artist(ARTIST) == artist  # known now
    assert replay.count("musicbrainz") == 1
    fresh = Replay()
    other = fresh.catalog()
    await other.album(short(ALBUM["id"]))  # its credit names the artist
    assert await other.artist(ARTIST) == artist and fresh.count("musicbrainz") == 1


async def test_an_artist_page_is_one_request_for_a_hundred_official_releases() -> None:
    replay = Replay()
    releases = await replay.catalog().artist_releases(ARTIST)
    assert replay.asked("musicbrainz") == [f"/ws/2/release {mbid(ARTIST)}"]
    recorded = groups(fixture("artist-releases")["body"]["releases"])
    assert fixture("artist-releases")["body"]["release-count"] == 34
    # One album for each release group, shown with its representative release.
    assert len(releases) == len(recorded) == 10
    assert {r.ref.id for r in releases} == {chosen(rows) for rows in recorded.values()}
    by_id = {short(row["id"]): row for rows in recorded.values() for row in rows}
    for release in releases:
        group = by_id[release.ref.id]["release-group"]
        assert release.release_date == group["first-release-date"]  # the album's year
        assert release.title and release.artist_refs and release.track_count and not release.tracks
    assert {r.kind for r in releases} == {ReleaseKind.ALBUM, ReleaseKind.EP, ReleaseKind.SINGLE}
    dates = [r.release_date or "" for r in releases]
    assert dates == sorted(dates, reverse=True)  # the newest first


# The recorded artist's ten release groups and the release each is shown with, worked out by
# hand from the fixture "artist-releases" (not by the code under test): the earliest of the
# releases on digital media alone, the lowest ID among those of one day; the CD, the vinyl
# or the cassette where a group has nothing else.
REPRESENTATIVES = [
    ("830fe01dbe98402fa3f3dbec185aa067", "ep", "2019-10-07", 12),  # its only release, a CD
    ("49c6f331638944d3a481ab630450ce9d", "album", "2019-10-07", 14),  # of 4 digital, 6 others
    ("5cfb11720444466fa1d0ee80e2cfb289", "ep", "2019-09-22", 4),
    ("a1f9fcd5ce9140f4acd93a44da695a94", "single", "2019-08-10", 2),  # of 2 digital
    ("2eca5859f9e843e0a30e5d0de764025a", "single", "2019-07-06", 1),  # of 3 digital
    ("73cc0e26f129463cac0cf47a58b3dabe", "single", "2015", 3),  # vinyl only
    ("823198f7811e49cba2d10d1be1b6a58d", "album", "2014-09-08", 10),  # of 2 digital, 5 others
    ("5d3e69517e0d4b34a59e6009bc26ff40", "single", "2014-09", 2),  # vinyl only
    # The earliest of three digital releases (in the fixture's years: 2011-01-01), not the
    # earlier cassette, nor the CD with a track more; its date is the group's first.
    ("2a79e73ce6d04c84a2796ce62334b7e1", "album", "2010", 9),
    ("828cbefc1ad14335a3afde2b87a96e94", "single", "2010", 2),  # a cassette only
]


async def test_the_recorded_artist_page_release_by_release() -> None:
    releases = await Replay().catalog().artist_releases(ARTIST)
    assert [
        (r.ref.id, r.kind.value, r.release_date, r.track_count) for r in releases
    ] == REPRESENTATIVES
    # The same releases for the same groups in a search, and for the songs of a search.
    results = await Replay().catalog().search(TERM, 100)
    page = {found for found, *_ in REPRESENTATIVES}
    by_artist = [a for a in results.albums if a.artist_refs[0].id == ARTIST]
    assert len(by_artist) == 6 and {a.ref.id for a in by_artist} <= page
    on_page = [s for s in results.songs if s.album is not None and s.album.id in page]
    assert len(on_page) >= 10


async def test_an_artist_page_read_in_pages_is_the_same_page() -> None:
    whole = await Replay().catalog().artist_releases(ARTIST)
    replay = Replay()
    paged = await replay.catalog(page=10).artist_releases(ARTIST)
    assert paged == whole
    assert replay.asked("musicbrainz") == [
        f"/ws/2/release {mbid(ARTIST)}",
        f"/ws/2/release {mbid(ARTIST)} offset=10",
        f"/ws/2/release {mbid(ARTIST)} offset=20",
        f"/ws/2/release {mbid(ARTIST)} offset=30",
    ]


async def test_a_long_discography_is_read_from_its_official_digital_releases() -> None:
    """More official releases than are read one by one (486 recorded, three pages read
    here): the artist's official digital releases, and the groups' first dates."""
    replay = Replay()
    releases = await replay.catalog(browse_pages=3).artist_releases(LONG)
    artist = mbid(LONG)
    digital = f'arid:{artist} AND status:official AND format:"digital media"'
    digital += " AND primarytype:(album OR ep OR single)"
    assert replay.asked("musicbrainz") == [
        f"/ws/2/release {artist}",
        f"/ws/2/release {digital}",
        f"/ws/2/release {digital} offset=100",
        f"/ws/2/release-group {artist}",
    ]
    recorded = groups(fixture("long-digital")["body"]["releases"])
    assert len(fixture("long-digital")["body"]["releases"]) == 105
    assert {r.ref.id for r in releases} == {chosen(rows) for rows in recorded.values()}
    dates = {
        g["id"]: g["first-release-date"] for g in fixture("long-groups")["body"]["release-groups"]
    }
    by_id = {short(row["id"]): row for rows in recorded.values() for row in rows}
    for release in releases:
        row = by_id[release.ref.id]
        assert release.release_date == (
            dates.get(row["release-group"]["id"]) or row["date"] or None
        )
    assert sum(1 for r in releases if r.kind is ReleaseKind.SINGLE) > 10


async def test_a_page_that_fails_fails_the_whole_page() -> None:
    """Never half a discography: it would be kept as if it were all of it."""
    replay = Replay()
    catalog = replay.catalog(page=10)
    replay.failing["musicbrainz"] = [None, None, (500, {})]
    with pytest.raises(CatalogError):
        await catalog.artist_releases(ARTIST)
    assert replay.count("musicbrainz") == 3


async def test_various_artists_have_no_page() -> None:
    replay = Replay()
    catalog = replay.catalog()
    various = short(VARIOUS_ARTISTS)
    assert await catalog.artist_releases(various) == ()
    assert await catalog.top_songs(various) == () and replay.log == []


# --- top songs ------------------------------------------------------------------------------------


async def test_top_songs_are_two_requests() -> None:
    replay = Replay()
    catalog = replay.catalog()
    songs = await catalog.top_songs(ARTIST, 10)
    ranking = fixture("top-songs-ranking")["body"]
    assert [line.split(" ")[0] for line in replay.log] == ["listenbrainz", "musicbrainz"]
    assert replay.asked("musicbrainz") == [
        "/ws/2/recording rid:(" + " OR ".join(r["recording_mbid"] for r in ranking) + ")"
    ]
    assert [s.title for s in songs] == [r["recording_name"] for r in ranking]  # as ranked
    page = {r.ref for r in await catalog.artist_releases(ARTIST)}
    assert all(s.album in page for s in songs)  # each on an album of the artist's page
    assert len(await catalog.top_songs(ARTIST, 1)) == 1


async def test_the_token_goes_to_listenbrainz_alone() -> None:
    replay = Replay()
    catalog = replay.catalog()
    await catalog.top_songs(ARTIST, 10)
    await catalog.search(TERM)
    await catalog.artwork(f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-500")
    assert replay.authorized == {"api.listenbrainz.org": 1}


async def test_no_top_songs_without_a_token_or_when_off() -> None:
    replay = Replay()
    assert await replay.catalog(listenbrainz_token=None).top_songs(ARTIST) == ()
    assert await replay.catalog(listenbrainz_token="").top_songs(ARTIST) == ()
    assert await replay.catalog(top_songs=False).top_songs(ARTIST) == ()
    assert await replay.catalog().top_songs(ARTIST, 0) == ()
    assert replay.log == []
    assert await replay.catalog().top_songs(LONG) == ()  # an artist without a ranking
    assert replay.log == [f"listenbrainz /1/popularity/top-recordings-for-artist/{mbid(LONG)}"]


async def test_a_token_listenbrainz_refuses() -> None:
    replay = Replay()
    with pytest.raises(CatalogError) as refused:
        await replay.catalog(listenbrainz_token="another").top_songs(ARTIST)
    assert refused.value.kind == "unauthorized" and "another" not in str(refused.value)
    assert replay.count("musicbrainz") == 0


# --- covers ---------------------------------------------------------------------------------------


async def test_a_cover_is_fetched_at_the_archive_s_size_next_up() -> None:
    replay = Replay()
    catalog = replay.catalog()
    album = await catalog.album(short(ALBUM["id"]))
    group = ALBUM["release-group"]["id"]
    for asked, fetched in ((100, 250), (250, 250), (300, 500), (600, 1200), (3000, 1200)):
        url = artwork_url(album.artwork_template, asked)
        assert url == f"{COVERS}/release-group/{group}/front-{asked}"
        assert await catalog.artwork(url) == (replay_image(), "image/jpeg")
        assert replay.asked("covers")[-1] == f"/release-group/{group}/front-{fetched}"
    assert replay.count("covers") == 5
    # The archive's redirect (fixture "cover-redirect") is followed to the image itself.
    assert fixture("cover-redirect")["headers"]["location"].startswith("https://archive.org/")
    assert replay.count("images") == 10  # two steps a cover


def replay_image() -> bytes:
    from tests.replay import IMAGE

    return IMAGE


async def test_only_the_catalog_s_own_image_addresses_are_fetched() -> None:
    replay = Replay()
    catalog = replay.catalog()
    group = ALBUM["release-group"]["id"]
    for foreign in (
        "https://example.invalid/300x300.jpg",
        f"http://coverartarchive.org/release-group/{group}/front-500",
        f"https://coverartarchive.org.example.invalid/release-group/{group}/front-500",
        f"{COVERS}/release-group/{group}/front-500?x=1",
        f"{COVERS}/release-group/{group}/front-{{w}}",
        f"{COVERS}/release/{group}/front-500",
        f"{COVERS}/release-group/../front-500",
    ):
        with pytest.raises(CatalogError) as refused:
            await catalog.artwork(foreign)
        assert refused.value.kind == "invalid" and "://" not in str(refused.value)
    assert replay.log == []


async def test_a_cover_redirected_elsewhere_is_not_followed() -> None:
    cover = f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-500"
    for elsewhere in (
        "https://images.example.invalid/cover.jpg",
        "http://ia800000.us.archive.org/cover.jpg",  # not over TLS
        "https://archive.org.example.invalid/cover.jpg",
        "https://notarchive.org/cover.jpg",
        "https://user@archive.org/cover.jpg",
        f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-250",  # back, unpaced
        "https://[bad",
        "",
    ):
        replay = Replay()
        replay.cover_redirect = elsewhere
        with pytest.raises(CatalogError) as refused:
            await replay.catalog().artwork(cover)
        assert refused.value.kind == "unavailable" and "://" not in str(refused.value)
        assert not any(line.startswith("elsewhere") for line in replay.log)
        assert replay.count("covers") == 1 and replay.count("images") == 1


async def test_a_release_group_without_a_cover_is_asked_about_once() -> None:
    replay = Replay()
    group = ALBUM["release-group"]["id"]
    replay.no_cover.add(group)
    catalog = replay.catalog()
    for size in (500, 500, 250, 1200):  # every view of the album asks again
        with pytest.raises(CatalogError) as missing:
            await catalog.artwork(f"{COVERS}/release-group/{group}/front-{size}")
        assert missing.value.kind == "not_found"
    assert replay.count("covers") == 1
    catalog._no_cover[group] = 0.0  # a day later: asked again
    replay.no_cover.clear()
    assert await catalog.artwork(f"{COVERS}/release-group/{group}/front-500")
    assert replay.count("covers") == 2


async def test_only_a_raster_image_is_a_cover() -> None:
    cover = f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-500"
    for claimed in ("image/svg+xml", "text/html", "image/x-icon", ""):
        replay = Replay()
        replay.image_type = claimed
        with pytest.raises(CatalogError) as refused:
            await replay.catalog().artwork(cover)
        assert refused.value.kind == "invalid"
    for claimed in ("image/png", "IMAGE/WebP; charset=binary", "image/gif"):
        replay = Replay()
        replay.image_type = claimed
        assert (await replay.catalog().artwork(cover))[1] == claimed.split(";")[0].lower()


async def test_a_size_the_archive_does_not_have_is_missing_alone() -> None:
    replay = Replay()
    catalog = replay.catalog()
    group = ALBUM["release-group"]["id"]
    replay.failing["images"] = [None, (404, {})]  # the redirect, then no such image
    for _ in range(3):
        with pytest.raises(CatalogError) as missing:
            await catalog.artwork(f"{COVERS}/release-group/{group}/front-1200")
        assert missing.value.kind == "not_found"
    assert (replay.count("covers"), replay.count("images")) == (1, 2)  # asked about once
    assert await catalog.artwork(f"{COVERS}/release-group/{group}/front-250")  # another size


async def test_the_archive_s_pause_holds_the_images() -> None:
    replay = Replay()
    catalog, clock = clocked(replay)
    group = ALBUM["release-group"]["id"]
    replay.failing["images"] = [(503, {"retry-after": "300"})]
    with pytest.raises(CatalogError) as limited:
        await catalog.artwork(f"{COVERS}/release-group/{group}/front-500")
    assert limited.value.kind == "rate_limited"
    assert catalog._paces["images"].paused_for() == 300.0
    with pytest.raises(CatalogError) as waiting:  # the next cover's image is not asked for
        await catalog.artwork(f"{COVERS}/release-group/{group}/front-250")
    assert waiting.value.kind == "rate_limited"
    assert (replay.count("covers"), replay.count("images")) == (2, 1)
    clock.now += 300.0
    assert await catalog.artwork(f"{COVERS}/release-group/{group}/front-250")


async def test_a_cover_s_redirect_that_says_when_to_follow_it() -> None:
    replay = Replay()
    catalog, _ = clocked(replay)
    group = ALBUM["release-group"]["id"]
    location = f"https://archive.org/download/mbid-{group}/mbid-{group}-1_thumb500.jpg"
    replay.failing["covers"] = [(307, {"location": location, "retry-after": "6"})]
    assert await catalog.artwork(f"{COVERS}/release-group/{group}/front-500")
    assert replay.times["covers"] == [1000.0] and replay.times["images"] == [1006.0, 1006.0]


async def test_covers_one_a_second_and_their_images_one_at_a_time() -> None:
    replay = Replay()
    replay.answer_takes = 0.2
    catalog = replay.catalog()
    group = ALBUM["release-group"]["id"]
    async with anyio.create_task_group() as tasks:
        for size in (250, 500, 1200):
            tasks.start_soon(catalog.artwork, f"{COVERS}/release-group/{group}/front-{size}")
    covers = replay.times["covers"]  # a second after the answer before, which took 0.2 s
    assert len(covers) == 3 and all(b - a >= 1.2 for a, b in pairwise(covers))
    images = replay.times["images"]
    assert len(images) == 6 and all(b - a >= 0.2 for a, b in pairwise(images))  # never two


async def test_covers_off() -> None:
    replay = Replay()
    catalog = replay.catalog(covers=False)
    album = await catalog.album(short(ALBUM["id"]))
    results = await catalog.search(TERM)
    page = await catalog.artist_releases(ARTIST)
    assert album.artwork_template is None and all(t.artwork_template is None for t in album.tracks)
    assert all(a.artwork_template is None for a in (*results.albums, *results.songs, *page))
    with pytest.raises(CatalogError) as off:
        await catalog.artwork(f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-500")
    assert off.value.kind == "not_found" and replay.count("covers") == 0


# --- 503, Retry-After -----------------------------------------------------------------------------


def clocked(replay: Replay, **kwargs: Any) -> tuple[MusicBrainzCatalog, Clock]:
    """The adapter over the replay, with the replay's time (it starts at 1000.0)."""
    return replay.catalog(**kwargs), replay.time


async def test_a_busy_answer_is_waited_out_and_asked_once_more() -> None:
    """MusicBrainz's own answer when its search is busy (fixture "search-busy"): 503 with
    ``Retry-After: 0`` - the request takes its next turn, a second later."""
    busy = fixture("search-busy")
    assert busy["status"] == 503 and busy["headers"] == {"retry-after": "0"}
    replay = Replay()
    catalog, _ = clocked(replay)
    replay.failing["musicbrainz"] = [(503, busy["headers"])]
    album = await catalog.album(short(ALBUM["id"]))
    assert album.tracks and replay.times["musicbrainz"] == [1000.0, 1001.0]


async def test_retry_after_is_honored_by_everything_the_adapter_asks() -> None:
    replay = Replay()
    catalog, _ = clocked(replay)
    replay.failing["musicbrainz"] = [(503, {"retry-after": "4"})]
    async with anyio.create_task_group() as group:  # an album, and a search right behind it
        group.start_soon(catalog.album, short(ALBUM["id"]))
        group.start_soon(catalog.search, TERM)
    # Nothing for four seconds after the 503; then one a second again.
    assert replay.times["musicbrainz"] == [1000.0, 1004.0, 1005.0, 1006.0, 1007.0]


async def test_a_long_retry_after_fails_the_request_and_keeps_others_away() -> None:
    replay = Replay()
    catalog, clock = clocked(replay)
    replay.failing["musicbrainz"] = [(503, {"retry-after": "120"})]
    with pytest.raises(CatalogError) as limited:
        await catalog.album(short(ALBUM["id"]))
    assert limited.value.kind == "rate_limited" and clock() == 1000.0
    with pytest.raises(CatalogError) as waiting:
        await catalog.search(TERM)
    assert waiting.value.kind == "rate_limited" and "asked to wait" in str(waiting.value)
    assert replay.count("musicbrainz") == 1  # nothing was sent meanwhile
    clock.now += 120.0
    assert (await catalog.album(short(ALBUM["id"]))).tracks
    assert replay.times["musicbrainz"] == [1000.0, 1120.0]
    # However long it asks: two hours are two hours.
    replay.failing["musicbrainz"] = [(503, {"retry-after": "7200"})]
    with pytest.raises(CatalogError):
        await catalog.album(short(ALBUM["id"]))
    clock.now += 7100.0
    with pytest.raises(CatalogError) as still:
        await catalog.album(short(ALBUM["id"]))
    assert still.value.kind == "rate_limited" and replay.count("musicbrainz") == 3
    clock.now += 100.0
    assert (await catalog.album(short(ALBUM["id"]))).tracks


async def test_a_pause_asked_of_one_request_stops_the_one_waiting_behind_it() -> None:
    """The answer is read before the next request is let go: a request that waited for
    its turn while the 503 was on its way is not sent."""
    replay = Replay()
    catalog, clock = clocked(replay)
    replay.answer_takes = 0.5
    replay.failing["musicbrainz"] = [(503, {"retry-after": "120"})]
    outcomes: list[str] = []

    async def ask() -> None:
        try:
            await catalog.album(short(ALBUM["id"]))
        except CatalogError as exc:
            outcomes.append(exc.kind)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(ask)
        tasks.start_soon(ask)
    assert outcomes == ["rate_limited", "rate_limited"]
    assert replay.times["musicbrainz"] == [1000.0]  # the second was never sent
    clock.now += 60.0
    await ask()
    assert outcomes[-1] == "rate_limited" and replay.count("musicbrainz") == 1
    clock.now += 60.0
    assert (await catalog.album(short(ALBUM["id"]))).tracks


async def test_requests_a_while_on_their_way_still_arrive_a_second_apart() -> None:
    """A connection to open, a slow network: the second is counted from the answer."""
    replay = Replay()
    catalog, _ = clocked(replay)
    replay.on_the_way, replay.answer_takes = 0.7, 0.2
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(catalog.album, short(ALBUM["id"]))
        tasks.start_soon(catalog.search, TERM)
    times = replay.times["musicbrainz"]
    assert times == pytest.approx([1000.7, 1002.6, 1004.5, 1006.4])
    assert all(later - earlier >= 1.0 for earlier, later in pairwise(times))


async def test_a_redirect_that_says_when_to_follow_it() -> None:
    replay = Replay()
    catalog, _ = clocked(replay)
    old = "0f0e0d0c-0b0a-4908-8706-050403020100"
    moved = f"https://musicbrainz.org/ws/2/release/{ALBUM['id']}"
    replay.failing["musicbrainz"] = [(301, {"location": moved, "retry-after": "4"})]
    assert (await catalog.album(short(old))).tracks
    assert replay.times["musicbrainz"] == [1000.0, 1004.0]
    replay.failing["musicbrainz"] = [(301, {"location": moved, "retry-after": "120"})]
    with pytest.raises(CatalogError) as waiting:
        await catalog.album(short(old))
    assert waiting.value.kind == "rate_limited" and replay.count("musicbrainz") == 3


async def test_a_second_busy_answer_is_the_request_s_failure() -> None:
    replay = Replay()
    catalog, _ = clocked(replay)
    replay.failing["musicbrainz"] = [(503, {"retry-after": "1"}), (503, {"retry-after": "1"})]
    with pytest.raises(CatalogError) as limited:
        await catalog.album(short(ALBUM["id"]))
    assert limited.value.kind == "rate_limited" and replay.count("musicbrainz") == 2
    assert (await catalog.album(short(ALBUM["id"]))).tracks  # after the pause it asked for
    assert replay.times["musicbrainz"] == [1000.0, 1001.0, 1002.0]


async def test_a_503_that_names_no_time_pauses_for_a_few_seconds() -> None:
    replay = Replay()
    catalog, _ = clocked(replay)
    replay.failing["musicbrainz"] = [(503, {}), (503, {})]
    with pytest.raises(CatalogError) as down:
        await catalog.album(short(ALBUM["id"]))
    assert down.value.kind == "unavailable"
    assert replay.times["musicbrainz"] == [1000.0, 1005.0]


async def test_429_from_the_other_services_pauses_that_service_alone() -> None:
    replay = Replay()
    catalog = replay.catalog()
    paces = catalog._paces
    replay.failing["covers"] = [(429, {"retry-after": "300"})]
    with pytest.raises(CatalogError) as limited:
        await catalog.artwork(f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-500")
    assert limited.value.kind == "rate_limited"
    assert paces["covers"].paused_for() == 300.0 and paces["musicbrainz"].paused_for() == 0.0
    assert (await catalog.album(short(ALBUM["id"]))).tracks  # MusicBrainz goes on
    replay.failing["listenbrainz"] = [(429, {"retry-after": "2"})]
    assert await catalog.top_songs(ARTIST)  # waited out, asked again
    assert replay.times["listenbrainz"] == [1000.0, 1002.0]


def test_retry_after_as_seconds_or_a_date() -> None:
    assert retry_after({"retry-after": "0"}) == 0.0
    assert retry_after({"retry-after": " 12 "}) == 12.0
    assert retry_after({"retry-after": "7200"}) == 7200.0  # as long as it asks
    assert retry_after({"retry-after": "1e999"}) is None
    assert retry_after({"retry-after": "-5"}) == 0.0
    assert retry_after({}) is None and retry_after({"retry-after": "soon"}) is None
    assert retry_after({"retry-after": "nan"}) is None
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=90), usegmt=True)
    assert 85.0 < (retry_after({"retry-after": later}) or 0.0) <= 90.0
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=90), usegmt=True)
    assert retry_after({"retry-after": past}) == 0.0


# --- failures -------------------------------------------------------------------------------------


async def test_failures_by_their_kind_and_without_addresses() -> None:
    replay = Replay()
    catalog = replay.catalog()
    for call in (catalog.album, catalog.song, catalog.artist, catalog.artist_releases):
        with pytest.raises(CatalogError) as missing:
            await call(MISSING)
        assert missing.value.kind == "not_found" and "://" not in str(missing.value)
    for status, kind in ((500, "unavailable"), (401, "unauthorized"), (403, "unauthorized")):
        replay.failing["musicbrainz"] = [(status, {})]
        with pytest.raises(CatalogError) as failed:
            await catalog.album(short(ALBUM["id"]))
        assert failed.value.kind == kind and "://" not in str(failed.value)
    replay.failing["musicbrainz"] = [(400, {})]  # a query MusicBrainz cannot read
    with pytest.raises(CatalogError) as refused:
        await catalog.search(TERM)
    assert refused.value.kind == "invalid"
    replay.failing["musicbrainz"] = [(301, {"location": "https://example.invalid/"})]
    with pytest.raises(CatalogError) as moved:  # redirects are not followed
        await catalog.album(short(ALBUM["id"]))
    assert moved.value.kind == "unavailable"
    assert not any(line.startswith("elsewhere") for line in replay.log)


async def test_an_id_musicbrainz_calls_invalid_is_not_found() -> None:
    """MusicBrainz's own answer for an ID it never issued (fixture
    "artist-releases-invalid-id"): 400, not 404."""
    recorded = fixture("artist-releases-invalid-id")
    assert recorded["status"] == 400 and recorded["body"]["error"] == "Invalid mbid."
    replay = Replay()
    replay.invalid.add(mbid(MISSING))
    catalog = replay.catalog()
    for call in (catalog.album, catalog.song, catalog.artist, catalog.artist_releases):
        with pytest.raises(CatalogError) as missing:
            await call(MISSING)
        assert missing.value.kind == "not_found"


async def test_a_connection_that_fails() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to https://musicbrainz.org/ws/2")

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse), headers={"user-agent": AGENT})
    Replay().install()
    catalog = MusicBrainzCatalog(http)
    with pytest.raises(CatalogError) as failed:
        await catalog.album(short(ALBUM["id"]))
    assert failed.value.kind == "unavailable" and "://" not in str(failed.value)
    await catalog.aclose()


async def test_an_answer_that_is_not_json() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>")),
        headers={"user-agent": AGENT},
    )
    Replay().install()
    catalog = MusicBrainzCatalog(http)
    with pytest.raises(CatalogError) as failed:
        await catalog.album(short(ALBUM["id"]))
    assert failed.value.kind == "invalid"
    with pytest.raises(CatalogError) as listed:
        await catalog.artist(ARTIST)
    assert listed.value.kind == "invalid"


# --- the client, the server -----------------------------------------------------------------------


async def test_every_request_carries_the_client_s_user_agent() -> None:
    replay = Replay()
    catalog = replay.catalog()
    await catalog.search(TERM)
    await catalog.top_songs(ARTIST)
    await catalog.artwork(f"{COVERS}/release-group/{ALBUM['release-group']['id']}/front-500")
    await catalog.check()
    assert replay.agents == {AGENT}


def test_no_way_around_the_one_request_a_second() -> None:
    """Not by an address that reads differently than it is asked, not by a pace handed in,
    not by another catalog's settings."""
    replay = Replay()
    for odd in (
        "https://musicbrainz.org/%3Ffoo",  # read as a query once decoded
        "https://musicbrainz.org/%2e%2e/x",
        "https://musicbrainz.org/a b",
        "https://musicbrainz.org/#x",
        "https://musicbrainz.org/?",
    ):
        assert server_address(odd) is None
        with pytest.raises(ValueError, match="plain http"):
            MusicBrainzCatalog(replay.client(), server=odd, mirror_requests_per_second=50)
    for spelled in ("https://musicbrainz.org/a/../b", "https://MUSICBRAINZ.org:443/x/"):
        named = MusicBrainzCatalog(replay.client(), server=spelled, mirror_requests_per_second=50)
        assert official(named.server) and named._paces["musicbrainz"].interval == 1.0
    # A faster pace put in the shared paces' place is slowed down when a catalog is built.
    catalog_module._paces["musicbrainz.org"] = Pace(0.01)
    usual = MusicBrainzCatalog(replay.client(), mirror_requests_per_second=50)
    assert usual._paces["musicbrainz"] is catalog_module._paces["musicbrainz.org"]
    assert usual._paces["musicbrainz"].interval == 1.0
    MusicBrainzCatalog(replay.client(), server="https://mirror.example.net/mb/")  # a prefix
    assert shared_pace("musicbrainz.org", 0.01) is usual._paces["musicbrainz"]
    assert usual._paces["musicbrainz"].interval == 1.0  # whatever it was asked to be
    assert shared_pace("coverartarchive.org", 0.0).interval == 1.0
    assert shared_pace("api.listenbrainz.org", 0.0).interval == 1.0
    # A catalog takes no pace of its own: every one built for a service shares its pace.
    with pytest.raises(TypeError):
        MusicBrainzCatalog(replay.client(), pace=Pace(1.0))  # type: ignore[call-arg]
    other = MusicBrainzCatalog(replay.client(), covers=False)
    assert all(other._paces[name] is usual._paces[name] for name in usual._paces)
    # A mirror under an international name is asked under its ASCII one.
    mirror = server_address("https://m\u00fcsic.example.net:8443/")
    assert mirror == "https://xn--msic-0ra.example.net:8443" and server_address(mirror) == mirror
    assert MusicBrainzCatalog(replay.client(), server=mirror).server == mirror


async def test_two_catalogs_of_one_service_never_ask_at_once() -> None:
    """Two catalogs built for MusicBrainz (one rebuilt after its settings changed, say):
    one allowance, and a pause asked of one holds the other."""
    replay = Replay()
    first = replay.catalog()
    second = replay.catalog(covers=False)
    replay.answer_takes = 0.1
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first.album, short(ALBUM["id"]))
        tasks.start_soon(second.album, short(ALBUM["id"]))
        tasks.start_soon(first.check)
        tasks.start_soon(second.check)
    times = replay.times["musicbrainz"]
    assert times == pytest.approx([1000.0, 1001.1, 1002.2, 1003.3])
    replay.failing["musicbrainz"] = [(503, {"retry-after": "120"})]
    with pytest.raises(CatalogError):
        await first.album(short(ALBUM["id"]))
    with pytest.raises(CatalogError) as held:
        await second.album(short(ALBUM["id"]))
    assert held.value.kind == "rate_limited" and replay.count("musicbrainz") == 5


def test_a_client_that_names_no_application_is_refused() -> None:
    replay = Replay()
    with pytest.raises(ValueError, match="User-Agent"):
        MusicBrainzCatalog(replay.client(agent=None))  # httpx's own name
    with pytest.raises(ValueError, match="User-Agent"):
        MusicBrainzCatalog(replay.client(agent=""))


async def test_a_mirror_is_asked_instead() -> None:
    replay = Replay()
    replay.hosts = {"mirror.example.net"}
    catalog = replay.catalog(server="https://mirror.example.net/")
    assert (await catalog.album(short(ALBUM["id"]))).tracks
    await catalog.check()
    assert replay.asked("musicbrainz") == [
        f"/ws/2/release/{ALBUM['id']}",
        f"/ws/2/artist/{VARIOUS_ARTISTS}",
    ]
    for wrong in (
        "ftp://mirror.example.net",
        "mirror.example.net",
        "https://u:p@mirror.example.net",
        "https://mirror.example.net/?x=1",
        "https://",
        "https://mirror.example.net:0",
    ):
        with pytest.raises(ValueError, match="plain http"):
            replay.catalog(server=wrong)


async def test_the_check_is_one_small_request() -> None:
    replay = Replay()
    await replay.catalog().check()
    assert replay.asked("musicbrainz") == [f"/ws/2/artist/{VARIOUS_ARTISTS}"]


async def test_no_view_asks_once_for_each_album_or_track() -> None:
    """What the README says each view costs."""
    replay = Replay()
    catalog = replay.catalog()
    costs: dict[str, int] = {}

    async def cost(name: str, call: Any) -> None:
        before = len(replay.log)
        await call
        costs[name] = len(replay.log) - before

    await cost("search", catalog.search(TERM, 25))
    await cost("artist", catalog.artist(ARTIST))
    await cost("artist page", catalog.artist_releases(ARTIST))
    await cost("album", catalog.album(short(ALBUM["id"])))
    await cost("song", catalog.song(short(ALBUM["media"][0]["tracks"][4]["id"])))
    await cost(
        "isrc", catalog.songs_by_isrc(ALBUM["media"][0]["tracks"][0]["recording"]["isrcs"][0])
    )
    await cost("top songs", catalog.top_songs(ARTIST, 10))
    await cost("artists of", catalog.artists_of(("a",), ("b",)))
    assert costs == {
        "search": 3,
        "artist": 0,  # the search named it
        "artist page": 1,
        "album": 1,
        "song": 1,
        "isrc": 1,
        "top songs": 2,
        "artists of": 0,
    }
