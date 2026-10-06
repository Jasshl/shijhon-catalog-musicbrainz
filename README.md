# shijhon-catalog-musicbrainz

A catalog adapter that lets [Shijhon](https://github.com/Jasshl/shijhon) use
[MusicBrainz](https://musicbrainz.org) as its catalog: search results, artist
discographies and album track lists, with covers from the
[Cover Art Archive](https://coverartarchive.org) and an artist's top songs from
[ListenBrainz](https://listenbrainz.org).

It is an example adapter and a catalog only: MusicBrainz has no audio, so whether its songs
play depends on the add-ons you have added to Shijhon. Search, discographies and covers
need no account.

**Status:** a beta, like Shijhon itself (`0.1.0b1`).

## Using it

1. **Install it into Shijhon's image.** With Shijhon's Compose file, add this line to
   `packaging/.env` and build the image again:

   ```sh
   SHIJHON_ADAPTERS=https://github.com/Jasshl/shijhon-catalog-musicbrainz/archive/refs/tags/v0.1.0b1.tar.gz
   ```

   ```sh
   docker compose up -d --build                       # in Shijhon's packaging/
   docker compose exec shijhon shijhon catalogs       # lists "musicbrainz"
   ```

   Other ways to install an adapter are in Shijhon's `docs/deployment.md`.

2. **Switch it on.** Choose MusicBrainz on the dashboard's Catalog page, or set it in
   Shijhon's configuration:

   ```toml
   [catalog]
   kind = "musicbrainz"
   ```

### Settings

All optional; also on the dashboard's Catalog page.

| Setting | Default | |
|---|---|---|
| `covers` | `true` | Album covers from the Cover Art Archive. |
| `top_songs` | `true` | An artist's top songs from ListenBrainz. They need `listenbrainz_token`. |
| `listenbrainz_token` | none | Your ListenBrainz user token (shown on your ListenBrainz profile). Kept secret. |
| `server` | `https://musicbrainz.org` | Change it only to use a MusicBrainz mirror of your own: at a public address (Shijhon refuses local network addresses for a catalog), with its search server. |
| `mirror_requests_per_second` | `1` | Requests a second to a mirror. MusicBrainz's own service always gets one a second. |

## What to expect

- **First searches take a few seconds.** MusicBrainz allows one request a second, so a
  first search takes three to five. Pages Shijhon has seen before appear at once.
- **One edition per album:** an official release, a digital one before a CD, the earliest
  of those.
- **Gaps.** MusicBrainz is written by volunteers: new or niche releases can be missing or
  lack track lengths. A track without a length is not shown.
- **Album search finds official digital releases.** An album that exists only on CD or
  vinyl is found on its artist's page and through its songs.
- **No artist pictures**, and some albums have no cover.
- **Live albums, soundtracks and remix albums** are shown as albums, and explicit or clean
  versions are not marked (MusicBrainz has no such flag).
- **Very long discographies** (over 500 releases) list only albums with an official
  digital release.
- **Top songs** need a ListenBrainz token, and exist only for artists ListenBrainz's users
  listen to.

## Development

This repository is a complete example of a catalog adapter; to write your own, start from
Shijhon's `docs/development.md` ("Writing a catalog adapter").

The commands need Shijhon's source next to this repository, in `../shijhon`.

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

The tests replay recorded answers (`tests/fixtures/README.md`). A live check against the
real services runs only on request: `MUSICBRAINZ_LIVE_SEARCH="<a term>" uv run pytest -m live -s`.

## License

Copyright (C) 2026 Jasshl. Free software under the GNU Affero General Public License,
version 3 or later ([LICENSE](LICENSE)), without any warranty.

Not affiliated with or endorsed by the MetaBrainz Foundation, which runs MusicBrainz,
ListenBrainz and the Cover Art Archive.
