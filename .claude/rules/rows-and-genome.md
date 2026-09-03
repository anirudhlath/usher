---
paths:
  - "src/usher/services/rows/**"
  - "src/usher/services/home.py"
  - "src/usher/services/taste.py"
  - "src/usher/services/similar.py"
  - "scripts/measure_rows.py"
  - "scripts/measure_pair_rates.py"
---

<!-- `similar.py` is a trigger on purpose: half this file is the tag genome, the
blend is the genome's only consumer, and the sessions most likely to undo the
removal below are the ones editing the blend or re-adding a signal. The rule
behind that and every other change to this list: a trigger is justified by
findings the file actually holds about that module, and the check is a grep. -->

# The home screen, row providers and the tag genome

Two subjects. The **similarity blend** material is for `similar.py` and
`scripts/measure_pair_rates.py` sessions; everything from *The home screen* down
is for `services/rows/`, `home.py` and `taste.py`, and neither half needs the
other.

## The similarity blend

- **The genome is not a term in the blend.** `_WEIGHTS` has no `"tags"` key and
  `_neighbors_for` does not pass `tags=`
  ([ADR-0024](../../docs/prd/decisions/0024-the-genome-is-one-dense-vector-per-title.md)
  carries that amendment; ADR-0035 settles only the *user-tag* term). Everything
  that *reads* it stays, and **`NeighborCandidate.tags` must answer `None`,
  never `0.0`** — its only reader counts `tags is not None`, so a zero reports a
  barely-covered catalog as fully covered: `genome_scores`, `genome_tags`,
  `GenomeRepository`, `NeighborCandidate.tags`, `NeighborSeed.has_genome` and
  `NeighborRebuild`'s coverage counters.
- **Removed, not zeroed.** `_WEIGHTS["tags"] = 0.0` is arithmetically the same
  program at every value, and it still enters `blend_fingerprint`, declares
  every stored row stale and buys a ~90-minute rebuild for a table whose scores
  are unchanged. No behavioural assertion can tell the two apart, so the guard
  is structural:
  `test_every_signal_the_blend_is_handed_has_a_weight_and_no_weight_is_zero`
  AST-scans the `_blend` call and asserts `0.0 not in _WEIGHTS.values()`.
  **The key and the argument move together** — `_blend`
  looks up `_WEIGHTS[name]` for every signal handed to it, so dropping one alone
  is a `KeyError` on the first pair of a rebuild.
- **The three surviving weights stay at 0.45 / 0.20 / 0.10.** Reverting them to
  M6's values is an unevidenced second decision riding on an evidenced first,
  and it would move every row rather than the covered minority.
- **The freed name `tags` is a trap.** A stored score records only a
  fingerprint, so a later reader finding `tags` back in `_WEIGHTS` cannot tell
  which signal a row holds. **The genome, if it returns, is `genome`; a
  user-tag term is `user_tags`.**
- **`blend_fingerprint(*, embedding_model: str)` is keyword-only, and
  `embedding_model` is an input because the largest weight is a cosine of two
  embeddings.** Without it a model swap left every stored row's meaning changed
  while `usher.similarity.neighbors.stale` read zero throughout. **Recompute a
  digest; never transcribe one.** Written up in `search-and-embeddings.md`.
- **Record the row count beside any "0 stale" verdict** — an empty table
  satisfies it, and `m09e` has emptied `title_neighbors` once already.
- **When a digest gains an input, every test that licenses an older digest by
  calling the current function becomes unsatisfiable rather than wrong.**
  `_M7_FOUR_SIGNAL_FINGERPRINT` is pinned as a literal and the case asserts the
  current function does *not* answer it.
- **Count a pair rate over the pool `nearest_for` returns, never over stored
  `title_neighbors` rows** — the stored rows are that pool already sorted by a
  blend weighting the very signal being counted, so they read high by
  construction.
- **Never square a coverage and call it a pair rate.** Pool membership and
  signal membership are positively correlated (measured 1.37–1.75× above the
  independent-draw prediction); use the measured factor.
- **No user-tag term, and the binding reason is the Jaccard distribution rather
  than the rate.** `_jaccard` returns `None` only when a *set* is empty, so two
  titles that each carry tags and share none yield a hard `0.0` that `_blend`
  renormalises as a confident negative — demoting the majority of the pairs the
  term fires on. **ADR-0014's rule covers *absence*; this is presence with no
  overlap.** The rate is also buyable by lowering the tag threshold, which
  clears the bar and makes the zero worse.
- ⚠️ **The same trap is live on genres.** `_jaccard(seed.genres,
  candidate.genres)` scores a skeleton science-fiction film against an enriched
  one at a hard `0` and cannot tell that from "we know neither one's genres". It
  costs nothing today only because `_POPULATION` excludes skeletons — **whoever
  widens `_POPULATION` owns this term.**
- **The genome is movies-only and frozen at 2023-07-20**, so its coverage of
  anything newer is structurally zero and decays as the catalog grows.

## The home screen

- **Rows build sequentially**
  ([ADR-0025](../../docs/prd/decisions/0025-rows-build-sequentially.md)), and
  **a non-overlap assertion passes against the exact `gather` it exists to
  kill** — coroutines that never suspend produce disjoint windows. What has
  teeth is a depth recorder shared by the providers asserting
  `max_in_flight == 1`, and it needs its own control: deleting the recorder's
  `await asyncio.sleep(0)` makes every implementation look sequential. A second
  case AST-scans `home.py` for `gather`/`TaskGroup`/`create_task`/`wait`,
  walking `ast.Import` **and** `ast.ImportFrom` and matching the bare name as
  well as the attribute.
- ⚠️ **A compose latency is a property of the household, not of the composer** —
  the same code measures two p95s a factor of thirty apart, one under budget and
  one over. Never quote one without naming the household it belongs to.
- **A `RowContext` field that costs a statement is a
  `Callable[[], Awaitable[...]]`, not a value.** The request path resolves its
  dependency graph before the handler runs, so an eager field is paid on every
  cache hit — including the hits the cache exists to make free.
- **A shelf family hydrates once per `propose`, not once per row.** `LLMRow`
  overrides `_known`, `_ownership` and `_artwork` so one generation costs one
  read of each rather than one per curated shelf.
- **Grep `services/rows/` for a hook's name before adding one.** A subclass
  *attribute* shadows a base-class *method* — `FranchiseRow._owned` is a tuple,
  so a new `BaseRow._owned` raised `TypeError: 'tuple' object is not callable`
  at render time in one provider of ten. The failure is invisible in the class
  that declares the hook.
- **Assert port-call counts derived from the screen** (`images.calls ==
  len(screen)`), not as literals, so a shelf the composer proposed and did not
  build costs nothing and the assertion says so.
- **The card kind is keyed on the *row's* `display_hint`, and the mapping must
  be total over `DisplayHint`** rather than over the hints the registry happens
  to emit. `wide` and `square` have no emitter today, so a registry-derived
  mapping looks complete and is a `KeyError` inside `hydrate` — a 500 on the
  home screen — the first time a provider uses one. Parametrise over the
  vocabulary, not over the implementations that use it.

## The row and screen caches

- **A screen refresh reuses every row whose own TTL is still running**, so a
  seconds-old screen can carry a five-minute-old shelf. That is the second cache
  layer working, and it is why the row half has **no grace window of its own**:
  the refresh unit is a *screen*, and a per-row grace with no per-row refresh
  behind it would serve stale rows nothing ever replaces.
- **A stale-serve grace window must be gated on there being a refresher.**
  `_stale_grace` is zero when `refresh is None`, so `usher home` — whose process
  ends when the command does — serves nothing stale. Passing a no-op refresher
  instead opens the window with nothing behind it, which is worse than the miss
  it avoids.
- **Two stale serves cannot overlap in wall clock and that is the feature**: the
  fast path returns out of a dict with no `await`, so a case asserting they
  intersect asserts serve-stale is broken. The pair with teeth is a read against
  the *in-flight refresh*, which is what rules out a queue clearing its dedup
  mark at `take()` rather than at `done()`. **When an overlap assertion is
  impossible because the fast path never suspends, name the other side of the
  real race rather than weakening the claim to a count.**
- **Enrichment invalidates rows, keyed on the title rather than on the write.**
  `RowCache.invalidate_titles` drops only entries whose cards name the enriched
  title, so a backfill of any size invalidates a screen at most once per card.
  A `clear()` behind the same name passes every case that names a title, which
  is why `test_invalidating_no_titles_drops_nothing` exists.
- **Both halves of the cache are scanned.** A screen is stored whole, so a row
  can reach one without ever being written to the row half — dropping only the
  row half leaves the next request a screen hit and no visible effect.
- **`title.updated` is not the repair**, though it looks like it should be: the
  console's handler is colour-only by design, so the frame repairs a card
  already on screen and nothing about the cached row behind it. A read-through
  loop that ends at the client does not close a cache the server reads from.
- 🔴 **Ask the cache before believing the catalog.** A stale entry and genuinely
  absent artwork produce the identical card, and the population statistic (most
  titles carry no `images` row at all) corroborates the wrong diagnosis. Null
  rates ordered by row TTL are what separate the two.

## `TasteService` and `user_taste`

- **`user_taste` has two readers with deliberately different predicates**, both
  on `TasteRepository` rather than the service. `get(user_id, *, model_name)`
  evaluates `STALE_TASTE` and answers *"should I recompute?"*; `latest(user_id)`
  is one primary-key probe with no predicate and no model argument and answers
  *"what is the best stored statement about this household?"*. Neither is the
  other spelled shorter:
  - **A request has no embedder**, so it cannot supply `model_name` — routed
    through `centroid()`, the taste ranking term is structurally `None` on the
    shipped default, a weight that reads like a signal.
  - **`latest` must not inherit the staleness predicate.** The watch state that
    moves the watermark is the one the centroid was computed *from*, so a
    predicated `latest` withholds the term from exactly the households that
    watch things, while looking correct on a fixture with one watch state.
  - **`latest` is read-only, and that is a boundary.** `centroid()` *writes* its
    refusals, so a request path allowed to write here would stamp the
    deployment's absent model onto the household's cache and re-invalidate it on
    every read.
- **`TitleEmbeddingRepository.list_for_titles` takes an optional `model_name`
  and `centroid()` deliberately does not pass one.** Scoping the window to the
  current checkpoint looks like an improvement and changes what a centroid *is*:
  mid-swap the mean would be taken over whichever subset a backfill had
  re-embedded, and `title_count` would report that as a fact about the
  household. Pinned by a case on the recorded argument, since on a single-model
  fixture the two spellings answer identically.
- **A memo on a per-user read needs its key asserted, not just its count.**
  `_engaged` is keyed `(service, user_id)` and re-reads on a disagreeing
  `max(watch_states.updated_at)`; `library_genre_counts()` takes no `user_id`
  and is memoised per service. A count alone is satisfied by a memo that
  re-reads and hands back the wrong entry, whose failure mode is one household's
  history deciding another's affinity, pool and shelves — rendered perfectly,
  raising nothing.

## The split genre vocabulary

- **Six services still read `titles.genres` raw**: `GenreAffinityProvider`,
  `TasteService`, `BecauseYouWatched`, `Seasonal`, `SimilarityService` and
  `CandidatePoolService`.
  [ADR-0039](../../docs/prd/decisions/0039-the-genre-vocabulary-is-usher-owned.md)
  fixed `/browse`'s filter and facets at read time and reached none of them;
  `GenreNormalisationService` (`usher genres --backfill`) is the writer that
  does, by rewriting the column through `canonicalise_genres`.
  `list_owned_by_tag` is deliberately **not** widened by ADR-0039 and stays
  exact containment — it is the call the two genre-shaped providers make, so it
  is the method a session acting on this bullet is most likely to break.
