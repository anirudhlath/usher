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
| 8 | A season the series lists that 404s on its own route | **still unverified** | 320 listed seasons across 30 series, **zero** absent. The propagate-and-park branch has still never met a real occurrence. Sample skews popular, so it is weak evidence of absence. **Widened 2026-08-11 to 626 listed seasons over 44 series, still zero — see T2's run below, which also scanned the append-layer form of the same question.** |
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
  disagree with the Specials summary on
  `name`/`overview`/`air_date`/`poster_path`/`vote_average`, and that
  disagreement is the only thing in the suite that can see the direction.
  Recorded as a deliberate fake affordance, not as fidelity.

**And that third bullet was written down and then not guarded, which is the
finding worth carrying past this task.** Found in review 2026-08-11, by
execution rather than by reading. The entry above correctly identified the
fixture disagreement as *"the only thing in the suite that can see the
direction"* — and nothing asserted it. With the inverted merge planted,
editing **only** `tests/fixtures/tmdb/series.json`'s season-0 entry to agree
with `season.json` — no code change, a plausible *"make the fixtures
internally consistent"* cleanup that no reviewer would read as a test change —
took the file from one red to **32 passed, zero red**. So the whole defence
between a correct merge and a `seasons[]` written wrong on every enriched
series in the catalog, across the ~130,806 detail fetches the crawl makes, was
an unstated coincidence between two JSON files.

Closed by `_assert_the_merge_direction_is_observable`, called by both cases
that read the disagreement, which fails on its own `E ` line naming every
field that has stopped disagreeing. Re-verified against the exact scenario:
inverted merge **plus** the fixture cleanup now fails 2 of 33; the cleanup
**alone**, with correct code, also fails those 2 — which is the behaviour
wanted, since the premise is a statement about the fixture and its job is to
say *"this case can no longer fail"* at the moment that becomes true rather
than at the next merge bug. A second case,
`test_a_season_block_is_merged_over_its_summary_and_never_under_it`, now
asserts the direction on the payload directly, so the property survives the
identity case being deleted or narrowed.

**One correction to the report of that finding, measured rather than assumed:
the cleanup has to touch all five shared fields, not four.** With
`name`/`air_date`/`poster_path`/`vote_average` normalised but `overview` left
alone, the pre-fix identity case still fails — `overview` is `""` on the
Specials summary and non-empty in `season.json`. It is the fifth field that
takes the pre-fix file to 32 green. This does not weaken the finding; it
sharpens the guard, which is why the premise requires **every** shared field
to disagree rather than *at least one*. A guard satisfied by one surviving
disagreement would itself be disarmed by a four-field cleanup.

**The general form:** when a case can only fail because two fixtures
disagree, the disagreement is a premise and gets asserted like any other —
`CLAUDE.md`'s ordering-premise rule (`assert far_id < near_id`) in the
fixture-consistency domain, and `testing-discipline.md`'s *"could this fixture
also be the row above or below"* asked of two files instead of one. The tell
is a docstring saying a fixture property is what makes a case able to fail:
that sentence is either an assertion or it is a comment nobody will re-check.

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
**The last clause is right about *seasons* and was read as a claim about
*requests*, which the live run below refutes: one attempt per season is
`ceil(len(missing)/20)` requests, and five were observed on one series.**
**The shipped append path against the live API, 2026-08-11 (M9 T2): 393
requests, and the refutation is a sentence this project had written in three
places.** The bar — eleven guesses and what would count as done — was written
to `/tmp/m9-exec/T2/bar.md` **before the first request**, and the driver
(`/tmp/m9-exec/T2/run.py`) ran outside the working tree reading the operator's
own `.env`. Sample: **14 series carrying 306 listed seasons, plus 2 movies as
a control**, each fetched down *both* paths — the shipped
`TmdbMetadataProvider.fetch` and a verbatim re-spelling of the pre-T1 `1+N`
path (`git show e38ccb5^`) — sequentially through one shipped `TmdbClient`
token bucket. Status distribution: **392 × 200 and 1 × 400**, the 400 being
the deliberate ceiling probe. **No 429 and no 5xx**, and no `retry-after` on
the 400 either; none was provoked and the absence is not offered as evidence.
Window **2026-08-11 21:15:51Z → 21:18:01Z**, and **the key was idle from
21:18:01Z**. The second window this pairing needs — group S's priority-tier
enrichment — **does not exist yet**: S3 had not started when this ended, which
is the whole point of the ordering, and S3 states its own window when it runs.
Two windows are wanted here and only one can honestly be written today.

**Refuted: "a series with more than 14 seasons needs a second request; a small
tail."** The count is `1 + ceil(|listed \ {0..13}| / 20)` and it **has no
ceiling** — the arithmetic above already said the bound is the size of the
upstream `seasons[]` array, and the PRD said "a second request" and "one
follow-up" anyway. Measured, and the formula predicted every one of the 14
exactly:

| listed seasons | append path | `1+N` path |
|---|---|---|
| 2, 6, 6, 8, 9, 10, 10, 11 | 1 | 3, 7, 7, 9, 10, 11, 11, 12 |
| **14** (numbers 0–13 — the exact window boundary) | **1** | 15 |
| 24 | 2 | 25 |
| 30 | 2 | 31 |
| 39 | **3** | 40 |
| 63 | **4** | 64 |
| 74 | **5** | 75 |

**Refuted, and it was this run's own prediction rather than the repository's:
there is no top-level volatility between the two paths.** The bar predicted
`popularity`/`vote_average`/`vote_count` would differ, because the two arms are
two separate detail requests made seconds apart. **All 14 composed payloads
were equal field for field — zero differences, nothing to report field by
field.** So the identity `test_the_composed_payload_equals_what_the_per_season_
path_produced` asserts on fixtures holds *exactly* against the live API, not
"modulo volatile fields". Bounded claim: seconds apart, not hours.

**And the equality was planted against, because a differ that cannot see a
difference is a check that cannot fail.** One series re-fetched down both
paths, baseline confirmed at 0 diffs, then five perturbations planted into a
copy of the `1+N` payload — a season's `episodes` dropped, one episode's
`name` changed, one episode removed, a whole season entry removed, a season
entry's top-level `id` removed. Each was caught and each named its own field
path (`.seasons[0].episodes`, `.seasons[0].episodes[0].name`,
`.seasons[0].episodes[len]` 6→5, `.seasons[len]` 2→1, `.seasons[0].id`).

**Not refuted, still unverified, and this is the one the diff exists for.**
Guess 8's shape at the request layer — a season the series lists, *inside* the
blind window, whose block is silently omitted while its own route answers
`200`, i.e. the append is not a substitute for the season route — occurred
**0 times in 306 listed seasons**, and no listed season's own route answered
anything but `200`. Combined with 2026-08-01 that is **626 listed seasons over
44 series, zero absent**. Still weak evidence of absence: the sample skews to
series TMDb curates well, though it deliberately reached past the popular end
(Panorama's 74 seasons, Horizon's 63, Bergerac's 10 — long-tail BBC catalogue
entries, not a second helping of prestige drama). Every one of the 14 listed
its seasons contiguously and every miss was a number ≥ 14, so the reconcile's
"TMDb permits any integer season number" branch **also** has still never met a
real occurrence.

**Confirmed, each against today's API:** 21 append items is still a **400,
`status_code: 27`**, *"Too many append to response objects: The maximum number
of remote calls is 20"*, and 20 items is still a `200` — so the derivation of
`SERIES_SEASON_SLOTS` is measured on both sides of the boundary, not just
below it. `season/0` was asked for on all 14 and arrived on **12**; the other
two (ids whose `seasons[]` begins at 1) got the silent omission, which
re-confirms the unlisted-number rule the whole blind window rests on. An
appended block still differs from the season route's own response **by the
top-level `id` and by nothing else** — that was three seasons on one series in
2026-08-01 and is now **306 seasons over 14**. A movie still costs exactly 1
request, carries `title` and no `name`, and no season machinery touches it.

**The ~10× restated on this sample, and the sample stated rather than
laundered.** These 14 series have a **median of 10.5 listed seasons and a mean
of 21.86**, against the **median of 9** over the 30 popular series the ~324k
figure rests on. In aggregate the run cost **25 requests against 320, i.e.
12.8×**. ⚠️ **That is not a better constant and must not be quoted as one:
this sample is deliberately tail-heavy** — 5 of the 14 were chosen *because*
they have more than 20 seasons, to exercise the follow-up branch at all — so
its median is an artefact of the selection even more than the 30-series median
is an artefact of popularity. What the run does add is the **form** of the
arithmetic, which is wrong in both terms and roughly cancels: a catalog total
for the `1+N` path is `Σ(1 + N)`, which needs the **mean** season count and
not the median, and the append side is `32,409 + Σ ceil(|listed \ {0..13}|/20)`
and not `32,409`. Season counts are right-skewed (mean 21.86 against median
10.5 even here), so the median understates the first and the missing follow-up
term understates the second. ~10× survives as an order-of-magnitude claim;
neither leg survives as a request budget.

**One number for whoever prices the crawl, with the caveat that makes it
usable.** The measure phase ran **347 requests in 24.0 s = 14.5 rps** against
a token bucket set to 30 — so the bucket was **not** the binding constraint;
downloading season blocks was. **This is not a movie-fetch rate and the
~130,806-detail-fetch crawl must not use it as one**: the two movie fetches in
this run took **0.064 s and 0.118 s** of wall time each.
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

**And since M9's T1, `--kind series` no longer reproduces the first request
the provider issues.** The script sends `SERIES_APPEND_TO_RESPONSE` alone;
`TmdbMetadataProvider.fetch` sends that plus `season/0…season/13`. It used to
match byte for byte, and that is a genuine loss for a tool whose whole job is
to diff the shipped request's response against a committed shape. Left alone
on purpose — the season blocks are popped before `fetch` returns, so
capturing them would record shapes nothing reads, and `season.json` already
records the season shape from its own route. **The reason first given for
leaving it was wrong and is corrected here**: it is *not* that the
namespace-only capture is "exactly the shape `raw_payloads` holds".
`raw_payloads`' `seasons[]` entries carry merged episode data no bare
`SERIES_APPEND_TO_RESPONSE` response has ever contained — true of the `1+N`
path too, so the fixtures have never recorded a stored payload's shape and
were never meant to. A wrong reason in a docstring outlives the decision it
justifies, which is why this is written down rather than quietly fixed.

## The priority tier priced, 2026-08-11 (M9 S2) — 539 requests, and the tier is not a fixed population

**The number S3 is to be authorised against: `130,806 × 0.0963 s` = 3.50 h of
wall clock on one `usher work` process, 95% CI [3.41, 3.59] h**, plus ~1.0 GiB
into `raw_payloads` and **261,612 follow-up jobs** nobody has priced. Sample:
**500 titles, a systematic 1-in-261 walk of the tier, 0.38% of it**, drained
through the shipped `usher work` on 2026-08-11 **21:44:00Z → 21:44:48Z**. The
bar — nine predictions and what would count as failure rather than refutation
— was written to [`/tmp/m9-enrich/BAR.md`](/tmp/m9-enrich/BAR.md) **before the
first request**, and the driver (`/tmp/m9-exec/S2/run.py`, with an
`httpx.AsyncClient.send` probe at `/tmp/m9-exec/S2/sitecustomize.py`) ran
outside the working tree reading the operator's own `.env`. Status
distribution over the priced sample: **499 × 200, 1 × 404**. No 429, no 5xx,
no transport error, no `Retry-After` anywhere; none was provoked and the
absence is not offered as evidence. Whole-task budget across three segments:
**539 requests**, key idle from **21:47:54Z**.

**Refuted, and it is the finding rather than a caveat: enrichment moves the
predicate the walk selects on, and 85.1% of the tier leaves the tier by being
enriched.** `vote_count` is in `EnrichService._ENRICHABLE`, the bulk loader
writes **IMDb `numVotes`** into that column and enrichment overwrites it with
**TMDb's `vote_count`** — two different electorates. Measured over all 537
titles this task enriched: **80 still carry `>= 100` (14.9%)**, median TMDb
vote count **16** against a median IMDb `numVotes` of **581** on the
unenriched tier, and the tier's own count fell **130,806 → 130,349** as a
direct arithmetic consequence of enriching 537 of it. Four things follow.

- **The keyset walk is safe and the reason is worth stating rather than
  assuming.** A row can only leave the tier by being enriched, which happens
  only after it was enqueued, which happens only after the cursor passed it —
  so no title is skipped and the walk still terminates. An `OFFSET` walk over
  the same shrinking population would skip rows silently, which is a second,
  independent argument for the keyset cursor.
- **`130,806` is a snapshot with a timestamp, not a property of the catalog.**
  It is the right fetch count for S3 only because the enqueue pass completes
  before the drain begins. Interleaving them would fetch fewer titles and
  nothing would say so.
- **A number re-derived from the predicate after S3 will not reproduce.** The
  group S preamble's tier statistics (161,789 tier movies; genome 11.87%; at
  least one MovieLens tag 34.47%) were taken *before* any enrichment and are
  correct as of then. Re-running those queries after S3 answers about a
  population roughly a seventh the size. ADR-0002's suggest benchmark
  (`vote_count >= 500`, 81,054 names) is drawn from the same column and has
  the same exposure.
- **`field_provenance` already records it and no predicate reads it.** An
  enriched row carries `"vote_count": "tmdb"`, so the information is not lost
  — it simply is not where the tier is expressed. Recorded rather than fixed:
  changing the column's meaning, splitting it, or moving the tier onto
  `field_provenance` are all schema-or-semantics decisions and this task has
  no mandate for one.

**Refuted: "161,789 movies at 30 rps, ≈1.5 h".** Wrong in three independent
places at once, all flattering. The population is **130,806** and not 161,789
(30,983 tier movies carry no `tmdb_id`, and `_ref_for` parks each on its first
attempt). The rate is **not** the token bucket: `JobWorker._run_once` is a
strictly sequential `for job in claimed:` and the bucket lives on one client
per process, so one worker runs at `1/latency`. Measured, one worker achieved
**10.38 rps** against a bucket set to 30 — the bucket idled at 35% of its
allowance, exactly as T2 found from the series side. And the time is **3.5 h**,
not 1.5 h. PRD 04's Phase-3 table carried the same shape (`~189k titles @ ~25
rps, 1.5–2.5 h`) and is corrected in this commit.

**The two halves of the per-title cycle, measured by two independent
instruments that agree to 0.5%:**

| | median | p95 | mean |
|---|---|---|---|
| HTTP request alone (probe, 500 requests) | 0.0580 s | 0.1049 s | 0.0637 s |
| whole job cycle (probe, inter-request starts) | 0.0896 s | 0.1411 s | 0.0963 s |
| whole job cycle (`raw_payloads.fetched_at` deltas, 499 rows) | 0.0892 s | 0.1389 s | 0.0963 s |

**HTTP is 65% of the cycle**; the other 35% is a title read, a
`raw_payloads` read, a JSONB insert, a title update, the two-request follow-up
enqueue, the job delete and a commit. **Use the mean and not the median to
extrapolate a total** — the distribution is right-tailed (max 0.56 s) and
`Σ` wants the mean; the median gives 3.26 h and is the wrong statistic for
the question, which is why both are here.

**Refuted, and it was this run's own prediction rather than the
repository's — three ways.** The bar predicted a median of 0.12 s in a
0.09–0.18 s band and a p95 of 0.35 s in a 0.20–0.80 s band: measured
**0.0896 s** (just below the band) and **0.1411 s** (far below it). It also
predicted that the 20 oldest tier movies would be *cheaper* per title than a
representative sample, on the argument that a 1919 film's payload is smaller.
It is: 8,603 JSON characters against 18,726. And they were **slower** —
median 0.113 s against 0.0892 s — because twenty jobs is all warm-up. The
first twenty gaps of the 500-title segment have a median of 0.1033 s against
0.0894 s for the remaining 479, which is the same effect measured where it can
be separated. **A twenty-title segment cannot price anything; it can only
price its own cold start.**

**Refuted before the run, and it is why the sample is not a prefix: `ORDER BY
id` over this catalog is chronological.** `titles.id` is a UUIDv7 minted in
IMDb `tconst` order, so the first 500 rows of the tier have a **median year of
1919** and a median `vote_count` of 367, against **2006** and 581 for the tier
as a whole. The systematic 1-in-261 sample used instead lands at mean year
1996.3 / median 2005 / median votes 614.5 against the tier's 1996.9 / 2006 /
581. **A prefix of a walk is a sample of the walk only if the ordering key is
independent of the thing being measured, and a UUIDv7 primary key on a
bulk-loaded catalog never is.**

**Confirmed, and it is what makes the extrapolation defensible: per-title cost
is flat across the tier.** Because the sample is id-ordered it is also
chronological, so the run's own quintiles are eras. Cost does not trend:

| quintile | mean year | mean payload chars | cycle median | HTTP median |
|---|---|---|---|---|
| 1 | 1957.3 | 16,435 | 0.0905 s | 0.0578 s |
| 2 | 1986.4 | 20,274 | 0.0962 s | 0.0564 s |
| 3 | 2004.1 | 16,449 | 0.0902 s | 0.0597 s |
| 4 | 2016.3 | 21,466 | 0.0865 s | 0.0575 s |
| 5 | 2018.0 | 19,007 | 0.0858 s | 0.0579 s |

A 31% swing in payload size moves the cycle by less than 12%, and in the
direction warm-up predicts rather than the direction payload size does. So
`130,806 × mean` is a linear extrapolation over a population measured not to
be heterogeneous in the cost dimension — which is a stronger claim than the
0.38% denominator alone supports and is the reason it is stated.

**The costs the plan does not price, all measured here.**

- **Two follow-up jobs per enriched title, always.** `EnrichService` enqueues
  an `INDEX` and a `DERIVE` at `BACKFILL` on every success — 537 enrichments
  produced exactly 537 of each. The full run therefore writes **261,612**
  further jobs on top of its 130,806. `DERIVE` drains on the same worker (it
  needs the provider, not the network — zero requests observed). `INDEX` does
  **not**: `composition.embedder` returns `(None, no-op)` unless
  `USHER_EMBEDDING_ENABLED` is on, which is off by default, so on the shipped
  defaults the run leaves **130,806 index jobs pending forever** and
  `title_embeddings` stays empty. Whoever runs S3 has to decide that
  deliberately; it is not a detail of a later task.
- **~1.0 GiB, confirmed within 2%.** Mean stored payload **6,914 bytes**
  (JSONB, TOAST-compressed, from 18,726 JSON characters), and
  `pg_total_relation_size('raw_payloads')` is **1.186×** the sum of the
  payload column. `6,914 × 1.186 × 130,806` = **1.07 GB / 1,023 MiB**.
- **The enqueue half is free: 5,000 jobs in 5 pages in 0.60 s**, interpreter
  start included, so the whole tier is ~16 s. `_PAGE` plans as an **Index Scan
  using pk_titles** with a filter — 36 ms for a 1,000-row page, 11,223 rows
  removed by filter, no `Seq Scan` and no `Sort`.
- **Parked: 1 in 500.** `TMDb has no entity at this reference (/movie/…)`, a
  404, parked at `attempts = 1` — the `PortDataMalformed` taxonomy firing on
  the path it was written for. Scaled honestly that is a Wilson 95% interval
  of **46 to 1,470 parked jobs** over the tier, which is an interval a single
  observation cannot narrow and must not be quoted as "about 260".

**Confirmed: a movie costs exactly one request, and it was checked rather than
assumed.** `TmdbMetadataProvider.fetch` issues one GET and the
`_compose_seasons` branch is `TitleKind.SERIES`-only, and 500 enrich jobs
produced exactly 500 requests with no retry anywhere.

**Confirmed: a re-run inside the freshness window costs zero requests — on the
second attempt at the test, because the first was invalid and the reason is
the vote-count finding again.** Re-running the committed script's `--limit 20`
and draining it made **19 requests**, which reads as a refutation and is not
one: enrichment had moved the predicate, so "the first twenty tier movies by
id" was a *different* twenty. Re-enqueued by explicit id against titles that
already held a `raw_payloads` row, the same drain made **0** requests — and
the probe's `.installed` marker was checked first, because a probe that did
not install and a probe that measured zero produce the identical empty file.
**A cache test has to name the rows it expects to hit, never re-derive them
from a predicate the system under test is allowed to move.**

**Still unverified after this run, named rather than implied:** a real 429 and
whether one carries `Retry-After`; TMDb's behaviour under sustained
concurrency (this was one sequential worker, as every run from this repository
has been); the season-omission branch (this segment was movies only, so it adds
nothing to the 626-listed-seasons count above); and what `N` concurrent
`usher work` processes actually achieve. On that last one the arithmetic and
the two traps are recorded rather than measured: reaching 30 rps needs **N = 3**
at the measured 10.38 rps each, `USHER_TMDB_REQUESTS_PER_SECOND` must then be
set to `30/N` **per process** because the bucket is per client, and
`JobWorker.startup()`'s default `older_than_seconds = 0.0` requeues
*everything* running — so restarting one worker mid-run steals the others'
live claims.

## The priority tier actually enriched, 2026-08-12 (M9 S3) — 130,334 requests, 1.98 h, and the first 5xx this repository has ever seen from TMDb

**The run S2 priced, executed whole.** 22:08:53Z → 00:07:46Z against
`api.themoviedb.org/3`, driving the shipped `usher work` on the shipped
`TmdbMetadataProvider`. Bar — nine numbered predictions and what would count
as failure rather than refutation — written to
[`/tmp/m9-exec/S3/BAR.md`](/tmp/m9-exec/S3/BAR.md) **before the first
request**; the enqueue driver (`/tmp/m9-exec/S3/enqueue_by_id.py`) and the
per-process `httpx.AsyncClient.send` probe
(`/tmp/m9-exec/S3/sitecustomize.py`) ran outside the working tree reading the
operator's own `.env`. The probe records **path only, never the query
string** — a v3 key rides in `api_key=` — and one file per pid, because three
daemons appending to one file is a torn line waiting to happen. All three
`.installed` markers asserted before a single number was believed. The key
was idle from **00:07:46Z**.

**Status distribution over the whole run, which is the evidence for most of
what follows: 130,141 × 200, 107 × 404, 86 × 502.** No 429, no transport
error, and **no `Retry-After` on any response** including all 86 of the 502s.
Two independent instruments agree: the probe counted 130,334 requests and
`raw_payloads` gained 130,141 rows, and the difference is exactly the
non-200s.

### The four numbers S3 was authorised against, against what it did

| | S2 predicted | measured | |
|---|---|---|---|
| wall clock | 3.50 h at 1 worker, 95% CI [3.41, 3.59] | **1.98 h at 3 → 2 workers** | see the crash below |
| fetches | 130,806 | **130,334** (130,806 − 537 cached + 65 retries) | ✅ |
| `raw_payloads` | ~1.0 GiB, mean 6,914 B | **995 MB**, mean **7,001 B** | ✅ within 1.3% |
| follow-up jobs | 261,612 | **261,294** = 2 × 130,647 successes | ✅ exactly two per success |

**Confirmed, and it is the one that makes the extrapolation defensible: S2's
0.38% sample priced the median request correctly and the tail not at all.**
HTTP median **0.0588 s** against S2's 0.0580 s — 1.4% apart over 500 requests
versus 130,334. But **p95 0.4267 s against S2's 0.1049 s, a 4.1× blowout**,
and mean 0.0993 s against 0.0637 s. Concurrency does not move the median
request; it moves the tail, and a sequential sample cannot see that.

**Refuted: "three workers at `30/N` each reach 30 rps".** That was this
repository's own arithmetic, recorded in PRD 04 and in S2's entry above, and
it is wrong because it assumes per-worker throughput survives concurrency. It
does not. Three workers achieved **19.76 rps** — 6.59 rps each against S2's
**10.38 rps** measured on one worker, a **37% per-worker loss** — and the
bucket, set to 10 per process, was never the binding constraint on any of
them. The scaling factor from one worker to three is **1.90×, not 3×**.

### One worker died 78 minutes in, and its claims are still `running`

**`usher work` crashed at 23:26:57Z with an unhandled
`MissingGreenlet: greenlet_spawn has not been called; can't call await_only()
here`**, killing one of the three daemons. This is a defect in the shipped
worker that only a multi-hour run reaches; nothing in `tests/` has ever
executed this path. Three consequences, all measured:

- **The run finished on two workers.** Phase 1 (3 workers, 78.1 min): 92,550
  requests, **19.76 rps**. Phase 2 (2 workers, 40.8 min): 37,784 requests,
  **15.43 rps**. Per-worker throughput actually *rose* when the third died —
  6.59 → 7.72 rps — which is the contention story from the other side.
- **Its 20 claimed jobs are orphaned in `status = 'running'` forever.**
  `JobWorker.startup()` is the only thing that requeues an abandoned claim
  and it runs once, at process start. So a worker that dies mid-batch takes
  its batch with it, and the only recovery is a restart — which
  `older_than_seconds = 0.0` makes steal every *other* worker's live claims.
  **That is a genuine dead end at N > 1**: with three workers there is no way
  to recover one's orphans without corrupting the other two. Deliberately not
  restarted; the 20 are reported as part of the shortfall instead.
- **The crash's last two log records are both the `ix_titles_imdb_id`
  conflict path**, 19 ms and 24 ms before death — the only unusual control
  flow in the run. Correlation stated, causation **not** established: the
  driver did not set `usher --traceback work`, so no stack was captured. The
  ~30 earlier conflicts on the same process did not kill it.

### A failure class no taxonomy in this file covers: `ix_titles_imdb_id`

**30 of the 139 parked jobs are not upstream failures at all — they are write
conflicts on the catalog's own unique index**, and this is the first time any
run has produced one. `titles.imdb_id` is `CREATE UNIQUE INDEX ... WHERE
imdb_id IS NOT NULL`; enrichment writes TMDb's `external_ids.imdb_id` over
the bulk export's, **the two crosswalks disagree, and the id TMDb hands back
is already held by a different catalog row**. Confirmed rather than inferred,
by re-fetching five of them through the shipped provider:

| catalog `imdb_id` | TMDb `external_ids.imdb_id` | already owned by |
|---|---|---|
| `tt0023002` | `tt9731440` | another movie |
| `tt0032420` | `tt0035828` | another movie |
| `tt0023046` | `tt0165558` | another movie |
| `tt0023322` | `tt0155020` | another movie |
| `tt0026564` | `tt0026577` | another movie (adjacent `tconst`) |

Rate **30 in 130,806 = 0.023%**. It is *not* `PortDataMalformed`, so it
retries: **every occurrence burns all five attempts and the full backoff
schedule** before parking, and the fetch is re-made each time because the
failing write rolls the `raw_payloads` insert back with it. That is the 65
retry requests in the arithmetic above. The title is left `skeleton` with
`enrichment_error` set — `EnrichService._record_failure` logs *"tier stays
skeleton"*, which is the M4 finding about a failure handler that must not
reset the tier, working correctly.

### The first 502s, in two exact bursts of 43

**86 × 502, all inside 526 s (23:07:50Z → 23:16:36Z), in two bursts of
exactly 43.** Before today this repository had observed **zero** 5xx from
TMDb in 1,644 requests across three runs. None carried `Retry-After`. All 86
were classified `PortUnavailable` (retryable), backed off and **recovered** —
not one parked job carries a 502-shaped error, which is the retry taxonomy
working on a branch that had never fired in production. The two-identical-
bursts shape suggests one upstream node cycling rather than load shedding,
and **no 429 appeared at any point**, so this is not the rate limit in
disguise. Guess 1 — whether a real 429 carries `Retry-After` — is *still*
unverified, and now so is whether TMDb ever sends `Retry-After` at all: 193
non-200s across this run carried none.

### The `index` lane does not drain beside the `enrich` lane, and `USHER_EMBEDDING_ENABLED` is not why

**`title_embeddings` sat at 542 through the entire enrich run and then jumped
to 4,929 within minutes of the enrich queue emptying.** The embedder was on
the whole time — this was head-of-line blocking, and the mechanism is one
line of the claim:

```sql
ORDER BY priority DESC, created_at
```

Every job in the table is at `JobPriority.BACKFILL` (20), and the enqueue
wrote all 130,804 `enrich` rows inside a **1.3-second window** at 22:08:54–55.
An `index` job is only created when its title finishes enriching, so **every
enrich job sorts ahead of every follow-up job**, and `LIMIT 20` never reaches
past them. Measured directly: the head of the claim queue returned 20 × 
`enrich` on every probe during the run, and the five embeddings that *did*
get written are the moments when fewer than 20 enrich jobs were claimable.

**Three independent checks that the embedder was on**, recorded because the
alternative reading — `composition.embedder` returning `(None, no-op)` —
produces an identical-looking backlog and was the live hypothesis for an hour:

1. `/proc/<pid>/environ` on the live claimers: `USHER_EMBEDDING_ENABLED=true`.
2. `composition.embedder` logs `"no embedding model configured; index jobs
   will not be claimed"` on the no-op branch. **That line is absent from all
   three worker logs**, while its structural sibling `"no LLM configured;
   curate jobs will not be claimed"` is present in all three — an internal
   control, in the same file, emitted by the same shape of guard.
3. A pre-run calibration drained 537 `index` jobs to 537 embeddings and 537
   `derive` jobs in 12.7 s, at **zero** outbound requests.

**So `USHER_EMBEDDING_ENABLED` is necessary and not sufficient**, and the
ruling that it be on was right for a reason one step removed from the one
given: it makes the index jobs *claimable*, but a bulk enqueue at a single
priority defers them wholesale until the bulk lane is empty. The plan's stated
risk — *"this puts a multi-hour job on the single `JobWorker` lane"* — is
confirmed, and it applies to the run's **own** follow-ups, not just to
`match` and `watch_history`.

### The per-pass gauge refresh is O(the enriched tier), and this run grew that 244×

`usher work` calls `SearchGauges.refresh` after **every** `run_once`, i.e.
every 20 jobs. Its two `count(*)`s run `_COUNT` over `enrichment_state <>
'skeleton'` with `_FINGERPRINT_SQL`'s `md5()` of seven concatenated columns
evaluated per row. `PostgresTitleEmbeddingRepository`'s own docstring prices
this at *"2k-10k rows"* and *"a query that runs a few times a day"*. Measured
on this run as the tier filled:

| enriched rows | one `count_stale` |
|---|---|
| 7,718 | 16.4 ms |
| 18,267 | 29.4 ms |
| 88,001 | **327.9 ms** |

At the run's end that is ~0.7 s of gauge per 20-job pass, per worker, against
~2.8 s of enrich work — and against ~1.4 s of `index`/`derive` work, where it
is a ~50% tax. The docstring's estimate was off by **13×** in population and
by four orders of magnitude in frequency. Recorded, not fixed: the fix is a
throttle or an index and both are decisions this task has no mandate for.

### The pre-state and the post-state, each against the population it was taken over

**Pre-state, read from the database at 22:02Z**, not from a script's stdout.
`alembic current` reported **m09a**, was upgraded to **m09c (head)** by this
task at 22:00Z, and the gap mattered only in that C2's `images` natural key
had to exist before `derive` ran. The plan's premise guard — one row,
`(skeleton, 1,272,367)`, and `count(raw_payloads) = 0` — **is not the state
that existed**, because S2 enriched 537 titles against this same scratch
database first: 1,271,830 `skeleton` + 537 `enriched`, 537 payloads, 537
pending `index` jobs and 2 parked `enrich`. Recorded as what it was.

**The tier was frozen before the first request** — `s3_tier_snapshot`, 130,806
ids, created 22:02:21Z:

```sql
SELECT id FROM titles WHERE kind='movie' AND tmdb_id IS NOT NULL
  AND (vote_count >= 100 OR enrichment_state <> 'skeleton')
```

The live predicate alone gave **130,349**; the 457 rows S2's enrichment had
already pushed below the floor bring it to **130,806**, reconstructing the
plan's population exactly. Every count below is over those 130,806 frozen ids
and says so.

| over the frozen tier (n = 130,806) | |
|---|---|
| `enriched` | **130,647** (99.88%) |
| still `skeleton` | **159** = 139 parked + 20 orphaned by the crash |
| `enrichment_error` non-NULL | 140 |
| `overview` (weight class C) | 129,926 (99.33%) |
| `tagline` (weight class C) | 54,567 (41.72%) |
| `genres` (weight class D) | 130,781 (99.98%) |
| `keywords` (weight class D) | 82,405 (63.00%) |
| `field_provenance.vote_count = 'tmdb'` | 130,647 |

**The shortfall is 159 and every one is accounted for**, which is what the
acceptance asked for: 109 titles TMDb has merged away (404, parked at
`attempts = 1`, the `PortDataMalformed` path), 30 `imdb_id` conflicts (parked
at `attempts = 5`), and 20 orphaned by the crash. It is **not** a `tmdb_id`
coverage finding — the `tmdb_id IS NOT NULL` conjunct had already removed
those 30,983 before the walk began.

**Confirmed: a re-run inside the freshness window costs nothing, at scale this
time.** All 537 titles S2 had already enriched were re-enqueued as part of the
snapshot and **not one was re-fetched** — `raw_payloads.fetched_at` on all 537
is still S2's 21:4xZ. S2 established this on 20 rows named by id; this is the
same claim over 537 inside a 130,806-row walk.

**Confirmed, and now over 130,806 rather than 537: the tier evicts itself.**
Of the frozen tier's 130,647 enriched rows, **21,640 (16.56%)** still carry
`vote_count >= 100` — against S2's 14.9% from a 537-row sample, so the small
sample was low by 1.7 points and directionally right. Median TMDb `vote_count`
**15** against a median frozen IMDb `numVotes` of **576**. The live predicate
`kind='movie' AND vote_count >= 100` has fallen **161,332 → 52,782**, and with
`tmdb_id` **130,349 → 21,799**. ⚠️ **Those last two are a different population
from every tier statistic in this file and in the group S preamble**, all of
which were taken before enrichment. A number re-derived from the predicate now
answers about a sixth of what it used to.

### What this run leaves behind, priced

`enrich` is drained. **261,294 follow-up jobs are not**: at the two surviving
workers' measured 34.2 jobs/s across both kinds, the remaining `index` +
`derive` backlog is **~1.9 h**. Those jobs are durable and nothing is lost.
`title_embeddings` was **11,585 and climbing at 17.0/s** when this was
written, `title_neighbors` 0, `people` 57,703, `credits` 290,760.
`pg_database_size` 2,746 MB.

**Still unverified after this run, named rather than implied:** a real 429 and
whether one carries `Retry-After` (130,334 requests, zero 429s, and now 193
non-200s with no `Retry-After` on any); the season-omission branch (movies
only, so this adds nothing to the 626-listed-seasons count); whether the
`MissingGreenlet` crash is caused by the conflict path or merely followed one;
and what **four or more** concurrent workers achieve, given that three
returned 1.90× and the marginal worker was measured to *reduce* per-worker
throughput.
