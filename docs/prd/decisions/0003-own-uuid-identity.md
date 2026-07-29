# ADR-0003 — Usher-owned UUIDs, provider IDs as indexed attributes

**Status:** Accepted

## Context

The obvious shortcut for a media catalog is to key titles by `tmdb_id`. It is
already unique, already the join key against the enrichment provider, and
removes a mapping layer.

## Decision

Every entity has a Usher-owned **UUIDv7** primary key. `tmdb_id`, `imdb_id`, and
`tvdb_id` are nullable, unique-indexed attributes — fast lookup paths, never
identity.

## Consequences

**Gained:**

- **Titles can exist without any provider ID.** TMDb lists ~1.23M movies; IMDb
  lists 12.7M titles. Anything obscure, regional, or personal that a source
  holds but no provider knows still gets a first-class catalog entry rather than
  being unrepresentable.
- **Merging is repointing, not a key rewrite.** The same film ingested twice
  under different provider IDs is resolved by repointing references, without a
  primary-key cascade through watch state, credits, rows, and embeddings.
- **Upstream churn is absorbed.** Provider IDs get merged, split, and
  re-pointed. A `tmdb_id` is a claim *about* a title, not the title itself.
- **Multiple providers coexist.** Nothing structurally privileges TMDb, so
  adding OMDb or TVDb later is additive.

**Given up:** a join layer, and the discipline of never leaking a provider ID
into an API contract as an identifier.

UUIDv7 over v4 for time-ordering — index locality stays good during bulk
imports that insert millions of rows.

## Evidence

Wikidata — the most complete free cross-reference available — maps only ~278k
titles with *both* IMDb and TMDb IDs, against 1.23M TMDb movies and 12.7M IMDb
titles. Provider ID spaces do not align, and no single one covers the catalog.
