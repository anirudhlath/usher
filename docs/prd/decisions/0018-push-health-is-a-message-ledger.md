# ADR-0018 — Push health is a message ledger, never an open socket

**Status:** Accepted. Implemented in M5.

## Context

[ADR-0004](0004-push-over-polling.md) verified Emby push end to end and
recorded one quirk: **a handshake against a nonexistent path also upgrades
and also receives `Sessions`.** So a successful upgrade is not evidence of
anything about the path, the subscription, or a proxy in between.

The same state arrives from three other directions. A reverse proxy that
forwards `Upgrade` and then buffers. A NAT table entry dropped while both
endpoints still believe the connection is open. A server that accepted the
subscription and lost the listener. In every one the connection object is
healthy and nothing is arriving.

`SourceAdapter.supports_push` is what [PRD 03](../03-sources-and-sync.md)'s
reconciler reads to decide whether a source needs covering. An adapter that
answered it from the socket would tell the reconciler to stand down for a
source that has silently stopped pushing.

## Decision

**No answer about push health is derived from the state of a socket.**

`PushHealth` counts received messages. `supports_push` is
`connected AND messages_received > 0 AND now - last_message_at <=
stale_after`, and there is no code path from "a connection object exists" to
`True`. A channel that has delivered nothing for `stale_after` raises
`PortUnavailable` out of its own iterator, so the lane stops holding it.
`PushSupervisor` resets its consecutive-failure counter on *delivery*, never
on connection, so an upgraded-but-silent socket walks that counter to its
ceiling and ends with `supports_push = false` persisted.

`verify()` opens no socket at all: it reports `null` ("not probed") for an
adapter with no channel and the running lane's ledger otherwise.
`SourceAdapter.probe_push` is a **concrete** method on the port whose body
is calls to `events()` and `supports_push`, so every adapter inherits the
rule rather than re-deriving it.

## Consequences

**Gained:** the failure ADR-0004 warns about is detected, acted on, and
visible — `usher.source.push.connected` reports delivery, so
[PRD 10](../10-telemetry-and-dashboards.md)'s "Push down" alert fires on it
instead of being permanently green.

**Given up:** an idle library's channel stays healthy only because Emby
sends periodic `Sessions`. If a future build stopped, `stale_after` would
fire on a healthy socket and the lane would reconnect every 90 s — visible
in `usher.source.push.reconnects`, cheap (the gap-closing delta returns 0
items), and tunable, but wrong. **The `Sessions` interval was measured in
M5's live run and it is not the property it was assumed to be** — see
Evidence.

**Also:** this is layered with `websockets`' own `ping_interval`/
`ping_timeout` rather than replacing them. Those detect a dead TCP peer.
This detects a live one that has stopped delivering, and neither substitutes
for the other. Measured on a real socket: aborting the transport under a
live channel raised `PortUnavailable` out of the iterator rather than
hanging or ending quietly, and the supervisor reconnected.

**Rejected:** probing on every `GET /admin/sources/{id}/status`. A dashboard
polling that route would open a socket per poll against a server
[PRD 01](../01-architecture.md) measures at 1–5 s per request, and would
still be answering a question about a socket that is not the one doing the
work.

## Evidence

ADR-0004's own control handshake, 2026-07-29: a connection to a nonexistent
path upgraded and received `Sessions`. **Re-measured against the same build
on 2026-08-02 and it still holds** — `/embywebsocket-nope` returned 101 with
`Upgrade: websocket` and delivered `Sessions`.

**And the same run found something stronger, which is now the primary
evidence for this decision.** A socket carrying **no credential of any
kind** — no `api_key`, no header — also upgrades, also accepts
`SessionsStart`, and then delivers `Sessions` *more often* than the
authenticated one: ~1 frame per second against a median of one per ~38 s,
carrying the **whole server's** session list rather than the row-filtered
view the authenticated socket receives. So on this build:

- an upgrade is not evidence the path is right (ADR-0004);
- **an upgrade is not evidence the socket is authenticated at all**;
- **messages arriving is not evidence either**, and a *higher* message rate
  is if anything a signal of the wrong thing.

A health check written as "the socket is open" passes all three. A health
check written as "messages are arriving" passes the last two. Only the
messages Usher actually *derives events from* — `UserDataChanged` — would
distinguish them, and those are exactly what an idle library does not send,
which is why `Sessions` is counted at all. The ledger is therefore a
necessary condition and not a sufficient one, and that is stated here rather
than implied: it rules out the silent-socket failure this ADR is named for,
and it does not rule out a socket that is live and authenticated as nobody.

Measured through the shipped `EmbyAdapter` on the same run: `supports_push`
read `False` with the connection open and `messages_received == 0`, and
`True` after the first frame — the pre-message assertion, against the real
server rather than a fake.

`CLAUDE.md`'s "Verified facts" section records the full run, including the
measured `Sessions` interval and what it means for
`push_stale_after_seconds`.
