# 07 — Client API

> ⏳ **Not yet brainstormed.** This file is a placeholder recording what the
> section must cover, so its absence is visible rather than silent.

## Scope of this section

- **HTTP surface** — home, search, browse, title/season/episode detail, person
  detail, collections, playback resolution, watch-state mutation, admin
  (sources, sync status, unmatched review, row regeneration).
- **Response DTOs and versioning** — how wire format decouples from
  [02](02-data-model.md) domain models; how `enrichment_state` is surfaced so
  clients render skeletons deliberately.
- **Pagination and partial responses** — cursor semantics for large result sets.
- **The SSE channel** — event taxonomy (`title.updated`, `watchstate.updated`,
  `row.invalidated`, `sync.progress`), subscription scoping, reconnect and
  replay semantics. Decided in principle in [03](03-sources-and-sync.md);
  the contract is defined here.
- **Image proxy** — URL scheme, resize parameters, cache headers.
- **Playback contract** — what `StreamTarget` exposes and how a client chooses
  between direct play, source-native, and deep-link forms.
- **Attribution endpoints** — required IMDb/TMDb strings, per
  [04](04-catalog-bootstrap.md).
- **The auth seam** — `current_user` dependency shape that returns the default
  user in v1 without foreclosing real authentication.

## Constraints already fixed

- No source-specific concept appears anywhere in the surface.
- Every response is servable from local state; no endpoint blocks on an upstream.
- Clients are pushed changes rather than polling.
