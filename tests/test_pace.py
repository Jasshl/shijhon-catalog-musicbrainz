"""The limiter: never above one request a second, shared, held back by the service."""

from __future__ import annotations

import time
from itertools import pairwise

import anyio
import pytest

from shijhon_catalog_musicbrainz.catalog import (
    MUSICBRAINZ,
    MusicBrainzCatalog,
    official,
    server_address,
    shared_pace,
)
from shijhon_catalog_musicbrainz.pace import Busy, Pace
from tests.replay import Clock, Replay, fixture, short

pytestmark = pytest.mark.anyio


def paced(interval: float = 1.0, **kwargs: float) -> tuple[Pace, Clock]:
    clock = Clock()
    return Pace(interval, clock=clock, sleep=clock.sleep, **kwargs), clock


async def test_never_more_than_one_a_second() -> None:
    pace, clock = paced()
    starts: list[float] = []

    async def request() -> None:
        async with pace.request():
            starts.append(clock())

    async with anyio.create_task_group() as group:
        for _ in range(12):
            group.start_soon(request)
    assert len(starts) == 12 and pace.turns == 12
    assert starts[0] == 1000.0  # the first one at once
    assert all(later - earlier >= 1.0 for earlier, later in pairwise(starts))
    assert starts[-1] == 1011.0  # and, answered at once, no slower than one a second


async def test_the_second_is_counted_from_the_answer_and_requests_never_overlap() -> None:
    """A request is on its way for an unknown time (a connection to open) and its answer
    takes a while: the next one starts a full second after the answer, so the service
    sees them at least a second apart however long each was on its way."""
    pace, clock = paced()
    under_way = 0
    sent: list[float] = []  # when the service sees each request

    async def request(on_the_way: float, answer: float) -> None:
        nonlocal under_way
        async with pace.request():
            under_way += 1
            assert under_way == 1  # one at a time
            await clock.sleep(on_the_way)
            sent.append(clock())
            await clock.sleep(answer)
            under_way -= 1

    async with anyio.create_task_group() as group:
        for on_the_way, answer in ((0.9, 0.3), (0.0, 0.2), (0.6, 0.1), (0.0, 0.0), (0.0, 0.0)):
            group.start_soon(request, on_the_way, answer)
    assert len(sent) == 5
    assert sent == pytest.approx([1000.9, 1002.2, 1004.0, 1005.1, 1006.1])
    assert all(later - earlier >= 1.0 for earlier, later in pairwise(sent))


async def test_a_request_after_a_quiet_time_is_at_once_and_the_next_a_second_later() -> None:
    pace, clock = paced()
    async with pace.request():
        pass
    clock.now += 60.0
    async with pace.request():
        assert clock() == 1060.0
    async with pace.request():
        assert clock() == 1061.0  # no burst saved up from the quiet minute


async def test_in_real_time() -> None:
    pace = Pace(0.05)
    starts: list[float] = []

    async def request() -> None:
        async with pace.request():
            starts.append(time.monotonic())
            await anyio.sleep(0.01)

    async with anyio.create_task_group() as group:
        for _ in range(6):
            group.start_soon(request)
    gaps = [later - earlier for earlier, later in pairwise(starts)]
    assert len(gaps) == 5 and min(gaps) >= 0.06


async def test_first_come_first_served() -> None:
    pace, _ = paced()
    order: list[int] = []

    async def request(number: int) -> None:
        async with pace.request():
            order.append(number)

    async with anyio.create_task_group() as group:
        for number in range(6):
            group.start_soon(request, number)
            await anyio.lowlevel.checkpoint()
    assert order == list(range(6))


async def test_a_pause_the_service_asked_for_holds_everyone() -> None:
    pace, clock = paced()
    async with pace.request():
        pace.hold(7.5)
    assert pace.paused_for() == 7.5
    async with pace.request():
        assert clock() == 1007.5
    async with pace.request():
        assert clock() == 1008.5
        pace.hold(0.0)  # "Retry-After: 0": just the next turn
    async with pace.request():
        assert clock() == 1009.5


async def test_a_pause_asked_for_while_requests_wait_holds_them_too() -> None:
    """The answer that asks for the pause is read before the next request is let go: a
    request that was already waiting is not sent into the pause."""
    pace, clock = paced()
    starts: list[float] = []

    async def request() -> None:
        async with pace.request():
            starts.append(clock())
            await clock.sleep(0.25)  # the answer arrives
            if len(starts) == 1:
                pace.hold(5.0)  # ... and it is 503, Retry-After: 5

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(request)
    assert starts == [1000.0, 1005.25, 1006.5]


async def test_a_long_pause_turns_requests_away_instead_of_keeping_them_waiting() -> None:
    pace, clock = paced(max_wait=20.0)
    pace.hold(120.0)
    with pytest.raises(Busy) as refused:
        async with pace.request():
            raise AssertionError("let go into the pause")
    assert refused.value.paused and clock() == 1000.0 and pace.turns == 0
    clock.now += 101.0  # 19 s left: worth waiting for
    async with pace.request():
        assert clock() == 1120.0


async def test_a_long_pause_asked_for_while_requests_wait_turns_them_away() -> None:
    pace, clock = paced(max_wait=20.0)
    outcomes: list[str] = []

    async def request(first: bool) -> None:
        try:
            async with pace.request():
                outcomes.append("let go")
                if first:
                    await clock.sleep(0.1)
                    pace.hold(600.0)
        except Busy as busy:
            assert busy.paused
            outcomes.append("away")

    async with anyio.create_task_group() as group:
        group.start_soon(request, True)
        group.start_soon(request, False)
        group.start_soon(request, False)
    assert outcomes == ["let go", "away", "away"] and pace.turns == 1 and pace._waiting == 0


async def test_too_many_waiting_requests_are_turned_away() -> None:
    pace, clock = paced(max_wait=5.0)
    outcomes: list[str] = []

    async def request() -> None:
        try:
            async with pace.request():
                outcomes.append("turn")
        except Busy as busy:
            assert not busy.paused
            outcomes.append("away")

    async with anyio.create_task_group() as group:
        for _ in range(10):
            group.start_soon(request)
    # The one at once and those within five seconds; the others never waited.
    assert outcomes.count("turn") == 6 and outcomes.count("away") == 4
    assert clock() == 1005.0 and pace._waiting == 0


async def test_no_request_starts_after_waiting_longer_than_the_bound() -> None:
    """The requests ahead took longer than was reckoned with: the one behind them is
    turned away when its turn comes too late, not sent."""
    pace, clock = paced(max_wait=5.0)
    outcomes: list[str] = []

    async def request(takes: float) -> None:
        try:
            async with pace.request():
                outcomes.append("sent")
                await clock.sleep(takes)
        except Busy as busy:
            assert not busy.paused and busy.seconds > 5.0
            outcomes.append("away")

    async with anyio.create_task_group() as group:
        group.start_soon(request, 7.0)  # a slow answer
        group.start_soon(request, 0.0)
        group.start_soon(request, 0.0)
    assert outcomes == ["sent", "away", "away"] and pace.turns == 1 and pace._waiting == 0
    async with pace.request():  # the pace itself goes on
        assert clock() == 1008.0


async def test_a_wait_that_took_too_long_after_all_is_no_turn() -> None:
    """The wait was reckoned to fit, and then took longer (a busy event loop, a sleep that
    overslept): the request is turned away, not sent late."""
    clock = Clock()

    async def oversleep(seconds: float) -> None:
        await clock.sleep(seconds + 4.5)

    pace = Pace(1.0, max_wait=5.0, clock=clock, sleep=oversleep)
    async with pace.request():
        pass
    with pytest.raises(Busy) as late:
        async with pace.request():
            raise AssertionError("sent after waiting longer than the bound")
    assert late.value.seconds == 5.5 and pace.turns == 1 and pace._users == 0


async def test_in_real_time_a_request_is_not_kept_in_line_beyond_the_bound() -> None:
    pace = Pace(0.0, max_wait=0.05)
    outcomes: list[str] = []

    async def slow() -> None:
        async with pace.request():
            await anyio.sleep(0.3)

    async def behind() -> None:
        started = time.monotonic()
        try:
            async with pace.request():
                outcomes.append("sent")
        except Busy:
            outcomes.append(f"away after {time.monotonic() - started:.2f}")

    async with anyio.create_task_group() as group:
        group.start_soon(slow)
        await anyio.sleep(0.01)
        group.start_soon(behind)
    assert outcomes in (["away after 0.05"], ["away after 0.06"], ["away after 0.07"])
    assert pace._waiting == 0 and pace._users == 0


async def test_a_second_event_loop_is_turned_away_not_served_beside_the_first() -> None:
    """Two loops in two threads would be two allowances (and a lock is no thread's
    friend): the pace is one loop's at a time."""
    pace = Pace(0.0)
    seen: list[str] = []

    async def other_loop() -> None:
        try:
            async with pace.request():
                seen.append("served")
        except Busy as busy:
            seen.append("elsewhere" if busy.elsewhere else "busy")

    async with pace.request():
        await anyio.to_thread.run_sync(anyio.run, other_loop)
    assert seen == ["elsewhere"] and pace._users == 0
    await anyio.to_thread.run_sync(anyio.run, other_loop)  # one after the other is fine
    assert seen == ["elsewhere", "served"]
    async with pace.request():
        pass


async def test_a_canceled_wait_leaves_the_pace_as_it_was() -> None:
    pace = Pace(0.2)
    async with pace.request():
        pass
    with anyio.move_on_after(0.05):
        async with pace.request():
            raise AssertionError("let go before its turn")
    assert pace.turns == 1 and pace._waiting == 0
    started = time.monotonic()
    async with pace.request():
        pass
    assert pace.turns == 2 and time.monotonic() - started < 0.2


async def test_a_request_given_up_half_way_still_counts() -> None:
    """It may have been sent: the next one keeps its distance from when it was given up."""
    pace, clock = paced()
    with pytest.raises(RuntimeError):
        async with pace.request():
            await clock.sleep(0.4)
            raise RuntimeError("the connection broke")
    async with pace.request():
        assert clock() == 1001.4


# --- one pace for each service, whatever the settings say ---------------------------------------


def test_musicbrainz_s_own_service_is_one_a_second_whatever_the_setting() -> None:
    replay = Replay()
    for server in (
        MUSICBRAINZ,
        "https://musicbrainz.org/",
        "https://beta.musicbrainz.org",
        "https://MusicBrainz.org.:443",
        "http://musicbrainz.org:8080/prefix/",
        "https://musicbrainz。org",  # an ideographic full stop: the same host on the wire
    ):
        catalog = MusicBrainzCatalog(replay.client(), server=server, mirror_requests_per_second=50)
        assert official(server) and catalog._paces["musicbrainz"].interval == 1.0
        assert catalog._paces["musicbrainz"] is shared_pace("musicbrainz.org", 1.0)
    assert server_address("https://musicbrainz。org") == "https://musicbrainz.org"
    assert server_address("https://MusicBrainz.org.:443/") == "https://musicbrainz.org"
    mirror = MusicBrainzCatalog(
        replay.client(), server="https://mirror.example.net", mirror_requests_per_second=50
    )
    assert not official("https://mirror.example.net")
    assert mirror._paces["musicbrainz"].interval == pytest.approx(0.02)
    # A look-alike host is no part of MusicBrainz, and its services keep their own pace.
    assert not official("https://musicbrainz.org.example.net")
    assert not official("https://notmusicbrainz.org")
    assert mirror._paces["listenbrainz"].interval == mirror._paces["covers"].interval == 1.0
    # A host the HTTP client would read differently than it is written is no address at all.
    wide = f"https://{chr(0xFF4D)}usicbrainz.org"  # a full-width letter
    for odd in ("https://musicbrainz%2Eorg", wide, "https://a b.org"):
        assert server_address(odd) is None and not official(odd)


def test_every_catalog_in_the_process_shares_the_pace() -> None:
    replay = Replay()
    first = MusicBrainzCatalog(replay.client())
    second = MusicBrainzCatalog(replay.client(), covers=False)
    for service in ("musicbrainz", "listenbrainz", "covers", "images"):
        assert first._paces[service] is second._paces[service]
    assert first._paces["musicbrainz"] is shared_pace("musicbrainz.org", 1.0)
    assert len({id(pace) for pace in first._paces.values()}) == 4  # one for each service
    other = MusicBrainzCatalog(replay.client(), server="https://mirror.example.net")
    assert other._paces["musicbrainz"] is not first._paces["musicbrainz"]
    assert other._paces["covers"] is first._paces["covers"]
    again = MusicBrainzCatalog(replay.client(), server="https://MIRROR.example.net:8443/")
    assert again._paces["musicbrainz"] is other._paces["musicbrainz"]  # one host, one pace


async def test_everything_the_adapter_does_is_one_request_at_a_time_at_the_one_pace() -> None:
    """Searches, an artist page, an album, a song, an ISRC and the check, from two
    catalogs at once, each request a while on its way: MusicBrainz sees them at least an
    interval apart."""
    interval = 0.03
    replay = Replay(clock=time.monotonic, sleep=anyio.sleep)
    replay.hosts = {"mirror.example.net"}
    replay.on_the_way = 0.004
    server = "https://mirror.example.net"
    rate = 1 / interval
    first = MusicBrainzCatalog(replay.client(), server=server, mirror_requests_per_second=rate)
    second = MusicBrainzCatalog(replay.client(), server=server, mirror_requests_per_second=rate)
    term = fixture("search-artists")["params"]["query"]
    album = short(fixture("album")["body"]["id"])
    track = short(fixture("album")["body"]["media"][0]["tracks"][0]["id"])
    artist = short(fixture("artist")["body"]["id"])
    isrc = fixture("album")["body"]["media"][0]["tracks"][0]["recording"]["isrcs"][0]
    async with anyio.create_task_group() as group:
        group.start_soon(first.search, term)
        group.start_soon(second.search, term)
        group.start_soon(first.artist_releases, artist)
        group.start_soon(second.album, album)
        group.start_soon(first.song, track)
        group.start_soon(second.songs_by_isrc, isrc)
        group.start_soon(first.check)
    times = replay.times["musicbrainz"]
    assert len(times) == 11  # 3 + 3 + 1 + 1 + 1 + 1 + 1
    gaps = [later - earlier for earlier, later in pairwise(times)]
    assert min(gaps) >= interval
