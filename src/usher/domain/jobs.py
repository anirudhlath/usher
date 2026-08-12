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

    `index` maintains PRD 03's fourth stage, and the asymmetry inside that
    stage is why it is a job at all. The full-text document is a `GENERATED
    ALWAYS AS (...) STORED` column on `titles`, so PostgreSQL recomputes it
    inside the statement that writes `name` or `overview` and **no job is
    involved** -- a skeleton title needs none to be fully searchable. The
    embedding needs a model, which the database cannot run, so it is queued;
    and because it is queued it can fail, park, or never be enqueued at all.

    That is why `title_embeddings` records `model_name` and a
    `source_fingerprint` of the exact text embedded: staleness becomes a SQL
    predicate rather than something inferred from the queue, and the backfill
    (`usher index --backfill`) is self-draining and re-runnable at zero write
    cost. **This kind's correctness does not depend on the queue being
    reliable**, which is the property M6 was built around.

    Its population is the enriched tier -- `enrichment_state <> 'skeleton'`,
    for which `ix_titles_enrichment_state` is already exactly the partial
    index -- not the whole 1.27M-row catalog. Embedding a skeleton produces a
    vector of its name, which full-text already does better and cheaper.

    **`index` was deliberately absent until M6**, and it stopped being so
    because the handler, the enqueue and the drain land in one milestone. A
    kind whose handler is a stub is a queue that grows forever.

    `derive` turns one cached provider payload into that title's people,
    credits and collection (ADR-0016). **Its unit of work is one title and
    that is what makes it a kind at all** -- everything it reads is one row of
    `raw_payloads`, found by `(provider, kind, reference)`, and no other
    title's data is touched. `SimilarityService`'s rebuild is the
    counter-example and is deliberately *not* a kind: a neighbour list is a
    function of every other embedded vector, so a job keyed on one `Title.id`
    would misdescribe what the work reads and 10,000 of them would each scan
    the whole population.

    Like `index` it is enqueued after enrichment's commit and at `BACKFILL`,
    and like `index` its correctness does not depend on the queue: `usher
    derive --backfill` walks the cache directly and re-derives idempotently,
    because the credit write is a scoped replace rather than an append. The
    two are deliberately **not ordered against each other** -- a title whose
    `index` job is claimed first embeds without its cast, `derive` then moves
    its fingerprint, and the backfill re-claims it. One wasted embed per
    enriched title, which is the fingerprint scheme working rather than a leak
    in it, and the only lever would be a `JobPriority` rung that does not
    exist between `BACKFILL` and `NEW`.

    `curate` buys one LLM completion and replaces one household's
    `curated_rows` (PRD 06). **Its key is a `user_id`**, which is what makes
    `(kind, key)` do the milestone's central cost work rather than merely
    tidying the queue. Nothing keyed per *request* -- a `generation_id`, a
    timestamp -- deduplicates at all, and a key naming no household would put
    two households on one screen.

    **What the queue actually does with a repeat, measured against real
    Postgres rather than reasoned from the statement** (2026-08-07, one
    session, `PostgresJobQueue`). Enqueueing `(curate, A)` writes **1** row;
    enqueueing it again at the same priority writes **0** and leaves one row;
    twice inside one batch writes **0** more (`SELECT DISTINCT ON (kind,
    key)`); at a *higher* priority it writes **1** as a promotion of the same
    row, not a second one; and `(curate, B)` writes **1**, so two households
    really are two jobs. Two halves of that are worth knowing before building
    on it, and the first is a number Task 17 would otherwise read backwards:

    - **A request arriving while the generation is `running` is coalesced
      into it at the same or a lower priority, promotes it at a higher one,
      and is discarded either way.** `status = 'running'` appears nowhere in
      `_ENQUEUE`'s `WHERE`, so what a repeat costs turns entirely on
      `jobs.priority < excluded.priority` -- which makes this two
      measurements, not one:

      | running row | repeat asks | rows written | row afterwards |
      |---|---|---|---|
      | `BACKFILL` | `BACKFILL` | 0 | `('running', 20)` |
      | `DEMAND` | `DEMAND` | 0 | `('running', 100)` |
      | `NEW` | `BACKFILL` | 0 | `('running', 50)` |
      | `BACKFILL` | `NEW` | **1** | `('running', 50)` |
      | `BACKFILL` | `DEMAND` | **1** | `('running', 100)` |

      `complete()` then deletes that one row in **every** line of the table,
      so the requested generation never runs and the queue is empty
      afterwards. For "one completion per household per day" that is the
      wanted answer.

      **What it is not is a signal, and `written == 0` is the wrong thing to
      read as one.** A promoting repeat reports success -- `enqueue` cannot
      distinguish creating a job from promoting one, both return 1 -- so a
      caller at `JobPriority.DEMAND` (`POST /admin/rows/regenerate`, and
      `api/routers/titles.py`'s existing promotion) is told 1 row was written
      and gets nothing back for it. A caller that wants a *fresh* generation
      after the one in flight has to arrange that above the queue; there is
      no return value here that tells it what happened. The one thing that
      does save the repeat is a *failure*: `_FAIL` returns the promoted row
      to `pending`, so the retry serves it. Measured 2026-08-07 against
      `pgvector/pgvector:pg17` through `PostgresJobQueue`, and pinned by the
      three `test_a_..._repeat_...` cases in
      `tests/integration/test_job_queue.py`, which are where to change this
      table rather than here.
    - **A parked `curate` job is not un-parked or promoted by asking again**,
      even at `DEMAND`. Measured: 0 rows written and the row still
      `('parked', 20)`. That is `_ENQUEUE`'s `WHERE jobs.status <> 'parked'`,
      and it is the right answer -- an empty catalog does not stop being
      empty because something asked twice -- but it means an operator has to
      release the row, exactly as for every other kind.

    **It is the first kind whose registration is conditional**, and that is
    not the stub M4 forbade. `composition.build_worker` registers it only
    when `composition.llm_client` built one, exactly as it registers `index`
    only when an embedder exists, and `run_once` claims `list(self._handlers)`
    -- so a deployment with `USHER_LLM_ENABLED=false` leaves curate work
    pending for a process that can run it rather than parking work whose only
    problem is the process it was offered to. The member itself is
    unconditional because it is domain vocabulary two things outside the
    worker need: the enqueue site (`POST /admin/rows/regenerate`) and
    `depth()`, which promises a key per kind so PRD 10's `usher.jobs.queued`
    never stops reporting a series.

    `watch_writeback` carries PRD 03's outbound write back to the source a
    client's own watch write is about. **Its key is the source's own
    `external_id`** -- the third kind to spell a key that way, alongside
    `match` and `watch_history` -- and it carries **no payload at all**, which
    is the whole design rather than an economy. The handler re-reads the
    household's current local row at run time and pushes that, so five `PUT`s
    during one minute of playback coalesce into **one** row (`(kind, key)` is
    unique) and the write that lands is the newest, and a retry is idempotent
    because it replays nothing. A job carrying the state it was enqueued with
    would have neither property: the queue would hold five stale positions and
    a backoff would eventually push an old one over a newer one.

    One job per source *copy*, never per file: a title write reads
    `media_items` with `episode_id IS NULL`, because an episode's row carries
    its series' `title_id` too and 999,927 of the one measured library's
    1,126,789 items are episodes -- so the unbounded read would put 20,000
    jobs on the queue for one press of a 20,000-episode series.

    **Its registration is unconditional**, which puts it with `match` and
    `watch_history` rather than with `curate` two paragraphs up: nothing about
    a write-back is optional. `composition.build_worker` withholds `enrich`,
    `derive`, `index` and `curate` from a deployment that lacks the
    collaborator each needs -- a TMDb key, an embedding model, an LLM
    endpoint -- and this one needs only the session's own repositories, so
    every build claims it and no household's own write is left to a process
    that never arrives.

    `sync` is `POST /admin/sources/{id}/sync`, as an enqueue -- M4 deferred
    the route to M9 with the capability already delivered through
    `usher.cli`, and M8 ratified the shape a triggered walk has to take:
    `usher sync`'s body minus the printing, run by a worker rather than
    inline, because a reconcile checkpoints and commits per batch and a route
    that drove a six-hour walk inside one request would be committing the
    request's session repeatedly before the handler returned. **Its key is
    `"{source_id}:{lane}"`, not a bare source id, and the composite is the
    whole design.** `(kind, key)` is unique, so a bare source id would
    coalesce a requested *full* walk into a pending *delta* one and answer
    202 for a walk that never happens -- the same trap a bare `user_id`
    would be for `curate` if two households shared one, one lane over.
    `lane` is one of `SyncRunKind.FULL`/`SyncRunKind.DELTA`'s wire values;
    `SyncRunKind.WATCH_STATE` is never a valid lane here, because the watch
    lane is not a thing an operator triggers on its own -- it is the second
    half of every triggered sync, run by the handler immediately after the
    item lane, exactly as `usher sync` already runs it. Registered
    unconditionally, the way `match`, `watch_history` and `watch_writeback`
    are: there is no optional process resource behind it, only the adapter
    factory every composition root already builds.

    **Adding a member here needs no migration**, verified rather than
    assumed: `db/models/jobs.py` declares `kind` through `enum_column`, whose
    `native_enum=False` compiles to a plain `VARCHAR(32)` and whose
    `create_constraint` defaults to `False` in SQLAlchemy 2.0, so the database
    holds no membership CHECK and no native enum type. Pydantic owns
    membership.
    """

    MATCH = "match"
    ENRICH = "enrich"
    WATCH_HISTORY = "watch_history"
    INDEX = "index"
    DERIVE = "derive"
    CURATE = "curate"
    WATCH_WRITEBACK = "watch_writeback"
    SYNC = "sync"


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

    `key` is the kind's own identifier for the work, and it is **one column,
    four kinds of identifier**: a `Title.id` for `enrich`, `index` and
    `derive`; a source's own `external_id` for `match`, `watch_history` and
    `watch_writeback`; a `User.id` for `curate`; and a composite
    `"{source_id}:{lane}"` for `sync`, the one kind whose key names two
    things rather than one -- see `JobKind.SYNC`. All four render as a
    string, so one column serves every kind without a polymorphic payload.
    `(kind, key)` is
    unique; enqueueing
    the same work twice promotes rather than duplicates.
    `usher.services.handlers` is where a key is converted back, and
    `_uuid_key` takes the expected thing as an argument precisely because
    several answers to "what is this key" means several different sentences
    in `jobs.last_error`. **`curate` is the one that names neither a title nor
    a source item**, which is why `(kind, key)` does this milestone's cost
    work rather than merely tidying the queue -- see `JobKind.CURATE`.

    **The three source-scoped kinds key on the source's id for the item, not
    on `MediaItem.id`, and that is a deliberate trade with a known cost.**
    Two of the three enqueue sites are inside a walk, which holds the source's
    own id and would need a round trip per item to turn it into a
    `MediaItem.id` -- 1,126,674 of them a walk, which is the shape of defect
    this whole pipeline is built to avoid. `watch_writeback`'s is not: it is
    one press, and it has already read the rows in order to find the copies at
    all, so it pays the same spelling for consistency rather than for cost.
    The cost is that `(kind, key)` is unique
    across *sources*: two servers that address different items by the same
    string collapse into one job, and the second item's work is skipped
    until something re-enqueues it. Emby and Jellyfin both mint per-server
    GUIDs, so this is currently unreachable rather than merely unlikely; the
    fix when it stops being is a composite key here plus a parse in the
    handlers, not a per-item lookup at enqueue time.

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
