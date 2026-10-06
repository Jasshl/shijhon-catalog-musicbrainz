"""The fixtures' sanitizer (``tools/sanitize.py``): the names, IDs and codes of a recorded
answer are replaced, and a request it does not know stops it."""

from __future__ import annotations

import json
import re

import pytest
from tools.sanitize import Sanitizer

from shijhon_catalog_musicbrainz.catalog import OFFICIAL_DIGITAL, RELEASE_INC, lucene
from tests.replay import FIXTURES

# Invented, in the shape of MusicBrainz's IDs: they stand for a recorded answer's own.
AN_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
ANOTHER_ID = "9f8e7d6c-5b4a-4f3e-9d2c-1b0a9f8e7d6c"
NAME = "Zzyzx Qwertz"  # stands for a recorded name: must not survive


def sanitizer() -> Sanitizer:
    return Sanitizer(b"k" * 32)


def record(url: str, params: dict[str, str], body: object, **more: object) -> dict[str, object]:
    return {"name": "raw", "url": url, "params": params, "status": 200, "body": body, **more}


def test_names_ids_and_codes_are_replaced_and_structure_stays() -> None:
    body = {
        "id": AN_ID,
        "title": NAME,
        "status": "Official",
        "date": "2019-03-08",
        "barcode": "0123456789012",
        "disambiguation": f"with {NAME}",
        "artist-credit": [
            {"name": NAME, "joinphrase": f" and the {NAME} band ", "artist": {"id": ANOTHER_ID}},
            {"name": "Someone", "joinphrase": " feat. ", "artist": {"id": ANOTHER_ID}},
        ],
        "media": [
            {
                "format": "Digital Media",
                "track-count": 1,
                "tracks": [
                    {
                        "id": ANOTHER_ID,
                        "number": "A1",
                        "title": NAME,
                        "length": 200000,
                        "recording": {"isrcs": ["USAAA1900001"], "title": NAME},
                    }
                ],
            }
        ],
        "release-group": {
            "id": AN_ID,
            "primary-type": "Album",
            "secondary-types": ["Live"],
            "first-release-date": f"2018-11-02 {NAME}",
        },
        "release-events": [{"area": {"name": NAME}}],
        "label-info": [{"catalog-number": NAME, "label": {"name": NAME, "type": "Production"}}],
        "country": NAME,
        "error": f"No such release: {NAME} {AN_ID}",
    }
    made = sanitizer().record(
        record(
            f"https://musicbrainz.org/ws/2/release/{AN_ID}",
            {"inc": RELEASE_INC, "fmt": "json"},
            body,
        ),
        "album",
    )
    text = json.dumps(made)
    for original in (
        NAME, "Zzyzx", AN_ID, ANOTHER_ID, "USAAA1900001", "0123456789012", "2019", "200000",
    ):  # fmt: skip
        assert original not in text
    out = made["body"]
    assert out["status"] == "Official" and out["media"][0]["format"] == "Digital Media"
    assert out["media"][0]["tracks"][0]["number"] == "A1" and out["date"].endswith("-03-08")
    assert out["artist-credit"][0]["joinphrase"] == " & "  # words that could name someone
    assert out["artist-credit"][1]["joinphrase"] == " feat. "
    assert out["release-group"]["secondary-types"] == ["Live"] and out["error"] == "an error"
    assert out["release-group"]["first-release-date"] == "" and out["country"] == "XW"
    assert out["id"] == out["release-group"]["id"] != AN_ID  # the same ID, the same invention
    assert out["title"] == out["media"][0]["tracks"][0]["title"]  # the same name too
    assert "release-events" not in out and len(out["barcode"]) == 13
    assert made["url"].startswith("https://musicbrainz.org/ws/2/release/")


def test_a_search_term_never_survives_whatever_it_holds() -> None:
    made = sanitizer()
    for term in ("Zzyzx: Qwertz", "zzyzx (qwertz)", 'zzyzx "qwertz" AND x', "zzyzx/qwertz"):
        words = lucene(term)
        for query in (words, f"({words}) AND {OFFICIAL_DIGITAL}"):
            out = made.query(query)
            assert "zzyzx" not in out and "qwertz" not in out
            assert out.endswith(OFFICIAL_DIGITAL) == query.endswith(OFFICIAL_DIGITAL)
    assert AN_ID not in made.query(f"arid:{AN_ID} AND {OFFICIAL_DIGITAL}")
    assert AN_ID not in made.query(f"rid:({AN_ID} OR {ANOTHER_ID})")
    assert made.query("isrc:USAAA1900001").startswith("isrc:ZZ")


def test_what_the_tool_does_not_know_stops_it() -> None:
    made = sanitizer()
    for query in (
        f"artist:{NAME}",
        f"arid:{AN_ID} AND status:bootleg",
        f"({NAME}) AND artist:{NAME} AND {OFFICIAL_DIGITAL}",
        f"rid:({AN_ID} OR {NAME})",
    ):
        with pytest.raises(SystemExit):
            made.query(query)
    for url in (
        f"https://musicbrainz.org/ws/2/artist/{NAME}",
        f"https://user:secret@musicbrainz.org/ws/2/release/{AN_ID}",
        f"https://musicbrainz.org/ws/2/release/{AN_ID}?token=secret",
        f"https://example.net/ws/2/release/{AN_ID}",
        f"http://musicbrainz.org/ws/2/release/{AN_ID}",
        f"https://archive.org/download/{NAME}.jpg",
    ):
        with pytest.raises(SystemExit):
            made.record(record(url, {}, {}), "x")
    good = f"https://musicbrainz.org/ws/2/release/{AN_ID}"
    for params in (
        {"token": "secret"},
        {"artist": NAME},
        {"inc": f"x&name={NAME}"},
        {"inc": "zzyzx"},  # no parameter of the adapter's
        {"type": "zzyzx"},
        {"status": "zzyzx"},
        {"limit": NAME},
    ):
        with pytest.raises(SystemExit):
            made.record(record(good, params, {}), "x")
    with pytest.raises(SystemExit):
        made.record(record(good, {}, {}, headers={"Location": f"https://example.net/{NAME}"}), "x")


def test_a_value_that_is_none_of_a_field_s_own_stops_the_tool() -> None:
    """A status, a type or a format is kept only when it is one of MusicBrainz's; a field
    the tool has not seen, or a number where it keeps none, could be anything."""
    good = f"https://musicbrainz.org/ws/2/release/{AN_ID}"
    for body in (
        {"status": NAME},
        {"status": "Alice"},
        {"media": [{"format": NAME}]},
        {"release-group": {"primary-type": NAME}},
        {"release-group": {"secondary-types": ["Live", NAME]}},
        {"label-info": [{"label": {"type": NAME}}]},
        {"unheard-of": NAME},
        {"unheard-of": 5},
        {NAME: "x"},
        {"title": 12345},  # an identifier, for all the tool knows
        {"id": 12345},
        {"media": [{"tracks": [{"recording": {"isrcs": [12345]}}]}]},
    ):
        with pytest.raises(SystemExit):
            sanitizer().record(record(good, {}, body), "x")
    kept = sanitizer().record(
        record(good, {}, {"track-count": 12, "score": 100, "video": False, "length": None}), "x"
    )
    assert kept["body"] == {"track-count": 12, "score": 100, "video": False, "length": None}
    made = sanitizer().record(
        record(
            "https://coverartarchive.org/release-group/" + AN_ID + "/front-500",
            {},
            None,
            headers={
                "location": f"https://archive.org/download/mbid-{AN_ID}/mbid-{AN_ID}-123_thumb500.jpg"
            },
        ),
        "cover",
    )
    assert AN_ID not in json.dumps(made) and "-123_" not in made["headers"]["location"]
    # Whatever the key: an invention is never the original (a single digit could come out
    # the same by chance).
    for key in range(40):
        other = Sanitizer(bytes([key]) * 32)
        for image in ("1", "7", "12", "38427631140"):
            assert other.digits(image) != image and len(other.digits(image)) == len(image)
        assert other.uuid(AN_ID) != AN_ID and other.isrc("USAAA1900001") != "USAAA1900001"
        assert other.name("Amber Fable").lower() != "amber fable"
    # ... also when the words to invent names from have run out.
    crowded = Sanitizer(bytes([2]) * 32)
    made_up = {crowded.name(f"source {number}") for number in range(4000)}
    assert len(made_up) == 4000
    for original in ("Cedar Cliff", "Amber Fable", "Untitled 4001", "untitled 4002"):
        invented = crowded.name(original)
        assert invented.lower() != original.lower() and invented not in made_up
        assert crowded.name(original) == invented  # and the same again
    # An address in any other spelling than the adapter's own is none the tool knows.
    for spelled in (
        f"https://archive.org/download/mbid-{AN_ID}/mbid-{AN_ID}-12345678901_THUMB500.JPG",
        f"https://archive.org/download/mbid-{AN_ID}/mbid-{AN_ID}-123_thumb999.jpg",
        f"https://ARCHIVE.org/download/mbid-{AN_ID}/mbid-{AN_ID}-123_thumb500.jpg",
        f"https://coverartarchive.org/release-group/{AN_ID}/front-12345678901",
        f"https://coverartarchive.org/release-group/{AN_ID.upper()}/front-500",
    ):
        with pytest.raises(SystemExit):
            sanitizer().address(spelled)
    for params in (
        {"limit": "99999"},
        {"limit": "0"},
        {"offset": "123456"},
        {"artist": AN_ID.upper()},
    ):
        with pytest.raises(SystemExit):
            sanitizer().record(record(good, params, {}), "x")


_STRUCTURE = {
    "status", "primary-type", "secondary-types", "format", "type", "country", "number",
    "joinphrase", "error", "help", "created", "disambiguation", "date", "first-release-date",
}  # fmt: skip


_IDENTIFIER = re.compile(r"[0-9a-f]{8}-[0-9a-f-]{27}|ZZ[A-Z]{3}\d{7}|(?<=-)\d{6,}(?=_thumb)")


def _texts(value: object, key: str | None = None) -> list[tuple[str | None, str]]:
    if isinstance(value, dict):
        return [found for k, v in value.items() for found in _texts(v, k)]
    if isinstance(value, list):
        return [found for v in value for found in _texts(v, key)]
    return [(key, value)] if isinstance(value, str) and value else []


def test_the_fixtures_hold_nothing_but_inventions_and_structure() -> None:
    """Every fixture again through the tool, with another key: its requests are ones the
    tool knows, and every text that comes out the same is structure - a status, a format,
    a date's shape - or MusicBrainz's own placeholder artist. Whatever names something is
    replaced again."""
    made = sanitizer()
    seen = 0
    for path in sorted(FIXTURES.glob("*.json")):
        raw = json.loads(path.read_text())
        again = made.record(raw, raw["name"])
        assert again["status"] == raw["status"] and set(again["params"]) == set(raw["params"])
        # The request: every ID, ISRC and image number in its address, its parameters and
        # its headers is replaced again; the search's words too.
        asked = json.dumps([raw["url"], raw["params"], raw.get("headers")])
        asked_again = json.dumps([again["url"], again["params"], again.get("headers")])
        for found in _IDENTIFIER.findall(asked):
            assert found not in asked_again or found == "89ad4ac3-39f7-470e-963a-56509c546377"
        if "query" in raw["params"] and "rid:" not in raw["params"]["query"]:
            assert raw["params"]["query"] != again["params"]["query"]
        before, after = _texts(raw["body"]), _texts(again["body"])
        assert [key for key, _ in before] == [key for key, _ in after]
        for (key, old), (_, new) in zip(before, after, strict=True):
            seen += 1
            if key in _STRUCTURE:
                continue
            assert old != new or old in ("Various Artists", "89ad4ac3-39f7-470e-963a-56509c546377")
    assert seen > 5000
