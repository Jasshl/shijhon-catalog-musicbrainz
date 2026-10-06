from __future__ import annotations

from collections.abc import Iterator

import pytest

from shijhon_catalog_musicbrainz import catalog


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def own_paces() -> Iterator[None]:
    """Each test with its own shared paces (they are one per service in a process)."""
    catalog._paces.clear()
    yield
    catalog._paces.clear()
