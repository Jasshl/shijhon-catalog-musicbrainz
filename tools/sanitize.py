"""Make fixtures of recorded answers: names, IDs and codes are replaced, the structure stays.

    uv run python -m tools.sanitize RAW_DIR [RAW_DIR ...] --manifest FILE --key FILE

- A MusicBrainz ID becomes another ID of the same shape, an ISRC or a barcode another code,
  a name, title or label an invented one. The key file keeps the replacements consistent:
  in one run the same original gets the same invention, so answers still refer to each
  other (make fixtures that refer to each other in one run: an invented name also depends
  on the names made before it). The key and the raw answers stay outside the repository.
- Years are moved by a fixed amount and lengths by up to a second.
- What is kept may still identify the records an answer came from: which releases a group
  has, their statuses, formats, countries and track counts, the days and months of their
  dates, lengths to within a second.
- Fields the adapter does not read that describe real people or places (areas, aliases,
  tags, release events and the others in ``DROP``) are dropped, and so are comments that
  are not plain edition words.
- Any text that is not known to be structure is treated as a name and replaced. What is
  kept as it is - a status, a type, a format, a country code, a track number, the words
  between credited artists, an error's text - is kept only when it is one of the values
  this tool lists for it.
- What the tool does not know stops it, and nothing is written rather than something
  unchecked: a field it has not seen, a number in a field it keeps no numbers of, a value
  that is none of a field's listed ones, a request other than the adapter's own (its
  address, its parameters, the shape of its search query).

The manifest (JSON, kept with the raw answers) names the fixtures:
``{"<raw record's name>": "<fixture name>"}``; a fixture name ending in ``+`` appends that
record's list to the fixture of that name (a second page). Records it does not name are
left out. MusicBrainz's placeholder artists ("Various Artists", "[unknown]") keep their IDs
and names.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from shijhon_catalog_musicbrainz.catalog import (
    BROWSE_INC,
    OFFICIAL_DIGITAL,
    RELEASE_INC,
    RELEASE_TYPES,
)
from shijhon_catalog_musicbrainz.choose import CD_FORMATS

REPOSITORY = Path(__file__).resolve().parent.parent
FIXTURES = REPOSITORY / "tests" / "fixtures"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_ISRC = re.compile(r"[A-Z0-9]{12}")
_UNESCAPED_COLON = re.compile(r"(?<!\\):")
_ID = _UUID.pattern
# The requests the adapter makes: nothing else becomes a fixture.
_SIZE = r"(?:250|500|1200)"
_ADDRESSES = {
    "musicbrainz.org": re.compile(rf"/ws/2/(artist|release|recording|release-group)(/{_ID})?"),
    "api.listenbrainz.org": re.compile(rf"/1/popularity/top-recordings-for-artist/{_ID}"),
    "coverartarchive.org": re.compile(rf"/release-group/{_ID}/front-{_SIZE}"),
    "archive.org": re.compile(rf"/download/mbid-{_ID}/mbid-{_ID}-(?P<image>\d+)_thumb{_SIZE}\.jpg"),
}
# The adapter's own parameters, with the values it sends.
_PARAMETERS: dict[str, re.Pattern[str]] = {
    "fmt": re.compile(r"json"),
    "limit": re.compile(r"[1-9]\d?|100"),
    "offset": re.compile(r"\d{1,4}"),
    "inc": re.compile("|".join(re.escape(inc) for inc in (RELEASE_INC, BROWSE_INC))),
    "type": re.compile(re.escape(RELEASE_TYPES)),
    "status": re.compile(r"official"),
    "release-group-status": re.compile(r"website-default"),
    "artist": re.compile(_ID),
    "track": re.compile(_ID),
}
_IDS = rf"\({_ID}(?: OR {_ID})*\)"
_ERRORS = {
    "Not Found",
    "Invalid mbid.",
    "The MusicBrainz web server is currently busy. Please try again later.",
    "Due to bad actors and AI scrapers causing undue traffic on our sites, you need to provide"
    " an Auth token for this endpoint. Sorry for this mess.",
    "For usage, please see: https://musicbrainz.org/development/mmd",
}
# The values kept as they are, field by field: MusicBrainz's own lists. Another value
# stops the tool (add it here once it is known to be one of MusicBrainz's).
_LISTED: dict[str, frozenset[str]] = {
    "status": frozenset(
        {"Official", "Promotion", "Bootleg", "Pseudo-Release", "Withdrawn", "Cancelled"}
    ),
    "primary-type": frozenset({"Album", "Single", "EP", "Broadcast", "Other"}),
    "secondary-types": frozenset(
        {
            "Compilation",
            "Soundtrack",
            "Spokenword",
            "Interview",
            "Audiobook",
            "Audio drama",
            "Live",
            "Remix",
            "DJ-mix",
            "Mixtape/Street",
            "Demo",
            "Field recording",
        }
    ),
    "format": CD_FORMATS
    | {
        "Digital Media",
        "Vinyl",
        '7" Vinyl',
        '10" Vinyl',
        '12" Vinyl',
        "Cassette",
        "DVD",
        "DVD-Video",
        "DVD-Audio",
        "Blu-ray",
        "SACD",
        "Hybrid SACD",
        "MiniDisc",
        "USB Flash Drive",
        "Flexi-disc",
        "Shellac",
        "VHS",
        "Other",
    },
    "type": frozenset(
        {
            "Person",
            "Group",
            "Orchestra",
            "Choir",
            "Character",
            "Other",
            "Original Production",
            "Production",
            "Bootleg Production",
            "Reissue Production",
            "Distributor",
            "Holding",
            "Rights Society",
            "Imprint",
            "Publisher",
            "Manufacturer",
        }
    ),
}
_SHAPED = {
    "country": re.compile(r"[A-Z]{2}"),  # an ISO code
    "number": re.compile(r"[A-Z]{0,2}\d{0,3}"),  # a track's number as printed: "7", "B2"
    "joinphrase": re.compile(
        r"(?:\s|[,&/+x-]|and|with|feat\.?|featuring|ft\.?|vs\.?|presents|pres\.|meets)*", re.I
    ),
}
_DATE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")
# Every field of an answer the tool knows (besides those it drops), and those whose
# numbers are kept: counts, places and scores.
FIELDS = frozenset(
    [
        "artist",
        "artist-credit",
        "artist_mbids",
        "artist_name",
        "artists",
        "artwork",
        "back",
        "barcode",
        "blue",
        "caa_id",
        "caa_release_mbid",
        "code",
        "count",
        "country",
        "cover-art-archive",
        "created",
        "darkened",
        "date",
        "disambiguation",
        "disc-count",
        "error",
        "first-release-date",
        "format",
        "front",
        "green",
        "help",
        "id",
        "isrcs",
        "joinphrase",
        "label",
        "label-info",
        "length",
        "media",
        "name",
        "number",
        "offset",
        "position",
        "pregap",
        "primary-type",
        "recording",
        "recording_mbid",
        "recording_name",
        "recordings",
        "red",
        "release-count",
        "release-group",
        "release-group-count",
        "release-group-offset",
        "release-groups",
        "release-offset",
        "release_color",
        "release_mbid",
        "release_name",
        "releases",
        "score",
        "secondary-types",
        "status",
        "title",
        "total_listen_count",
        "total_user_count",
        "track",
        "track-count",
        "track-offset",
        "tracks",
        "type",
        "video",
    ]
)
NUMBERS = frozenset(
    [
        "blue",
        "code",
        "count",
        "disc-count",
        "green",
        "offset",
        "position",
        "red",
        "release-count",
        "release-group-count",
        "release-group-offset",
        "release-offset",
        "score",
        "total_listen_count",
        "total_user_count",
        "track-count",
        "track-offset",
    ]
)
# MusicBrainz's own placeholder artists keep their IDs and names.
KEEP_IDS = {
    "89ad4ac3-39f7-470e-963a-56509c546377": "Various Artists",
    "125ec42a-7229-4250-afc5-e057484327fe": "[unknown]",
}
KEEP_NAMES = {name.lower() for name in KEEP_IDS.values()}
DROP = {
    "aliases",
    "annotation",
    "area",
    "artist-credit-id",
    "asin",
    "begin-area",
    "catalog-number",
    "end-area",
    "format-id",
    "gender",
    "gender-id",
    "genres",
    "ipis",
    "isnis",
    "label-code",
    "life-span",
    "packaging",
    "packaging-id",
    "primary-type-id",
    "quality",
    "release-events",
    "secondary-type-ids",
    "sort-name",
    "status-id",
    "tags",
    "text-representation",
    "type-id",
}
DATES = {"date", "first-release-date"}
LENGTHS = {"length"}
# A comment that is only edition words stays (the adapter's choice reads "clean").
_PLAIN_COMMENT = re.compile(
    r"(?:(?:deluxe|clean|explicit|censored|edited|amended|remaster(?:ed)?|bonus|edition|"
    r"version|tracks?|mono|stereo|digital|expanded|anniversary|\d{2,4}(?:th)?|bit|hi-res)\W*)+",
    re.IGNORECASE,
)
ADJECTIVES = [
    "amber",
    "ashen",
    "bright",
    "broken",
    "dappled",
    "cedar",
    "clear",
    "cobalt",
    "copper",
    "crimson",
    "distant",
    "early",
    "eastern",
    "faded",
    "gentle",
    "gilded",
    "glass",
    "golden",
    "hollow",
    "idle",
    "ivory",
    "late",
    "lunar",
    "mellow",
    "misty",
    "narrow",
    "northern",
    "open",
    "pale",
    "paper",
    "plain",
    "quiet",
    "rapid",
    "ragged",
    "rustic",
    "silent",
    "silver",
    "slow",
    "small",
    "southern",
    "still",
    "sudden",
    "tawny",
    "velvet",
    "violet",
    "warm",
    "western",
    "wild",
    "winter",
    "wooden",
    "young",
]
NOUNS = [
    "alcove",
    "alley",
    "atlas",
    "avenue",
    "beacon",
    "byway",
    "causeway",
    "cellar",
    "chapel",
    "circuit",
    "cliff",
    "copse",
    "corner",
    "current",
    "dale",
    "engine",
    "estuary",
    "fable",
    "ferry",
    "field",
    "garden",
    "harbor",
    "hollow",
    "inlet",
    "junction",
    "lantern",
    "ledger",
    "letter",
    "lighthouse",
    "meadow",
    "mirror",
    "morning",
    "needle",
    "overpass",
    "paddock",
    "parade",
    "pavilion",
    "pier",
    "quarry",
    "radio",
    "railway",
    "rampart",
    "river",
    "satellite",
    "season",
    "shingle",
    "sluice",
    "station",
    "sandbar",
    "theater",
    "thicket",
    "tunnel",
    "valley",
    "voyage",
    "wharf",
    "willow",
    "window",
]


class Sanitizer:
    def __init__(self, key: bytes) -> None:
        self.key = key
        self.names: dict[str, str] = {}
        self.used: set[str] = set()
        self.years = 1 + self._number("years") % 3  # every date moves this many years back

    def _digest(self, *parts: str) -> bytes:
        return hmac.new(self.key, "\x00".join(parts).encode(), hashlib.sha256).digest()

    def _number(self, *parts: str) -> int:
        return int.from_bytes(self._digest(*parts)[:8], "big")

    def uuid(self, value: str) -> str:
        value = value.lower()
        if value in KEEP_IDS:
            return value
        h = self._digest("id", value).hex()
        made = f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"
        return made if made != value else _other(made, "0123456789abcdef")

    def isrc(self, value: str) -> str:
        n = self._number("isrc", value.upper())
        letters = "".join(chr(65 + (n >> (5 * i)) % 26) for i in range(3))
        made = f"ZZ{letters}{n % 10**7:07d}"
        return made if made != value.upper() else _other(made, "0123456789")

    def digits(self, value: str) -> str:
        """As many digits, and never the same ones."""
        made = "".join(str(self._number("digits", value, str(i)) % 10) for i in range(len(value)))
        return made if made != value else _other(made, "0123456789")

    def name(self, value: str) -> str:
        """An invented name with about as many words as the original."""
        known = value.strip().lower()
        if not known or known in KEEP_NAMES:
            return value
        if known in self.names:
            return self.names[known]
        words = max(2, min(4, len(known.split())))
        invented = ""
        for attempt in range(1000):
            n = self._number("name", known, str(attempt))
            picked = [ADJECTIVES[n % len(ADJECTIVES)]]
            for i in range(1, words):
                picked.append(NOUNS[(n >> (11 * i)) % len(NOUNS)])
            invented = " ".join(picked).title()
            if self._free(invented, known):
                break
        number = len(self.used)
        while not self._free(invented, known):  # the words ran out: numbered ones
            number += 1
            invented = f"Untitled {number}"
        self.used.add(invented)
        self.names[known] = invented
        return invented

    def _free(self, invented: str, known: str) -> bool:
        """An invention nobody has yet, and not the original itself."""
        return bool(invented) and invented not in self.used and invented.lower() != known

    def date(self, value: str) -> str:
        """A date moved by the tool's years; anything that is no date becomes none."""
        if not _DATE.fullmatch(value):
            return ""
        return f"{int(value[:4]) - self.years}{value[4:]}"

    def length(self, value: int) -> int:
        return max(1000, value + self._number("length", str(value)) % 1999 - 999)

    def text(self, key: str | None, value: str) -> str:
        if _UUID.fullmatch(value):
            return self.uuid(value)
        if key in ("error", "help"):
            return value if value in _ERRORS else "an error"
        if key in _LISTED:
            if value not in _LISTED[key]:
                raise SystemExit(f"a value this tool does not list for {key!r}: nothing written")
            return value
        if key in _SHAPED:
            if _SHAPED[key].fullmatch(value):
                return value
            return {"country": "XW", "number": "1", "joinphrase": " & "}[key]
        if key in DATES:
            return self.date(value)
        if key == "isrcs":
            return self.isrc(value)
        if key == "barcode":
            return self.digits(value)
        if key == "created":
            return "2026-01-01T00:00:00.000Z"
        if key == "disambiguation":
            return value if _PLAIN_COMMENT.fullmatch(value) else ""
        return self.name(value)

    def walk(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            unknown = sorted(set(value) - FIELDS - DROP)
            if unknown:
                raise SystemExit(
                    f"fields this tool does not know ({len(unknown)}): nothing written"
                )
            return {k: self.walk(v, k) for k, v in value.items() if k not in DROP}
        if isinstance(value, list):
            # (A list's items are read under the list's key: "isrcs", "secondary-types", ...)
            return [self.walk(v, key) for v in value]
        if isinstance(value, str):
            return self.text(key, value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int | float):
            if key in LENGTHS:
                return self.length(int(value))
            if key == "caa_id":
                return self._number("caa", str(value)) % 10**11
            if key not in NUMBERS:
                raise SystemExit(f"a number in {key!r}, which keeps none: nothing written")
            return value
        raise SystemExit("a value of a kind this tool does not know: nothing written")

    def query(self, value: str) -> str:
        """A search query, in one of the shapes the adapter sends: its IDs and ISRCs mapped,
        its free words as their invented name. Any other shape is refused."""
        mapped = _UUID.sub(lambda m: self.uuid(m.group()), value)
        filtered = f" AND {OFFICIAL_DIGITAL}"
        if not _UNESCAPED_COLON.search(value):  # plain words
            return self.name(value).lower()
        if re.fullmatch(rf"(arid:{_ID}|rgid:{_IDS}){re.escape(filtered)}", value, re.I):
            return mapped
        if re.fullmatch(rf"rid:{_IDS}", value, re.I):
            return mapped
        if (code := re.fullmatch(r"isrc:([A-Z0-9]{12})", value)) is not None:
            return f"isrc:{self.isrc(code.group(1))}"
        if value.startswith("(") and value.endswith(")" + filtered):
            words = value[1 : -len(")" + filtered)]
            if not _UNESCAPED_COLON.search(words):
                return f"({self.name(words).lower()}){filtered}"
        raise SystemExit("a search query of a shape this tool does not know: nothing written")

    def address(self, value: str) -> str:
        """One of the adapter's addresses, its IDs mapped; any other is refused."""
        parts = urlsplit(value)
        known = _ADDRESSES.get(parts.hostname or "")
        plain = parts.scheme == "https" and parts.netloc == parts.hostname
        if known is None or not plain or parts.query or parts.fragment:
            raise SystemExit("an address this tool does not know: nothing written")
        found = known.fullmatch(parts.path)
        if found is None:
            raise SystemExit("an address this tool does not know: nothing written")
        path = parts.path
        if "image" in found.groupdict():  # the image's number at the archive
            start, end = found.span("image")
            path = path[:start] + self.digits(found.group("image")) + path[end:]
        return f"https://{parts.hostname}" + _UUID.sub(lambda m: self.uuid(m.group()), path)

    def parameter(self, key: str, value: str) -> str:
        if key == "query":
            return self.query(value)
        if key not in _PARAMETERS or not _PARAMETERS[key].fullmatch(value):
            raise SystemExit("a request parameter this tool does not know: nothing written")
        return _UUID.sub(lambda m: self.uuid(m.group()), value)

    def record(self, raw: dict[str, Any], name: str) -> dict[str, Any]:
        params = {k: self.parameter(k, str(v)) for k, v in raw.get("params", {}).items()}
        record: dict[str, Any] = {
            "name": name,
            "url": self.address(raw["url"]),
            "params": params,
            "status": raw["status"],
        }
        headers = {}
        for key, value in raw.get("headers", {}).items():
            if key.lower() == "location":
                headers["location"] = self.address(value)
            elif key.lower() == "retry-after" and re.fullmatch(r"\d{1,6}", value):
                headers["retry-after"] = value
        if headers:
            record["headers"] = headers
        if raw.get("constructed"):
            record["constructed"] = True
        record["body"] = self.walk(raw.get("body"))
        return record


def _other(made: str, alphabet: str) -> str:
    """``made`` with its last character changed: an invention that came out as the
    original (it can, by chance) is still none."""
    last = alphabet[(alphabet.index(made[-1]) + 1) % len(alphabet)] if made else ""
    return made[:-1] + last


def _lists(body: Any) -> list[list[Any]]:
    return [v for v in body.values() if isinstance(v, list)] if isinstance(body, dict) else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("raw", nargs="+", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    for path in (*args.raw, args.manifest, args.key):
        resolved = path.resolve()
        if resolved == REPOSITORY or REPOSITORY in resolved.parents:
            raise SystemExit("raw answers, the manifest and the key stay outside this repository")
    if not args.key.exists():
        args.key.write_bytes(os.urandom(32))
    sanitizer = Sanitizer(args.key.read_bytes())
    manifest: dict[str, str] = json.loads(args.manifest.read_text())
    raw = {
        record["name"]: record
        for folder in args.raw
        for path in sorted(folder.glob("*.json"))
        if isinstance(record := json.loads(path.read_text()), dict) and "name" in record
    }
    fixtures: dict[str, dict[str, Any]] = {}
    for source, name in manifest.items():  # in the manifest's order: pages after their first
        record = sanitizer.record(raw[source], name.rstrip("+"))
        if name.endswith("+"):
            first = fixtures[name.rstrip("+")]
            for kept, more in zip(_lists(first["body"]), _lists(record["body"]), strict=True):
                kept += more
        else:
            fixtures[name] = record
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name, record in fixtures.items():
        text = json.dumps(record, indent=1, ensure_ascii=False) + "\n"
        (FIXTURES / f"{name}.json").write_text(text)
        print(f"{name}: {len(text) // 1024} KiB")


if __name__ == "__main__":
    main()
