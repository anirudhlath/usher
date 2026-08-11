---
paths:
  - "src/usher/adapters/tmdb/**"
  - "src/usher/services/enrich.py"
  - "src/usher/services/handlers.py"
---

# TMDb and the enrichment stage

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**The TMDb half of M4's live verification: ten open guesses, eight
settled, and the two corrections both went the same way — TMDb is more
silent than the code assumed, not louder.** Run 2026-08-01 against
`api.themoviedb.org/3` with a real v3 key, driving the shipped
`TmdbClient`/`TmdbMetadataProvider`/`usher.adapters.tmdb.mapping`/
`usher.services.matching._confident` from a throwaway script outside the
working tree. **712 requests total**, GET only, no write route of any
kind touched. Before this run **no request had ever been made** from this
repository and every TMDb fixture was a transcription of documentation.
The whole status distribution, since it is the evidence for half the table
below: **699 × 200, 7 × 404, 2 × 401, 2 × 422, 2 × 400 — and no 429 and no
5xx at all.** Every one of the thirteen non-200s was deliberately provoked
and is accounted for in the table below.

| # | Guess | Verdict | Evidence |
|---|---|---|---|
| 1 | TMDb sends `Retry-After` on a 429 | **still unverified** | Zero 429s in 712 requests at 25 rps, and no `retry-after` header on *any* response including the 401s and 422s. Deliberately not provoked. |
| 2 | An invalid `append_to_response` namespace errors | **refuted** | `200`, key silently absent — for a wrong-space namespace *and* for `zzz_not_a_namespace`. |
| 3 | The 404 body shape | **confirmed & recorded** | `{"success": false, "status_code": 34, "status_message": "The resource you requested could not be found."}`, `application/json;charset=utf-8`, on `/movie`, `/tv` and `/tv/{id}/season/{n}` alike. |
| 4 | A v4 read access token is JWT-shaped | **unverifiable here, cost bounded** | The configured credential is a classic 32-hex v3 key; `_is_v4_token` correctly says no. A false positive was measured instead: the v3 key sent as `Authorization: Bearer` answers **401** (`status_code: 7`), i.e. loud and immediate, never a wrong answer. |
| 5 | The changes window's inclusivity and its 14-day cap | **confirmed, and it is the boundary** | `start == end` is a valid one-day window (4,278 results); `[d, d+1]` covers both days deduplicated; `[today-14, today]` → 200; `[today-15, today]` → **422**, `"Invalid date range: Should be a range no longer than 14 days."` The shipped clamp sits exactly on it with nothing spare. |
| 6 | `credits` is a valid TV append namespace | **confirmed** | Present with 14 cast entries. `aggregate_credits` is *also* valid — a second view, not a replacement. |
| 7 | `append_to_response=season/N` works | **confirmed, and shipped — see below** | It does, and it collapses a series from 1+N requests to 1. `TmdbMetadataProvider.fetch` has issued the blind window since M9's T1. |
| 8 | A season the series lists that 404s on its own route | **still unverified** | 320 listed seasons across 30 series, **zero** absent. The propagate-and-park branch has still never met a real occurrence. Sample skews popular, so it is weak evidence of absence. |
| 9 | Search orders by relevance with the obvious answer first | **confirmed** | 263 of 266 confident resolutions were TMDb's **first** result (max rank 3; series 126/126 at rank 0), and the top result was an exact normalised name match on 269 of 320 probes. |
| 10 | `spoken_languages[].iso_639_1` and `origin_country` are well-formed | **confirmed** | Zero anomalies over 59 detail payloads; `origin_country` present on 29/29 movies and 30/30 series, always a list of strings. |
**Two things live TMDb contradicted, both now fixed with a failing test
first.**

- **A 4xx that is not a 429 is `PortDataMalformed`, not `PortUnavailable`.**
  Observed: **422** for a 15-day change window (`status_code: 20`) and
  **400** for a 21-item `append_to_response` (`status_code: 27`, *"the
  maximum number of remote calls is 20"*). Both were classified as outages,
  so `JobWorker` would spend five rate-limited retries and a backoff
  schedule reaching the identical answer and then park with the wrong
  reason. 408 is excluded and stays retryable — TMDb has never been
  observed sending one, but `Settings.tmdb_base_url` exists so a household
  can front TMDb with a proxy.
- **TMDb's year filter is exact where the match ladder's is ±1.** All 294
  candidates returned across 320 probes carried *exactly* the year asked
  for, so `_confident`'s own `abs(candidate.year - item.year) <= 1` never
  fired once and tier 4 silently ran at ±0. 26 of 320 came back empty
  rather than one year off; re-asking those without the year resolves
  **13**, every one a title TMDb dates a year away from IMDb (Danny Phantom
  2003/2004, Toast of London 2012/2013, …). `TmdbMetadataProvider._search_one`
  now retries yearless when the filtered search finds nothing. A *fallback*
  and not a widening, because dropping the filter outright was measured too
  and is worse: 6 of 133 already-resolving names stop resolving, since
  "exactly one survivor" across every year at once is a harder test than
  within one.
**`_confident` against TMDb's own search: 83.1%, and 87.2% with the
yearless fallback.** The number the Emby half explicitly could not take.
320 IMDb names (160 movies / 160 series) stratified into four `numVotes`
bands, each searched through the shipped provider and judged by the shipped
rule: **87.5% of movies**, **78.8% of series**; by band, 90.0% / 91.3% /
81.3% / 70.0% descending, so a real library — which sits at the popular end
— should expect the high eighties to low nineties. Failures decompose as 26
zero-result, 22 results-but-no-exact-name, 6 ambiguous. Compare tier 3's
72.2%/75.3% for the identical predicate over the local 1.27M-row catalog:
**different candidate sets and different name samples, so these are
counterparts, not a before/after.** The IMDb-derived names are a proxy for
Emby names, which were not available to this run — stated rather than
implied.
**`append_to_response=season/N` works, and it is worth ~10x on the series
half of the enrichment path.** One request carrying
`credits,keywords,images,videos,external_ids,content_ratings` plus
`season/0…season/13` — **exactly** TMDb's 20-item ceiling — returned Game of
Thrones' entire hierarchy, **all 373 episodes across 9 seasons**, in place
of the ten requests the shipped path costs. Four supporting facts, each
measured because the change rests on it:

- The ceiling is **enforced**: 21 items is a **400**, `status_code: 27`.
  Six namespaces already appended leaves exactly 14 season slots.
- `season/0` (specials) appends like any other, 300 episodes on GoT.
- An unlisted season number is **silently omitted**, not an error — which
  is also the cheap detector guess 8 was scanned with.
- The appended block is identical to the season's own detail response
  **but for a missing top-level `id`**, and the series' own `seasons[]`
  summary carries that same id (3627/3624/107971 on GoT, byte-identical to
  the season route's). So `_compose_seasons`' existing merge-over-the-summary
  would lose nothing.
**Implemented 2026-08-11 (M9 T1), against fixtures only — no live call was
made by that change.** `TmdbMetadataProvider.fetch` asks for
`season/0…season/13` blind alongside the six namespaces, pops every
`season/N` block off the payload before returning it, reconciles the blind
window against the `seasons[]` summary the *same* response carries, and
follows up for any listed number the window missed; a follow-up carries no
namespaces so it gets all twenty slots. **Identity with the `1+N` payload is
the contract and the request count is only the benefit** —
`mapping.seasons_and_episodes`, `EnrichService._store_hierarchy` and
`DeriveService` all read `raw_payloads` rows written months earlier, so a
divergence is invisible until a derivation much later returns nothing, and
`test_the_composed_payload_equals_what_the_per_season_path_produced` is the
case that holds it. **Three things that case had to be given to have teeth**,
each of which a first draft got wrong:

- **The two spellings must reach different endpoints or the equality is a
  tautology.** The fake serves the season route and the appended blocks
  independently and each arm has one of them turned off, so the assertion is
  between two transports rather than between one and itself.
- **The fake's season-route response has to carry the summary's own `id`.**
  The committed fixture is one `season.json` reused for every number, so
  before this change the fake answered `id: 96000001` for season 0 as well —
  a disagreement the live run measured the real API not to have, and one that
  would have failed the identity case on the fake rather than on the provider.
- **On faithful data the merge *direction* is unobservable**, because the
  block and the summary agree on every shared key, so block-over-summary and
  summary-over-block produce the identical dict. The fake keeps
  `season.json`'s prose whatever the number, which makes season 0's block
  disagree with the Specials summary on `name`/`air_date`/`poster_path`, and
  that disagreement is the only thing in the suite that can see the
  direction. Recorded as a deliberate fake affordance, not as fidelity.

**And one property the `1+N` shape had that the appended one cannot have:
a missing season used to be loud.** The old `fetch` let a season's own 404
propagate and park the job, arguing that "a catalog that says a show has
seven seasons when it has eight is wrong with no signal anywhere, and a
parked job is at least visible". `append_to_response` cannot express that: a
season the series does not have and a season TMDb declines to serve are the
**same 200 with the key absent**, so the two are indistinguishable at the
request layer and a listed season whose block never arrives now yields a
`Season` row with no episodes rather than a parked job. The reconcile still
spends one follow-up on it, so the case is paid for even though it is not
reported. Traded knowingly, and the trade is cheap only because guess 8 is
what it is — 320 listed seasons over 30 series, zero absent, still
unverified rather than confirmed.

Four plants, each run against the whole `tests/unit` selection: the merge
direction inverted and a surviving `season/N` key each fail the identity case
**alone**; the reconcile-against-`seasons[]` loop deleted fails 3; and the
slot arithmetic loosened by one (a 21st item assembled) fails 3. One
equivalent-mutant control — the two literal `*_APPEND_TO_RESPONSE` constants
swapped — passes all five gate steps.
**The arithmetic, corrected 2026-08-01 — it was internally inconsistent
when first recorded, and the wrong number was the headline one.** The
path shipped until M9's T1 cost `1 + N` requests for a series (one detail,
one per season); the appended path, which is the shipped one now, costs 1.
At **32,409 series** and a **median of
9 seasons** that is 32,409 × 10 = **~324k requests** against **~32k**, i.e.
**~10x** — not the "~190k → ~35k, ~5x" first written here. `~190k` was
[PRD 04](../../docs/prd/04-catalog-bootstrap.md)'s Phase-3 tier-1 line, "~189k
titles with ≥100 IMDb votes", borrowed one section over: a *whole-catalog
title* count read as a *series request* count. Nothing measured it. The two
figures cannot both be right — 32,409 × 10 is 324k, and ~190k would need a
median of ~4.9 seasons.
**The median is measured, and its sample is not a library.** 320 listed
seasons across the 30 series the 2026-08-01 run walked, which is also the
sample guess 8 is scanned against and which that entry already calls
popular-skewed and weak evidence. Popular series have many seasons, so a
real 32,409-series library's median is very likely *lower* and ~324k is an
upper bound on the measurement taken rather than a prediction. Recorded
with its sample instead of laundered into a constant — the same treatment
`_confident`'s 72–75% and 83.1% get, and for the same reason.
**~32k, not ~35k, and the difference is the ceiling.** One request per
series is 32,409 exactly. Six namespaces leave 14 season slots, so a series
with more than 14 seasons needs a second request; that is a small tail, so
~32k is the figure and ~35k a generous allowance for it. Both are the same
number to one significant figure; the ~10x is what matters and it holds
either way. **The shipped fetch has a second source of follow-ups the
arithmetic above does not price**, and it is the same small tail seen from
the other side: a season the series lists whose block never arrives inside
the blind window costs one follow-up too, whether it sits outside the window
or TMDb simply omitted it. Guess 8 above is still unverified — zero absent
seasons in 320 — so the second case has never been observed at all, and the
follow-up is bounded at one attempt per fetch either way.
**TMDb's movie/TV divergence runs through three layers of its API, not
one, and all three are now measured rather than read.** The field-name and
endpoint rows were read from `developer.themoviedb.org` on 2026-07-31 and
**every one was confirmed live on 2026-08-01** over 29 movie and 30 series
detail responses.

- **Field names.** `title`/`name`, `original_title`/`original_name`,
  `release_date`/`first_air_date`, `runtime` (minutes) against
  `episode_run_time` (an array), `keywords.keywords` against
  `keywords.results`, a top-level `imdb_id` against `external_ids.imdb_id`.
  Tabulated in `usher.adapters.tmdb.mapping`'s docstring. Live: 29/29
  movies carried the whole movie column and **none** of the series column;
  30/30 series the mirror, with `external_ids.tvdb_id` non-null on all 30.
- **Endpoints.** `/movie/{id}` against `/tv/{id}`; `/search/movie` with
  `primary_release_year` against `/search/tv` with `first_air_date_year`;
  `/movie/changes` against `/tv/changes`; and a series' episodes live
  behind `/tv/{id}/season/{n}`, which has no movie counterpart at all.
- **`append_to_response` vocabularies.** `release_dates` is a movie-only
  namespace and `content_ratings` is the TV-only equivalent. **The
  consequence was stated wrongly and is corrected**: a shared list does not
  ask for a namespace that does not exist and get an error, it gets `200`
  with the key absent. So the failure is silent — half the catalog loses
  its certification on a response that looks entirely successful — which is
  a *stronger* reason for the split than the one previously recorded.
**`episode_run_time` is empty on 86.7% of series** — `[]` on 26 of 30 live
detail responses, Game of Thrones among them. `Title.runtime_minutes` is
simply not a fact TMDb still holds about most television, and `None` is the
answer rather than a mapping gap. The committed `series.json` fixture
carries the rarer populated shape, so the common one needed its own case
(`test_an_empty_episode_run_time_is_the_common_case_and_is_not_a_failure`).
**ADR-0011 is not a theoretical hazard: 12 of 14 small ids probed are live
in both id spaces, and every pair is an unrelated work.** Live 2026-08-01 —
`550` is *Fight Club* and *Till Death Us Do Part*; `238` is *The Godfather*
and *Star Cops*; `680` is *Pulp Fiction* and *Shaquille*; `605` is *The
Matrix Revolutions* and *Sabrina, the Teenage Witch*. No movie payload
carried a `name` key and no series payload a `title` key, so
`kind_of_payload`'s exactly-one rule resolved all 24 correctly and
`title_from_payload` produced two unrelated canonical titles per id with no
possibility of conflation.
**A kind-less TMDb reference is `PortDataMalformed`, never a guess.**
ADR-0011 at the request layer: 26,968 ids are live in both spaces, so
`GET /movie/{id}` for a ref that meant a series returns a **real payload
for an unrelated film**, which is then written onto the title as enriched
metadata with no error anywhere. Verified live through the real provider.
**A TMDb 404 is `PortDataMalformed`, not `PortUnavailable`.** The catalog
holds 291,737 TMDb ids from a bulk export that ages, and TMDb answers 404
for an id it has merged away. Retrying cannot turn any of them into an
answer, so this is the branch that makes `JobWorker`'s park-immediately
path fire in production rather than only in a test. Confirmed live, body
shape and all, and now generalised to the whole 4xx range above.
**The committed TMDb fixtures were transcriptions and they held up.** The
first shape diff any of them has ever had (2026-08-01, via
`scripts/capture_tmdb_fixture.py`) found **not one key in any fixture that
the live response lacks** — every field the mapper reads was transcribed
correctly from documentation. The live API carried six the fixtures did
not, all now added shape-only so the *next* diff is empty and a real drift
is visible: `softcore` (a boolean, on movie details, series details, search
results and the change feed), `iso_3166_1` on every `images.*` entry, and
**`networks` on the season detail**, which the `tv-season-details`
reference page does not show. Two differences are deliberately left open
because they are value-level, not shape-level, and closing them would make
a fixture claim something false — see `tests/fixtures/tmdb/README.md`.
**Still not verified after this run, named rather than implied:** a real
429 and whether one carries `Retry-After`; a v4 read access token in any
form (so `_is_v4_token`'s positive branch has never been exercised against
a real credential); a season TMDb lists that its own route refuses; TMDb's
behaviour under sustained concurrency (this run was sequential through one
token bucket at 25 rps); and any of it against a non-`US` `tmdb_region`.
**A TMDb v3 API key in the query string lands in every trace.**
`HTTPXClientInstrumentor` (wired in `configure_tracing`) records the full
URL as a span attribute, and TMDb v3 has no header form for a v3 key. So
`TmdbClient` sends an `Authorization: Bearer` header whenever the
configured secret is JWT-shaped (a v4 "API Read Access Token", which
TMDb's own docs say works on v3 endpoints and gives "the same level of
access") and falls back to `api_key` otherwise. For the same reason no
exception message in that module may carry a URL — `EmbySession`
interpolates the httpx exception into its own message and explains why
that is safe *there*; it is not safe here.
**`EnrichmentState.ENRICHED > EnrichmentState.STUB` is `False`, and the
consequence is not the one you would guess.** A tier guard spelled as a
direct comparison does not "sometimes downgrade" — it never promotes
anything at all, silently, because `ENRICHED` is lexicographically below
both other rungs. So a test asserting "an enriched title stays enriched"
passes against the bug (`ENRICHED` is the top rung, so nothing moves
either way) and the case that catches it is **promoting a stub**. The M4
plan's own mutation table pointed at the wrong one.
**A failure handler that resets the tier is invisible to a test seeded at
that tier.** `enrichment_state=SKELETON` alongside the error is exactly
what a careless handler reaches for, and a case seeded with a skeleton
cannot see it — the write is a no-op. Found by mutation on
`EnrichService`; `tests/unit/test_services_enrich.py` parametrizes over
all three rungs now. Same family as "a concurrency test must assert on
observed overlap, not on a count".
**Enrichment must read season ids back before writing episodes.**
`MetadataProvider.to_result` mints a fresh UUIDv7 per `Season`, and a
season the catalog already holds keeps the id it was inserted with — so
an episode carrying the minted id names no row and fails on
`fk_episodes_season_id_seasons`, on the **second** enrichment rather than
the first. `IngestService._ensure_seasons` re-reads for exactly this
reason; `EnrichService._store_hierarchy` now does too, and no port fake
can see either (a dict has no foreign keys).
**A job key that does not parse must become a `UsherPortError` inside the
handler.** `uuid.UUID("not-a-uuid")` raises `ValueError`, and `JobWorker`
deliberately lets anything that is not a `UsherPortError` propagate — "a
bug in a handler is not an upstream failure". So one corrupted `enrich`
key would take the worker process down instead of parking its own job.
`usher.services.handlers` converts every key, once.

**`--kind search` sends `primary_release_year` whatever it is given**, so it
records the `/search/movie` shape and never `/search/tv`'s. Fine for a shape
diff (the two pages are the same shape but for `title`/`name` and
`release_date`/`first_air_date`), worth knowing before reading its output as
evidence about TV search.
