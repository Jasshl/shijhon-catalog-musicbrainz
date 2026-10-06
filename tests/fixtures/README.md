# Fixtures

Answers of real requests to MusicBrainz, the Cover Art Archive and ListenBrainz, recorded
once and sanitized by `tools/sanitize.py`:

- Names, titles, labels, IDs, ISRCs and barcodes are replaced by invented ones, except
  MusicBrainz's own placeholder artists: "Various Artists" keeps its ID and name.
- Years are moved and lengths changed by up to a second.
- Areas, aliases, tags, release events and some other fields the adapter does not read are
  dropped.
- The structure is kept: which releases a group has, types, statuses, formats, countries,
  track counts, the days and months of dates. It may still identify the records an answer
  came from.

The raw answers and the sanitizer's key are not part of this repository.

Each file: `{"name", "url", "params", "status", "body"}` (and the `headers` that matter).

| Fixture | What was asked |
|---|---|
| `search-artists`, `search-albums`, `search-songs` | the three requests of one search |
| `search-albums-whole` | the official digital releases of the release groups an album search shows (its fourth request, when more match than a page holds) |
| `album`, `song` | a release with its tracks; the release of one track |
| `artist`, `artist-releases` | an artist; its official releases (34, in 10 release groups) |
| `songs-by-isrc` | the recordings with one ISRC |
| `top-songs-recordings` | recordings by their IDs (the second request of top songs) |
| `long-releases`, `long-digital`, `long-groups` | a long discography: the first page of 486 official releases (recorded with two releases on it), its 105 official digital releases (two pages, joined), its release groups |
| `artist-various` | the adapter's check |
| `song-not-found`, `artist-releases-invalid-id` | MusicBrainz's 404, and its 400 for an ID it never issued |
| `search-busy` | MusicBrainz's 503 with `Retry-After`, as it arrived during recording |
| `cover-redirect` | the Cover Art Archive's redirect to the image |
| `top-songs-no-token` | ListenBrainz's answer without a token (401) |
| `top-songs-ranking` | **not recorded**: written after ListenBrainz's documentation of the answer (`"constructed": true`), since the endpoint needs a user's token |
