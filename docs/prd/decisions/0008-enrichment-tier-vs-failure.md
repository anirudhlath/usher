# ADR-0008 — Enrichment tier is orthogonal to enrichment failure

**Status:** Accepted

## Context

`EnrichmentState` originally had four members: `skeleton`, `stub`,
`enriched`, `failed`. `failed` was modeled as a fourth rung on the same
ladder as the other three — setting a `Title`'s `enrichment_state` to
`failed` was the only way to record that the last enrichment attempt didn't
succeed.

Two problems fell out of that shape:

1. **Failure destroyed tier information.** Moving a `skeleton` or `stub`
   Title to `failed` overwrote the fact that it was still a perfectly
   usable skeleton or stub — genres, ratings, and runtime it already had
   didn't stop being true because the *next* enrichment attempt failed. A
   retry had no record of which tier to restore on success.
2. **`StrEnum` ordering is lexicographic, not ladder position.** PRD
   [03](../03-sources-and-sync.md) describes demand promoting an
   unenriched title's enrichment job to top priority, and [09](../09-roadmap.md)
   places that ("demand promotion") in M5, with the ingest pipeline's
   own stub-on-sight/enrich steps in M4 — so a "don't downgrade a
   title's tier" guard is expected well before this ladder sees much
   use. Written the natural way, as
   `if new_state > title.enrichment_state`, it type-checks, runs, raises
   nothing — and silently inverts the comparison it exists to make.

## Decision

`EnrichmentState` is a three-value ladder: `skeleton | stub | enriched`.
`failed` is removed.

Failure is tracked separately, on `Title.enrichment_error: str | None`. A
non-null value means the most recent enrichment attempt failed; the tier
(`enrichment_state`) is left exactly as it was before the attempt.

Comparing tiers uses an explicit mapping,
`usher.domain.enums.ENRICHMENT_RANK: dict[EnrichmentState, int]`, never the
enum members' own ordering.

## Consequences

**Gained:**

- Setting `enrichment_error` no longer destroys tier information — a
  failed enrichment attempt on a skeleton Title stays a skeleton Title,
  still fully usable, with `enrichment_error` set alongside it.
- A retry knows what tier it's working from, because the tier was never
  overwritten to record the failure.
- [02-data-model.md](../02-data-model.md)'s existing promise that the
  `failed` state "carries `enrichment_error`" is now actually true — no
  such field existed before this ADR.
- `ENRICHMENT_RANK` gives tier comparisons exactly one correct spelling,
  closing the silent-inversion failure mode described above.

**Given up:** a single field can no longer answer "did the last attempt
fail" — that's now two fields (`enrichment_state`, `enrichment_error`)
instead of one. Accepted: the two questions ("how complete is this title"
and "did we just fail to improve it") are independent, and conflating them
was the bug.

**Also worth recording:** `skeleton` and `stub` differ by *provenance* as
much as by completeness. `skeleton` comes from a bulk dataset and often
already carries genres, ratings, and runtime; `stub` is only whatever a
source's own API returned on first sight. Neither is a strict subset of
the other's fields, even though `stub` outranks `skeleton` in
`ENRICHMENT_RANK`.

## Evidence

Verified directly against Python 3.13's `enum.StrEnum`, with the original
four-member enum:

```pycon
>>> EnrichmentState.ENRICHED > EnrichmentState.SKELETON
False
>>> sorted(EnrichmentState)
[<EnrichmentState.ENRICHED: 'enriched'>, <EnrichmentState.FAILED: 'failed'>,
 <EnrichmentState.SKELETON: 'skeleton'>, <EnrichmentState.STUB: 'stub'>]
```

`"enriched" < "skeleton"` lexicographically (`e` < `s`), so the tier a
title should be *most* likely to compare as "greatest" compares as the
least — the exact inversion a naive `>` comparison would hit.
