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

## Why not GraphQL

Screen-shaped endpoints already solve the over-fetching problem GraphQL exists
to address, and REST is simpler to cache, debug, and proxy. A Strawberry layer
over the same services can be added if a client needs flexible field selection —
the services, not the routers, hold the logic, so this is not a rewrite.
