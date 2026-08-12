---
paths:
  - "src/usher/services/rows/**"
  - "src/usher/services/home.py"
  - "src/usher/services/taste.py"
  - "src/usher/services/derive.py"
---

# The home screen, row providers and the tag genome

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**M7's measurements, taken 2026-08-04/05 on this host against
`pgvector/pgvector:pg17` (pgvector 0.8.6) unless stated otherwise. The
`titles.popularity` re-measure has its own entry inside M6's gate section
below, because it is a correction to that gate's headline.**
**The genome cosine does NOT saturate, measured against a bar written before
the run — so the vectors ship raw and are not mean-centred.** Over all 16,376
vectors and all **268,157,000** ordered off-diagonal pairs: **mean 0.6101, sd
0.0913, min 0.2556, p1 0.4075, p50 0.6095, p99 0.8165, max 0.9913**, top-10
neighbour gap **0.2456**. The bar, written first: saturated if mean ≥ 0.70, or
p1 ≥ 0.50, or sd < 0.05, or the top-10 gap < 0.15. **No clause fired**, and it
is measurably better-ordered than a signal this repository already shipped
(name-only skeleton embeddings, mean 0.5867 / sd 0.055). Both centred variants
were measured and neither ships — per-vector `v − mean(v)` gives 0.3875 /
0.1249 / gap 0.3813, per-tag `v − μ` gives 0.0034 / 0.1887 / gap 0.6313 — and
**nothing is foreclosed**, because the stored population *is* the corpus:
`SELECT avg(relevance::vector)::real[] FROM genome_scores` recovers μ (a bare
`avg(relevance)` is not usable and `halfvec` has no subscripting).
**"A full pairwise cosine over all 16,376 vectors runs in 1.190 ms" is wrong
by five orders of magnitude**, and it was repeated in five places in the M7
plan. A real self-join is 121M unordered pairs of 1,128 lanes and measures
**384 s**. The numbers that matter, on a real 15,565-row table: **`get_pair` is
0.062 ms** (two primary-key probes under a `BitmapOr`, the only read this table
has), and an **unindexed KNN — one seed against all 15,565 — is 59.4–66.2 ms**
at 93,617 buffers, dominated by one TOAST fetch per row. The no-HNSW decision
is unchanged and always rested on the access pattern; but 1.190 ms would have
foreclosed the question of whether a consumer could want KNN, and 384 s
reopens it.
**The genome's real coverage, with its denominators — measured 2026-08-05
against a `--phase all` catalog of 1,271,570 titles.** 15,565 genome vectors
joined: **1.22%** of all titles, **1.73%** of 899,991 movies, **7.61%** of the
≥100-IMDb-vote priority tier (measured at 204,494 titles, not the ~189k PRD 04
estimated), **10.68%** of a real household's 5,020 owned titles. **The number
that decides whether the term does anything is none of those** — it is the
*candidate-pair* rate, because both sides of a pair need a vector: **1.81%**
(9,069 of 502,000 pairs), **measured, never squared** (`coverage²` would have
said 1.14% and a real pool is not an independent draw). That is far below the
10% floor the weight assumes, so at 0.25 the genome reorders about one
neighbour list in fifty-five. **The term is kept anyway and the choice deferred
to M9**: 1.81% is a conservative floor (no TMDb key ran, so documents are
name-shaped and the pool is name-selected, which weakens exactly the
correlation being measured), and `blend_fingerprint` makes reverting cheap and
detectable. The genome is **movies-only and frozen at 2023-07-20**, so coverage
of anything newer is structurally zero and decays.
⚠️ **And the population is half of that number — settled 2026-08-11 by M9 Task
S1, before anything in M9 quoted the rate as a baseline.** The arithmetic is
the load-bearing half: 502,000 candidate pairs over `_CANDIDATE_POOL` (100) is
exactly **5,020 seeds**, and `SimilarityService.rebuild`'s seeds are
`list_embedded`, i.e. rows of `title_embeddings` with a non-NULL vector. Those
5,020 were **one household's owned titles** — the measurement script's own
predicate is `UPDATE titles SET enrichment_state = 'enriched' WHERE EXISTS
(SELECT 1 FROM media_items m WHERE m.title_id = titles.id AND m.available)`,
printing *"promoted 5020 owned titles to the enriched tier"* — so the run moved
the **tier label** and not the **document**: `search_document`'s weight classes
C and D (overview, tagline, keywords) were empty, exactly as the script's own
docstring says. Corroborating counts, both read-only 2026-08-11: `usher-m9-pg`
holds 1,272,367 titles / **0** embeddings / 0 neighbours / 0 `media_items`, and
`usher-postgres-1` holds 1,271,138 / 0 / 0 / 0. Neither is M7's catalog, which
was **1,271,570** titles in a scratch database (`m7gate`) that no longer
exists — three distinct catalogs, and the absent `media_items` is the stronger
tell, because neither survivor even holds the household that defined the
population. **So 1.81% is a floor over 5,020 owned, name-shaped, pre-TMDb seeds
and is not a baseline for the population M9 measures** — M9 enriches and embeds
movies with a TMDb id in the ≥100-vote tier, ~130,806 of them, and every input
to the pair rate changes: the seed set (26× larger, selected by votes rather
than by ownership), the document (classes C and D filled), and therefore the
pool `nearest_for` draws. A later number placed beside this one is a second
measurement, never a delta.
**The sequential row build's cost, so boundary call 8 is a decision rather than
a preference.** Measured 2026-08-04 via `usher home --repeat 5` against a real
**1,271,570**-title catalog with a synthetic household (5,200 owned copies, 360
watch states over two years including 60 episodes, 50 collections, 1,800
credits and 6,000 `title_neighbors` rows): **cold p50
23.9 ms, p95 35.9 ms**, warm 0.0 ms, 8 rows / 115 cards, slowest provider
`because-you-watched` at 4.3 ms = **34%** of build time. The revisit rule was
written before the run and needs **both** clauses: p95 > 400 ms *and* no single
provider at ≥ 50%. p95 is **11× under** the budget, so the second never
applies.

⚠️ **And that p95 is a property of the household, not of the composer.**
Re-measured 2026-08-05 against the scale ceiling — `scripts/measure_rows.py`'s
full seeding, **1,277,878 owned items and 1,277,878 watch states, 1,086,149
played** — compose is **p50 710.3 ms, p95 783.4 ms**, 2× *over* the budget,
with `genre-affinity` at **98%** of build time and `next-up` costing **302.9 ms
to propose**. The decision does not move, because the revisit rule needs *both*
clauses and the second does not fire: the answer is to fix one provider, not to
run nine on one session. **Never quote the 11× without the 5,200-copy
household it belongs to.**
[ADR-0025](../../docs/prd/decisions/0025-rows-build-sequentially.md).
**A non-overlap assertion passes against the exact `gather` it exists to
kill.** `asyncio.gather` over coroutines that never suspend produces N
*disjoint* windows, so "these did not overlap" is satisfied by the concurrency
the case forbids. What has teeth is a **depth recorder shared by the
providers** asserting `max_in_flight == 1` — `AsyncSession`'s real contract,
one statement in flight at a time — which a `gather` drives to 9 on the first
pass; and it needs its own control, because deleting the recorder's
`await asyncio.sleep(0)` makes every implementation look sequential. A second
case AST-scans `services/home.py` for `gather`/`TaskGroup`/`create_task`/`wait`,
walking `ast.Import` **and** `ast.ImportFrom` and matching the bare name as
well as the attribute.
**Four reads on the home path that nothing needed, found 2026-08-10 by
counting statements against fakes rather than by timing anything.** Each is
recorded as a before/after count, because a count is the only assertion a fake
can carry honestly -- a timing assertion against an in-memory dict measures the
dict.

- **The engaged window was read once per public method.** `genre_affinity` and
  `centroid` both open with `TasteService._engaged`, so one
  `CandidatePoolService.for_user` on a deployment with an embedder issued
  `list_recent(50)` **twice** per generation for a window that cannot move
  between them (one job, one transaction). Memoised per `(service, user_id)`:
  **2 -> 1**.
- **`library_genre_counts()` was read once per answer.** It takes no `user_id`
  -- an `unnest(genres) GROUP BY` over the whole owned library, 1.27M titles,
  identical for every household -- and was paid per generation *and* per
  home-screen build. Memoised per service: **3 -> 1** over two households and
  three asks.
- **`GET /home` paid the whole genre-affinity read before the screen cache
  could answer.** `get_row_context` is a FastAPI dependency and FastAPI
  resolves the graph before the handler runs, so `HomeService.compose_report`'s
  `get_screen` check came *after* `list_recent(50)` + `list_by_ids(50)` +
  the library aggregate had already run. `RowContext.affinities` is now
  `Callable[[], Awaitable[Sequence[GenreAffinity]]]`, awaited by the one
  provider that reads it: a 30 s cache hit costs **1 -> 0**.
- **Four curated shelves hydrated independently.** `CuratedProvider.propose`
  mints up to five `LLMRow`s from one `list_for_user`, `_MAX_PER_FAMILY` is 4,
  and each `BaseRow.build` issued its own `list_by_ids` + `owned_title_ids`:
  **8 statements for ~22 ids**, in one request. A shared per-`propose`
  hydration read at *build* time makes it **4 -> 1** of each.

**And the trap that cost twelve cases: a new hook on `BaseRow` is shadowed by
any subclass attribute of the same name.** The ownership read was extracted as
`BaseRow._owned(ctx, title_ids)` so `LLMRow` could share it -- and
`FranchiseRow` already carries `self._owned`, a tuple of its collection's
members. A subclass *attribute* shadows a base-class *method*, so `hydrate`
raised `TypeError: 'tuple' object is not callable` on one provider in ten, at
render time. Measured: 12 failures across three files, all from one name. The
hook is `_ownership`; **grep `services/rows/` for a hook's name before adding
one**, because the failure is invisible in the class that declares it.

**A memo on a per-user read needs its key asserted, not just its count.** The
`_engaged` memo is keyed by `user_id`, and the case that says so asserts *two*
reads for two households **and** the genre each one got: a count alone is
satisfied by a memo that re-reads and then hands back the wrong entry anyway,
and the failure mode of the unkeyed version is one household's watch history
deciding another's affinity, centroid, candidate pool and paid-for shelves --
rendered perfectly, raising nothing. Its lifetime is argued rather than assumed
(`api/deps.py` builds one `TasteService` per request; `composition.
build_pipeline` builds one per unit of work), and it re-reads on a disagreeing
`max(watch_states.updated_at)` so the argument is not load-bearing: two
existing cases hold one service across a merge and require the second read to
see it, and **both failed against the first draft of the memo**, which is how
the watermark check got written.
**Two stale serves cannot overlap in wall clock, and that is the feature
rather than a gap in the test.** Found 2026-08-11 building M9's serve-stale
path against the acceptance criterion "two concurrent requests over one stale
key schedule exactly one refresh, and the case proves the two genuinely
overlapped". They cannot: a stale serve returns out of a dict with no `await`
in it, so `asyncio.gather` over two of them produces the **disjoint** windows
this file already records as the trap one entry up, and a case asserting they
intersect would be asserting that serve-stale is broken. The pair that has
teeth is a **read against the in-flight refresh** — recorded intersecting in
`tests/unit/test_api_lanes.py`, with the lane parked inside `build` — because
the mutation it rules out is a queue that clears its dedup mark at `take()`
rather than at `done()`, which reopens the stampede for exactly the length of
a refresh and which no count can see. Corollary worth carrying: **when a
"these overlapped" assertion is impossible because the fast path never
suspends, the honest move is to name the other side of the real race, not to
weaken the claim to a count.**
**A screen refresh reuses every row whose own TTL is still running, so a
seconds-old screen can carry a five-minute-old shelf.** Found the same day by
writing the integration case for "the refresh reads state committed after the
screen was cached" and watching it come back unchanged: `_SCREEN_TTL` is 30 s
and `recently-added`'s is 5 minutes, so `HomeService.rebuild` re-proposes,
re-selects and re-orders while `_build` answers out of the row half. That is
PRD 06's second layer working — rebuilding every row on every 30 s screen
expiry is the cost it exists to avoid — and it is why the row half has **no
grace window of its own**: the refresh unit is a *screen*, one entry per
household bounded by the `users` table, and a per-row grace with no per-row
refresh behind it would serve stale rows that nothing ever replaces, over a
key space that is `because-you-watched-<seed>`. Both halves are pinned
(`test_a_screen_refresh_reuses_a_row_whose_own_ttl_has_not_moved` and its
neighbour, which expires the row entry alongside the screen).
**A stale-serve grace window must be gated on there being a refresher, and the
gate is one line.** `HomeService`'s `_stale_grace` is zero when `refresh is
None`, so a composer with nowhere to send the key — `usher home`, whose process
ends when the command does — serves nothing stale at all. Without it the
milestone's obvious spelling, "pass a no-op refresher from the CLI", opens the
window with nothing behind it: a 31-second-old screen is served and never
replaced, silently, which is strictly worse than the miss it avoided. The plan
asked for the no-op; the gate is what makes it wrong to give one.

**A fifth read on the home path, added rather than removed — `+1` per shelf,
and the curated family's `4 -> 1` for a third time.** Measured 2026-08-11
(M9 C6) against fakes, because a count is the only assertion a fake can carry
honestly. `RowCard.artwork` makes `BaseRow.hydrate` four port calls rather than
three (`ImageRepository.primary_for_titles`, one statement per shelf whatever
the shelf's length), and `LLMRow._artwork` shares one read across a
generation exactly as `_known` and `_ownership` already do: without the
override the four curated shelves the composer builds out of one
`list_for_user` would issue four, which would take one generation from three
statements to **twelve**. Both numbers are asserted as counts derived from the
screen (`images.calls == len(screen)`) rather than as literals, so a shelf the
composer proposed and did not build, or one that built empty, costs nothing and
the assertion says so.

⚠️ **Any claim about what this costs in milliseconds has to name its
household**, which is why this entry has none: the two p95s above differ by
30x on the same composer, and a fourth read per shelf is a different fraction
of 35.9 ms than of 783.4 ms.

**And the hook-name trap fired in reverse, which is the cheap outcome the
entry above exists to buy.** `grep -rn "_artwork\|artwork" src/usher/services/rows/`
before writing the hook found nothing — the name was free in all ten providers
— so `BaseRow._artwork` shipped without repeating `_owned`'s twelve failures.
The check cost one command. `_images` was checked at the same time and is also
free, but `_artwork` was chosen because it matches the field it fills.

**The kind a card is painted with is keyed on the *row's* `display_hint`, and
the mapping is total over `DisplayHint` rather than over the hints the registry
emits.** `wide` and `square` have **no emitter in `services/rows/`** — all ten
providers return `PORTRAIT` or `LANDSCAPE` — so a mapping written from the
registry is complete-looking and is a `KeyError` inside `hydrate`, i.e. a 500
on a home screen, the first time a provider uses one. Measured by planting
`SQUARE -> BACKDROP` alone: it fails **exactly one case in 3,497**, the one
parametrised over the enum. Parametrise over the vocabulary, not over the
implementations that happen to use it.

**`user_taste` now has two readers with deliberately different predicates, and
conflating them re-breaks the term that needed the second one.** Added
2026-08-11 (M9 F5). `TasteRepository.get(user_id, *, model_name)` evaluates
`STALE_TASTE` and answers *"should I recompute?"*; `TasteRepository.latest(
user_id)` is one primary-key probe with **no predicate and no model argument**
and answers *"what is the best statement about this household anyone has
stored?"*. Both are needed and neither is the other spelled shorter:

- **A request has no embedder**, so it cannot supply `get`'s `model_name` —
  `create_app`'s lifespan builds a model only under `worker_enabled`, and
  `centroid()` checks the embedder first precisely because a process with no
  model has no honest value for that key. Routed through `centroid()`, PRD 05's
  taste ranking term is structurally `None` on the shipped default: a weight
  that reads like a signal, which is the `GenreAffinityProvider` failure PRD 06
  corrected once already.
- **`latest` must not inherit the staleness predicate**, and the reason is not
  performance. The watch state that moves the watermark is the same watch state
  the centroid was computed *from*, so a predicated `latest` would withhold the
  term from exactly the households that watch things — i.e. all of them — while
  looking correct on a fixture that never adds a second watch state. Pinned by
  `test_latest_answers_a_row_that_get_calls_stale`, which asserts `get` answers
  `None` on the same fixture so that `return await self.get(...)` cannot pass.
- **`latest` is read-only, and that is a boundary rather than a naming
  choice.** `centroid()` *writes* its refusals (a household below `_MIN_TITLES`
  gets a stored NULL-centroid row, which is what stops it being recomputed on
  every read forever), so a request path allowed to write here would stamp the
  deployment's absent model onto the household's cache and then invalidate it
  on every subsequent read.

Related, same task: **`TitleEmbeddingRepository.list_for_titles` gained a
keyword-only *optional* `model_name`, and `centroid()` deliberately does not
pass one.** Scoping the window's vector read to the current checkpoint looks
like an improvement and changes what a centroid *is*: mid-swap the mean would
be taken over whichever subset the backfill had re-embedded, and `title_count`
would report that as a fact about the household. `CandidatePoolService` keeps
the unscoped call for its own documented reason (the width mismatch is why
`_cosine` answers "no opinion" rather than raising inside a nightly job). Both
are pinned by cases asserting the recorded argument is `None`, because on a
single-model fixture the two spellings answer identically.

**The candidate-pair rate, re-measured over an enriched population — one walk,
both signals, 2026-08-12 (M9 S5).** `scripts/measure_pair_rates.py` drove
`rebuild`'s own page shape read-only over the whole embedded population:
**130,647 seeds, 262 pages of 500, 13,064,700 candidate pairs, 5,125 s
(85.4 min)** against S4's ~80-minute prediction, on a box with nothing else
dispatched. Counted over the **pool** `nearest_for` returns, never over stored
rows, and writing nothing.

| both sides carry | pairs | **pair rate** | seeds | single-side | coverage² | measured ÷ coverage² |
|---|---|---|---|---|---|---|
| a `genome_scores` row | 323,297 | **2.4746%** | 15,525 | 11.883% | 1.412% | **1.75×** |
| ≥ 5 MovieLens tags | 794,606 | **6.0821%** | 27,558 | 21.094% | 4.449% | **1.37×** |
| ≥ 10 MovieLens tags | 404,993 | **3.0999%** | 18,470 | 14.137% | 1.999% | **1.55×** |

⚠️ **2.4746% is a second measurement of the genome, never a delta against
1.81%** — S1 settled that M7's number came from 5,020 owned, name-selected,
pre-TMDb seeds in a database that no longer exists. What is new is that the
genome rate is now known over a population whose documents carry real
`overview`/`tagline`/`genres`/`keywords`, which is the thing S3's enrichment
existed to produce, and it is **still four times below the 10% floor the 0.25
weight assumes**.

**`coverage²` is wrong in a measurable direction, and the size of the error is
the finding.** All three signals beat their independent-draw prediction —
1.37–1.75× — so pool membership and signal membership are positively
correlated, most strongly for the genome (whose coverage concentrates in
popular, older, heavily-embedded films). This is the first time this project
has had the correction factor rather than the warning.

**A stricter tag threshold scores a *lower* pair rate, not a higher one, and
the decomposition is exact.** The M9 bar guessed `>= 10` would score higher on
fewer titles. It cannot: pairs at `>= 10` are a strict subset of pairs at
`>= 5` over an identical denominator, so the rate is monotone by construction.
The interesting half is *why the gap is what it is*: coverage falls
21.094% → 14.137% (×0.670), a pair needs **both** sides so that enters squared
(×0.449), and the heavily-tagged population really is more clustered
(1.55/1.37 = ×1.135). 6.0821% × 0.449 × 1.135 = **3.099%**, the measured value.
"More tags per title" is a real effect and it is swamped by its own coverage
loss, because both-sides squares everything.

**And the membership rate is not the signal.** Measured the same day against
`ml-latest/tags.csv` itself (21.3M rows, read outside the tree, no row
committed) over a **uniform random** 2,000-seed sample — 6.4125% both-sides at
`>= 5`, agreeing with the exhaustive walk's 6.0821% within 5.4%, which is the
sample's own control:

| tag-set Jaccard | n | mean | median | p90 | p99 | share sharing **no** tag |
|---|---|---|---|---|---|---|
| pool pairs, both `>= 5` | 12,825 | 0.0221 | 0.0055 | 0.0625 | 0.1765 | **47.3%** |
| random pairs, same population | 20,000 | 0.0038 | 0.0000 | 0.0115 | 0.0625 | 83.3% |
| pool pairs, **marginal** (tagged, no genome) | 2,595 | 0.0261 | **0.0000** | 0.0833 | 0.2222 | **62.3%** |
| random pairs, marginal | 20,000 | 0.0034 | 0.0000 | 0.0000 | 0.0769 | 92.7% |

**Near-chance is refuted — it is 5.8× chance overall and 7.7× on the marginal
population — and the conclusion that guess drew is confirmed anyway, for a
sharper reason.** The bar's illustrative "two 4-tag sets sharing one tag gives
0.14" sits between p90 and p99 of the marginal distribution, not at its centre:
the **median marginal pool pair shares no tag at all**. That is not a missing
signal, it is a **present** one — `_jaccard` returns `None` only when a *set* is
empty, so two titles with five tags each and nothing in common yield a hard
`0.0`, which `_blend` renormalises as a confident negative and which would
therefore **demote** 62.3% of the very pairs the term was added to promote,
relative to pairs carrying no tag data at all. ADR-0014's argument, arriving
from the set-valued side.

**A pair rate is a statement about membership and this one says so.** Nothing
here measures whether a tags term makes a neighbour list *better*; that would
need relevance judgements this project has never had, and the same caveat M7
attached to the genome's 0.25 weight applies unchanged.

**The bar's own prediction of 3–5% was refuted upward, and the arithmetic
behind it fails on both inputs.** It scaled M7's observed single-side-to-pair
ratio of 0.238 by a tier-wide 14.46%. The population that is actually embedded
carries 21.094%, not 14.46% — and **the ratio is not a constant even within one
walk**: 2.4746/11.883 = **0.208** for the genome against 6.0821/21.094 =
**0.288** for tags, a 38% spread over the identical pool. Carrying M7's 0.238
onto the right coverage predicts 5.02%; the measurement is 21% above that.

**`ml_tags_tmp.n_tags` is not a distinct-tag count, and the input is very
slightly generous.** Of the 27,558 embedded titles it puts at `>= 5`, **61.7%
disagree with a case-folded distinct count** and **485 (1.8%) hold fewer than
five distinct tags**. At 1.8% it moves no verdict, but a threshold named "≥ 5
tags" is counting tag *applications*.

⚠️ **The tag plant no longer reproduces the bar's table exactly, and the two
cells that moved are the two that localise the loss.** Re-measured 2026-08-12,
the join over titles of **any kind** gives 49,055 / 15,385 / 33,670 / 14,448 /
6,266 against the bar's 49,05**6** / 15,385 / 33,67**1** / 14,448 / 6,266 —
three cells exact, two one lower, so exactly one title stopped matching and it
carries **no genome and one-to-four tags**. Corroborated independently: the
tier's "any tags" read is 45,090 where the plan recorded 45,091 while genome,
`>= 5` and `genome-or-5` all agree. The mechanism is available and was
demonstrated rather than assumed — **`imdb_id` is in
`EnrichService._ENRICHABLE`**, so TMDb's `external_ids.imdb_id` overwrites
IMDb's own, and streaming all 12,707,540 rows of `title.basics.tsv.gz` finds
**28 enriched titles whose current `imdb_id` is not a tconst IMDb holds at
all**. That 28 is a *lower bound* on rewrites — a rewrite onto another valid
tconst is invisible to the check — and none of the 28 could be tied to a tagged
id through `links.csv`, so the individual title is characterised but not named.
**A tag plant is not stationary across an enrichment run.**

**The definition to join on is "titles of any kind", and the 381 are a
classification finding rather than a defect.** Filtered to `kind = 'movie'` the
same queries give 48,674 / 15,385 / 33,289 / 14,222 / 6,135; the difference is
exactly **381 titles this catalog classifies as `series` whose IMDb ids appear
in a movies-only dataset**. They cost the walk nothing — the embedded
population is `kind = 'movie'` and holds **zero** series — so the label on the
bar's row is wrong while its number is right.

**The genome's 15,565 rows reconcile to the walk's 15,525 seeds with no
residue**: **33** sit outside the frozen `s3_tier_snapshot` entirely (22 movies
with no `tmdb_id` but ≥ 100 votes, 8 with neither, 3 with a `tmdb_id` and fewer
than 100 IMDb votes) and **7** are in the tier but are still `skeleton` — part
of S4's 159-row gap — and `_POPULATION` excludes skeletons, so no `index` job
was ever owed for them. 15,525 + 33 + 7 = 15,565.

**`nearest_for` was asserted deterministic rather than assumed, on a full
page**: two reads of the same 500 seeds returned identical pools, id for id and
in the same order. That is what licenses comparing this walk against a later
`SimilarityService.rebuild()`, and the walk's genome counter is byte-for-byte
that rebuild's `pairs_with_tags / candidate_pairs` — pinned to it by
`tests/unit/test_scripts_measure_pair_rates.py` over one shared fake.

**The one fatal spelling, measured on the fixture that kills it.** Counting the
*stored* rows rather than the *pool* answers **147/1,000 = 14.70%** where the
pool answers **182/1,560 = 11.67%** on the same 40-title population: plausible,
four points high, and high **by construction**, because the stored rows are the
pool already sorted by a blend that weights the very signal being counted at
0.25. The first draft of `scripts/measure_pair_rates.py` was written with that
spelling on purpose and the case was red against it before it was fixed.
