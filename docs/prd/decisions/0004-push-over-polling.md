# ADR-0004 — Push events primary, reconciliation as backstop

**Status:** Accepted

## Context

The initial sync design was tiered polling: watch state every 2 minutes, recent
additions every 15, a full walk nightly. Polling a slow upstream on a fixed
interval is both wasteful and laggy.

## Decision

Source adapters expose an optional push channel. For Emby that is the WebSocket
at `/embywebsocket`. Push drives the fast path; reconciliation becomes a
correctness backstop rather than the mechanism.

Usher also pushes to its own clients over SSE, so nothing polls in either
direction.

## Consequences

**Gained:** near-real-time watch state and library updates; far less load on a
slow upstream; the read-through priority queue in
[03](../03-sources-and-sync.md) becomes viable because updates can be *pushed*
to clients when enrichment lands.

**Accepted cost:** a stateful long-lived connection to maintain — heartbeats,
reconnect with backoff, and delta reconcile on reconnect.

**Not given up:** reconciliation still runs. Sockets drop and events are missed;
a nightly full walk remains the source of truth for availability.

## Evidence

Verified by decompiling Emby Server 4.9.5.0 — the version this deployment runs —
rather than relying on documentation:

- `SessionWebSocketListener` accepts `api_key` and `deviceId` query parameters
  and resolves the token against the same repository for user tokens and admin
  API keys alike. **No role check exists in the subscription path**, so a normal
  user token suffices.
- `LibraryChanged` is dispatched per-user with payload filtering; it is not a
  broadcast and not admin-gated.
- `UserDataChanged` is scoped to the user's own data.

**Important correction to common guidance:** Jellyfin added websocket permission
enforcement after forking from Emby 3.5.2, so Jellyfin-derived advice claims
admin gating that Emby does not have.

**Operational facts:** Emby sends no keepalive of its own (a standing feature
request, declined). nginx closes idle connections at 60 s and Cloudflare at
~100 s, so a 20–25 s client heartbeat is required. Reverse proxies that fail to
forward `Upgrade` return 404 instead of 101 — the documented failure signature.

## Open risk

A probe of this deployment returned **404** on `/embywebsocket`. Inconclusive —
the probe was not a complete handshake and the token had expired — but it must
be retested with a live token before the push path is assumed available.

Fallback order: Emby per-user webhooks (available since 4.8 without dashboard
access, but requires the server operator to enable "Notifications" under the
account's Feature Access, and Emby Premiere on the server), then polling. The
adapter reports `supports_push` accordingly and the reconciler covers the gap,
so no other layer changes.
