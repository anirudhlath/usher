"""The client event channel (PRD 07's SSE surface, PRD 03's read-through loop), as a port."""

import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ClientEventKind(StrEnum):
    """PRD 07's SSE table, restricted to what this process emits."""

    TITLE_UPDATED = "title.updated"
    WATCHSTATE_UPDATED = "watchstate.updated"
    # Payload is a **row slug**, and deliberately no `title_id`: a row is not a title.
    ROW_INVALIDATED = "row.invalidated"
    SYNC_PROGRESS = "sync.progress"
    # Scoped to **no title**, the same call `sync.progress` makes, and it is what makes
    # PRD 07's "Admin UI only" true rather than advisory: a `?titles=` subscriber never
    # sees one.
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
