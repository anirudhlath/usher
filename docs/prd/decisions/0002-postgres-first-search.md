# ADR-0002 — Postgres-first search, Meilisearch behind a measurable gate

**Status:** Accepted — reverses an earlier recommendation

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

- pgvector ≥ 0.8.5 — CVE-2026-3172 and HNSW vacuum corruption fixes.
- `hnsw.iterative_scan = relaxed_order` is **off by default** and must be set,
  or filtered vector search suffers severe recall collapse.
- GIN `fastupdate = off`; the default pending-list buffering causes p99 spikes.
- Fuse with RRF, never by adding scores from incompatible scales.

## Uncertainty

No rigorous public benchmark of `pg_trgm` typo tolerance against
Meilisearch/Typesense on a labelled dataset appears to exist. That hole sits
directly under this decision — which is exactly why the upgrade gate is an
empirical test against our own catalog rather than a judgement call.
