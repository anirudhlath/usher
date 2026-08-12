# Test fixtures — what is synthetic, what is not, and why

`CLAUDE.md`'s hardest rule is **"ship importers, never data"**: IMDb's
non-commercial licence and TMDb's terms both forbid redistribution, so no row
of either dataset may be committed here or reach a release artifact.

**This rule was broken from M1 to M4 and nothing noticed.** `bulk/` carried
verbatim IMDb rows — real ids, titles, years, runtimes, genres, and two
`title.ratings` rows complete with vote counts, which is the most
licence-restricted part of that dataset — under a README that said they were
"typed by hand" and therefore fine. Hand-typing a real value does not make it
synthetic, and a false assurance in a licensing note is worse than the data
it covers, because it stops the next reader from checking. The fixtures were
rewritten on 2026-08-01 and `tests/unit/test_no_third_party_data.py` now
enforces the rule mechanically.

## The rule, stated so it can be checked

Nothing under `src/` or `tests/` carries a third-party **identifier** or a
third-party **record**.

- **Identifiers.** Every IMDb id is in the reserved band `tt99` + 6 digits
  (`nm99` for a name id); every TMDb, TVDb and TVRage id in a fixture is at
  or above **90,000,000**; every TMDb `credit_id`/`_id` and every Emby object
  id is zero-filled. The bands are chosen to be unreachable: real tconsts sat
  around `tt3xxxxxxx` in 2026, TMDb's own daily export tops out near 1.4M
  movie ids and 230k series ids, and the largest TVDb id this project has
  observed live is a seven-digit episode id.
- **Records.** Every title, original title, year, end year, runtime, genre
  list, rating, vote count, popularity, air date, release date, overview,
  tagline, person name, character name, company name and image path in these
  files is invented. No value here describes a real work.

**What is deliberately kept, because it is format rather than data:** field
names, nesting, JSON types, and the upstreams' *controlled vocabularies* —
IMDb's `titleType` values and genre names, TMDb's `iso_639_1`/`iso_3166_1`
codes and certification strings, Emby's `VideoRange`/`ExtendedVideoType`
tokens, codec and container names. Those are the protocol. A fixture that
invented them would exercise nothing, and none of them says anything about a
particular work.

Enforced by `tests/unit/test_no_third_party_data.py`. Three checks scan
`src/` and `tests/` — an IMDb-band pattern, a floor on every id inside a
fixture, and a hashed regression list of the identifiers this repository is
known to have committed. A fourth scans the **whole repository**, `docs/`
included, for a dataset *row*: an IMDb TSV line or a TMDb id-export record,
matched on shape. Two more fail if the scans stop scanning. Mutation-
verified: eleven mutations, eleven killed.

The split is deliberate. `docs/` and `CLAUDE.md` are the project's
engineering record and neither ships, so a sentence naming a real row as the
*specimen* for a measurement is a factual claim about a dataset rather than
a copy of one — but a *row* is a copy wherever it sits, and a plan that
transcribes one is data plus the instruction that recreates it. That is not
hypothetical: `docs/plans/2026-07-30-m2-bootstrap.md` held the original
fixture verbatim, and `usher.adapters.bulk.tmdb_ids`' module docstring held
two real TMDb export records inside the shipped package. The three
location-scoped checks missed both; the shape-scoped one found them.

## The identifier allocation

Ids are allotted rather than random so a fixture and the test that reads it
can be matched up by eye.

| Id | Stands for |
|---|---|
| `tt99000001` | a `short` — dropped by the `titleType` filter |
| `tt99000002` | a second subject for malformed-row cases |
| `tt99000010` | the row whose title carries literal `"` characters |
| `tt99000020` / TMDb `90000020` | the movie workhorse (bulk, crosswalk, matching) |
| `tt99000030` / TMDb `90000030` / TVDb `91000030` | the series workhorse |
| `tt99000040` | a `tvMiniSeries` |
| `tt99000050` | a `tvMovie` with every optional column `\N` |
| `tt99000060` | a `tvEpisode` — dropped |
| `tt99000070` | `isAdult=1` — dropped |
| `tt99000080` | a `videoGame` — dropped |
| `tt99000090` | a ratings row with no matching basics row |
| `tt99000100` / TMDb `90000100` | `emby/movie_item.json` |
| `tt99000110` / TVDb `91000110` | `emby/episode_item.json` |
| `tt99000120` / TMDb `90000120` | `emby/multi_version_movie.json` |
| `tt99000150` | the Greek-final-sigma title, where Postgres `lower()`, Python `str.lower()` and `str.casefold()` disagree |
| `tt99001000` | the 8-digit-tconst validation case |
| TMDb `90000550` / `90001399` | `tmdb/movie.json` / `tmdb/series.json` |
| TMDb `96000000`/`96000001` | seasons; `97000001`/`97000002` episodes |
| TMDb `93xxxxxx` / `94xxxxxx` / `95xxxxxx` | people / companies+networks / genres+keywords |

`movie_ids.slice.jsonl` and `tv_series_ids.slice.jsonl` both carry
`90000045`, deliberately: TMDb's movie and series id spaces overlap on 26,968
ids (measured 2026-07-30), which is what
[ADR-0011](../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md)
exists for. A fixture without a collision could not exercise it.

## Do a test's ids need to be recognisable?

No, and this was the mistake the old note made. Nothing in this suite gets
its meaning from an id being famous: every assertion is `id in → same id
out`, or `id in → the row seeded under that id`. A synthetic id is
indistinguishable to the code under test and costs nothing, so it is always
preferred. If a case ever genuinely needs a well-known id — none does today —
say so at that case, with the reason.

## Regenerating

Never paste a live capture in. Both capture scripts replace every leaf value
with the *name of its type*, so their output is a shape to diff against, not
a fixture to commit; each says so in its own module docstring.

```bash
set -a; . ./.env; set +a
uv run python scripts/capture_tmdb_fixture.py --kind movie --id <a real id>   > /tmp/shape.json
uv run python scripts/capture_emby_fixture.py --type Episode                  > /tmp/shape.json
```

Diff that against the committed fixture, then hand-edit the fixture to add
any missing **key** with an invented value. `tests/fixtures/tmdb/README.md`
records what the first such diff (2026-08-01) found.

See `bulk/README.md`, `tmdb/README.md` and `emby/README.md` for what each
set's rows are shaped to pin.
