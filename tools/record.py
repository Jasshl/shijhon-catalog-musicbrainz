"""Record real answers for new fixtures: the adapter's own requests, at its own pace (one a
second) and with Shijhon's User-Agent.

    uv run python -m tools.record RAW_DIR search "a term"
    uv run python -m tools.record RAW_DIR album|song|artist|releases|top ID
    uv run python -m tools.record RAW_DIR isrc CODE
    uv run python -m tools.record RAW_DIR check

RAW_DIR must lie outside this repository: raw answers hold real names and are never
committed. ``tools/sanitize.py`` makes fixtures of them. Keep it small - a recording is a
handful of requests, not a crawl. ``top`` needs LISTENBRAINZ_TOKEN in the environment.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx
from pydantic import SecretStr
from shijhon.catalog.plugin import Context

from shijhon_catalog_musicbrainz.catalog import Settings, build

REPOSITORY = Path(__file__).resolve().parent.parent
_KEPT_HEADERS = ("content-type", "location", "retry-after")


def outside_repository(path: Path) -> Path:
    path = path.resolve()
    if path == REPOSITORY or REPOSITORY in path.parents:
        raise SystemExit("the raw answers must be kept outside this repository")
    return path


async def record(raw: Path, number: int, command: str, argument: str) -> None:
    """``number``: of the records ``raw`` holds already (the next ones count on)."""
    token = os.environ.get("LISTENBRAINZ_TOKEN")
    settings = Settings(listenbrainz_token=SecretStr(token) if token else None)
    catalog = build(settings, Context())
    records: list[dict[str, Any]] = []

    async def keep(response: httpx.Response) -> None:
        await response.aread()
        url = response.request.url
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        slug = re.sub(r"[^a-z0-9]+", "-", f"{command}-{url.path.rsplit('/', 2)[-1]}".lower())
        record = {
            "name": f"{number + len(records) + 1:02d}-{slug.strip('-')[:40]}",
            "url": str(url.copy_with(query=None)),
            "params": dict(url.params),
            "status": response.status_code,
            "headers": {k: v for k, v in response.headers.items() if k.lower() in _KEPT_HEADERS},
            "body": body,
        }
        records.append(record)
        print(record["name"], response.status_code)

    catalog.http.event_hooks["response"].append(keep)
    try:
        if command == "search":
            await catalog.search(argument, 25)
        elif command == "album":
            await catalog.album(argument)
        elif command == "song":
            await catalog.song(argument)
        elif command == "artist":
            await catalog.artist(argument)
        elif command == "releases":
            await catalog.artist_releases(argument)
        elif command == "top":
            await catalog.top_songs(argument, 10)
        elif command == "isrc":
            await catalog.songs_by_isrc(argument)
        elif command == "check":
            await catalog.check()
        else:
            raise SystemExit(__doc__)
    finally:
        await catalog.aclose()
        for record in records:
            await anyio.Path(raw / f"{record['name']}.json").write_text(
                json.dumps(record, indent=1) + "\n"
            )


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    argument = sys.argv[3] if len(sys.argv) > 3 else ""
    raw = outside_repository(Path(sys.argv[1]))
    raw.mkdir(parents=True, exist_ok=True)
    anyio.run(record, raw, len(list(raw.glob("*.json"))), sys.argv[2], argument)


if __name__ == "__main__":
    main()
