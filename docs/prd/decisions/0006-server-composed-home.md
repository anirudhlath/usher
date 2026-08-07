# ADR-0006 — The server composes the home screen

**Status:** Accepted

## Context

Usher will serve several independently built clients. Row composition — which
rows exist, how they're ordered, what's relevant right now — could live on
either side of the boundary.

## Decision

The server composes. `/home` returns ordered, hydrated rows; clients render them
in order. Rows carry a display *hint* (`portrait | landscape | wide | square`)
but never a layout.

The API is REST with OpenAPI. GraphQL is not in v1; the seam is left open.

## Consequences

**Gained:**

- **New row types reach every client with zero client work.** This is the
  entire point of the dynamic `RowProvider` model in
  [06](../06-rows-and-recommendations.md) — a seasonal row or a new similarity
  strategy ships once, server-side.
- **One request paints a screen**, which is what makes the home screen feel
  instant over a slow link.
- **Composition logic exists once**, not reimplemented per client with drifting
  behaviour.

**Given up:**

- Clients cannot reorder rows to local taste without server support.
- A client with genuinely different needs (a watch app, a car head unit) is
  served the same composition as a TV app.

**Mitigation, if that becomes real:** add an optional layout-profile parameter
later. Composition stays server-side; the profile only constrains it. That is
strictly additive, so choosing the simple thing now costs nothing later.

## What was actually built — M7

This ADR governed a milestone before it had a system to describe, and its
Consequences were written entirely in the future tense for three milestones.
`GET /home` shipped in M7; what follows is the same list checked against it.

**"One request paints a screen" now has a number.** Measured 2026-08-04 against
a real 1,271,570-title catalog with a synthetic household: **cold p50 23.9 ms,
p95 35.9 ms**, warm 0.0 ms, eight rows and 115 cards in one response. The claim
was about *perceived* instantaneity over a slow link, which this does not
measure — what it does establish is that the server side of it is not the
constraint. `usher.home.compose.duration` is the standing instrument
([10](../10-telemetry-and-dashboards.md)).

**"New row types reach every client with zero client work" is now a checked
claim.** Nine providers shipped with M7, registered as one tuple in
`services/rows/__init__.py`; the composition point is a registration, and
**five** cross-provider invariants are parametrised over the registry, so a
tenth provider inherits five cases the day it is written. The half that was
*not*
free is `RowContext`: two of the fields [06](../06-rows-and-recommendations.md)
specified had no reader in any of the nine and were deleted rather than kept.

**The tenth arrived and the prediction held (M8, `CuratedProvider`).** The
registry is ten, no client changed, and the five parametrised invariants
covered it on the day it was written — the two that had a *counted* assertion
(`len(BASE_SCORES) == 10` and the registry roll-call by name) failed until the
count was updated, which is the mechanism working rather than a cost. What was
*not* free the second time was every restated count in prose: nine copies of
"nine providers" across `src/`, `tests/`, this ADR and
[10](../10-telemetry-and-dashboards.md)'s `provider`-label vocabulary, each
true when written and none derived from the registry.

**No cursor, and the display hint reached the wire as an enum.**
`portrait | landscape | wide | square` is this ADR's only concrete vocabulary
and it appears in `/openapi.json` as an enum rather than as a string — no
column count, no card width, so the mitigation above stays available. An empty
database answers `200 {"rows": []}` rather than a 404, and deliberately without
a padded generic row, which would look personalised on a household that has
watched nothing.

**Nine of ten providers**, and the tenth is named rather than missing:
`CuratedProvider` and `curated_rows` are M8's whole family
([09](../09-roadmap.md)'s M7 boundary call 2).

**Two consequences this ADR did not anticipate, both recorded elsewhere and
named here so the trail is complete:**

- **The build is sequential, not concurrent.**
  [06](../06-rows-and-recommendations.md) said "concurrently" and that was a
  corruption rather than a preference —
  [ADR-0025](0025-rows-build-sequentially.md).
- **A card carries no artwork**, absent rather than null, which is the same
  call `GET /titles/{id}` made for its `images` key. "The server composes" does
  not mean the server invents a field it has no table for.

## Why not GraphQL

Screen-shaped endpoints already solve the over-fetching problem GraphQL exists
to address, and REST is simpler to cache, debug, and proxy. A Strawberry layer
over the same services can be added if a client needs flexible field selection —
the services, not the routers, hold the logic, so this is not a rewrite.
