# TMDb fixtures — shape-recorded, value-synthetic

The field names, nesting and types in these files were transcribed from
**TMDb's published API documentation** (`developer.themoviedb.org/reference/
movie-details`, `tv-series-details`, `tv-season-details`,
`movie-release-dates`, `tv-series-content-ratings`, `movie-keywords`,
`tv-series-keywords`, `search-movie`, `search-tv`, `changes-movie-list`) on
2026-07-31. **Every value in these files is invented** — every
human-readable string, every date, every runtime and count, and (since
2026-08-01) every identifier.

That is a licensing constraint, not a style. A real TMDb response is TMDb
metadata, which TMDb's terms forbid redistributing and which `CLAUDE.md`'s
"ship importers, never data" already forbids committing. The same rule
governs `tests/fixtures/emby/`, and for the same reason. Read
[`../README.md`](../README.md) for the rule, the reserved identifier bands,
and the guard that now enforces both.

**Every identifier is synthetic too, as of 2026-08-01, and the old note here
was wrong to say otherwise.** It claimed the numeric ids were "addresses
rather than metadata" and kept the real ones. Two things were wrong with
that. TMDb's reference pages illustrate `/movie` and `/tv` with real
responses, so "transcribed from published documentation" was transcribing a
real payload — and this file kept not only the ids but the real `runtime`,
`release_date`, `first_air_date`, `last_air_date`, episode air dates, season
and episode counts, `credit_id` ObjectIds, TVDb and TVRage ids, and (on
`movie.json`) an `imdb_id` belonging to an entirely different film from the
one the rest of the record was shaped after. None of that is an address.

Nothing was lost by replacing them. Ids here are opaque to every test that
reads these files: each asserts `id in → same id out`, so a synthetic id is
indistinguishable to the code under test. The ids are still id-*shaped*
(integers for TMDb/TVDb, `tt` + digits for IMDb, 24 hex characters for a
`credit_id`), which is what would catch an id-handling bug; they are simply
in a band no real id occupies.

**These began as transcriptions of documentation, and were shape-verified
against the live API on 2026-08-01** (M4 Task 26). Regenerate a scrubbed
*shape* from a live account with:

    export USHER_TMDB_API_KEY=...
    uv run python scripts/capture_tmdb_fixture.py --kind movie --id <a real TMDb id>

against a *real* id — the script has no default for exactly the reason this
note exists — and diff that against these. Its output is deliberately never committed —
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
