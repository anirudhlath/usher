---
paths:
  - "src/usher/adapters/tmdb/**"
  - "src/usher/services/enrich.py"
  - "src/usher/services/handlers.py"
  - "scripts/measure_worker_lane.py"
  - "scripts/enqueue_tier_enrichment.py"
---

# TMDb and the enrichment stage

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

## ⚠️ Read this first — `m10a` renamed the rating columns, and the M9 runs below predate it

`m10a` / [ADR-0040](../../docs/prd/decisions/0040-rating-columns-name-their-source.md)
(2026-08-19) split three columns that each had two writers. **Every number this
file records was taken under the old spelling and is still correct as taken;
what moved is the name you have to type to reproduce it.** Migration docstring:
`src/usher/db/migrations/versions/m10a_rating_provenance.py`.

| before `m10a` | today | who writes it now |
|---|---|---|
| `titles.vote_count` | **`titles.tmdb_vote_count`** | TMDb enrichment only |
| `titles.community_rating` | **`titles.tmdb_vote_average`** | TMDb enrichment only |
| `titles.popularity` | **`titles.tmdb_popularity`** | TMDb enrichment, *plus* `link_crosswalk`'s copy from `tmdb_ids` during `--phase crosswalk\|all` |
| — (new column) | **`titles.imdb_num_votes`** | `BulkCatalogRepository.apply_ratings` only |
| — (new column) | **`titles.imdb_average_rating`** | `apply_ratings` only |

`field_provenance`'s three keys are renamed in the same revision, because
`adapters/tmdb/mapping.py` derives them from the `Title` field names — without
that, an already-enriched row would keep `"community_rating": "tmdb"` while its
next enrichment added `"tmdb_vote_average": "tmdb"` beside it forever, since
`services/enrich.py` **merges** provenance rather than assigning it.

**Three consequences you will trip over reading the rest of this file:**

1. `_ENRICHABLE` (`services/enrich.py:100-123`) holds `tmdb_vote_average`,
   `tmdb_vote_count`, `tmdb_popularity` — no bare `vote_count`.
2. `apply_ratings` (`db/repositories/bulk.py:622-660`) writes
   `imdb_average_rating`/`imdb_num_votes` and nothing else. **The dual write is
   gone**, and that statement's own comment says so. So "enrichment overwrites
   the bulk loader's votes" is history, not behaviour.
3. The enrichment tier is now spelled on the IMDb half — see below — so it no
   longer evicts itself.

## Commands

Nothing here runs in CI. Both scripts write to a real database or open real
sockets, and each says so in its own module docstring.

```bash
# Enqueue the priority tier. Writes `jobs` rows only; `usher work` is what
# spends the TMDb budget. There is deliberately no `usher enrich --backfill`.
export USHER_DATABASE_URL="postgresql+asyncpg://usher:usher@localhost:5432/usher"
export USHER_SECRET_KEY="<32+ char secret>"
uv run python scripts/enqueue_tier_enrichment.py --limit 500
uv run usher work --once                     # one pass
uv run usher work                            # the daemon that drains it
uv run usher sync-status                     # queue depth and parked jobs

# Price the worker lane against a local stub. Never touches api.themoviedb.org.
uv run python scripts/measure_worker_lane.py --jobs 600 --seconds 45
uv run python scripts/measure_worker_lane.py --database-url "$USHER_DATABASE_URL"
uv run python scripts/measure_worker_lane.py --sigma 1.205   # the p95-matched tail; see W1

# Diff the live API's *shape* against the committed fixtures. NOT a test, and
# its output is never committed. `--id`/`--query` have no defaults on purpose:
# a default would be a real third-party identifier in this repository.
set -a; . ./.env; set +a                     # never a literal key
uv run python scripts/capture_tmdb_fixture.py --kind movie  --id <id> > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind series --id <id> > /tmp/shape.json
uv run python scripts/capture_tmdb_fixture.py --kind season --id <id> --season 1
uv run python scripts/capture_tmdb_fixture.py --kind search --query <title> --year <year>
uv run python scripts/capture_tmdb_fixture.py --kind changes

# What pins this subsystem. Run these before believing a change here.
uv run pytest tests/unit/test_adapters_tmdb_client.py \
              tests/unit/test_adapters_tmdb_provider.py \
              tests/unit/test_adapters_tmdb_mapping.py \
              tests/unit/test_tmdb_mapping_credits.py \
              tests/unit/test_services_enrich.py \
              tests/unit/test_scripts_enqueue_tier_enrichment.py
uv run pytest tests/integration/test_services_enrich.py    # needs Docker
```

`/tmp` on this host is **tmpfs**, so a shape dump belongs there and a
pre-registered bar does not — `CLAUDE.md`'s live-verification section has the
rule and `/var/tmp` is the durable answer.

**Settings that change what this stage does** (`config.py`, all `USHER_`-prefixed
and all `SecretStr` where they hold a credential):

| setting | default | what it decides |
|---|---|---|
| `tmdb_api_key` | `None` | a v3 32-hex key or a v4 JWT; `_is_v4_token` (`adapters/tmdb/client.py:89`) picks header vs query form |
| `tmdb_base_url` | `https://api.themoviedb.org/3` | exists so a household can front TMDb with a proxy — which is why 408 stays retryable |
| `tmdb_requests_per_second` | `30.0` | the token bucket, **per client and therefore per process** |
| `tmdb_region` | `US` | nothing has ever been verified against another value |
| `enrich_cache_max_age_days` | `30` | inside it, a re-enqueued title re-reads `raw_payloads` and spends zero requests |

## How the stage is wired, top to bottom

`enrich` job → `handlers.enrich_handler` (`services/handlers.py:108`) →
`EnrichService.enrich` (`services/enrich.py:186`) → `_apply` → `_ref_for` →
`TmdbMetadataProvider.fetch` (`adapters/tmdb/provider.py:257`) → `TmdbClient` →
`adapters/tmdb/mapping` → `raw_payloads` insert → title update →
`_store_hierarchy` → two follow-up jobs.

- **`_ENRICHABLE` is enumerated, not derived from the result's own
  `field_provenance`.** Driving the merge off the provider's bookkeeping means a
  mapper that forgot one provenance entry silently stops merging that field, and
  nothing would ever say so.
- **A job key that does not parse must become a `UsherPortError` inside the
  handler.** `uuid.UUID("not-a-uuid")` raises `ValueError`, and `JobWorker`
  deliberately lets anything that is not a `UsherPortError` propagate — *"a bug
  in a handler is not an upstream failure"*. So one corrupted `enrich` key would
  take the worker process down instead of parking its own job.
  `usher.services.handlers` converts every key, once.
- **Two follow-up jobs per enriched title, always** — one `INDEX` and one
  `DERIVE`. ⚠️ **The rung is not a constant, as of 2026-08-26 (issue #73):** the
  follow-ups inherit the rung the `enrich` job was claimed at whenever that is
  `VISIBLE` or above, and stay at `BACKFILL` otherwise. Every number in the S2/S3
  sections below is unaffected — those enqueued at `NEW`, the clamped branch —
  but a reader taking "at `BACKFILL`" as a present-tense property of the code
  will be wrong for anything a client opened or scrolled past.
- **Enrichment must read season ids back before writing episodes.**
  `MetadataProvider.to_result` mints a fresh UUIDv7 per `Season`, and a season
  the catalog already holds keeps the id it was inserted with — so an episode
  carrying the minted id names no row and fails on
  `fk_episodes_season_id_seasons`, on the **second** enrichment rather than the
  first. `IngestService._ensure_seasons` re-reads for exactly this reason;
  `EnrichService._store_hierarchy` now does too, and **no port fake can see
  either** (a dict has no foreign keys).
- **`EnrichmentState.ENRICHED > EnrichmentState.STUB` is `False`, and the
  consequence is not the one you would guess.** A tier guard spelled as a direct
  comparison does not "sometimes downgrade" — it never promotes anything at all,
  silently, because `ENRICHED` is lexicographically below both other rungs. So a
  test asserting "an enriched title stays enriched" passes against the bug, and
  the case that catches it is **promoting a stub**. The M4 plan's own mutation
  table pointed at the wrong one.
- **A failure handler that resets the tier is invisible to a test seeded at that
  tier.** `enrichment_state=SKELETON` alongside the error is exactly what a
  careless `_record_failure` reaches for, and a case seeded with a skeleton
  cannot see it — the write is a no-op. Found by mutation;
  `tests/unit/test_services_enrich.py` parametrizes over all three rungs now.
  Same family as *"a concurrency test must assert on observed overlap, not on a
  count"*.
- **A TMDb v3 API key in the query string lands in every trace.**
  `HTTPXClientInstrumentor` (wired in `configure_tracing`) records the full URL
  as a span attribute, and TMDb v3 has no header form for a v3 key. So
  `TmdbClient` sends `Authorization: Bearer` whenever the configured secret is
  JWT-shaped and falls back to `api_key` otherwise. **For the same reason no
  exception message in that module may carry a URL** — `EmbySession`
  interpolates the httpx exception into its own message and explains why that is
  safe *there*; it is not safe here.

## The 712-request live run, 2026-08-01 (M4) — ten guesses, eight settled

Run against `api.themoviedb.org/3` with a real v3 key, driving the shipped
`TmdbClient`/`TmdbMetadataProvider`/`usher.adapters.tmdb.mapping`/
`usher.services.matching._confident` from a throwaway script outside the working
tree. **712 requests, GET only, no write route of any kind.** Before this run
**no request had ever been made** from this repository and every TMDb fixture was
a transcription of documentation. Status distribution, since it is the evidence
for half the table: **699 × 200, 7 × 404, 2 × 401, 2 × 422, 2 × 400 — no 429 and
no 5xx at all.** All thirteen non-200s were deliberately provoked.
**Both corrections went the same way: TMDb is more silent than the code assumed,
not louder.**

| # | Guess | Verdict | Evidence |
|---|---|---|---|
| 1 | TMDb sends `Retry-After` on a 429 | **still unverified** | Zero 429s in 712 requests at 25 rps, and no `retry-after` header on *any* response including the 401s and 422s. Deliberately not provoked. |
| 2 | An invalid `append_to_response` namespace errors | **refuted** | `200`, key silently absent — for a wrong-space namespace *and* for `zzz_not_a_namespace`. |
| 3 | The 404 body shape | **confirmed & recorded** | `{"success": false, "status_code": 34, "status_message": "The resource you requested could not be found."}`, `application/json;charset=utf-8`, on `/movie`, `/tv` and `/tv/{id}/season/{n}` alike. |
| 4 | A v4 read access token is JWT-shaped | **unverifiable here, cost bounded** | The configured credential is a classic 32-hex v3 key; `_is_v4_token` correctly says no. A false positive was measured instead: the v3 key sent as `Authorization: Bearer` answers **401** (`status_code: 7`) — loud and immediate, never a wrong answer. |
| 5 | The changes window's inclusivity and its 14-day cap | **confirmed, and it is the boundary** | `start == end` is a valid one-day window (4,278 results); `[d, d+1]` covers both days deduplicated; `[today-14, today]` → 200; `[today-15, today]` → **422**, *"Invalid date range: Should be a range no longer than 14 days."* The shipped clamp sits exactly on it with nothing spare. |
| 6 | `credits` is a valid TV append namespace | **confirmed** | Present with 14 cast entries. `aggregate_credits` is *also* valid — a second view, not a replacement. |
| 7 | `append_to_response=season/N` works | **confirmed, and shipped** | It collapses a series from 1+N requests to 1. Section below. |
| 8 | A season the series lists that 404s on its own route | **still unverified** | **626 listed seasons over 44 series across two runs, zero absent** (320/30 on 2026-08-01, 306/14 on 2026-08-11). No listed season's own route answered anything but `200`. Sample skews to series TMDb curates well, so this is weak evidence of absence — the movies-only S2/S3 runs add nothing to it. |
| 9 | Search orders by relevance with the obvious answer first | **confirmed** | 263 of 266 confident resolutions were TMDb's **first** result (max rank 3; series 126/126 at rank 0), and the top result was an exact normalised name match on 269 of 320 probes. |
| 10 | `spoken_languages[].iso_639_1` and `origin_country` are well-formed | **confirmed** | Zero anomalies over 59 detail payloads; `origin_country` present on 29/29 movies and 30/30 series, always a list of strings. |

**Two things live TMDb contradicted, both fixed with a failing test first.**

- **A 4xx that is not a 429 is `PortDataMalformed`, not `PortUnavailable`.**
  Observed: **422** for a 15-day change window (`status_code: 20`) and **400**
  for a 21-item `append_to_response` (`status_code: 27`, *"the maximum number of
  remote calls is 20"*). Both were classified as outages, so `JobWorker` would
  spend five rate-limited retries and a backoff schedule reaching the identical
  answer and then park with the wrong reason. **408 is excluded and stays
  retryable** — TMDb has never been observed sending one, but `tmdb_base_url`
  exists so a household can front TMDb with a proxy.
- **TMDb's year filter is exact where the match ladder's is ±1.** All 294
  candidates returned across 320 probes carried *exactly* the year asked for, so
  `_confident`'s own `abs(candidate.year - item.year) <= 1` never fired once and
  tier 4 silently ran at ±0. 26 of 320 came back empty rather than one year off;
  re-asking those without the year resolves **13**, every one a title TMDb dates
  a year away from IMDb (Danny Phantom 2003/2004, Toast of London 2012/2013, …).
  `TmdbMetadataProvider._search_one` (`provider.py:329`) now retries yearless
  when the filtered search finds nothing. **A fallback and not a widening**,
  because dropping the filter outright was measured too and is worse: 6 of 133
  already-resolving names stop resolving, since "exactly one survivor" across
  every year at once is a harder test than within one.

**`_confident` against TMDb's own search: 83.1%, and 87.2% with the yearless
fallback** — the number the Emby half explicitly could not take. 320 IMDb names
(160 movies / 160 series) stratified into four `numVotes` bands: **87.5% of
movies**, **78.8% of series**; by band, 90.0% / 91.3% / 81.3% / 70.0%
descending, so a real library — which sits at the popular end — should expect the
high eighties to low nineties. Failures decompose as 26 zero-result, 22
results-but-no-exact-name, 6 ambiguous. Compare tier 3's 72.2%/75.3% for the
identical predicate over the local 1.27M-row catalog: **different candidate sets
and different name samples, so these are counterparts, not a before/after.** The
IMDb-derived names are a proxy for Emby names, which were not available to this
run — stated rather than implied.

**Two identity findings, both `PortDataMalformed` and neither a guess.**

- **ADR-0011 is not theoretical: 12 of 14 small ids probed are live in both id
  spaces, and every pair is an unrelated work.** `550` is *Fight Club* and *Till
  Death Us Do Part*; `238` is *The Godfather* and *Star Cops*; `680` is *Pulp
  Fiction* and *Shaquille*. No movie payload carried a `name` key and no series
  payload a `title` key, so `kind_of_payload`'s exactly-one rule resolved all 24
  correctly. At the request layer **26,968 ids are live in both spaces**, so
  `GET /movie/{id}` for a ref that meant a series returns a real payload for an
  unrelated film, written onto the title as enriched metadata with no error
  anywhere. Hence: a kind-less TMDb reference is `PortDataMalformed`, never a
  guess.
- **A TMDb 404 is `PortDataMalformed`, not `PortUnavailable`.** The catalog holds
  291,737 TMDb ids from a bulk export that ages, and TMDb answers 404 for an id
  it has merged away. Retrying cannot turn any of them into an answer, so this is
  the branch that makes `JobWorker`'s park-immediately path fire in production
  rather than only in a test.

**The committed TMDb fixtures were transcriptions and they held up.** The first
shape diff any of them has ever had (via `scripts/capture_tmdb_fixture.py`) found
**not one key in any fixture that the live response lacks**. The live API carried
six the fixtures did not, all now added shape-only so the *next* diff is empty
and a real drift is visible: `softcore` (a boolean, on movie details, series
details, search results and the change feed), `iso_3166_1` on every `images.*`
entry, and **`networks` on the season detail**, which the `tv-season-details`
reference page does not show. Two differences are deliberately left open because
they are value-level, not shape-level — `tests/fixtures/tmdb/README.md`.

**`--kind search` sends `primary_release_year` whatever it is given**, so it
records the `/search/movie` shape and never `/search/tv`'s. Fine for a shape diff
(the two pages differ only in `title`/`name` and `release_date`/`first_air_date`),
worth knowing before reading its output as evidence about TV search.

**And since M9's T1, `--kind series` no longer reproduces the first request the
provider issues.** The script sends `SERIES_APPEND_TO_RESPONSE` alone; `fetch`
sends that plus `season/0…season/13`. Left alone on purpose — the season blocks
are popped before `fetch` returns, so capturing them would record shapes nothing
reads, and `season.json` already records the season shape from its own route.
**The reason first given for leaving it was wrong and is corrected here**: it is
*not* that the namespace-only capture is "exactly the shape `raw_payloads`
holds". `raw_payloads`' `seasons[]` entries carry merged episode data no bare
`SERIES_APPEND_TO_RESPONSE` response has ever contained — true of the `1+N` path
too, so the fixtures have never recorded a stored payload's shape and were never
meant to. **A wrong reason in a docstring outlives the decision it justifies.**

## `append_to_response=season/N` — one request per series, and what the shape cost

One request carrying `credits,keywords,images,videos,external_ids,content_ratings`
plus `season/0…season/13` — **exactly** TMDb's 20-item ceiling — returned Game of
Thrones' entire hierarchy, **all 373 episodes across 9 seasons**, in place of the
ten requests the pre-T1 path cost. Four supporting facts, each measured because
the change rests on it:

- The ceiling is **enforced**: 21 items is a **400**, `status_code: 27`. Six
  namespaces already appended leaves exactly 14 season slots — which is how
  `SERIES_SEASON_SLOTS` / `BLIND_SEASON_WINDOW` (`provider.py:118-130`) are
  derived, measured on **both** sides of the boundary rather than only below it.
- `season/0` (specials) appends like any other, 300 episodes on GoT.
- An unlisted season number is **silently omitted**, not an error — which is also
  the cheap detector guess 8 is scanned with. Re-confirmed live: `season/0` was
  asked for on all 14 T2 series and arrived on **12**; the other two begin at 1.
- The appended block is identical to the season's own detail response **but for a
  missing top-level `id`**, and the series' own `seasons[]` summary carries that
  same id. That was three seasons on one series in 2026-08-01 and is now **306
  seasons over 14** in 2026-08-11. So `_compose_seasons`' merge-over-the-summary
  loses nothing.

**Shipped 2026-08-11 (M9 T1), against fixtures only.** `fetch` asks for the blind
window alongside the six namespaces, pops every `season/N` block off the payload
before returning it, reconciles the blind window against the `seasons[]` summary
the *same* response carries, and follows up for any listed number the window
missed; a follow-up carries no namespaces so it gets all twenty slots.
**Identity with the `1+N` payload is the contract and the request count is only
the benefit** — `mapping.seasons_and_episodes`, `EnrichService._store_hierarchy`
and `DeriveService` all read `raw_payloads` rows written months earlier, so a
divergence is invisible until a derivation much later returns nothing.
`test_the_composed_payload_equals_what_the_per_season_path_produced` holds it.

### The identity case needed three things to have teeth, and the third was the trap

- **The two spellings must reach different endpoints or the equality is a
  tautology.** The fake serves the season route and the appended blocks
  independently and each arm has one of them turned off.
- **The fake's season-route response has to carry the summary's own `id`.** The
  committed fixture is one `season.json` reused for every number, so the fake
  answered `id: 96000001` for season 0 as well — a disagreement the live run
  measured the real API *not* to have, which would have failed the identity case
  on the fake rather than on the provider.
- **On faithful data the merge *direction* is unobservable**, because the block
  and the summary agree on every shared key. The fake keeps `season.json`'s prose
  whatever the number, which makes season 0's block disagree with the Specials
  summary on `name`/`overview`/`air_date`/`poster_path`/`vote_average`, and that
  disagreement is the only thing in the suite that can see the direction.

🔴 **That third bullet was written down and then not guarded, which is the
finding worth carrying past this task.** Found in review 2026-08-11, by execution
rather than by reading: the entry called the fixture disagreement *"the only thing
in the suite that can see the direction"* — and nothing asserted it. With the
inverted merge planted, editing **only** `tests/fixtures/tmdb/series.json`'s
season-0 entry to agree with `season.json` — no code change, a plausible *"make
the fixtures internally consistent"* cleanup no reviewer would read as a test
change — took the file from one red to **32 passed, zero red**.

Closed by `_assert_the_merge_direction_is_observable`, called by both cases that
read the disagreement, failing on its own `E ` line naming every field that has
stopped disagreeing. Re-verified: inverted merge **plus** the cleanup fails 2 of
33; the cleanup **alone**, with correct code, also fails those 2 — which is
wanted, since the premise is a statement about the fixture and its job is to say
*"this case can no longer fail"* the moment that becomes true.
`test_a_season_block_is_merged_over_its_summary_and_never_under_it` asserts the
direction on the payload directly, so the property survives the identity case
being deleted. **The cleanup has to touch all five shared fields, not four** —
with `overview` left alone the pre-fix case still fails, because it is `""` on the
Specials summary — which is why the premise requires **every** shared field to
disagree rather than *at least one*.

**The general form:** when a case can only fail because two fixtures disagree,
the disagreement is a premise and gets asserted like any other — `CLAUDE.md`'s
ordering-premise rule (`assert far_id < near_id`) in the fixture-consistency
domain. **The tell is a docstring saying a fixture property is what makes a case
able to fail: that sentence is either an assertion or a comment nobody will
re-check.**

Four plants, each against the whole `tests/unit` selection: the merge direction
inverted and a surviving `season/N` key each fail the identity case **alone**;
the reconcile-against-`seasons[]` loop deleted fails 3; the slot arithmetic
loosened by one (a 21st item assembled) fails 3. One equivalent-mutant control —
the two literal `*_APPEND_TO_RESPONSE` constants swapped — passes all five gate
steps.

### The one property `1+N` had that the appended shape cannot

**A missing season used to be loud.** The old `fetch` let a season's own 404
propagate and park the job, arguing that *"a catalog that says a show has seven
seasons when it has eight is wrong with no signal anywhere, and a parked job is
at least visible"*. `append_to_response` cannot express that: a season the series
does not have and a season TMDb declines to serve are the **same 200 with the key
absent**, so a listed season whose block never arrives now yields a `Season` row
with no episodes rather than a parked job. The reconcile still spends one
follow-up on it, so the case is paid for even though it is not reported. Traded
knowingly, and **the trade is cheap only because guess 8 is what it is.**

### The ~10× — stated once, with the sample it rests on

At **32,409 series** and a **median of 9 seasons** (the 30 series the 2026-08-01
run walked), `1+N` costs 32,409 × 10 = **~324k requests** against **~32k** for the
appended path: **~10×**.

- **It was first written as "~190k → ~35k, ~5x" and that was internally
  inconsistent.** `~190k` was [PRD 04](../../docs/prd/04-catalog-bootstrap.md)'s
  Phase-3 tier-1 line, *"~189k titles with ≥100 IMDb votes"*, borrowed one
  section over: a **whole-catalog title count read as a series request count**.
  Nothing measured it, and ~190k would need a median of ~4.9 seasons.
- **The median is measured and its sample is not a library.** Popular series have
  many seasons, so a real 32,409-series library's median is very likely *lower*
  and ~324k is an upper bound on the measurement taken rather than a prediction.
- **~32k, not ~35k, and the difference is the ceiling.** One request per series
  is 32,409 exactly; a series with more than 14 listed seasons needs another.
  Both round the same to one significant figure.
- ⚠️ **The form of that arithmetic is wrong in both terms and roughly cancels.**
  A catalog total for `1+N` is `Σ(1 + N)`, which needs the **mean** season count
  and not the median; the append side is `32,409 + Σ ceil(|listed \ {0..13}|/20)`
  and not `32,409`. Season counts are right-skewed, so the median understates the
  first and the missing follow-up term understates the second. **~10× survives as
  an order-of-magnitude claim; neither leg survives as a request budget.**
- ⚠️ T2's own aggregate was **25 requests against 320, i.e. 12.8×** over 14
  series with a median of 10.5 listed seasons and a mean of 21.86. **That is not
  a better constant and must not be quoted as one** — 5 of the 14 were chosen
  *because* they have more than 20 seasons, to exercise the follow-up branch at
  all.

## The shipped append path against the live API, 2026-08-11 (M9 T2) — 393 requests

**The refutation is a sentence this project had written in three places.** The
bar — eleven guesses and what would count as done — was written **before the
first request** [to `/tmp/m9-exec/T2/`, which is tmpfs on this host and no longer
exists; the bar's date-stamped content survives only in what this section
transcribes, and `CLAUDE.md`'s rule now says `/var/tmp` for exactly this reason].
The driver ran outside the working tree reading the operator's own `.env`.
Sample: **14 series carrying 306 listed seasons, plus 2 movies as a control**,
each fetched down *both* paths — the shipped `fetch` and a verbatim re-spelling
of the pre-T1 `1+N` path (`git show e38ccb5^`) — sequentially through one shipped
`TmdbClient` token bucket. **392 × 200 and 1 × 400**, the 400 being the
deliberate ceiling probe. No 429, no 5xx, no `Retry-After`. Window
**21:15:51Z → 21:18:01Z**, key idle from **21:18:01Z**.

🔴 **Refuted: "a series with more than 14 seasons needs a second request; a small
tail."** The count is `1 + ceil(|listed \ {0..13}| / 20)` and it **has no
ceiling** — the bound is the size of the upstream `seasons[]` array. The formula
predicted every one of the 14 exactly:

| listed seasons | append path | `1+N` path |
|---|---|---|
| 2, 6, 6, 8, 9, 10, 10, 11 | 1 | 3, 7, 7, 9, 10, 11, 11, 12 |
| **14** (numbers 0–13 — the exact window boundary) | **1** | 15 |
| 24 | 2 | 25 |
| 30 | 2 | 31 |
| 39 | **3** | 40 |
| 63 | **4** | 64 |
| 74 | **5** | 75 |

🔴 **Refuted, and it was this run's own prediction rather than the repository's:
there is no top-level volatility between the two paths.** The bar predicted
`popularity`/`vote_average`/`vote_count` — the payload's own field names — would
differ, because the two arms are two separate detail requests made seconds apart.
**All 14 composed payloads were equal field for field.** So the identity case
holds *exactly* against the live API, not "modulo volatile fields". Bounded
claim: seconds apart, not hours.

**And the equality was planted against, because a differ that cannot see a
difference is a check that cannot fail.** One series re-fetched down both paths,
baseline confirmed at 0 diffs, then five perturbations planted into a copy of the
`1+N` payload — a season's `episodes` dropped, one episode's `name` changed, one
episode removed, a whole season entry removed, a season entry's top-level `id`
removed. Each was caught and each named its own field path.

**Guess 8's request-layer shape occurred 0 times in 306 listed seasons.** Every
one of the 14 listed its seasons contiguously and every miss was a number ≥ 14,
so the reconcile's *"TMDb permits any integer season number"* branch **also** has
still never met a real occurrence. The sample deliberately reached past the
popular end — Panorama's 74 seasons, Horizon's 63, Bergerac's 10, long-tail BBC
catalogue entries rather than a second helping of prestige drama — and still
skews to series TMDb curates well.

**A movie still costs exactly 1 request**, carries `title` and no `name`, and no
season machinery touches it.

⚠️ **One rate number, with the caveat that makes it usable.** The measure phase
ran **347 requests in 24.0 s = 14.5 rps** against a bucket set to 30 — so the
bucket was **not** the binding constraint; downloading season blocks was. **This
is not a movie-fetch rate**: the two movie fetches in this run took **0.064 s and
0.118 s** of wall time each.

## Movie/TV divergence runs through three layers of the API, not one

Read from `developer.themoviedb.org` on 2026-07-31 and **every row confirmed live
on 2026-08-01** over 29 movie and 30 series detail responses.

- **Field names.** `title`/`name`, `original_title`/`original_name`,
  `release_date`/`first_air_date`, `runtime` (minutes) against
  `episode_run_time` (an array), `keywords.keywords` against `keywords.results`,
  a top-level `imdb_id` against `external_ids.imdb_id`. Tabulated in
  `usher.adapters.tmdb.mapping`'s docstring. Live: 29/29 movies carried the whole
  movie column and **none** of the series column; 30/30 series the mirror, with
  `external_ids.tvdb_id` non-null on all 30.
- **Endpoints.** `/movie/{id}` against `/tv/{id}`; `/search/movie` with
  `primary_release_year` against `/search/tv` with `first_air_date_year`;
  `/movie/changes` against `/tv/changes`; and a series' episodes live behind
  `/tv/{id}/season/{n}`, which has no movie counterpart at all.
- **`append_to_response` vocabularies.** `release_dates` is movie-only and
  `content_ratings` is the TV-only equivalent. **The consequence was stated
  wrongly and is corrected**: a shared list does not ask for a namespace that
  does not exist and get an error, it gets `200` with the key absent. So the
  failure is silent — half the catalog loses its certification on a response that
  looks entirely successful — which is a *stronger* reason for the split than the
  one previously recorded.

**`episode_run_time` is empty on 86.7% of series** — `[]` on 26 of 30 live detail
responses, Game of Thrones among them. `Title.runtime_minutes` is simply not a
fact TMDb still holds about most television, and `None` is the answer rather than
a mapping gap. The committed `series.json` carries the rarer populated shape, so
the common one needed its own case
(`test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure`).

**Artwork derives from a payload cached before `images` was appended, and the
top-level pair is why.** M9's C3 reads `images.{posters,backdrops,logos}[]` plus
the top-level `poster_path`/`backdrop_path`. Only the first is an
`append_to_response` namespace; the second pair are ordinary detail fields, so
**every** cached payload derives the two references M9's artwork consumers
render, and only payloads fetched with the namespace derive the rest. **An
operator reading a low `images written` against a large cache is seeing the age
of the cache, not a defect** — say so rather than letting it read as one.

**The top-level pair is the only primary signal a TMDb payload carries.** Nothing
inside `images.posters[]` is flagged — the array is vote-ordered and carries the
payload's own `vote_average`/`vote_count`, which is a popularity signal and not
TMDb's own pick. So `is_primary` comes from those two top-level keys or from
nowhere, and a derivation that ignored them leaves every row unflagged, at which
point `ImageRepository.primary_for_titles` falls back to first-in-read-order —
which is id order, which is whichever language variant the array happened to list
first. There is no top-level *logo* path, so `logo` never gets a primary at all
and the fallback is the intended path for that kind.

## The priority tier priced, 2026-08-11 (M9 S2) — 539 requests

**The number S3 was authorised against: `130,806 × 0.0963 s` = 3.50 h of wall
clock on one `usher work` process, 95% CI [3.41, 3.59] h**, plus ~1.0 GiB into
`raw_payloads` and **261,612 follow-up jobs** nobody had priced. Sample: **500
titles, a systematic 1-in-261 walk of the tier, 0.38% of it**, drained through
the shipped `usher work` on 2026-08-11 **21:44:00Z → 21:44:48Z**. The bar — nine
predictions and what would count as failure rather than refutation — was written
**before the first request** [to `/tmp/m9-enrich/` and `/tmp/m9-exec/S2/`, tmpfs
on this host, so those files no longer exist and only this transcription
survives]; the driver and its `httpx.AsyncClient.send` probe ran outside the
working tree reading the operator's own `.env`. **499 × 200, 1 × 404.** No 429,
no 5xx, no transport error, no `Retry-After`. Whole-task budget **539 requests**,
key idle from **21:47:54Z**.

### ✅ Closed: the tier used to evict itself, and `m10a` stopped it

**What this run found, and it was the finding rather than a caveat:** the
predicate the walk selected on was *moved by the walk*. `vote_count` (today
`tmdb_vote_count`) was in `_ENRICHABLE`, the bulk loader wrote **IMDb
`numVotes`** into that column, and enrichment overwrote it with **TMDb's
`vote_count`** — two different electorates. Measured over all 537 titles this
task enriched: **80 still carried `>= 100` (14.9%)**, median TMDb vote count
**16** against a median IMDb `numVotes` of **581** on the unenriched tier, and
the tier's own count fell **130,806 → 130,349** as a direct arithmetic
consequence of enriching 537 of it.

✅ **That defect is closed and the closure is a rename plus a redirect, not a
workaround.** ADR-0040's Task 2 stopped the dual write and `m10a` split the
column; `apply_ratings` now writes `imdb_num_votes`, and `scripts/enqueue_tier_enrichment.py`
selects on it (`_PAGE` and `is_tier_movie` both, `TIER_MIN_VOTES = 100`). **The
tier now moves only when IMDb publishes a new dump**, because `imdb_num_votes`
has exactly one writer and no crawl touches it. Do not read the paragraph above
as live behaviour.

⚠️ **The redirect briefly emptied the predicate, which is worth knowing before
you re-spell one of these.** Between Task 2 and the repair, a tier spelled
`tmdb_vote_count >= 100` read **NULL >= 100 on every row of a freshly
bootstrapped catalog** — zero rows, so the crawl this script exists to start
could not start itself (issue #42). Nothing else fills that column: `upsert_titles`
omits it from its `DO UPDATE` list and `link_crosswalk` writes only
`tmdb_popularity`.

**The repair was a restoration, not a re-choice, and that is measured.** The tier
was always *de facto* an IMDb-votes tier — the column held IMDb `numVotes` on
exactly the skeleton rows where every unenriched candidate lives. Re-measured on
the deployed 1,272,870-title catalog after the ADR-0040 rebuild (script docstring,
2026-08-19):

| predicate | recorded 2026-08-11 | `imdb_num_votes` today |
|---|---|---|
| `kind='movie' AND >= 100` | 161,789 | **162,196** (+0.25%) |
| `+ tmdb_id IS NOT NULL` | 130,806 | **131,113** (+0.23%) |

Both within a quarter of a percent, the residual being eight days of vote growth
— **which is precisely why every downstream statistic in this file remains valid
and a re-choice would have destroyed them.**

**Three things that still follow, restated against today's schema:**

- **The keyset walk is safe, and the argument is now the boring one.** Under the
  old self-evicting predicate it needed a real argument: a row could only leave
  the tier by being enriched, which happened only after it was enqueued, which
  happened only after the cursor passed it. Under `imdb_num_votes` the population
  is stationary for the length of a run, so nothing can leave at all. **What
  survives from that argument is the reason it is a keyset and not an `OFFSET`**
  — an offset walk over a shrinking population skips rows silently, and
  `list_unmatched`'s offset (43.7 ms at offset 0, 388.9 ms at offset 1,126,574)
  is the measured shape of the same walk. The script's own comment says so.
- **`130,806` is a snapshot with a timestamp, not a property of the catalog.** It
  was the right fetch count for S3 only because the enqueue pass completed before
  the drain began. Interleaving them would fetch fewer titles and nothing would
  say so.
- **The `tmdb_id IS NOT NULL` conjunct is a correctness property, not an
  optimisation.** For the 30,983 tier movies without one, `_ref_for` raises
  `PortDataMalformed`, whose queue form is `retryable=False` — the job parks on
  its **first** attempt and needs a human. Dropping the conjunct buys 30,983
  parked rows, no data, and a `usher sync-status` an operator can no longer read.

✅ **ADR-0002's suggest benchmark shared this exposure and was re-anchored on
2026-08-19.** Its sampling frame is now `imdb_num_votes >= 500`, where it
reproduces to **+0.19%** — 48,639 unique-named movies against 48,549,
`shared_lower_names` 81,088 against 81,054, 2,991 cases against 2,993. Before the
split the old spelling selected **8,523** and `usher eval suggest --full` refused
to record a baseline against it. **The threshold is the only part of that
paragraph that did not have to move.**

🔴 **Refuted: "161,789 movies at 30 rps, ≈1.5 h".** Wrong in three independent
places at once, all flattering. The population is **130,806**, not 161,789
(30,983 tier movies carry no `tmdb_id`). The rate is **not** the token bucket:
`JobWorker._run_once` was a strictly sequential `for job in claimed:` and the
bucket lives on one client per process, so one worker ran at `1/latency` —
measured at **10.38 rps against a bucket set to 30**, idling at 35% of its
allowance. And the time is **3.5 h**, not 1.5 h. PRD 04's Phase-3 table carried
the same shape and was corrected in the same commit.

### The per-title cycle, by two instruments that agree to 0.5%

| | median | p95 | mean |
|---|---|---|---|
| HTTP request alone (probe, 500 requests) | 0.0580 s | 0.1049 s | 0.0637 s |
| whole job cycle (probe, inter-request starts) | 0.0896 s | 0.1411 s | 0.0963 s |
| whole job cycle (`raw_payloads.fetched_at` deltas, 499 rows) | 0.0892 s | 0.1389 s | 0.0963 s |

**HTTP is 65% of the cycle**; the other 35% is a title read, a `raw_payloads`
read, a JSONB insert, a title update, the two-request follow-up enqueue, the job
delete and a commit. **Use the mean and not the median to extrapolate a total** —
the distribution is right-tailed (max 0.56 s) and `Σ` wants the mean; the median
gives 3.26 h and is the wrong statistic for the question, which is why both are
here.

🔴 **Refuted three ways, and all three were this run's own predictions.** The bar
predicted a median of 0.12 s in a 0.09–0.18 s band and a p95 of 0.35 s in a
0.20–0.80 s band: measured **0.0896 s** (just below) and **0.1411 s** (far
below). It also predicted the 20 oldest tier movies would be *cheaper* per title,
on the argument that a 1919 film's payload is smaller. It is — 8,603 JSON
characters against 18,726 — and they were **slower**, median 0.113 s against
0.0892 s, because twenty jobs is all warm-up. The first twenty gaps of the
500-title segment have a median of 0.1033 s against 0.0894 s for the remaining
479. **A twenty-title segment cannot price anything; it can only price its own
cold start.**

🔴 **Refuted before the run, and it is why the sample is not a prefix: `ORDER BY
id` over this catalog is chronological.** `titles.id` is a UUIDv7 minted in IMDb
`tconst` order, so the first 500 rows of the tier have a **median year of 1919**
and a median `imdb_num_votes` of 367 (spelled `vote_count` at the time), against
**2006** and 581 for the tier as a whole. The systematic 1-in-261 sample used
instead lands at mean year 1996.3 / median 2005 / median votes 614.5 against the
tier's 1996.9 / 2006 / 581. **A prefix of a walk is a sample of the walk only if
the ordering key is independent of the thing being measured, and a UUIDv7 primary
key on a bulk-loaded catalog never is.**

✅ **Confirmed, and it is what makes the extrapolation defensible: per-title cost
is flat across the tier.** Because the sample is id-ordered it is also
chronological, so its quintiles are eras. A 31% swing in payload size moves the
cycle by less than 12%, and in the direction warm-up predicts rather than the
direction payload size does:

| quintile | mean year | mean payload chars | cycle median | HTTP median |
|---|---|---|---|---|
| 1 | 1957.3 | 16,435 | 0.0905 s | 0.0578 s |
| 2 | 1986.4 | 20,274 | 0.0962 s | 0.0564 s |
| 3 | 2004.1 | 16,449 | 0.0902 s | 0.0597 s |
| 4 | 2016.3 | 21,466 | 0.0865 s | 0.0575 s |
| 5 | 2018.0 | 19,007 | 0.0858 s | 0.0579 s |

### The costs the plan did not price

- **Two follow-up jobs per enriched title** — 537 enrichments produced exactly
  537 `INDEX` and 537 `DERIVE`, so the full run writes **261,612** further jobs on
  top of its 130,806. `DERIVE` drains on the same worker (it needs the provider,
  not the network — zero requests observed). `INDEX` does **not**:
  `composition.embedder` returns `(None, no-op)` unless `USHER_EMBEDDING_ENABLED`
  is on, which is off by default, so on shipped defaults the run leaves
  **130,806 index jobs pending forever** and `title_embeddings` stays empty.
  Whoever runs this has to decide that deliberately.
- **~1.0 GiB, confirmed within 2%.** Mean stored payload **6,914 bytes** (JSONB,
  TOAST-compressed, from 18,726 JSON characters), and
  `pg_total_relation_size('raw_payloads')` is **1.186×** the sum of the payload
  column. `6,914 × 1.186 × 130,806` = **1.07 GB / 1,023 MiB**.
- **The enqueue half is free: 5,000 jobs in 5 pages in 0.60 s**, interpreter start
  included, so the whole tier is ~16 s. `_PAGE` plans as an **Index Scan using
  pk_titles** with a filter — 36 ms for a 1,000-row page, 11,223 rows removed by
  filter, no `Seq Scan` and no `Sort`.
- **Parked: 1 in 500** — a 404, parked at `attempts = 1`, the
  `PortDataMalformed` taxonomy firing on the path it was written for. Scaled
  honestly that is a Wilson 95% interval of **46 to 1,470 parked jobs** over the
  tier, which **must not be quoted as "about 260"**.

✅ **Confirmed: a re-run inside the freshness window costs zero requests — on the
second attempt at the test, because the first was invalid and the reason is the
vote-count finding again.** Re-running the committed script's `--limit 20` and
draining it made **19 requests**, which reads as a refutation and is not one:
enrichment had moved the predicate, so *"the first twenty tier movies by id"* was
a **different** twenty. Re-enqueued by explicit id against titles that already
held a `raw_payloads` row, the same drain made **0** requests — and the probe's
`.installed` marker was checked first, because a probe that did not install and a
probe that measured zero produce the identical empty file. **A cache test has to
name the rows it expects to hit, never re-derive them from a predicate the system
under test is allowed to move.**

## The tier actually enriched, 2026-08-12 (M9 S3) — 130,334 requests, 1.98 h, and the first 5xx

**The run S2 priced, executed whole.** 22:08:53Z → 00:07:46Z, driving the shipped
`usher work` on the shipped `TmdbMetadataProvider`. Bar and drivers written
**before the first request** [to `/tmp/m9-exec/S3/`, tmpfs, so they no longer
exist]. The probe records **path only, never the query string** — a v3 key rides
in `api_key=` — and one file per pid, because three daemons appending to one file
is a torn line waiting to happen. All three `.installed` markers were asserted
before a single number was believed. Key idle from **00:07:46Z**.

**Status distribution: 130,141 × 200, 107 × 404, 86 × 502.** No 429, no transport
error, **no `Retry-After` on any response**. Two independent instruments agree:
the probe counted 130,334 requests and `raw_payloads` gained 130,141 rows, and
the difference is exactly the non-200s.

| | S2 predicted | measured | |
|---|---|---|---|
| wall clock | 3.50 h at 1 worker, 95% CI [3.41, 3.59] | **1.98 h at 3 → 2 workers** | see the crash below |
| fetches | 130,806 | **130,334** (130,806 − 537 cached + 65 retries) | ✅ |
| `raw_payloads` | ~1.0 GiB, mean 6,914 B | **995 MB**, mean **7,001 B** | ✅ within 1.3% |
| follow-up jobs | 261,612 | **261,294** = 2 × 130,647 successes | ✅ exactly two per success |

✅ **S2's 0.38% sample priced the median request correctly and the tail not at
all.** HTTP median **0.0588 s** against S2's 0.0580 s — 1.4% apart over 500
requests versus 130,334. But **p95 0.4267 s against S2's 0.1049 s, a 4.1×
blowout**, and mean 0.0993 s against 0.0637 s. **Concurrency does not move the
median request; it moves the tail, and a sequential sample cannot see that.**

🔴 **Refuted: "three workers at `30/N` each reach 30 rps".** That was this
repository's own arithmetic, recorded in PRD 04 and in S2's dispatch, and it is
wrong because it assumes per-worker throughput survives concurrency. It does not.
Three workers achieved **19.76 rps** — 6.59 rps each against S2's **10.38 rps**
on one worker, a **37% per-worker loss** — and the bucket, set to 10 per process,
was never binding on any of them. **The scaling factor from one worker to three
is 1.90×, not 3×.**

### One worker died 78 minutes in

**`usher work` crashed at 23:26:57Z with an unhandled `MissingGreenlet:
greenlet_spawn has not been called; can't call await_only() here`.** A defect in
the shipped worker that only a multi-hour run reaches; nothing in `tests/` has
ever executed this path.

- **The run finished on two workers.** Phase 1 (3 workers, 78.1 min): 92,550
  requests, **19.76 rps**. Phase 2 (2 workers, 40.8 min): 37,784 requests,
  **15.43 rps**. Per-worker throughput actually *rose* when the third died —
  6.59 → 7.72 rps — which is the contention story from the other side.
- **Its 20 claimed jobs were orphaned in `status = 'running'` forever**, because
  `JobWorker.startup()` was the only thing that requeued an abandoned claim and
  it ran once, at process start — while `older_than_seconds = 0.0` made a restart
  steal every *other* worker's live claims. ✅ **Closed by W1**, below:
  `startup()` no longer exists, `recover()` runs on a timer with a lease, and
  those twenty would now come back after five minutes.
- **The crash's last two log records are both the `ix_titles_imdb_id` conflict
  path**, 19 ms and 24 ms before death. Correlation stated, causation **not**
  established; the ~30 earlier conflicts on the same process did not kill it.

### A failure class no taxonomy in this file covers: `ix_titles_imdb_id`

**30 of the 139 parked jobs are not upstream failures at all — they are write
conflicts on the catalog's own unique index.** `titles.imdb_id` is
`CREATE UNIQUE INDEX ... WHERE imdb_id IS NOT NULL`; enrichment writes TMDb's
`external_ids.imdb_id` over the bulk export's, **the two crosswalks disagree, and
the id TMDb hands back is already held by a different catalog row**. Confirmed
rather than inferred by re-fetching five through the shipped provider
(`tt0023002`→`tt9731440`, `tt0026564`→`tt0026577` on an adjacent `tconst`, …) —
every target already owned by another movie.

Rate **30 in 130,806 = 0.023%**. It is *not* `PortDataMalformed`, so it retries:
**every occurrence burns all five attempts and the full backoff schedule** before
parking, and the fetch is re-made each time because the failing write rolls the
`raw_payloads` insert back with it. That is the 65 retry requests above. The title
is left `skeleton` with `enrichment_error` set — `_record_failure` logging *"tier
stays skeleton"*, which is the M4 finding about a failure handler that must not
reset the tier, working correctly.

### The first 502s, in two exact bursts of 43

**86 × 502, all inside 526 s (23:07:50Z → 23:16:36Z).** Before this run the
repository had observed **zero** 5xx from TMDb in 1,644 requests across three
runs. None carried `Retry-After`. All 86 were classified `PortUnavailable`
(retryable), backed off and **recovered** — not one parked job carries a
502-shaped error, which is the retry taxonomy working on a branch that had never
fired in production. The two-identical-bursts shape suggests one upstream node
cycling rather than load shedding, and **no 429 appeared at any point**, so this
is not the rate limit in disguise.

### The pre-state, the frozen tier and the post-state

**Pre-state read from the database at 22:02Z**, not from a script's stdout. The
plan's premise guard — one row, `(skeleton, 1,272,367)`, `count(raw_payloads) =
0` — **is not the state that existed**, because S2 enriched 537 titles against
the same scratch database first: 1,271,830 `skeleton` + 537 `enriched`, 537
payloads, 537 pending `index` jobs, 2 parked `enrich`. Recorded as what it was.

**The tier was frozen before the first request** — `s3_tier_snapshot`, 130,806
ids, created 22:02:21Z. In today's spelling:

```sql
SELECT id FROM titles WHERE kind='movie' AND tmdb_id IS NOT NULL
  AND (imdb_num_votes >= 100 OR enrichment_state <> 'skeleton')
```

⚠️ The run itself said `vote_count`, which is what that column was called before
`m10a`; on a catalog at or above `m10a` the copy-pasteable spelling is the one
above. That predicate alone gave **130,349**; the 457 rows S2's enrichment had
already pushed below the floor bring it to **130,806**, reconstructing the plan's
population exactly. Every count below is over those frozen ids.

| over the frozen tier (n = 130,806) | |
|---|---|
| `enriched` | **130,647** (99.88%) |
| still `skeleton` | **159** = 139 parked + 20 orphaned by the crash |
| `enrichment_error` non-NULL | 140 |
| `overview` (weight class C) | 129,926 (99.33%) |
| `tagline` (weight class C) | 54,567 (41.72%) |
| `genres` (weight class D) | 130,781 (99.98%) |
| `keywords` (weight class D) | 82,405 (63.00%) |
| `field_provenance.tmdb_vote_count = 'tmdb'` | 130,647 |

⚠️ That last row was taken as `field_provenance.vote_count`; `m10a` renames the
JSONB key along with the column, so the spelling above is what reproduces today
and the old one returns 0. The measurement is untouched: all 132,415 rows
carrying provenance were measured to carry all three keys with every value
`tmdb`, which is why that rename needed no inference.

**The shortfall is 159 and every one is accounted for**: 109 titles TMDb has
merged away (404, parked at `attempts = 1`), 30 `imdb_id` conflicts (parked at
`attempts = 5`), 20 orphaned by the crash. It is **not** a `tmdb_id` coverage
finding — the `tmdb_id IS NOT NULL` conjunct had already removed those 30,983
before the walk began.

✅ **A re-run inside the freshness window costs nothing, at scale.** All 537
titles S2 had enriched were re-enqueued as part of the snapshot and **not one was
re-fetched** — `raw_payloads.fetched_at` on all 537 is still S2's 21:4xZ.

✅ **Self-eviction confirmed over 130,806 rather than 537 — and then closed by
`m10a`.** Of the frozen tier's 130,647 enriched rows, **21,640 (16.56%)** still
carried `>= 100` in the dual-written column, against S2's 14.9% from 537 rows, so
the small sample was low by 1.7 points and directionally right. Median TMDb
`vote_count` **15** against a median frozen IMDb `numVotes` of **576**. Under the
old spelling `kind='movie' AND vote_count >= 100` had fallen **161,332 → 52,782**
and with `tmdb_id` **130,349 → 21,799**. ⚠️ **Those two numbers are a different
population from every other tier statistic in this file**, all of which were taken
before enrichment — and they are also **not reproducible today**, because that
column is now `tmdb_vote_count` and holds only TMDb's count while the tier is read
from `imdb_num_votes`, which the crawl never touched. They stand as the
measurement that motivated ADR-0040, not as a query to re-run.

**What the run left behind, priced.** `enrich` drained; **261,294 follow-up jobs
did not** — at the two surviving workers' 34.2 jobs/s, ~1.9 h of `index` +
`derive` backlog. Those jobs are durable and nothing is lost. `title_embeddings`
was **11,585 and climbing at 17.0/s**, `title_neighbors` 0, `people` 57,703,
`credits` 290,760, `pg_database_size` 2,746 MB.

### The `index` lane did not drain beside the `enrich` lane, and the flag was not why

**`title_embeddings` sat at 542 through the entire enrich run and then jumped to
4,929 within minutes of the enrich queue emptying** — with the embedder on the
whole time. This is head-of-line blocking, and the mechanism is one line of the
claim: `ORDER BY priority DESC, created_at`. Every job was at
`JobPriority.BACKFILL` (20) and the enqueue wrote all 130,804 `enrich` rows
inside a **1.3-second window**, while an `index` job is only created when its
title finishes enriching — so **every enrich job sorts ahead of every follow-up
job** and `LIMIT 20` never reaches past them.

**Three independent checks that the embedder was on**, recorded because the
alternative reading — `composition.embedder` returning `(None, no-op)` — produces
an identical-looking backlog and was the live hypothesis for an hour:
`/proc/<pid>/environ` on the live claimers; the absence of the no-op branch's
`"no embedding model configured"` line from all three worker logs *while* its
structural sibling `"no LLM configured; curate jobs will not be claimed"` was
present in all three; and a pre-run calibration that drained 537 `index` jobs to
537 embeddings in 12.7 s at zero outbound requests.

**So `USHER_EMBEDDING_ENABLED` is necessary and not sufficient** — it makes index
jobs *claimable*, but a bulk enqueue at a single priority defers them wholesale
until the bulk lane is empty. W1's pool narrows this and does not remove it: a
pool does not fix an *ordering*. What W1 changed is that a `sync` or a
`bootstrap` no longer stops the other kinds dead for its duration.

⚠️ **The gauge-refresh tax this run also measured has moved to its own file.**
`usher work` calls `SearchGauges.refresh` after **every** `run_once`, i.e. every
20 jobs, and this run grew its population 244× (16.4 ms at 7,718 enriched rows →
327.9 ms at 88,001). `.claude/rules/search-and-embeddings.md` carries the current
numbers, the 130,647-row figure it flattened to, and the idle-worker consequence.

## M9 Task W1 (2026-08-12) — the lane was the ceiling, not the policy

**Bar written, hashed and committed before the first line of source changed** —
[`/var/tmp/w1/BAR.md`](/var/tmp/w1/BAR.md),
`sha256 4178b99eca239f970f2da9ef2ee5c1323c578297928216cd450fa6e7a5aad4f1`,
2026-08-12T15:27:48-05:00, against a clean tree at `3625b94`.
`scripts/measure_worker_lane.py` re-hashes it at run time and prints the digest
in its own log, so an edit made after a number was seen shows up.

➡️ **The lane's own design — the scope factory, `asyncio.wait` over `TaskGroup`,
the `SourceRegistry` split, the adapter-construction lock, the lease and
heartbeat — is in `.claude/rules/api-telemetry-and-lanes.md`** (`services/jobs.py`
is its trigger) and in
[ADR-0037](../../docs/prd/decisions/0037-the-worker-is-a-bounded-pool-of-scopes.md).
What stays here is the part about TMDb's rate.

**Measured against a local stub, never against TMDb.** ADR-0005 chose ~25 rps as
courtesy against a stated ~40 and S3 already drew 86 × 502 from that server, so
probing the real ceiling is not something this bar permits — and the stub is the
*accurate* instrument as well as the courteous one, because it isolates the lane
from upstream variance. Both arms ran back to back against **one** throwaway
`pgvector/pgvector:pg17`, the baseline from a `git archive 3625b94` tree.

**The success criterion was deliberately not a throughput number**, because S3's
19.76 rps came from a bucket that was never binding, so a faster number would not
distinguish "the lane got better" from "the box got faster". The bar is that
throughput **tracks the configured limit from a single process**:

| configured | baseline `3625b94` | ratio | after `m9/w1` | ratio |
|---|---|---|---|---|
| 5 rps | 4.991 | 0.998 ✅ | 5.003 | **1.001 ✅** |
| 12 rps | 9.641 | 0.803 ❌ | 11.943 | **0.995 ✅** |
| 24 rps | 10.075 | **0.420 ❌** | 24.187 | **1.008 ✅** |

Band `[0.85, 1.05]`, declared in the bar. Requests counted **at the stub**, over
the window from the first request past a 2 s warm-up to the last, 1,500 enrich
jobs seeded per arm, 45 s per setting. `retried = 0` and `enriched = done` in all
six runs — the premise guard that separates a rate from a measurement of the
backoff schedule. **The baseline's ceiling is ~10 rps whatever it is asked for**,
which is S2's own one-worker 10.38 rps reproduced against a stub; the 5 rps row is
the control that makes the other two mean something.

**Observed overlap, not a count** (`CLAUDE.md`'s fourth evidence rule). Peak
concurrent in-flight at the stub: **1, 1, 1** on the baseline against **5, 12,
12** after; intersection-over-union **0.0000** against 0.108 / 0.423 / 1.227.

🔴 **Prediction B1 was wrong in the flattering direction and the harness said
so.** The bar predicted a sequential ceiling of 6–9 rps; measured 9.64–10.08.
**The cause is the stub, not the lane: no two-parameter lognormal reproduces all
three of S3's statistics**, and the fit shipped in the first run matched the
median and the *mean* while under-reproducing the p95 by 39%. The comment beside
it claimed the p95 fit "lands the mean within a percent", from an arithmetic slip
— `(ln(0.4267) + 2.834) / 1.645` is **1.205**, not the 0.9007 written next to it.

| `sigma` | median | mean | p95 |
|---|---|---|---|
| S3, live, n = 130,334 | 0.0588 | 0.0993 | 0.4267 |
| 0.9 (shipped default) | 0.0587 | 0.0882 (-11%) | 0.2585 (**-39%**) |
| 1.205 | 0.0588 | 0.1214 (+22%) | 0.4267 |

**The real distribution is more skewed than a lognormal**, which is S3's own
*"concurrency moves the tail"* arriving as a fitting problem. Caught only because
the harness prints its drawn median, mean and p95 **beside the live ones** — a
harness printing the median alone would have agreed to four decimal places and
said nothing. Both fits are run and both reported; the default stays at 0.9
because that is what the first run was taken with, and **moving an instrument
after seeing a number is how a bar stops being one.**

**Both arms again at the p95-matched tail, which is the sterner test**
(`--sigma 1.205`, same box, same throwaway Postgres):

| configured | baseline `3625b94` | ratio | after `m9/w1` | ratio |
|---|---|---|---|---|
| 5 rps | 4.473 | 0.895 ✅ | 5.004 | **1.001 ✅** |
| 12 rps | 7.415 | 0.618 ❌ | 11.833 | **0.986 ✅** |
| 24 rps | 6.744 | **0.281 ❌** | 21.626 | **0.901 ✅** |

Peak in-flight **1, 1, 1** against **5, 12, 12**; IoU **0.0000** against
0.259 / 0.796 / 1.702. Quiet: drift **-0.0166** and **+0.0056**, foreign 0. **B1
is vindicated at the fit it was derived from** — the bar's 6–9 rps against a
measured 6.7–7.4; the 9.6–10.1 of the first run was the stub's missing tail.

⚠️ **The sterner run prices `job_concurrency = 12` honestly: it holds ~21.6 rps,
not 25.** The ratio at 24 rps is 0.901 — inside the declared band and the lowest
number in the table — so twelve in flight sits *at* ADR-0005's policy limit rather
than comfortably inside it, which is exactly where Little's law over the p95 put
it. An operator who wants a firm 25 rps against a real TMDb tail should raise
`USHER_JOB_CONCURRENCY` (and the pool with it, which `Settings` will insist on).
Recorded rather than changed: **the default was derived before the measurement and
moving it to fit the measurement is how a derivation becomes a curve fit.**

### Per-kind concurrency, and the one number that is not measured

`usher.services.jobs.KIND_CONCURRENCY` (`services/jobs.py:170-182`) is total over
`JobKind` — **nine members, no default** — so a new kind with no entry fails
`tests/unit/test_config.py::test_the_worker_concurrency_settings_have_the_measured_defaults`
rather than silently inheriting a number chosen for something else. Six rows
cover the nine:

| kind | in flight | from |
|---|---|---|
| `enrich` | the global, 12 | Little's law over S3: p95 0.4267 s HTTP + ~0.033 s bookkeeping ≈ 0.46 s a job, so ~11.5 to hold ADR-0005's ~25 rps |
| `match`, `watch_history`, `watch_writeback` | 4 | a *household* server, never run under concurrency by this project; a deliberately conservative constant, not a throughput claim |
| `derive` | 4 | ⚠️ **the one number the source calls "not measured"** — the connection-pool budget (four of `db_pool_size`'s twenty), not a throughput |
| `index` | 1 | `fastembed` holds 8,000–10,700 tokens/s **flat across the size range**; a CPU ceiling is not raised by asking from more coroutines, and the parallelism unit is already `embedding_batch_size` |
| `curate` | 1 | pool 600 renders ~12,540 prompt tokens, **56 tokens** under `max_model_len` — no room for a second generation's KV cache |
| `sync`, `bootstrap` | 1 | a walk of 1,126,674 items; ADR-0015's retraction ceiling is per run; `bulk_load_window` commits the caller's session |

**Two rows carry a not-measured mark, and only one of them is the source's own
phrase.** The household cap is a policy constant that says so; `derive` is the
row the comment singles out as *"the one number here that is not measured"*. The
measurement that would move it is derive jobs/s against 1, 2, 4 and 8 in flight
on one pool; nothing here has run it.

### 🔴 Refuted: the shared-session explanation for S3's `MissingGreenlet`

W1 was dispatched on the hypothesis that the crash was *"the canonical symptom of
an `AsyncSession` touched from the wrong context"*. **The deployment shape refutes
that outright, and no measurement was needed to see it.** The process that died
was `usher work`, which held **one** session for the life of the command and ran
**one** job at a time — `asyncio.run(_work(...))` creates no tasks — so there was
no second coroutine to touch that session. What the per-job scope removes is a
real hazard demonstrated by
`tests/integration/test_services_jobs.py::test_two_concurrent_jobs_on_one_shared_session_really_do_break`,
i.e. the state W1 would have created had it stopped at the `gather` — **not**
evidence about the crash. **So `MissingGreenlet` is not claimed fixed, and "did
not reproduce" is not "fixed": it took ~78 min and ~92,000 jobs to appear once.**

⟲ **Correction (2026-08-19, issue #8): the stack was discarded, not
un-captured.** The original write-up blamed a missing `usher --traceback work`
and **that reads the cause backwards** — `MissingGreenlet` is a `SQLAlchemyError`,
which was in `cli.OPERATOR_ERRORS`, so the CLI's own boundary caught a programming
error and replaced its traceback with one line. Narrowed to `DBAPIError`, and
`JobWorker._run` now logs the crashing job's kind and key with its traceback
before re-raising. One candidate mechanism now has a measured artefact behind it —
a caught `RepositoryConflict` leaving an **expired** `TitleRow` in the identity
map — but 1,598 jobs of reproduction did not fire it.
`.claude/rules/db-and-sql.md` carries that half.

## Enrichment was deleting genres, not re-spelling them (2026-08-19, issue #30)

`genres` is in `_ENRICHABLE`, so a provider that supplies **any** genre replaces
the whole array. `titles.genres` unions two importers' vocabularies — IMDb's 28
labels from the bulk phase, TMDb's 19 movie / 16 television ones from here — and
the two are **disjoint on every concept they both name**. What that does to a
title with a label TMDb has no word for is not a re-spelling.
[ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md).

**The measurement, and the control is what makes it evidence.** Issue #30
*inferred* the deletion from the replace-list plus the label distribution. It is
now observed, by joining the real `title.basics.tsv.gz` (2026-08-10) to the live
catalog (1,272,866 titles):

| | |
|---|---|
| enriched titles the dump also gives genres for | 132,116 |
| …that lost at least one IMDb label | **53,724 (40.7%)** |
| total label deletions | **69,160** |
| …of a concept TMDb cannot express | **11,466** |
| **skeletons that lost a label** | **0 of 1,021,623** |

**The zero is the control.** Without it, 53,724 is equally consistent with the
dump being newer than the catalog, with the parser dropping labels, or with the
comparison being wrong — the same shape as *"a run that did not run is not a
pass"*, one lane over.

The mechanism is confirmed independently, from `raw_payloads`: of 132,407
enriched titles with a cached TMDb payload, **130,826 of the 130,826 whose
payload supplied any genre have `titles.genres` byte-identical to that payload's
list, and zero differ.** The 1,581 that differ supplied *no* genres, where
`_changes` skips the field — which is also why only 108 enriched titles retain an
IMDb-only label at all, and four of those are TMDb's own TV `News`.

Per label, deleted against survived: `Biography` 5,562/34, `Musical` 2,767/39,
`Sport` 2,115/13, **`Film-Noir` 827/0**, `Short` 174/0, `Game-Show` 21/2. And
`Drama` 13,141, `Crime` 5,506, `Romance` 5,360 — those are TMDb *disagreeing*,
which it is entitled to do and is usually right about. **The two are different
defects and only one of them is a defect**, which is why the fix keys on the
provider's vocabulary rather than on "keep everything".

**The rule that shipped.** `MetadataProvider.genre_vocabulary` — the canonical
concepts a provider can express, i.e. **the set it is entitled to delete** —
abstract with no default, for `EnrichmentResult.seasons`' reason.
`EnrichService._genres_after` (`enrich.py:424`) keeps any existing label whose
concept is outside it. TMDb's is derived from `TMDB_GENRE_NAMES` rather than
restated, because two hand-maintained copies of one vocabulary drift and **the
failure is silent**: a concept wrongly in the set is a label enrichment goes on
deleting, which looks exactly like enrichment working.

**It stales nothing extra.** `genres` is segment 6 of `compose_document`, so a
preserved label moves `_FINGERPRINT_SQL` — but `_apply` already enqueues an
`INDEX` job for every successful enrichment, so a title reaching the merge was
being re-embedded on that pass anyway. That is what makes this affordable where
normalising the existing 1,272,866 rows is not.

**Two of the issue's own claims were wrong, and the same `GROUP BY` over
`unnest(genres)` said so.** `Reality-TV`/`Talk-Show`/`News` are listed there as
having no TMDb equivalent — TMDb's **television** vocabulary has `Reality`, `Talk`
and `News`, so they are re-spellings, and the real gap is seven concepts (`Adult`,
`Biography`, `Film-Noir`, `Game-Show`, `Musical`, `Short`, `Sport`). And *"whether
TMDb's TV vocabulary ever reaches this column"* is filed there as unmeasured:
**all of it does** — `Sci-Fi & Fantasy` 165, `Action & Adventure` 154, `Reality`
57, `War & Politics` 25, `Kids` 19, `Soap` 19, `Talk` 4 — and three of those
*fuse* concepts the movie vocabulary keeps apart, which is why an alias maps to a
**tuple** of canonical labels rather than to one. The general form, and the reason
this section exists: **an issue's "not measured" section is a list of queries
nobody ran, not a list of things that are hard to know.**

## Still unverified against TMDb, named rather than implied

Consolidated across every run this repository has made — 712 (M4) + 393 (T2) +
539 (S2) + 130,334 (S3), all sequential-or-three-worker, all `tmdb_region=US`:

- **A real 429, and whether one carries `Retry-After`.** Zero 429s in any run.
  Widened by S3: **193 non-200s carried no `Retry-After` at all**, so whether
  TMDb ever sends the header is now open too.
- **A v4 read access token in any form**, so `_is_v4_token`'s positive branch has
  never been exercised against a real credential.
- **A season TMDb lists that its own route refuses** — guess 8, 626 listed
  seasons over 44 series, zero absent. The movies-only S2/S3 runs add nothing.
- **TMDb under sustained concurrency beyond three workers**, given that three
  returned 1.90× and the marginal worker was measured to *reduce* per-worker
  throughput.
- **Any of it against a non-`US` `tmdb_region`.**
- **Whether the `MissingGreenlet` crash is caused by the `ix_titles_imdb_id`
  conflict path or merely followed one.**
