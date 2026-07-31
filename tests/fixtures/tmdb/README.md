# TMDb fixtures — shape-recorded, value-synthetic

The field names, nesting and types in these files were transcribed from
**TMDb's published API documentation** (`developer.themoviedb.org/reference/
movie-details`, `tv-series-details`, `tv-season-details`,
`movie-release-dates`, `tv-series-content-ratings`, `movie-keywords`,
`tv-series-keywords`, `search-movie`, `search-tv`, `changes-movie-list`) on
2026-07-31. **Every human-readable value is invented.**

That is a licensing constraint, not a style. A real TMDb response is TMDb
metadata, which TMDb's terms forbid redistributing and which `CLAUDE.md`'s
"ship importers, never data" already forbids committing. The same rule
governs `tests/fixtures/emby/`, and for the same reason.

Numeric ids (`550`, `1399`, `3624`) are kept because they are addresses
rather than metadata, and because a fixture whose id did not look like a
TMDb id would hide an id-handling bug.

**These are transcriptions of documentation, not recordings of responses.**
Nobody has run a request against TMDb and diffed the result. Regenerate a
scrubbed *shape* from a live account with:

    export USHER_TMDB_API_KEY=...
    uv run python scripts/capture_tmdb_fixture.py --kind movie --id 550

and diff that against these. Its output is deliberately never committed —
see that script's module docstring.

| File | Endpoint |
|---|---|
| `movie.json` | `GET /movie/{id}?append_to_response=credits,keywords,images,videos,external_ids,release_dates` |
| `series.json` | `GET /tv/{id}?append_to_response=credits,keywords,images,videos,external_ids,content_ratings` |
| `season.json` | `GET /tv/{id}/season/{n}` |
| `search_movie.json` | `GET /search/movie` |
| `search_tv.json` | `GET /search/tv` |
| `movie_changes.json` | `GET /movie/changes` |
