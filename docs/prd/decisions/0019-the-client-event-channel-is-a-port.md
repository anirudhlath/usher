# ADR-0019 — The client event channel is a port, with one implementation

**Status:** Accepted. Implemented in M5.

## Context

[PRD 03](../03-sources-and-sync.md) ends its read-through loop with "the
client is told when it changes", and [PRD 07](../07-client-api.md) specifies
`GET /events` with a `?titles=` filter, `Last-Event-ID` replay and
`resync_required` on overflow. Three services publish (`EnrichService`,
`PushApplyService`, `ReconcileService`) and all three may depend only on
`domain/` and `ports/`
([ADR-0009](0009-repositories-are-ports.md)).

M5's bus is in-memory, which means it is in-process — and `usher work` is a
separate process, so an enrichment completing there reaches no SSE client.

## Decision

`EventPublisher` is a port with one method (`publish`) and one
implementation in M5 (`InMemoryEventBus`), plus a `NullEventPublisher` for
composition roots with no clients.

**Subscription is not on the port.** A Postgres `LISTEN/NOTIFY`
implementation publishes with an `INSERT` plus a `NOTIFY` and subscribes on
a dedicated connection whose lifecycle has nothing in common with an
in-memory queue's; one ABC carrying both would draw the shape around the
first implementation.

**The server process runs the job worker**, gated by `worker_enabled`, so
PRD 03's loop closes in the shipped deployment.

## Consequences

**Gained:** a service publishes through a contract rather than through three
independently-injected callables, and the contract is drawn to be
satisfiable by a lossy transport — every case is about one subscriber's
stream, and `resync_required` answers every gap.

**Given up:** a port with one implementation, which
[ADR-0001](0001-abc-over-protocol.md)'s own reasoning says to be careful
about spending effort on. Bought back by three callers, a named second
implementation, and a `NullEventPublisher` that is a real deployment rather
than a test double.

**Accepted:** `usher work` as a standalone process publishes nowhere. A
client that refetches still gets the enriched title — degradation rather
than breakage, [PRD 08](../08-operations.md)'s own principle — and the fix
is the second implementation rather than a branch in three services.

**Also:** `bootstrap.progress`, which PRD 07 lists, is not emitted for
exactly this reason, and PRD 07 now says so rather than leaving a client
waiting on a handler that never fires.

## Evidence

The blocking spelling of the fan-out (`await queue.put(...)` in place of
`put_nowait`) does not answer wrongly, it deadlocks — so the property this
port exists to guarantee is asserted by driving the coroutine one step by
hand (`coro.send(None)` must raise `StopIteration`), and every burst in the
suite goes through `tests/contract/event_publisher_contract.publish_all` so
the mutation fails rather than hanging. `CLAUDE.md` records the measurement.

Fan-out cost is measured rather than assumed: 25 subscribers against 200
over the same burst is **6.0x/6.3x/6.2x** as shipped, where linear predicts
8x and quadratic ~64x, against **25.6x** for a `publish` given an artificial
per-subscriber scan.
