---
paths:
  - "src/usher/adapters/tmdb/**"
  - "src/usher/services/enrich.py"
  - "src/usher/services/handlers.py"
  - "scripts/measure_worker_lane.py"
  - "scripts/enqueue_tier_enrichment.py"
---

# TMDb and the enrichment stage

Rules for this subsystem; the ADRs and docstrings named below hold the detail.

## Rating columns name their source (`m10a`, [ADR-0040](../../docs/prd/decisions/0040-rating-columns-name-their-source.md))

Never write a bare `vote_count`, `community_rating` or `popularity` — those
columns are gone (`db/migrations/versions/m10a_rating_provenance.py`).
`tmdb_vote_count`, `tmdb_vote_average` and `tmdb_popularity` are enrichment's
alone, bar `link_crosswalk`'s `tmdb_popularity` write on `--phase crosswalk|all`;
`imdb_num_votes` and `imdb_average_rating` are `apply_ratings`' alone.
`field_provenance`'s keys carry the same names, since `mapping.py` derives them
from `Title` field names and `enrich.py` **merges** provenance rather than
assigning it — an old key would sit beside its new spelling forever.

**The enrichment tier is spelled on the IMDb half so it cannot evict itself.**
`enqueue_tier_enrichment.py` selects `imdb_num_votes >= 100`, which no crawl
writes: a tier read from a column enrichment overwrites shrinks as it drains, and
one re-spelled onto `tmdb_vote_count` reads `NULL >= 100` on a fresh catalog and
selects nothing (issue #42).

## Commands

Neither script runs in CI; each writes to a real database or opens real sockets,
says so in its docstring, and needs `USHER_DATABASE_URL`/`USHER_SECRET_KEY` set.

```bash
# The tier writes `jobs` rows only; `usher work` spends the TMDb budget. There
# is deliberately no `usher enrich --backfill`.
uv run python scripts/enqueue_tier_enrichment.py --limit 500
uv run usher work --once     # one pass; `usher work` is the daemon
uv run usher sync-status     # queue depth and parked jobs
uv run python scripts/measure_worker_lane.py --jobs 600 --seconds 45   # a stub

# Shape-diff the live API against the fixtures: NOT a test, output never
# committed, `--id`/`--query` defaultless so no real third-party id lands here.
# Weaker evidence than it looks — `--kind search` records `/search/movie` only,
# `--kind series` omits the blind window, and no fixture records `raw_payloads`.
set -a; . ./.env; set +a     # never a literal key
uv run python scripts/capture_tmdb_fixture.py --kind movie --id <id> > /tmp/shape.json

uv run pytest tests/unit -k "tmdb or enrich"
uv run pytest tests/integration/test_services_enrich.py    # needs Docker
```

Settings (`config.py`, `USHER_`-prefixed): `tmdb_api_key` takes a v3 32-hex key or
a v4 JWT and `_is_v4_token` picks header vs query form; `tmdb_requests_per_second`
(30.0) is a token bucket **per client, therefore per process**; inside
`enrich_cache_max_age_days` (30) a re-enqueued title re-reads `raw_payloads` free;
`tmdb_base_url` fronts TMDb with a proxy, which is why 408 stays retryable.

## How the stage is wired

`enrich` job → `handlers.enrich_handler` → `EnrichService.enrich` → `_apply` →
`_ref_for` → `TmdbMetadataProvider.fetch` → `TmdbClient` → `mapping` →
`raw_payloads` insert → title update → `_store_hierarchy` → two follow-up jobs.

- **`_ENRICHABLE` is enumerated, not derived from the result's `field_provenance`**
  — off the provider's bookkeeping, a mapper that forgot an entry silently stops
  merging that field.
- **A job key that does not parse must become a `UsherPortError` in the handler**,
  which is why `services/handlers.py` converts every key: `JobWorker` lets
  anything else propagate, so one corrupted key takes the worker down.
- **Two follow-up jobs per enriched title, always** — one `INDEX`, one `DERIVE`,
  at the `enrich` job's rung if that is `VISIBLE` or above, else `BACKFILL` (#73).
- **Read season ids back before writing episodes** (`_store_hierarchy` does, as
  `_ensure_seasons` does). `to_result` mints a fresh UUIDv7 per `Season` while a
  season the catalog holds keeps its own, so an episode carrying the minted id
  fails `fk_episodes_season_id_seasons` on the **second** enrichment — and **no
  port fake sees this**, a dict having no foreign keys.
- **Never compare `EnrichmentState` members directly** — `ENRICHED > STUB` is
  `False`, so a guard spelled that way never promotes anything, and the case that
  catches it is **promoting a stub**. A failure handler that resets the tier is
  likewise invisible to a test seeded at that tier.
- **No credential and no URL may reach a log, a span or an exception message.**
  `HTTPXClientInstrumentor` records the full URL as a span attribute and v3 has
  no header form for its key, so `TmdbClient` sends `Authorization: Bearer` for a
  JWT-shaped secret and `api_key` otherwise.

## The failure taxonomy

- **A 4xx that is not 429 is `PortDataMalformed`** — `retryable=False`, so the
  job parks on its first attempt instead of spending five retries to reach the
  identical answer. **404 is the case that fires in production**, the catalog
  holding bulk-export ids that age. **408 is excluded and stays retryable** for
  the proxy case above, and **5xx is `PortUnavailable`**.
- **A kind-less TMDb reference is `PortDataMalformed`, never a guess**
  ([ADR-0011](../../docs/prd/decisions/0011-tmdb-id-is-namespaced-by-kind.md)):
  tens of thousands of ids are live in *both* id spaces and name unrelated works,
  so `GET /movie/{id}` for a ref that meant a series writes an unrelated film on
  as enriched metadata, no error anywhere. `kind_of_payload` discriminates on
  `title` xor `name`.
- **An `ix_titles_imdb_id` conflict is a write failure, not an upstream one** —
  enrichment writes TMDb's `external_ids.imdb_id` over the bulk export's and
  another row already holds it. Not being `PortDataMalformed`, it burns all five
  attempts (re-fetching each time, since the failing write rolls the
  `raw_payloads` insert back) and parks the title `skeleton` with its
  `enrichment_error`.

**TMDb's year filter is exact where the match ladder's is ±1**, so `_confident`'s
±1 never fires against TMDb results. `_search_one` retries **yearless** when a
filtered search finds nothing — a fallback, **not a widening**: dropping the
filter outright resolves *fewer* names.

## `append_to_response=season/N` — one request per series

`fetch` asks for six namespaces plus a blind window of `season/0…season/13`, pops
every `season/N` block off before returning the payload, reconciles the window
against the `seasons[]` summary the *same* response carries, and follows up for
any listed number the window missed.

- **20 items is TMDb's enforced ceiling** (21 is a 400), and six namespaces leave
  exactly 14 season slots — the origin of `SERIES_SEASON_SLOTS` /
  `BLIND_SEASON_WINDOW` (`provider.py`). A follow-up carries no namespaces, so it
  gets all twenty; the count is `1 + ceil(|listed \ {0..13}| / 20)`, bounded by
  the `seasons[]` array alone.
- **An unlisted season number is silently omitted, not an error**, and so is a
  season TMDb declines to serve — the same 200 with the key absent either way, so
  **a missing season used to park the job and no longer can**: it yields an
  episode-less `Season`, paid for by the reconcile's follow-up but never reported.
- **A block is merged *over* its `seasons[]` summary, never under it**, being the
  season's own detail response but for a missing top-level `id`. `season/0`
  (specials) appends like any other namespace.

**Identity with the `1+N` payload is the contract; the request count is only the
benefit** — mapping, `_store_hierarchy` and `DeriveService` all read
`raw_payloads` rows written months earlier, so a divergence stays invisible until
some later derivation returns nothing. `_assert_the_merge_direction_is_observable`
asserts the identity case's premise: without the fixture disagreement the merge
direction is unobservable.

## Movie/TV divergence runs through three layers of the API

- **Field names** — `runtime` (minutes) against `episode_run_time` (an array,
  `[]` on most series, so `runtime_minutes = None` is an answer and not a gap),
  top-level `imdb_id` against `external_ids.imdb_id`; table in the docstring.
- **Endpoints** — `/movie/{id}` vs `/tv/{id}`, `/search/movie` with
  `primary_release_year` vs `/search/tv` with `first_air_date_year`, two
  `changes` feeds, and episodes behind `/tv/{id}/season/{n}`.
- **`append_to_response` vocabularies, which must be split per kind** —
  `release_dates` is movie-only, `content_ratings` its TV equivalent, and asking
  for a namespace that does not exist is **not** an error: it is `200` with the
  key absent, so one shared list silently loses half the certifications.

**The top-level `poster_path`/`backdrop_path` pair is the only primary artwork
signal a payload carries**; the vote-ordered `images.posters[]` flags nothing.
Only `images` is a namespace, so every cached payload derives that pair and only
namespace-fetched ones derive the rest — read a low `images written` count on an
old cache as the cache's age, not a defect. With no top-level *logo* path, `logo`
falls back to read order by design.

## Running the tier

- **`tmdb_id IS NOT NULL` is a correctness conjunct, not an optimisation** —
  without it `_ref_for` raises `PortDataMalformed` for every tier movie lacking
  one, parking tens of thousands of jobs on their first attempt. **The walk is a
  keyset, never an `OFFSET`**, which silently skips rows as the tier drains, and
  **a cache test must name the rows it expects to hit** rather than re-deriving
  them from a predicate the code under test can move.
- **`INDEX` follow-ups pile up unless `USHER_EMBEDDING_ENABLED` is on** (off by
  default) — and even then a bulk enqueue defers them, because every `enrich` row
  lands at one priority within seconds and the claim's `ORDER BY priority DESC,
  created_at` puts all of them ahead of every follow-up; a pool does not fix an
  ordering.
- **`tmdb_requests_per_second` is per process**, so in-flight count comes from
  `job_concurrency`. `services/jobs.py`'s `KIND_CONCURRENCY` is total over
  `JobKind` with **no default**, so a new kind fails `tests/unit/test_config.py`
  rather than inheriting someone else's number.

## Enrichment may delete only genres its provider can express ([ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md))

`genres` is in `_ENRICHABLE`, so a provider supplying any genre replaces the
whole array — and `titles.genres` unions IMDb's vocabulary with TMDb's, disjoint
on concepts only one names, so `Film-Noir`, `Biography`, `Musical`, `Sport`,
`Short`, `Game-Show` and `Adult` get *deleted* rather than re-spelled.
`MetadataProvider.genre_vocabulary` is the set a provider is **entitled to
delete** (abstract, no default) and `_genres_after` keeps any label outside it.
TMDb's is *derived* from `TMDB_GENRE_NAMES` rather than restated — a concept
wrongly in the set is a label enrichment goes on deleting, which looks exactly
like enrichment working — and an alias maps to a **tuple**, TMDb's TV vocabulary
fusing concepts the movie vocabulary keeps apart.

## Still unverified against TMDb — do not claim otherwise

No run has drawn a 429 or any `Retry-After`, so `PortRateLimited.retry_after` has
never been fed by TMDb; `_is_v4_token`'s positive branch has never met a real v4
token; no season TMDb lists has been refused by its own route, so the reconcile's
arbitrary-season branch is unexercised; nothing has run beyond three workers or a
non-`US` `tmdb_region`; and whether `MissingGreenlet` in a long `usher work` run
is *caused* by the `ix_titles_imdb_id` conflict path or merely follows one is open
(`.claude/rules/db-and-sql.md` has the candidate mechanism).
