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
about spending effort on. What buys it back is that three services publish
(`EnrichService`, `WatchStateSyncService` and the push lane) and all three
may depend only on `domain/` and `ports/`
([ADR-0009](../../../docs/prd/decisions/0009-repositories-are-ports.md)), so
without a port the bus would be a bare callable injected into three
signatures with no shared contract.
"""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ClientEventKind(StrEnum):
    """PRD 07's SSE table, restricted to what this process emits.

    `bootstrap.progress` is absent because bootstrap runs in the CLI process
    while the bus is in-process, so there is no channel from one to the other.
    PRD 10's argument for keeping its metric catalogue honest applies harder
    here: an empty dashboard panel is a puzzle, and an SSE event type nothing
    emits is a client handler that waits forever.

    **`row.invalidated` landed with M7**, which is the milestone that composes
    a row -- and it landed in the same commit as its publisher, for the reason
    above pointed the other way. This docstring used to say it was absent
    "because nothing composes a row until M7"; that sentence is replaced rather
    than left to read falsely.
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
