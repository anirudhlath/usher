# ADR-0011 — `tmdb_id` is unique per kind, not globally

**Status:** Accepted 2026-07-30. Implemented in M2
([plan](../../plans/2026-07-30-m2-bootstrap.md), Task 2).

## Context

[ADR-0003](0003-own-uuid-identity.md) makes `tmdb_id` a nullable,
unique-indexed *attribute* rather than an identity, and M1 implemented that
as `ix_titles_tmdb_id`: `UNIQUE (tmdb_id) WHERE tmdb_id IS NOT NULL`.

That index encodes an assumption nobody stated: that a TMDb id identifies one
TMDb entity. It does not. TMDb keys movies and TV series in **two independent
integer spaces**, both of which land in this single column. TMDb movie 1 and
TMDb TV series 1 are different works.

The assumption survived M1 because nothing wrote a `tmdb_id` in bulk. M2's
Phase 2 crosswalk does, from Wikidata's P4947 (TMDb movie ID) and P4983 (TMDb
TV series ID), and the collision is not marginal.

## Decision

The unique index becomes composite: `UNIQUE (tmdb_id, kind) WHERE tmdb_id IS
NOT NULL`, named `ix_titles_tmdb_id_kind`.

`TitleRepository.get_by_tmdb_id` gains a required `kind: TitleKind`
parameter. It is not a filter; it is the other half of the key.

`imdb_id` and `tvdb_id` keep their single-column indexes — see Consequences.

## Consequences

**Gained:**

- Phase 2 can link television. Under the old index, roughly 47% of TV titles
  Wikidata could resolve would have been silently skipped.
- `get_by_tmdb_id` stops being able to raise a raw storage exception. With
  the widened index and the old signature, `scalar_one_or_none()` raises
  `sqlalchemy.exc.MultipleResultsFound` out of the port — precisely what
  [ADR-0009](0009-repositories-are-ports.md)'s `db is driven, not driving`
  contract exists to prevent. Reproduced directly before the signature
  change.
- `tmdb_ids` (M2's TMDb id universe table) is keyed the same way, so the two
  agree by construction rather than by convention.

**Given up:**

- Every `get_by_tmdb_id` call site must supply a kind. In practice none is
  burdened: M4's matcher reads `ProviderIds.Tmdb` off a `SourceItem` that
  already carries `SourceItemKind`.
- `Title.tmdb_id` read on its own is no longer meaningful without `kind`
  beside it. An API response exposing one must expose both, which
  [02](../02-data-model.md) now states.

**Deliberately not changed:**

- **`imdb_id`** stays single-column unique. IMDb's `tt` ids are one global
  namespace spanning film, television, and episodes; there is no second
  space to collide with.
- **`tvdb_id`** stays single-column unique. TheTVDB does have separate series
  and movie id spaces, but Usher only ever writes series ids to it in M2
  (Wikidata P4835 is *TheTVDB.com series ID*). The hazard is real in
  principle and unmeasured in practice; widening it should follow evidence,
  the way this change did, not symmetry. Recorded here so the asymmetry is a
  decision rather than an oversight.

## Evidence

Measured 2026-07-30 against `query.wikidata.org`, joining P345 (IMDb ID) with
P4947 and P4983 respectively:

| | |
|---|---|
| IMDb↔TMDb **movie** pairs | 277,678 (277,042 distinct TMDb ids) |
| IMDb↔TMDb **series** pairs | 57,343 (56,975 distinct TMDb ids) |
| Integers live in **both** namespaces | **26,968** |
| Share of distinct TV ids a single-column unique index blocks | **47.3%** |

The colliding ids are not exotic: the smallest are 2, 3, 5, 6, 11, 13, 14,
15, 16, 17.

Verified directly against `pgvector/pgvector:pg17`:

- With `UNIQUE (tmdb_id, kind) WHERE tmdb_id IS NOT NULL`, a movie and a
  series both holding `tmdb_id = 1` insert cleanly; a second *movie* at
  `tmdb_id = 1` still raises, with `constraint_name =
  ix_titles_tmdb_id_kind`.
- `EXPLAIN` on `WHERE tmdb_id = 1 AND kind = 'movie'` reports `Index Scan
  using ix_titles_tmdb_id_kind`, so the widened index still serves the
  lookup it replaced.
- With the widened index and the *old* one-argument signature,
  `get_by_tmdb_id(550)` against a movie and a series both at 550 raises
  `sqlalchemy.exc.MultipleResultsFound`.

A related data-quality finding from the same measurement, which M2's
crosswalk loader has to handle separately: **569 TMDb movie ids are claimed
by more than one IMDb id**, and 243 IMDb ids claim more than one TMDb movie
id. Only one of each pair can win a unique index, so the loader deduplicates
deterministically and *counts* the losers rather than raising.
