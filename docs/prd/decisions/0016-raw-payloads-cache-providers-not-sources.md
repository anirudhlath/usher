# ADR-0016 — `raw_payloads` caches providers, not sources

**Status:** Accepted — corrects PRD 02 and PRD 03
**Date:** 2026-07-31

## Context

Two things in the PRD pointed `raw_payloads` in a direction M4's schema had
to settle before creating the table.

1. **PRD 03's ingest stage said to store every *source* item's raw payload
   there.** The one real source measured holds 1,126,674 items. An Emby item
   payload is roughly 8 kB, so that is ~9 GB — against a database
   [08](../08-operations.md) budgets at 8–12 GB in total. What it buys is
   avoiding a refetch that costs one request against a server Usher already
   walks nightly.
2. **PRD 02 listed a separate `provider_cache_meta` table** holding "fetch
   timestamps — enforces TMDb's ≤6-month cache term". Keyed the same way as
   `raw_payloads` and answering a question a column on that table answers
   once.

## Decision

`raw_payloads` holds **provider** responses only, keyed
`(provider, kind, reference)`, and carries its own `fetched_at`. No
`provider_cache_meta` table is created.

## Consequences

- Provider payloads are worth caching in a way source payloads are not, and
  the difference is not size: a provider fetch costs a rate-limited network
  call, TMDb's terms cap re-fetching at 6 months, and three later milestones
  (`Person`/`Credit`/`Collection` in M7, `Image` in M9) re-derive entities
  from a payload M4 already holds — with no second network call, which is
  what PRD 02 says the cache is for.
- `fetched_at` is the compliance answer directly: "the oldest cache entry
  against the 6-month ceiling" is `min(fetched_at)`, which
  `ix_raw_payloads_fetched_at` serves. A second table would have to be
  written, read, and kept consistent to answer the same question.
- The `server_default` on `fetched_at` covers the INSERT arm only. An upsert
  that refreshes a payload must set it explicitly — a stale timestamp on
  fresh data is exactly the answer the column exists to prevent.
- Re-reading a source item is a single request against a server Usher is
  already connected to, so nothing is lost by not caching it. If a future
  milestone genuinely needs a source payload after the fact, it fetches it.

## Evidence

- 1,126,674 items on the measured source (94,438 movies, 32,409 series,
  999,827 episodes) — CLAUDE.md, verified against the live Emby 4.9.5.0.
- The 8–12 GB database budget — [08](../08-operations.md).
- TMDb's ≤6-month caching term — [04](../04-catalog-bootstrap.md).
