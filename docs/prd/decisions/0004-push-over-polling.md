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

## Risk resolved — push verified end to end 2026-07-29

**Confirmed working against the live server.** Authenticated with a normal
(non-admin) user token and a stable `DeviceId`, connected to
`/embywebsocket?api_key=…&deviceId=…`, subscribed with
`{"MessageType":"SessionsStart","Data":"0,1000"}`, and observed:

| Message | Trigger |
|---|---|
| `Sessions` | Periodic, on the subscription interval |
| `UserDataChanged` ×2 | Fired immediately on a REST played/unplayed toggle |

The `UserDataChanged` pair is the decisive result: a state change made out of
band produced push events on the socket within seconds. **That is the
watch-state sync mechanism, working.**

**Two earlier findings were wrong and are corrected here:**

1. A first probe returned 404. That was an artifact of hand-written curl
   headers, not a proxy stripping `Upgrade`. A proper handshake returns
   **HTTP 101**.
2. A second test saw a 101 but no messages, and was read as "the proxy upgrades
   blindly". Also wrong — the socket was simply idle. Nothing was playing and no
   user data was changing, so there was nothing to push. Triggering a real
   change produced messages immediately.

The lesson worth keeping: **silence on an event stream is not evidence of a
broken stream.** Verify push channels by causing an event, never by waiting.

**One genuine quirk:** a control handshake against a nonexistent path also
upgrades and receives `Sessions`, so Emby's listener appears to handle the
upgrade regardless of path. Harmless, but it means path alone is not a health
signal — the adapter's health check must assert on *received messages*, not on
a successful handshake.

Fallback order: Emby per-user webhooks (available since 4.8 without dashboard
access, but requires the server operator to enable "Notifications" under the
account's Feature Access, and Emby Premiere on the server), then polling. The
adapter reports `supports_push` accordingly and the reconciler covers the gap,
so no other layer changes.

## Implemented in M5, and what the second live run settled — 2026-08-02

This ADR's own run recorded *which message types arrived* and not one byte of
any payload. M5's run parsed them, against the same 4.9.5.0 build, driving
the shipped `EmbyPushChannel` over the real `websockets` client. Six results
that change what this document asserted:

- **The envelope is not uniform.** `UserDataChanged` and `LibraryChanged`
  carry `MessageId` — a distinct 32-hex value per *message* — and `Sessions`
  carries none at all.
- **`Sessions` is not periodic on the socket Usher actually uses.**
  `"0,1000"` is `initialDelayMs,intervalMs` and an *unauthenticated* socket
  really does receive one frame a second; the authenticated, row-filtered
  stream arrives when the filtered view changes — median 34.7 s, max 60.1 s
  over 133 intervals in 70 minutes. The table above should be read as
  "periodically, because something on the server keeps changing", not as a
  heartbeat.
- **`LibraryChanged` was observed for the first time** — seven of them —
  and its arrays hold id strings rather than item objects. One carried a
  real `ItemsRemoved` on a library from which nothing was deleted, which is
  [ADR-0015](0015-availability-is-retracted-only-by-a-finished-walk.md)'s
  argument as a measurement.
- **A `UserDataChanged` entry is honest**, matching `GET /Users/{u}/Items/
  {item}` field for field including `PlayCount` and `LastPlayedDate` — which
  the *listing* route is not
  ([ADR-0014](0014-absence-is-not-zero.md)). Usher still reports both as
  absent; the reasoning is in [03](../03-sources-and-sync.md).
- **Emby re-delivers nothing after a disconnect.** Measured over a 61 s
  outage with a real change inside it: the reconnected channel received no
  `UserDataChanged` for it in 90 s of listening, while a second socket that
  stayed up received it at the time. The delta reconcile on reconnect, which
  this ADR lists as an "accepted cost", is the only cover there is.
- **The quirk below is worse than recorded.** A socket with no credential at
  all also upgrades and also receives `Sessions` — see
  [ADR-0018](0018-push-health-is-a-message-ledger.md), which this section is
  the evidence for.
