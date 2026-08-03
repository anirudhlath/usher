# ADR-0002 — Postgres-first search, Meilisearch behind a measurable gate

**Status:** Accepted — reverses an earlier recommendation. Implemented in M6;
**the gate's outcome is ⏳ and its definition has been corrected** — see
Uncertainty.

## Context

The initial design specified PostgreSQL as the canonical store plus Meilisearch
as a dedicated search and vector index. Research to validate that split
undermined its own premise.

## Decision

v1 implements search entirely in PostgreSQL: weighted `tsvector` full-text, a
separate `pg_trgm` + `fuzzystrmatch` autocomplete path, and `pgvector` for
embeddings, fused with Reciprocal Rank Fusion.

`SearchIndex` is an ABC. Meilisearch may be added later for the instant-search
box only, gated on a **measurable** failure: recall@5 on a typo test set built
from real catalog titles weighted toward short names.

## Consequences

**Gained:** no dual-write synchronisation, no ghost documents, no
reindex-on-facet-change, no second stateful service, transactional consistency
between catalog and index, and arbitrary SQL for ranking blends.

**Given up:** typo tolerance is genuinely weaker. This is the real cost and it
is not hand-waved — see below.

**Retained:** the upgrade path costs one ABC implementation because nothing
above the port knows which engine answers.

## Evidence

**The headline argument against Postgres does not apply to this workload.**
The widely-cited benchmark showing 8–25 s `ts_rank` queries is driven by
match-set cardinality — it appears when a single query matches ~1M rows, which
long-text search produces and title search does not. Two details are almost
always omitted from citations of it: the same article's own mitigation (capping
candidates at ~10k) brought it to 144–400 ms, and the authors *started* with a
34k movie dataset and abandoned it because it was too small to show any
difference against Elasticsearch. A competing vendor concedes the inverse:
for terms matching 1–10 documents, Postgres full-text search is faster than
theirs.

**Ranking blend is application code regardless of engine.** Meilisearch's custom
ranking rules are only `attribute:asc|desc` applied as bucket-sort tiebreakers —
no arithmetic exists in the API. Typesense caps `sort_by` at three fields and
has an open, explicitly non-committed feature request for expression scoring.
Engines that *can* evaluate `0.6·semantic + 0.2·log(popularity) + 0.2·recency`
in-engine (Vespa, Elasticsearch `script_score`) are not appropriate for a
single-host solo-maintained deployment. So the engine is a candidate generator
and the blend is ours either way — which is precisely what makes starting
simple low-risk rather than a gamble.

**The genuine weakness is typo tolerance.** `pg_trgm` is trigram-set overlap,
not edit distance. A four-character word yields ~5 trigrams, so one typo
destroys most of them, and transpositions are close to a blind spot — `Up`,
`Her`, `Dune`, `Alien` are the worst case. A Postgres vendor's own comparison
concluded that only Typesense and Meilisearch handled misspellings properly.
Mitigation is the narrow autocomplete path with `levenshtein_less_equal` re-rank
over a capped candidate set; `fuzzystrmatch` is a core contrib module, so
Postgres does have edit distance — it simply isn't indexable and must run over
a narrowed set.

**Typesense is excluded outright.** It is fully memory-resident with no on-disk
index; the index rebuilds into RAM on every start and the server returns HTTP
503 throughout — officially "5–60 minutes depending on dataset size", roughly
2–15 minutes at this scale. The maintainers rejected dump-on-shutdown and
recommend a 3-node HA cluster instead. Unacceptable on a host that reboots for
kernel and GPU driver updates.

**Scale is not the constraint.** At 1M × 384 dimensions on a 64 GB host, every
option's memory footprint is noise (pgvector ~2–3 GB HNSW, ~1.5 GB with
`halfvec`). Published RAM objections are calibrated for per-GB cloud billing or
20M+ document corpora.

## Notes for implementation

Each of these was implemented in M6, and three of them turned out to need a
correction that is recorded where it belongs rather than here.

- pgvector ≥ 0.8.5 — CVE-2026-3172 and HNSW vacuum corruption fixes. The
  image ships **0.8.6**, so the floor is met.
- `hnsw.iterative_scan = relaxed_order` is **off by default** and must be set,
  or filtered vector search suffers severe recall collapse. **Measured in M6
  and it is worse than "recall collapse" suggests**: at 2% filter selectivity
  the default returns **0.88 rows on average against a request for ten** —
  HNSW visits `ef_search` candidates, the filter kills them, and the scan is
  already over. That is an empty endpoint, not a degraded ranking, and
  `ef_search` is the wrong lever (40 → 200 with the GUC off still yields
  4.24 of 10). `relaxed_order` beats `strict_order` on recall, 0.508 against
  0.100, because strict ordering terminates earlier to buy a guarantee RRF
  re-ranking does not need. ⏳ **A cheaper answer exists and is not built**:
  at high filter selectivity a plain btree on the filter column lets the
  planner abandon HNSW and answer exactly, which would make the ANN question
  exist only inside a selectivity band. Recorded here rather than shipped —
  no such index is in M6, and choosing the band deliberately needs a run
  against real embeddings.
- GIN `fastupdate = off`; the default pending-list buffering causes p99
  spikes. **The mechanism is now measured**: a 1.6 MB pending list cost 231
  buffers against 30 — 7.7× read amplification on the index stage, invisible
  in `EXPLAIN` unless you look at buffers.
- Fuse with RRF, never by adding scores from incompatible scales. **Five
  traps were reproduced in M6**, one of which is silent and total:
  `row_number()` returns `bigint`, so `1 / (60 + rank)` is integer division —
  every score becomes `0.0`, the result comes back in `id` order, and nothing
  errors.
- **The `pg_trgm` mitigation named above — a capped candidate set plus a
  `levenshtein_less_equal` re-rank — is mandatory rather than advisory.**
  This ADR's cardinality argument holds and is sharper than it was stated: at
  a constant 1.32M corpus, latency spans 0.12 ms → 556.76 ms driven entirely
  by match-set size, and ranking has **no `LIMIT` pushdown** — of a 601.9 ms
  ranked query, 42 ms is the index scan and the other 560 ms is fetching
  650,000 heap tuples so `ts_rank_cd` can score them.

## Uncertainty

No rigorous public benchmark of `pg_trgm` typo tolerance against
Meilisearch/Typesense on a labelled dataset appears to exist. That hole sits
directly under this decision — which is exactly why the upgrade gate is an
empirical test against our own catalog rather than a judgement call.

**⏳ The gate has not been run against the real catalog.** M6 built
everything it measures — `PostgresSuggestIndex`, the trigram index, the
`levenshtein_less_equal` re-rank — and the run itself is outstanding. Nothing
in this ADR is decided by M6, and Meilisearch is not added either way
(boundary call 7): a second stateful service bolted on at the end of a
milestone is not what a measurement with a decision attached is for.

**And a synthetic dry run of that gate found something about the gate
itself.** 604 single-edit typo cases over 34 genuinely short real titles
planted in a 2.08M-name corpus with real collisions:

| strategy | recall@5 | transposition | p50 | p95 |
|---|---|---|---|---|
| [05](../05-search-and-similarity.md) as literally written | 66.2% | 34.5% | 181 ms | 241 ms |
| GIN `%` @0.1 | **93.5%** | 82.8% | 582 ms | 1,893 ms |
| GiST KNN `ORDER BY name <-> q` | **93.5%** | 82.8% | 281 ms | **342 ms** |
| btree `lower(name) text_pattern_ops` prefix | — (no typo tolerance) | — | **0.12–1.30 ms** | 0.14–18.85 ms |

**Recall is arguable; latency is not.** Every configuration reaching 93.5%
has a p50 of 281–582 ms and a p95 up to 1,893 ms, against an as-you-type
budget of roughly **50 ms**. Recall is the half that *passes*.

> **This ADR and [PRD 05](../05-search-and-similarity.md) both define the
> gate purely as recall@5, and that omission is itself a finding.** A gate
> that a configuration can pass while being 6–37× too slow for the box it
> exists to serve is not measuring the thing the decision turns on. **The
> gate must measure latency as well as recall**, and a run reporting only
> recall does not close it.

Two more corrections to the gate's own construction, both measured:

- **The "cap candidates" mitigation must be an *ordered* cap.** PRD 05's
  `LIMIT 3000` with no `ORDER BY` truncates arbitrarily, which makes
  *lowering* the similarity threshold make recall **worse**: 66.2% @0.3 →
  48.5% @0.1 → 2.6% @0.05.
- **This ADR's own worked examples sit below `pg_trgm`'s default threshold.**
  `similarity('dune','dnue') = 0.111`, `('her','hor') = 0.143`,
  `('up','uo') = 0.200`, against a 0.3 default — so `name % 'dnue'` matches
  *nothing*. "Transpositions are close to a blind spot" was exact.

Recall by title length under the best tuning, which is the shape a real
result set will have: **2–4 characters 79.9%**, 5–6 characters 97.5%, 7+
characters 100%.

**The measurement that would replace all of this is
[10](../10-telemetry-and-dashboards.md)'s `search_queries` table** — zero-result
and no-click rates on queries somebody actually typed. It is assigned to M9,
because three of its seven columns need an HTTP surface to fill. Until then
the synthetic set is the best evidence available, which is a statement about
its timing rather than a criticism of it.
