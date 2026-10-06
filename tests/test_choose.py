"""The representative-release rule (``choose``): which release stands for a release group,
and which release a song is shown on."""

from __future__ import annotations

import random

from shijhon_catalog_musicbrainz.catalog import candidate
from shijhon_catalog_musicbrainz.choose import (
    Candidate,
    date_key,
    format_rank,
    home,
    representative,
)
from tests.replay import fixture

DIGITAL, CD, VINYL = ("Digital Media",), ("CD",), ('12" Vinyl',)


def release(
    id: str,
    *,
    status: str | None = "Official",
    formats: tuple[str | None, ...] = DIGITAL,
    date: str | None = "2020-05-01",
    tracks: int = 10,
    comment: str = "",
    primary: str | None = "Album",
    secondary: tuple[str, ...] = (),
) -> Candidate:
    return Candidate(id, status, formats, date, tracks, comment, primary, secondary)


def chosen(*releases: Candidate) -> str:
    """The representative's ID - the same whatever order the releases come in."""
    picked = {representative(random.sample(releases, len(releases))).id for _ in range(20)}
    assert len(picked) == 1
    return picked.pop()


def test_an_official_release_before_any_other() -> None:
    assert (
        chosen(
            release("a", status="Bootleg", date="1999-01-01", tracks=30),
            release("b", status="Promotion", date="2000-01-01"),
            release("c", status="Official", formats=VINYL, date="2021-01-01", tracks=8),
        )
        == "c"
    )
    assert chosen(release("a", status="Bootleg"), release("b", status="Promotion")) == "b"
    assert chosen(release("a", status=None), release("b", status="Bootleg")) == "b"
    assert chosen(release("a", status="Pseudo-Release"), release("b", status="Withdrawn")) == "b"


def test_digital_then_cd_then_other_formats() -> None:
    assert (
        chosen(
            release("a", formats=VINYL, date="1975-03-01"),
            release("b", formats=CD, date="1987-06-01"),
            release("c", formats=DIGITAL, date="2009-09-09"),
        )
        == "c"
    )
    assert (
        chosen(release("a", formats=VINYL, date="1975"), release("b", formats=CD, date="1987"))
        == "b"
    )
    # A set of mixed formats, and a release without media, come after both.
    assert (
        chosen(
            release("a", formats=("CD", '12" Vinyl'), date="1990"),
            release("b", formats=CD, date="1995"),
        )
        == "b"
    )
    assert (
        chosen(release("a", formats=(), date="1990"), release("b", formats=CD, date="1995")) == "b"
    )
    assert (
        chosen(release("a", formats=(None,)), release("b", formats=("Cassette",), date="2021"))
        == "a"
    )
    assert format_rank(("Digital Media", "Digital Media")) == 0
    assert format_rank(("SHM-CD", "CD")) == 1 and format_rank(("Enhanced CD",)) == 1
    assert format_rank(("CD", "DVD-Video")) == 2 and format_rank(()) == 2


def test_the_earliest_release() -> None:
    assert (
        chosen(
            release("a", date="2020-05-02"),
            release("b", date="2020-05-01"),
            release("c", date="2021"),
        )
        == "b"
    )
    # A date without a day or month is the end of that month or year; no date is last.
    assert chosen(release("a", date="2020"), release("b", date="2020-12-31")) == "b"
    assert chosen(release("a", date="2020-05"), release("b", date="2020-05-31")) == "b"
    assert chosen(release("a", date="2020-05"), release("b", date="2020-06-01")) == "a"
    assert chosen(release("a", date=None), release("b", date="2024")) == "b"
    assert (
        chosen(release("a", date=""), release("b", date="not a date"), release("c", date="2030"))
        == "c"
    )
    assert date_key("2020") < date_key("2021-01-01") and date_key(None) > date_key("9998")


def test_the_most_tracks_among_releases_of_one_day_then_the_lowest_id() -> None:
    assert chosen(release("a", tracks=12), release("b", tracks=14), release("c", tracks=13)) == "b"
    # ... but an earlier release wins over a fuller one: the original before a deluxe edition.
    assert chosen(release("a", tracks=12), release("b", tracks=24, date="2020-11-20")) == "a"
    assert chosen(release("b"), release("a"), release("C")) == "a"
    assert chosen(release("B"), release("c")) == "B"  # whatever the letters' case


def test_a_release_called_clean_comes_after_the_others_of_its_format() -> None:
    assert chosen(release("a", comment="clean"), release("b", comment="explicit")) == "b"
    assert chosen(release("a", comment="Clean version", date="2019"), release("b")) == "b"
    assert chosen(release("a", comment="censored"), release("b", comment="deluxe edition")) == "b"
    assert chosen(release("a", comment="edited"), release("b", date="2022")) == "b"
    assert chosen(release("a", comment="cleaned-up remaster"), release("b", date="2022")) == "a"
    # The format counts first: the choice among digital releases never looks at others.
    assert chosen(release("a", comment="clean"), release("b", formats=CD)) == "a"


def test_a_group_with_a_digital_release_is_chosen_from_its_digital_releases_alone() -> None:
    """What makes a long discography cheap: its official digital releases are enough."""
    group = [
        release("a", formats=VINYL, date="1970"),
        release("b", formats=CD, date="1986", tracks=14),
        release("c", formats=DIGITAL, date="2011-02-01"),
        release("d", formats=DIGITAL, date="2009-09-09"),
        release("e", status="Bootleg", date="1969"),
    ]
    digital = [r for r in group if r.formats == DIGITAL and r.status == "Official"]
    assert representative(group).id == representative(digital).id == "d"


def test_a_song_is_shown_on_a_studio_album_first() -> None:
    releases = [
        release("single", primary="Single", date="2019-01-01"),
        release("ep", primary="EP", date="2019-02-01"),
        release("album", date="2019-06-01"),
        release("best-of", secondary=("Compilation",), date="2018-01-01"),
        release("live", secondary=("Live",), date="2017-01-01"),
        release("bootleg", status="Bootleg", date="2010-01-01"),
    ]
    assert home(random.sample(releases, len(releases))).id == "album"
    assert home(releases[:2] + releases[3:]).id == "ep"
    assert home([releases[0], *releases[3:]]).id == "single"
    assert home(releases[3:]).id == "live"  # then as any release: the earliest
    assert home(releases[5:]).id == "bootleg"


def test_a_song_s_release_is_its_group_s_representative_when_that_has_it() -> None:
    """The same order within a group: a song found by a search lands on the release its
    album is shown with."""
    album = [
        release("a2", date="2020-05-01", tracks=12),
        release("a1", date="2020-05-01", tracks=12),
        release("a3", formats=CD, date="2020-04-24", tracks=13),
    ]
    other = [release("s1", primary="Single", date="2020-03-01")]
    assert representative(album).id == "a1"
    assert home(album + other).id == "a1"
    # A bonus track that only the CD has is shown on the CD.
    assert home([album[2], *other]).id == "a3"


def test_the_recorded_discography_is_chosen_by_the_rule() -> None:
    """The recorded artist's releases, group by group, against the rule written out."""
    groups: dict[str, list[Candidate]] = {}
    for row in fixture("artist-releases")["body"]["releases"]:
        found = candidate(row)
        assert found is not None
        groups.setdefault(row["release-group"]["id"], []).append(found)
    assert len(groups) > 5 and any(len(members) > 5 for members in groups.values())
    for members in groups.values():
        best = representative(members)
        assert best.status == "Official"
        digital = [m for m in members if format_rank(m.formats) == 0]
        if digital:
            assert best in digital
            assert date_key(best.date) == min(date_key(m.date) for m in digital)
        elif any(format_rank(m.formats) == 1 for m in members):
            assert format_rank(best.formats) == 1
