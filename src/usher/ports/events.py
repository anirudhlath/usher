"""The client event channel (PRD 07's SSE surface, PRD 03's read-through
loop), as a port.

**One method, and subscription is not on it.** A Postgres `LISTEN/NOTIFY`
implementation -- the named second one, for a deployment that splits the
worker from the server -- publishes with an `INSERT` plus a `NOTIFY` and
subscribes on a dedicated connection whose lifecycle has nothing in common
with an in-memory queue's. Putting both on one ABC would draw the shape
around the first implementation and make the second satisfy it. M5's
in-memory bus therefore implements this *and* offers `subscribe`, which is
its own concern rather than the port's.

**`publish` is `async` and the shipped implementation never suspends in
it.** That is not a contradiction, it is the contract: the port is async
because a transport can be, and the guarantee every implementation owes is
that a subscriber which has stopped reading cannot slow, block, or fail the
service that published. `EnrichService` finishing a title must not depend on
a browser tab.

**A port with one implementation, deliberately** -- the shape
[ADR-0001](../../../docs/prd/decisions/0001-abc-over-protocol.md) warns
about spending effort on. What buys it back is that four services publish
(`EnrichService`, `PushApplyService`, `ReconcileService` and, since M9's E7,
`BootstrapService`) and all four may depend only on `domain/` and `ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), so
without a port the bus would be a bare callable injected into four
signatures with no shared contract.

*This list read `EnrichService`, `WatchStateSyncService` and the push lane
until 2026-08-11, and `WatchStateSyncService` holds no `EventPublisher` at
all -- `grep` finds none in `services/watch_sync.py`, its own docstring at
:332 says the nightly walk "invalidates no rows and publishes no
`row.invalidated`", and PRD 07 says the same. `services/events.py`'s module
docstring and
[ADR-0019](../../../docs/prd/decisions/0019-the-client-event-channel-is-a-port.md)
both already named the right three, so this was one file disagreeing with
two.*

**Every one of the three publishes only after its own subject has
committed, and that is now a recorded decision rather than three separate
acts of care** --
[ADR-0033](../../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md),
which measured all five publish sites from a second connection at the
instant of the publish. What the rule buys is **ordering, not durability**:
this bus is in-process and lossy by design, and an outbox table is the
answer to a different question.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ClientEventKind(StrEnum):
    """PRD 07's SSE table, restricted to what this process emits.

    The standing rule, which binds both ways: PRD 10's argument for keeping
    its metric catalogue honest applies harder here, because an empty
    dashboard panel is a puzzle while an SSE event type nothing emits is a
    client handler that waits forever -- and a *publisher* with no member is
    a `KeyError` inside a response that has already answered 200. So a member
    lands in the same commit as its publisher, never before it.

    **`row.invalidated` landed with M7**, which is the milestone that composes
    a row. This docstring used to say it was absent "because nothing composes
    a row until M7"; that sentence was replaced rather than left to read
    falsely.

    **`bootstrap.progress` landed with M9's E7, and its absence sentence gets
    the same treatment.** It read *"absent because bootstrap runs in the CLI
    process while the bus is in-process, so there is no channel from one to
    the other"* -- true until E5, which put `JobKind.BOOTSTRAP` on the queue.
    A bootstrap started through `POST /admin/bootstrap/{phase}` runs on the
    worker lane, which in the shipped default is the API process holding this
    bus, so the channel now exists. What has *not* changed is the
    split-deployment answer: with `usher work` in its own container the frames
    reach a `NullEventPublisher` and no client is told, exactly as
    `title.updated` has degraded since M5, and that is the reason the
    `LISTEN/NOTIFY` implementation named above still has no owner.
    """

    TITLE_UPDATED = "title.updated"
    WATCHSTATE_UPDATED = "watchstate.updated"
    # Payload is a **row slug**, and deliberately no `title_id`: a row is not a
    # title. This is therefore the one event the `?titles=` filter cannot
    # express -- it reaches unfiltered subscribers and no others, which is
    # correct rather than a limitation. A client that sent `?titles=` is on a
    # detail screen, and PRD 07's own reason for the filter is "so a detail
    # screen isn't woken by unrelated churn". A `title_id` here would be a
    # filter key that half-works: it would wake exactly the detail screens
    # subscribed to whichever title happened to be attached, which is neither
    # "every subscriber" nor "the right ones".
    ROW_INVALIDATED = "row.invalidated"
    SYNC_PROGRESS = "sync.progress"
    # Scoped to **no title**, the same call `sync.progress` makes, and it is
    # what makes PRD 07's "Admin UI only" true rather than advisory: a
    # `?titles=` subscriber never sees one. A bulk import touches most of the
    # catalog, so a frame carrying a title id would wake every detail screen
    # in the household, per batch, for the length of a 1.27M-row load.
    #
    # **No `percent`, and PRD 07's payload column is corrected rather than
    # satisfied.** Nothing on `BulkCursor` can supply a denominator: it
    # carries `revision`, `position` and `rows_seen`, and `position` is
    # documented as "a dataset-defined integer offset whose only contract is
    # that resuming from it never misses a record" -- a byte offset for IMDb,
    # a page number for the Wikidata crosswalk, whose SPARQL result set has
    # no total at all. Widening `BulkCursor`/`BulkBatch` with a total is a
    # port change across all four M2 datasets that one of them could not
    # satisfy anyway.
    BOOTSTRAP_PROGRESS = "bootstrap.progress"
    # Not a domain event: the channel telling a client its own stream has a
    # hole in it. PRD 07: "On buffer overflow the server emits
    # `resync_required` rather than silently skipping events -- a client
    # that missed changes is told to refetch instead of being left quietly
    # stale."
    RESYNC_REQUIRED = "resync_required"


@dataclass(frozen=True, slots=True)
class ClientEvent:
    """One thing worth telling a client.

    `title_id` is the **filter key**, and an episode event carries its
    series' title alongside its own episode id for exactly that reason: a
    client watching a series subscribes with the series' title id, because
    that is the only id it has before it fetches a season. A filter keyed on
    `episode_id` would wake nobody.

    `data` is the SSE payload and is deliberately untyped at this layer --
    `api/dto/events.py` owns the wire shape, and the port owning it too
    would make every payload change a port change. Everything in it must be
    JSON-serialisable, and nothing in it may be a credential: PRD 08's rule
    reaches a response body exactly as it reaches a log line.
    """

    kind: ClientEventKind
    title_id: uuid.UUID | None = None
    episode_id: uuid.UUID | None = None
    data: Mapping[str, Any] = field(default_factory=dict)


class EventPublisher(ABC):
    @abstractmethod
    async def publish(self, event: ClientEvent) -> None:
        """Offer an event to whoever is listening.

        **Never raises, and never blocks on a subscriber.** Both halves are
        contract rather than courtesy: this is called from
        `EnrichService.enrich`, from the push lane, and from a reconcile's
        per-batch flush, and none of those may fail or stall because a
        client stopped reading. An implementation that cannot deliver drops
        or diverts, and tells *that subscriber* (`RESYNC_REQUIRED`) rather
        than telling the publisher.

        Delivery is best-effort and unordered *across* subscribers. Within
        one subscriber's stream, order is preserved and a gap is announced.
        That is the whole guarantee, and it is drawn to be satisfiable by a
        lossy transport rather than by an in-process queue.
        """


class NullEventPublisher(EventPublisher):
    """Publishes nowhere.

    A real deployment rather than a test double: `usher work` as a
    standalone process has no SSE clients to tell, and M5's bus is
    in-process. Without this, every service would need a
    `publisher is not None` branch, which is three places to forget it.
    """

    async def publish(self, event: ClientEvent) -> None:
        return None
