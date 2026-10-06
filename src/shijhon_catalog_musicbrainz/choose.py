"""Which release stands for a release group, and which release a song is shown on.

MusicBrainz keeps every pressing and edition of an album as a *release* of one *release
group*; Shijhon shows one album. The adapter hands over one release for each release
group - the first in this order (``order``):

1. an **official** release before any other status (promotion, bootleg, withdrawn, ...);
2. a **digital** release, then a **CD**, then any other format (vinyl, cassette, mixed
   sets). Digital releases carry the track lists and lengths that streaming sources have;
   CDs are what most owned files were ripped from;
3. a release whose comment does not call it **clean**, censored, edited or amended
   (MusicBrainz has no flag for it; the comment only settles a choice, it is reported
   nowhere);
4. the **earliest** release date (a date without a day or month counts as the end of that
   month or year; no date is last): the original edition before reissues and deluxe ones;
5. the **most tracks**, among releases of one day;
6. the lowest MusicBrainz ID, so that the choice is the same every time.

The order is total and depends on nothing but the releases themselves: the same releases
give the same choice in every view and after every restart (what each view reads of a
group's releases is said where it reads them, in ``catalog``). Because digital releases come
first, the choice for a group that has an official digital release is known from its
official digital releases alone, which is what makes long discographies cheap to read.

A song (a MusicBrainz recording) is on many releases. It is shown on the first of them in
the same order, with one step added after the status (``home_order``): a studio album
before an EP, a single, and anything with a secondary type (compilation, live, soundtrack,
remix, ...). That release is the one its release group is shown with whenever that one
has the song.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# MusicBrainz's audio CD formats (its "CD" format and the formats below it).
CD_FORMATS = frozenset(
    {
        "CD",
        "CD-R",
        "8cm CD",
        "Blu-spec CD",
        "Blu-spec CD2",
        "Copy Control CD",
        "Enhanced CD",
        "HDCD",
        "HQCD",
        "Minimax CD",
        "Mixed Mode CD",
        "SHM-CD",
        "Hybrid SACD (CD layer)",
        "DualDisc (CD side)",
    }
)
DIGITAL = "Digital Media"
_STATUS = {"official": 0, "promotion": 1, "bootleg": 2, "withdrawn": 3}
_CLEAN = re.compile(r"\b(clean|censored|edited|amended)\b", re.IGNORECASE)
_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_NO_DATE = (9999, 99, 99)


@dataclass(frozen=True)
class Candidate:
    """What the choice looks at, of one release."""

    id: str  # the release's MusicBrainz ID
    status: str | None = None  # "Official", "Bootleg", ...
    formats: tuple[str | None, ...] = ()  # of its media, as the answer lists them
    date: str | None = None  # "YYYY", "YYYY-MM" or "YYYY-MM-DD"
    tracks: int = 0  # on all its media
    comment: str = ""  # MusicBrainz's disambiguation
    # Of its release group (``home_order`` only).
    primary: str | None = None
    secondary: tuple[str, ...] = ()


def status_rank(status: str | None) -> int:
    return _STATUS.get((status or "").strip().lower(), len(_STATUS))


def format_rank(formats: Iterable[str | None]) -> int:
    """0 digital, 1 CD, 2 anything else - also a set of mixed formats, and no media."""
    named = tuple(formats)
    if named and all(f == DIGITAL for f in named):
        return 0
    if named and all(f in CD_FORMATS for f in named):
        return 1
    return 2


def date_key(date: str | None) -> tuple[int, int, int]:
    match = _DATE.fullmatch(date or "")
    if match is None:
        return _NO_DATE
    year, month, day = match.groups()
    return int(year), int(month) if month else 99, int(day) if day else 99


def type_rank(primary: str | None, secondary: Iterable[str]) -> int:
    """0 a studio album, 1 an EP, 2 a single, 3 anything else."""
    if tuple(secondary):
        return 3
    return {"album": 0, "ep": 1, "single": 2}.get((primary or "").lower(), 3)


def _rest(release: Candidate) -> tuple[int, int, tuple[int, int, int], int, str]:
    return (
        format_rank(release.formats),
        1 if _CLEAN.search(release.comment) else 0,
        date_key(release.date),
        -release.tracks,
        release.id.lower(),
    )


def order(release: Candidate) -> tuple[int, tuple[int, int, tuple[int, int, int], int, str]]:
    return status_rank(release.status), _rest(release)


def home_order(
    release: Candidate,
) -> tuple[int, int, tuple[int, int, tuple[int, int, int], int, str]]:
    return (
        status_rank(release.status),
        type_rank(release.primary, release.secondary),
        _rest(release),
    )


def representative(releases: Iterable[Candidate]) -> Candidate:
    """The release a release group is shown with (the releases are one group's)."""
    return min(releases, key=order)


def home(releases: Iterable[Candidate]) -> Candidate:
    """The release a song is shown on, of those it is on."""
    return min(releases, key=home_order)
