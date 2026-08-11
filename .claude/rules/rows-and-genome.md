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
