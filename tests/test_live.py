"""One live check against the real services (opt-in, outside the default run):

    MUSICBRAINZ_LIVE_SEARCH="<a term that finds albums>" uv run pytest -m live -s

Shijhon's contract kit, asked of MusicBrainz itself at the adapter's own pace (one request
a second, about twenty requests in all). Timings and counts are printed - never names.
With LISTENBRAINZ_TOKEN set, top songs are asked for too.
"""

from __future__ import annotations

import os
import time

import pytest
from pydantic import SecretStr
from shijhon.catalog import contract
from shijhon.catalog.plugin import Context

from shijhon_catalog_musicbrainz import Settings
from shijhon_catalog_musicbrainz.catalog import build
from tests.replay import MISSING

pytestmark = [pytest.mark.anyio, pytest.mark.live]


async def test_musicbrainz_itself_keeps_the_contract() -> None:
    term = os.environ.get("MUSICBRAINZ_LIVE_SEARCH")
    if not term:
        pytest.skip("set MUSICBRAINZ_LIVE_SEARCH to a term that finds albums")
    token = os.environ.get("LISTENBRAINZ_TOKEN")
    settings = Settings(listenbrainz_token=SecretStr(token) if token else None)
    catalog = build(settings, Context(timeout_seconds=30.0))
    starts: list[float] = []

    async def note(request: object) -> None:
        starts.append(time.monotonic())

    catalog.http.event_hooks["request"].append(note)
    started = time.monotonic()
    try:
        await contract.check_catalog(catalog, contract.Sample(search=term, missing_id=MISSING))
    finally:
        await catalog.aclose()
    print(f"\nrequests by service: {catalog.requests}; {time.monotonic() - started:.1f}s in all")
    assert catalog.requests["musicbrainz"] >= 8
