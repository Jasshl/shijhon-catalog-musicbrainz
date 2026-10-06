"""Shijhon's adapter contract kit (``shijhon.catalog.contract``) against this adapter: its
declaration, and its catalog answering from the replayed fixtures."""

from __future__ import annotations

import pytest
from shijhon.catalog import contract

from shijhon_catalog_musicbrainz import adapter
from tests.replay import MISSING, Replay, fixture, short

pytestmark = pytest.mark.anyio


def test_the_declaration_keeps_the_contract() -> None:
    contract.check_declaration("musicbrainz", adapter)


@pytest.mark.parametrize("limit", [25, 5, 1])
async def test_the_catalog_keeps_the_contract(limit: int) -> None:
    replay = Replay()
    catalog = replay.catalog()
    sample = contract.Sample(
        search=str(fixture("search-artists")["params"]["query"]),
        album=short(fixture("album")["body"]["id"]),  # the album recorded with its tracks
        missing_id=MISSING,
        limit=limit,
    )
    await contract.check_catalog(catalog, sample)
    assert replay.count("covers") == 1  # one cover, from the catalog's image host only
    assert replay.count("listenbrainz") == 2  # the artist's top songs, and nobody's
    assert not any(line.startswith("elsewhere") for line in replay.log)
    await catalog.aclose()


async def test_the_contract_with_the_default_missing_id_and_without_covers_or_a_token() -> None:
    """An ID that is none of the catalog's ("0"), covers off, no ListenBrainz token."""
    replay = Replay()
    catalog = replay.catalog(covers=False, listenbrainz_token=None)
    sample = contract.Sample(
        search=str(fixture("search-artists")["params"]["query"]),
        album=short(fixture("album")["body"]["id"]),
        artwork=False,
    )
    await contract.check_catalog(catalog, sample)
    assert replay.count("covers") == 0 and replay.count("listenbrainz") == 0
