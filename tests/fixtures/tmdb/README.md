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

**These began as transcriptions of documentation, and were shape-verified
against the live API on 2026-08-01** (M4 Task 26). Regenerate a scrubbed
*shape* from a live account with:

    export USHER_TMDB_API_KEY=...
    uv run python scripts/capture_tmdb_fixture.py --kind movie --id 550

and diff that against these. Its output is deliberately never committed —
see that script's module docstring.

**What the first live diff found.** Not one key in any of these files is
absent from the live response, so every field the mapper reads was
transcribed correctly. The live API carried six keys these files did not,
all of which have since been added (shape only, values invented) so the
*next* diff is empty and a real drift is visible rather than buried:
`softcore` (a boolean, on movie details, series details, search results and
the change feed), `iso_3166_1` on every `images.*` entry, and `networks` on
the season detail — which the `tv-season-details` reference page does not
show.

Two differences are deliberately *not* closed, because they are value-level
rather than shape-level and closing them would make the fixture claim
something false:

- `series.json` carries a populated `episode_run_time`. Live, it is `[]` on
  26 of 30 series (86.7%). The fixture keeps the rarer shape because that is
  the one with a value in it to map;
  `test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure`
  covers the common one.
- Several nullable fields (`belongs_to_collection`, `external_ids.*`,
  `credits.crew[].profile_path`) are null in one and populated in the other,
  and several arrays are empty in the fixture and populated live. Both are
  valid shapes for the same field.

| File | Endpoint |
|---|---|
| `movie.json` | `GET /movie/{id}?append_to_response=credits,keywords,images,videos,external_ids,release_dates` |
| `series.json` | `GET /tv/{id}?append_to_response=credits,keywords,images,videos,external_ids,content_ratings` |
| `season.json` | `GET /tv/{id}/season/{n}` |
| `search_movie.json` | `GET /search/movie` |
| `search_tv.json` | `GET /search/tv` |
| `movie_changes.json` | `GET /movie/changes` |
