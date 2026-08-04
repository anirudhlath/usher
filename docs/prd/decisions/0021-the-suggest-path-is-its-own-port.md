# ADR-0021 — The suggest path is its own port, so a dual write cannot arrive quietly

**Status:** Accepted. Implemented in M6 — settles a provisional marker in
[PRD 05](../05-search-and-similarity.md).
**Date:** 2026-08-02

## Context

[PRD 05](../05-search-and-similarity.md) has always treated autocomplete as
its own narrow path, separate from full-text search — "do not route
as-you-type queries through the full-text index" is the first sentence of
that section. But `SearchIndex.suggest` was one method on the same ABC as
`search`, `index` and `remove`, and PRD 05 carried a 🔶 asking M6 to decide
whether it should be its own port.

The hint that it should is in
[ADR-0002](0002-postgres-first-search.md) itself: Meilisearch is gated **"for
the instant-search box only"**. If the swap, when it happens, is scoped to
`suggest`, then `suggest` is the swap boundary and the rest of the class is
not.

## Decision

**Yes — `SuggestIndex` is a separate ABC with exactly one method:**
`suggest(prefix, limit) -> list[SearchHit]`.

**The argument that decides it is not tidiness. It is dual-write
visibility.**

ADR-0002's entire case for Postgres-first is "no dual-write synchronisation,
no ghost documents, no reindex-on-facet-change, no second stateful service."
If the gate fails and Meilisearch is added for the instant-search box,
documents must then be written to *both* engines — which is precisely the
cost ADR-0002 refused. Splitting the port puts that cost **in the type
system**: adding Meilisearch means adding a write path to a port that today
has none, and that is a visible, deliberate act with a review attached.

Folded into `SearchIndex`, the same change looks like **implementing a method
that was already there** — the most dangerous shape a costly change can take,
because nothing in the diff says a second copy of the catalog just came into
existence.

## Consequences

**Gained.** The follow-up ADR-0002's gate might trigger costs one class, and
its price is legible *before* anyone pays it. `SearchIndex` keeps
`index_many`/`remove`, for the semantic half, which genuinely is a written
artefact.

**Given up: `SuggestIndex` has no `index` and no `remove`.**
`PostgresSuggestIndex` queries `titles` directly through the
`ix_titles_name_trgm` index and maintains nothing of its own, so abstract
write methods would exist solely to be no-opped by the only implementation —
and a port method whose only implementation no-ops it documents nothing. The
day a second implementation needs them is the day the dual write becomes real
and gets paid for on purpose.

The same reasoning is what refused PRD 05's narrow `title_search_names` table
(boundary call 3): with no aliases and no people in M6 it would hold exactly
one row per title duplicating `titles(id, name, kind, popularity)` — a second
copy and a second staleness problem, in the milestone whose whole purpose is
to delete staleness problems.

**Also: two ports means two contract suites and two fakes, and
`FakeSuggestIndex` is more forgiving than the real thing in the *dangerous*
direction.** It computes edit distance in Python over its whole dict, so the
one property the real path exists for — capping candidates before the re-rank
— is structurally absent, and its typo tolerance is therefore *better* than
the shipped one. The cap case is **skipped against the fake by capability
flag** rather than passed, and asserted against real Postgres by the work the
statement does rather than by wall clock. A contract case that silently
passes because the double cannot express the property is the failure a
contract suite exists to prevent.

**Rejected: keeping one port and documenting the boundary in prose.** A
comment saying "only `suggest` is expected to be swapped" is not checkable,
does not appear in a diff that adds a write path, and is exactly the kind of
constraint this project has repeatedly found to have quietly stopped holding.

## Evidence

**The shipped implementations are themselves the argument, and this is the
part that would be lost if it were not written down.**
`PostgresSuggestIndex` and `PostgresSearchIndex` share the `AsyncSession`,
the `titles` table, and **nothing else**:

| | `PostgresSearchIndex` | `PostgresSuggestIndex` |
|---|---|---|
| Index used | GIN over `titles.search_document` (`fastupdate = off`), plus HNSW over `title_embeddings.embedding` | GIN `gin_trgm_ops` over `titles.name` |
| Query | `websearch_to_tsquery` + `ts_rank_cd` with an explicit weight array, and `<=>` on the vector lane | `%` over a capped candidate CTE, then `levenshtein_less_equal` as a re-rank |
| Ordering | RRF over two lanes, `id` breaking every tie | edit distance, then popularity, then `id` |
| GUC | `hnsw.iterative_scan`, `hnsw.ef_search` | `pg_trgm.similarity_threshold` |
| Writes | `index_many` / `remove` on `title_embeddings` | **none** |

**Two methods on one port that share no SQL, no index, no GUC and no ranking
rule were already two ports.** The split did not create a boundary; it named
one that was there.

The measurement that a future Meilisearch decision would actually be made on
is [ADR-0002](0002-postgres-first-search.md)'s gate — recall@5, and (as that
ADR now records) latency, on a typo set built from real catalog titles. **The
port split is what makes acting on that measurement cheap.** It is
deliberately not evidence *for* the split: the split is right whichever way
the gate goes.

## Uncertainty

**If the gate passes comfortably, the split buys a boundary nobody ever
crosses** — a port with one implementation, which
[ADR-0001](0001-abc-over-protocol.md)'s own reasoning says to be careful
about spending effort on. That cost is real and is stated plainly rather than
argued away. It is bought back cheaply: one method, no write path, one fake,
and a named second implementation with a measurable trigger.

**And the gate itself has not run** — Task 26 is outstanding at the time this
ADR is written, so nobody yet knows which way the decision this port exists
to make will go. The split was chosen on the structure of the two
implementations, not on the gate's result, which is the order that keeps it
an architecture decision rather than a reaction.
