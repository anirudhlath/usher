---
paths:
  - "src/usher/services/rows/**"
  - "src/usher/services/home.py"
  - "src/usher/services/taste.py"
  - "src/usher/services/similar.py"
  - "scripts/measure_rows.py"
  - "scripts/measure_pair_rates.py"
---

<!-- Trigger list, and both changes to it are measurements rather than guesses.

`similar.py` was ADDED on 2026-08-12 (M9 S7). Half this file is about the tag
genome, the genome's only consumer was `SimilarityService`'s blend, and the
trigger list did not name that module — so the two sessions most likely to undo
the S7 decision (anyone editing the blend, and anyone re-adding a signal) were
the two this file never loaded for. Checked rather than assumed:
`grep -rln genome src/usher/services/` finds `home.py`, `taste.py`,
`curation_pool.py`, `bootstrap.py` and `similar.py` — the first two were
already triggers, the last was the one that *blends* it and was not.
`curation_pool.py` and `bootstrap.py` are left off deliberately: they read the
vocabulary and import the file, and neither can reach a weight.

`derive.py` was REMOVED on 2026-09-02, on the same test applied the other way.
It had been a trigger and this file says nothing about it — `grep -n derive`
found the frontmatter line and one unrelated use of the word "derived", so a
`services/derive.py` session paid for ~600 lines of home-screen and genome
material and got no finding. Nothing else triggers on that module now; if
`DeriveService` earns findings, they belong beside `usher derive`'s other half
in `bootstrap-and-datasets.md` or in a file of their own, not here. The rule
both changes come from: **a trigger is justified by findings the file actually
holds about that module, and the check is a grep.** -->



# The home screen, row providers and the tag genome

Verified facts, loaded when working in this subsystem. Measured or observed,
never assumed — each entry carries its date, its sample and what it refuted.
The always-on conventions live in `CLAUDE.md`; this file is the evidence.

**Two subjects, and roughly half of this file is the one you are probably not
here for.** The **similarity blend** material is the three genome entries that
open the file (cosine distribution, the 1.190 ms refutation, coverage and its
denominators) plus everything from *The candidate-pair rate* through *the
binding reason a user-tag term was refused*. It exists for `similar.py` and
`scripts/measure_pair_rates.py` sessions — it is here rather than in
`search-and-embeddings.md` because the blend is the genome's only consumer —
and a session editing `services/rows/`, `home.py` or `taste.py` can skip all of
it. **Everything else is the home screen**: the sequential build, the row and
screen caches, `RowContext`, `BaseRow`'s hooks, `TasteService`'s memos, the
split genre vocabulary its providers still read, and the TTL that enrichment
invalidates.

**M7's measurements, taken 2026-08-04/05 on this host against
`pgvector/pgvector:pg17` (pgvector 0.8.6) unless stated otherwise.** Nothing
here re-measures `titles.popularity`; that column is `titles.tmdb_popularity`
since `m10a`/ADR-0040, M6's gate measured it NULL on all 1,271,138 rows, and
the correction (**77%** on a partly-enriched catalog) is in
`db-and-sql.md`'s M9 B7 section — not in this file, which is where a reader
following an older cross-reference will look.
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
≥100-IMDb-vote priority tier (204,494 titles, not the ~189k PRD 04 estimated),
**10.68%** of a real household's 5,020 owned titles. **The number that decides
whether the term does anything is none of those** — it is the *candidate-pair*
rate, because both sides of a pair need a vector, and it is **measured, never
squared** (a real pool is not an independent draw; see the measured correction
factors below). The genome is **movies-only and frozen at 2023-07-20**, so
coverage of anything newer is structurally zero and decays.

⚠️ **M7's 1.81% pair rate is void as a baseline and must never be differenced
against a later number** — settled 2026-08-11 by M9 Task S1, before anything in
M9 quoted it. The arithmetic gives it away: 502,000 candidate pairs over
`_CANDIDATE_POOL` (100) is exactly **5,020 seeds**, and those 5,020 were **one
household's owned titles**, promoted to the enriched *tier label* by a script
that never filled the *document* — `search_document`'s weight classes C and D
were empty. That database no longer exists, and neither surviving catalog even
holds the household that defined the population. **A later number placed beside
it is a second measurement, never a delta** — which is exactly what S5's
2.4746% below is.
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

- **The engaged window was read once per public method.** `TasteService.
  genre_affinity` and `.centroid` both open with `_engaged`, and one caller that
  wants both (the candidate pool, on a deployment with an embedder) therefore
  issued `list_recent(50)` **twice** per generation for a window that cannot
  move between them — one job, one transaction. The memo lives on
  `TasteService`, keyed `(service, user_id)`: **2 -> 1**.
- **`library_genre_counts()` was read once per answer.** It takes no `user_id`
  -- an `unnest(genres) GROUP BY` over the whole owned library, 1.27M titles,
  identical for every household -- and was paid per generation *and* per
  home-screen build. Memoised per service: **3 -> 1** over two households and
  three asks.
- **`RowContext.affinities` is a `Callable[[], Awaitable[...]]` and not a
  value, and that is the whole fix for a cache that could not save anything.**
  Building the context eagerly ran `list_recent(50)` + `list_by_ids(50)` + the
  library aggregate *before* `HomeService.compose_report`'s `get_screen` check
  could answer — the request path resolves its dependency graph before the
  handler runs, so an eager field is paid on every hit. Awaited by the one
  provider that reads it, a 30 s cache hit costs **1 -> 0**. **Any field added
  to `RowContext` that costs a statement owes the same treatment.**
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
rendered perfectly, raising nothing. **Its lifetime is deliberately not
load-bearing**: one `TasteService` is built per request and per unit of work,
but rather than rest on that the memo re-reads on a disagreeing
`max(watch_states.updated_at)`. Two existing cases hold one service across a
merge and require the second read to see it, and **both failed against the
first draft of the memo**, which is how the watermark check got written.
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
would report that as a fact about the household. Pinned by a case asserting the
recorded argument is `None`, because on a single-model fixture the two
spellings answer identically — **which is why passing one here is a mistake no
test outside that case can catch.**

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

🔴 **The one fatal spelling of that script, and it is the one that looks
right.** Counting the *stored* `title_neighbors` rows rather than the *pool*
`nearest_for` returns answers **147/1,000 = 14.70%** where the pool answers
**182/1,560 = 11.67%** on the same 40-title population: plausible, four points
high, and high **by construction**, because the stored rows are the pool already
sorted by a blend weighting the very signal being counted at 0.25. The first
draft of `scripts/measure_pair_rates.py` was written that way on purpose and the
case was red against it before it was fixed. What licenses comparing a
read-only walk against a later `rebuild()` at all is that **`nearest_for` was
asserted deterministic on a full page** — two reads of the same 500 seeds, same
ids, same order — and the walk's counter is byte-for-byte `rebuild`'s
`pairs_with_tags / candidate_pairs`, pinned by
`tests/unit/test_scripts_measure_pair_rates.py` over one shared fake.

Two caveats on the tag inputs, neither of which moves a verdict: the threshold
counts tag *applications*, not distinct tags (485 of the 27,558 titles at
`>= 5` hold fewer than five distinct ones), and **a tag plant is not stationary
across an enrichment run** — `imdb_id` is in `EnrichService._ENRICHABLE`, so
TMDb's `external_ids.imdb_id` overwrites IMDb's own and 28 enriched titles now
carry a tconst IMDb does not hold at all. Re-derive a join count; do not
transcribe one across a milestone.

## The genome term was removed on that number, and the read was kept (2026-08-12, M9 S7)

**`SimilarityService._WEIGHTS` loses `"tags"` and `_neighbors_for` stops passing
`tags=` to `_blend`. Nothing else moves.** `genome_scores`, `genome_tags`,
`GenomeRepository`, the pairwise statement, `NeighborCandidate.tags`,
`NeighborSeed.has_genome` and `NeighborRebuild`'s three coverage counters all
stay. [ADR-0024](../../docs/prd/decisions/0024-the-genome-is-one-dense-vector-per-title.md)
carries the amendment; this is what a session working in this subsystem needs.

**The removal changes no score on ~97.5% of pairs, and the arithmetic is
exact.** `_blend` renormalises over present signals, so with `W` the weights of
the non-genome signals present:

    score_with_genome = (W · score_without_genome + 0.25 · g) / (W + 0.25)

With all three others present `W = 0.75` and that is exactly
`0.75 · score_without + 0.25 · g`. Two things follow and both are worth
carrying:

- **The three surviving weights are left at M7's 0.45 / 0.20 / 0.10 rather than
  reverted to M6's 0.60 / 0.25 / 0.15**, so a genome-less pair is scored under
  precisely the denominator it already was and its score is byte-identical. The
  two spellings differ only on keywords-against-genres (0.600/0.267/0.133
  against 0.600/0.250/0.150), which the pair rate says nothing about — an
  unevidenced second decision riding on an evidenced first, and one that would
  move every row rather than the 2.4746%.
- **The term was a promotion exactly when `g > score_without`**, and the genome
  cosine's measured distribution is min 0.2556 / p1 0.4075 / p50 0.6095 / mean
  0.6101 over 268,157,000 pairs — so a genome-bearing candidate scoring below
  0.4075 on everything else was promoted with 99% probability, and genome
  coverage concentrates in popular, older, heavily-embedded films (the 1.75×
  correlation above). ⚠️ **The distribution of the three-signal score over a
  real pool is NOT measured, and no rebuild will ever measure it** — whatever
  `title_neighbors` holds was scored *without* the genome term, so it is the
  distribution of `score_without` and not of the four-signal score the
  promotion was relative to. How often the term actually reordered a list
  remains unknown and is not claimed. The identity is exact; the frequency is
  not.

**Removed, not zeroed, and this is the half a reviewer should check first.**
`_blend` adds `_WEIGHTS[name] * value` to the numerator **and** `_WEIGHTS[name]`
to the denominator, so `_WEIGHTS["tags"] = 0.0` is *arithmetically the same
program* as the signal being absent, to full precision, at every value — and it
still enters `blend_fingerprint`, declares every stored row stale and buys an
85-minute rebuild for a table whose every score is unchanged. **No behavioural
assertion anywhere can tell the two apart**, which is why the guard is
structural: `test_every_signal_the_blend_is_handed_has_a_weight_and_no_weight_
is_zero` asserts `{keywords of the _blend call} == set(_WEIGHTS)` over an AST
scan and `0.0 not in _WEIGHTS.values()`. The key and the argument also *have*
to move together — `_blend` looks up `_WEIGHTS[name]` for every signal it is
handed, so removing the key alone is a `KeyError` on the first pair of the
first page of a rebuild.

**ADR-0014's `None`-not-0.0 rule on this field survived the removal and changed
consumer.** Nothing blends the value, so its only reader is
`pairs_with_tags`, which counts `tags is not None`. A port answering `0.0` for a
half-covered pair would report a barely-covered catalog as fully covered —
**making a dead signal look live**, in the one number a later milestone would
re-open this decision on. That is the argument for keeping the read at all, and
it is why the removal saves the blend arithmetic and **not** the `<=>` or the
TOAST fetch per candidate pair. PRD 05's cost sentence is corrected rather than
quoted as a saving.

**The obligation the removal created, and how it was closed.** Dropping the key
moves the digest from `78900b2bd89a649774d7fd3efe082621` (M7/M8's four signals)
to `78f3ecd20e654c0f6aa4bdf646ec099b`, staling every stored row: at 130,647
embedded titles that is a full quadratic walk, priced by S4 at ~80 minutes and
measured by S5 at 85.4. **S7 did not run one; H7 did**, 2026-08-12 against
`usher-m9-pg` in **88.3 minutes** (3.4% over S5, taken while a whole-suite
mutation sweep held the same box, so not a clean baseline). `stale_neighbors()`
**0** against **3,266,175 rows** — 130,647 seeds × 25, one fingerprint value in
the table — and the control S7 demanded held exactly: the rebuild's own
`pairs_with_tags / candidate_pairs` is the same **323,297 / 13,064,700 =
2.4746%** S5's read-only walk reported, with `seeds_with_genome` agreeing at
**15,525**, so the pool was invariant to the weight change and S5's tags figure
is not void.

🔴 **Do not quote `78f3ecd2…` as the current fingerprint, and do not quote H7's
row count as the table's state.** Both were overtaken the next day and in
opposite ways:

- **`78f3ecd20e654c0f6aa4bdf646ec099b` is the digest that could not move —
  the bug, not the fix.** It is what the post-S7 three-signal blend produced
  while `blend_fingerprint` hashed only its three constants (`_WEIGHTS`,
  `_NEIGHBORS_PER_TITLE`, `_CANDIDATE_POOL`), and on 2026-08-13 swapping
  `USHER_EMBEDDING_MODEL` from `fastembed:BAAI/bge-small-en-v1.5` to
  `openai:BAAI/bge-m3` was demonstrated to leave it at exactly that value — the
  largest weight, 0.45, is a cosine of two *embeddings*, so every stored row's
  meaning had changed while `usher.similarity.neighbors.stale` read zero
  throughout. That is why `embedding_model` is now the **fourth** input, and
  why quoting this digest as current states the defect as the repair. **The
  signature is
  `blend_fingerprint(*, embedding_model: str)` — keyword-only; a bare
  `blend_fingerprint()` is a `TypeError`** — and there is therefore no single
  current value to quote: `openai:BAAI/bge-m3` gives
  `a7013154c014e0ff1b60ef5d8534a115` and `fastembed:BAAI/bge-small-en-v1.5`
  gives `772433d709b3d77d5815ed26726534e1`. **Recompute it, never transcribe
  it.** Written up in `search-and-embeddings.md`.
- **H7's 3,266,175 rows were deleted by `m09e`**, which widened
  `title_embeddings.embedding` from `halfvec(384)` to `halfvec(1024)` and took
  every neighbour row with it (3,266,175 → **0**). ⚠️ So **this section's own
  warning is live again in the opposite direction**: it was written because
  *"`blend_fingerprint` reports no stale rows" is satisfied by an empty table*,
  the table was then filled, and it was emptied again. The live catalog was
  re-embedded and rebuilt on 2026-08-13 to **3,268,000** rows and those were
  re-stamped `a7013154…` in place rather than recomputed — which was only legal
  because `title_embeddings` held exactly one `model_name` and `title_neighbors`
  exactly one fingerprint. **Check both counts before ever doing that**, and
  record the row count beside any future "0 stale" verdict.

`_M7_FOUR_SIGNAL_FINGERPRINT` is pinned as a literal in
`tests/unit/test_services_similar.py`, and the case around it **asserts the
current function does not answer it** rather than reproducing it. It used to
reproduce it by monkeypatching `_WEIGHTS`; adding a fourth input changed the
payload's *shape*, so no arguments can produce a three-key digest any more.
**When a digest gains an input, every test that licenses an older digest by
calling the current function silently becomes unsatisfiable rather than
wrong.**

⚠️ **The freed name `tags` is a trap and it is resolved in ADR-0024 rather than
in a commit message.** `_WEIGHTS`, `NeighborCandidate.tags` and
`pairs_with_tags` all spell the *tag genome* as `tags`, and S6 evaluated
MovieLens **user tags** under the same word (refused at 6.0821%). A stored
score records only a fingerprint, so a later reader finding `tags` back in
`_WEIGHTS` could not tell which signal a row contains. **The genome, if it
returns, is `genome`; a user-tag term is `user_tags`.**

**`coverage²` is wrong in a measurable direction, and the size of the error is
the finding.** All three signals beat their independent-draw prediction —
1.37–1.75× — so pool membership and signal membership are positively
correlated, most strongly for the genome (whose coverage concentrates in
popular, older, heavily-embedded films). **Use the measured factor; never
square a coverage and call it a pair rate.** A stricter threshold cannot help,
either: pairs at `>= 10` are a strict subset of pairs at `>= 5` over an
identical denominator, so the rate is monotone by construction and the
"more tags per title" effect (×1.135 clustering) is swamped by its own
coverage loss (×0.449, because both sides square).

### 🔴 The binding reason a user-tag term was refused is the Jaccard distribution, not the pair rate

**A pair rate is a statement about *membership*.** Nothing here measures whether
a tags term makes a neighbour list *better*; that needs relevance judgements
this project has never had. What decides the question is the shape of the value
the term would feed `_blend`, measured against `ml-latest/tags.csv` itself
(21.3M rows, read outside the tree, no row committed) over a uniform random
2,000-seed sample — 6.4125% both-sides at `>= 5`, agreeing with the exhaustive
walk's 6.0821% within 5.4%, which is the sample's own control:

| tag-set Jaccard | n | mean | median | p90 | share sharing **no** tag |
|---|---|---|---|---|---|
| pool pairs, both `>= 5` | 12,825 | 0.0221 | 0.0055 | 0.0625 | **47.3%** |
| random pairs, same population | 20,000 | 0.0038 | 0.0000 | 0.0115 | 83.3% |
| pool pairs, **marginal** (tagged, no genome) | 2,595 | 0.0261 | **0.0000** | 0.0833 | **62.3%** |
| random pairs, marginal | 20,000 | 0.0034 | 0.0000 | 0.0000 | 92.7% |

Near-chance is refuted — it is 5.8× chance overall and 7.7× on the marginal
population — **and the term is still wrong**, for a sharper reason than a low
rate. The **median marginal pool pair shares no tag at all**, and `_jaccard`
returns `None` only when a *set* is empty, so two titles with five tags each
and nothing in common yield a hard `0.0` that `_blend` renormalises as a
confident negative. The term would **demote 62.3% of the very pairs it fired
on**, relative to pairs carrying no tag data at all. **ADR-0014's rule covers
*absence*; this is presence with no overlap** — the default over an open
user-tag vocabulary, and evidence over a closed ~19-value genre one.

**The verdict —
[ADR-0035](../../docs/prd/decisions/0035-the-tags-similarity-term.md),
2026-08-12 (M9 S6):** 6.0821% is the `< 10%` arm of the pre-registered bar, so
**no user-tag term is built and no line of `src/` moved on this arm** —
`services/similar.py` and `tests/unit/test_services_similar.py` are absent from
that diff, which is what the bar required. **The rate is the weaker of the two
reasons and should not be the first thing a later reader re-checks.**

⚠️ **And the rate is buyable, which is why it cannot be the criterion.**
Single-side coverage at `>= 1 tag` is **34.47%**, projecting 11.9% (independent
draws) to **16.3%** (× the measured 1.37) — over the floor on both arithmetics.
**A later reader who lowers the threshold will clear the bar and make the zero
worse.** Raising coverage at `>= 5` is the direction that is closed: clearing
10% needs **27.0%** against 21.094%, ~7,700 more `>= 5`-tagged titles inside the
same 130,647-title population, while the archive is frozen at 2023-07-20 and
**19,222** of this catalog's tagged titles carry one to four tags and no genome.
Enrichment reaches titles MovieLens never tagged, so every further pass moves
coverage **down**. The scoped follow-up ADR-0035 names is three read-only
measurements — the `>= 1` rate over *distinct* tags, the empty-overlap share at
whatever threshold clears, and whether TF-IDF over the tag strings or an
embedding of the joined tag text puts the median firing pair above zero — and
no build.

## Every row provider here reads the split genre vocabulary, and none of them was fixed

✅ **Superseded in the writer half, 2026-09-01.** The six readers' code is
unchanged and everything below still describes it — but there is now a writer
that reaches all of them: `GenreNormalisationService` (`usher genres
--backfill`, `services/genres.py`) rewrites `titles.genres` through
`canonicalise_genres` (`domain/genres.py`), so `library_genre_counts()` stops
offering two shelves for one concept once the backfill has run. The other half
of that story — 79,913 rows rewritten, 304 embeddings staled — is in
`search-and-embeddings.md` under *"Weight class D and segment 6 both carried
two spellings of one concept"*; the SQL that does the rewrite is in
`db-and-sql.md`.

`titles.genres` unions two importers' vocabularies and zero of 1,272,866 titles
carry both spellings of any concept (2026-08-19,
[ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md)).
That ADR fixed `/browse`'s filter and facets at read time and reached **none**
of `GenreAffinityProvider`, `TasteService`, `BecauseYouWatched`, `Seasonal`,
`SimilarityService` or `CandidatePoolService` — all six read the raw column,
and `TitleRepository.list_owned_by_tag` is deliberately still exact
containment. So `library_genre_counts()` still offers a household two shelves
for one concept, and a `Sci-Fi` shelf cannot reach an enriched title. **Five of
the six are in this file's trigger paths; `CandidatePoolService`
(`services/curation_pool.py`) is not**, so a session that fixes the readers has
to open that module deliberately — it is listed here because the set is the
finding, not because this file covers it.

**Effect size unmeasured, and the denominator is why**: this household owns 180
titles, which is too few to say anything, and the honest version of the
question is a catalog-scale sample rather than a live A/B.

⚠️ **The similarity Jaccard term is the one to watch.** `_jaccard(seed.genres,
candidate.genres)` scores a skeleton science-fiction film against an enriched
one at a hard **0** while both are science fiction, and cannot tell that from
"we do not know either one's genres" — the exact ambiguity this file already
records for the tag term. It costs nothing **today** because `_POPULATION`
excludes skeletons, so both sides of every stored pair speak TMDb's vocabulary.
It becomes real the moment the embedded population widens past the enriched
tier. That is a trap that is not sprung, written down as unsprung on purpose:
whoever widens `_POPULATION` owns this term.

## A row TTL is a bet that the catalog does not change, and enrichment is the write that breaks it (2026-08-26)

**The row cache had two invalidation triggers and enrichment was not one of
them**, so a card built before its title was enriched kept its skeleton name
and its absent artwork for the length of its TTL. Measured against the deployed
1,276,208-title catalog, comparing a live `GET /home` against a cold compose of
the *same eight rows* in a fresh process (`cli._home`'s wiring, cache cleared):

| | live `/home` | cold compose |
|---|---|---|
| cards | 145 | 145 |
| `artwork: null` | **55 (38%)** | **5 (3%)** |

**The null rate is ordered by TTL, which is the finding rather than the
totals** — and it is what separates "the catalog has no artwork" from "this
entry is old":

| row | TTL | null |
|---|---|---|
| `continue-watching`, `next-up` | 60 s | 0% |
| `recently-added` | 5 min | 12% |
| `genre-affinity` | 1 h | 0–5% |
| `because-you-watched` | **6 h** | **75–90%** |

🔴 **The obvious diagnosis is wrong and the spot-check is what refuted it.**
"Those titles are skeletons TMDb never reached" fits the shape exactly — only
132,410 of 1,276,208 titles (10.4%) carry any `images` row, and it is
`because-you-watched` that draws from the whole catalog while `recently-added`
and `genre-affinity` draw from the owned library. It is still false: the eight
null-artwork cards sampled by id were **all `enriched`, all carrying 10 posters
and 2–21 images each**, and `_PRIMARY_FOR_TITLES` run verbatim against those
ids returned a `is_primary` poster for every one. The stale entry and the
absent artwork produce the identical card, and the population statistic
corroborates the wrong one. **Ask the cache before believing the catalog.**

**`title.updated` is not the repair, and it looks like it should be.**
`EnrichService` has published that frame since M4 and the console has handled
it since the design handoff — but the handler is colour-only by design
(`patterns.md` §7; `Home.tsx` says *"It does not refetch"* in terms), so the
frame repairs a card that is already on screen and nothing about the cached row
behind it. **A read-through loop that ends at the client does not close a cache
the server reads from.**

**The fix is keyed on the title, not on the write, and that is what keeps it
out of the fan-out the PRD refuses.** `RowCache.invalidate_titles` drops only
entries whose cards name the enriched title, so a screen of 145 cards is
invalidated at most 145 times by a backfill of any size — against S3's 130,647
enrichments in 1.98 h, fewer rebuilds than the 30 s screen TTL forces over the
same window (237). A `clear()` behind the same name passes every case that
names a title and has none of this property, which is why
`test_invalidating_no_titles_drops_nothing` exists.

**Both halves of the cache are scanned.** A screen is stored whole, so a row
can reach one without ever being written to the row half — dropping only the
row half is `invalidate`'s own recorded subtle bug (the next request is a
screen cache hit and the invalidation had no visible effect) arriving through
the other door.
