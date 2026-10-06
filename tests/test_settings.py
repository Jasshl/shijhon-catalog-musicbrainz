"""The adapter's declaration in Shijhon (``shijhon.catalog.plugin``): found as
``kind = "musicbrainz"``, its settings read from the configuration, and the catalog built
from them with Shijhon's own HTTP client."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from shijhon.catalog import plugin
from shijhon.catalog.base import scope
from shijhon.catalog.plugin import Context
from shijhon.catalog.setup import build_catalog
from shijhon.config import CatalogSettings, load_settings
from shijhon.delivery.netpolicy import USER_AGENT

from shijhon_catalog_musicbrainz import MusicBrainzCatalog, Settings, adapter
from shijhon_catalog_musicbrainz.catalog import build, problem

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.upper().startswith("SHIJHON_"):
            monkeypatch.delenv(name)
    yield


def test_the_adapter_is_installed_as_musicbrainz() -> None:
    assert plugin.adapter("musicbrainz") is adapter
    assert plugin.validate("musicbrainz", adapter) is adapter
    assert adapter.label == "MusicBrainz" and adapter.settings is Settings
    assert set(adapter.words) == set(Settings.model_fields)


async def test_nothing_needs_to_be_set() -> None:
    """``kind = "musicbrainz"`` alone: MusicBrainz itself, covers on, no key."""
    settings: Any = load_settings(None, catalog={"kind": "musicbrainz"}).catalog
    assert isinstance(settings, CatalogSettings) and isinstance(settings, Settings)
    assert (settings.server, settings.covers, settings.top_songs) == (
        "https://musicbrainz.org", True, True,
    )  # fmt: skip
    assert settings.listenbrainz_token is None and settings.mirror_requests_per_second == 1.0
    assert problem(settings) is None
    built = build_catalog(settings)
    assert isinstance(built, MusicBrainzCatalog)
    assert (built.key, built.region, scope(built)) == ("musicbrainz", "", "musicbrainz")
    assert built.server == "https://musicbrainz.org" and built.covers and built._token is None
    assert built._paces["musicbrainz"].interval == 1.0
    # Shijhon's own client: it names the application and where to read about it.
    assert built.http.headers["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("Shijhon/") and "(+https://" in USER_AGENT
    await built.aclose()


async def test_the_configuration_file(tmp_path: Path) -> None:
    config = tmp_path / "shijhon.toml"
    config.write_text(
        "[catalog]\n"
        'kind = "musicbrainz"\n'
        'server = "https://mirror.example.net/"\n'
        "mirror_requests_per_second = 20\n"
        "covers = false\n"
        "top_songs = true\n"
        'listenbrainz_token = "user-token-SECRET"\n'
        "cache_seconds = 600\n"
    )
    settings = load_settings(config)
    catalog: Any = settings.catalog
    assert isinstance(catalog, Settings) and catalog.cache_seconds == 600  # Shijhon's own
    assert isinstance(catalog.listenbrainz_token, SecretStr)
    assert "SECRET" not in repr(settings) and "SECRET" not in str(catalog)
    built = build_catalog(catalog)
    assert isinstance(built, MusicBrainzCatalog)
    assert built.server == "https://mirror.example.net" and not built.covers
    assert built._token == "user-token-SECRET" and built.top
    assert built._paces["musicbrainz"].interval == pytest.approx(0.05)
    assert built.key == "musicbrainz"  # a mirror is the same catalog: the same IDs
    await built.aclose()


async def test_the_mirror_s_rate_never_applies_to_musicbrainz_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(mirror_requests_per_second=50)
    with caplog.at_level(logging.WARNING):
        built = build(settings, Context())
    assert built._paces["musicbrainz"].interval == 1.0
    assert "one request a second" in caplog.text
    await built.aclose()


def test_settings_that_cannot_be() -> None:
    for wrong in ("mirror.example.net", "ftp://example.net", "https://user:pw@example.net", ""):
        found = problem(Settings(server=wrong))
        assert found is not None and found.setting == "server"
        assert wrong == "" or wrong not in found.row + found.notice  # never the value
        with pytest.raises(ValueError, match="plain http") as refused:
            build(Settings(server=wrong), Context())
        assert wrong == "" or wrong not in str(refused.value)
    for rate in (0, -1, 1000):
        with pytest.raises(ValidationError):
            Settings(mirror_requests_per_second=rate)


async def test_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHIJHON_CATALOG__KIND", "musicbrainz")
    monkeypatch.setenv("SHIJHON_CATALOG__COVERS", "false")
    monkeypatch.setenv("SHIJHON_CATALOG__LISTENBRAINZ_TOKEN", "from-the-environment")
    catalog: Any = load_settings(None).catalog
    assert catalog.kind == "musicbrainz" and catalog.covers is False
    assert catalog.listenbrainz_token.get_secret_value() == "from-the-environment"
