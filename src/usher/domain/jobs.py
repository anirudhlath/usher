"""The priority work queue's domain vocabulary (PRD 03, PRD 08).

One row per outstanding unit of work, deduplicated on `(kind, key)`. A
completed job's row is **deleted** rather than marked -- hence no `DONE`
member on `JobStatus` -- because the queue's only two interesting
populations are "waiting" and "poisoned", and keeping a terminal row per
title would make PRD 10's `usher.jobs.queued` gauge a count over a table
that grows without bound.
"""

import uuid
from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pydantic import AwareDatetime, Field

from usher.domain.base import DomainModel
from usher.domain.ids import new_id


class JobKind(StrEnum):
    """What a worker does with a claimed job.

    `index` is deliberately absent. PRD 03's fourth stage updates a search
    document and computes an embedding; neither artefact exists before M6,
    which owns both. A kind whose handler is a stub is a queue that grows
    forever, and adding the member later is one line plus a backfill
    enqueue.
    """

    MATCH = "match"
    ENRICH = "enrich"
    WATCH_HISTORY = "watch_history"


class JobStatus(StrEnum):
    """A job is waiting, held by a worker, or poisoned.

    Not a ladder -- unlike `EnrichmentState` (ADR-0008) there is no "is this
    an improvement" comparison to get wrong -- so no rank mapping exists and
    none is needed.
    """

    PENDING = "pending"
    RUNNING = "running"
    PARKED = "parked"


class JobPriority(IntEnum):
    """PRD 03's read-through table, as named constants.

    **Higher is more urgent**, so every claim query orders
    `priority DESC, created_at ASC`. An `IntEnum` rather than a `StrEnum`
    because the column is an integer a `GREATEST()` runs over during
    promotion, and because the ordering has to be arithmetic rather than
    lexicographic -- which is the same trap `ENRICHMENT_RANK` exists for,
    avoided here by the type rather than by a side table.

    `DEMAND` and `VISIBLE` are unused in M4: nothing here serves a client.
    They are defined now because the promotion clause in the enqueue
    statement (`SET priority = GREATEST(...)`) is written in M4 and would
    otherwise be written against a scale that does not yet have a top.
    """

    DEMAND = 100  # a client opened this title right now (M5)
    VISIBLE = 80  # in a row the client just requested (M5)
    NEW = 50  # newly seen on a source
    BACKFILL = 20  # background sweep


class Job(DomainModel):
    """One outstanding unit of work.

    `key` is the kind's own identifier for the work -- a `Title.id` for
    `enrich`, a `MediaItem.id` for `match`, a `MediaItem.id` for
    `watch_history` -- as a string, so one column serves every kind without
    a polymorphic payload. `(kind, key)` is unique; enqueueing the same work
    twice promotes rather than duplicates.

    `priority` is typed `int`, not `JobPriority`: promotion is
    `GREATEST(priority, excluded.priority)` in SQL, so any value on the
    0-100 scale can legitimately come back off a row, and a member-typed
    field would reject one. `JobPriority` names the four rungs the
    application actually writes; the bounds are what the column enforces.

    `traceparent` is a W3C trace context captured at enqueue time. The
    worker starts its span with a `Link` to it rather than as a child, since
    the request that enqueued the work has usually already returned -- a
    child span of a finished parent is a lie about causality, a link is
    not. PRD 10's "why did the title I just opened take 45 seconds" is that
    link followed backwards.
    """

    id: uuid.UUID = Field(default_factory=new_id)
    kind: JobKind
    key: str = Field(min_length=1)
    priority: int = Field(default=JobPriority.NEW, ge=0, le=100)
    status: JobStatus = JobStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    run_after: AwareDatetime | None = None
    traceparent: str | None = None
    last_error: str | None = None

    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
