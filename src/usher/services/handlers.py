"""One handler per `JobKind`: the thin layer between a `Job` and a service.

`JobWorker` knows nothing about TMDb, about media sources or about language
models, and the six services know nothing about the queue's shape. This
module is the only place the two vocabularies meet, which is what keeps
`usher.services.jobs` a generic claim/run/park loop rather than a switch
statement over the pipeline.

**One column, three kinds of identifier.** `Job.key` is a string so that one
column serves every kind without a polymorphic payload, and what it names
depends entirely on the kind: a `Title.id` for `enrich`, `index` and
`derive`; a source's own `external_id` for `match`, `watch_history` and
`watch_writeback`; a `User.id` for `curate`. The conversion is what makes
that legible, and it is why `_uuid_key` takes the *expected* thing as an
argument — the failure a handler writes into `jobs.last_error` has to say
which of the three the key failed to be.

**A key that does not parse is `PortDataMalformed`, never a `ValueError`.**
`uuid.UUID("not-a-uuid")` raises a `ValueError`, and `JobWorker` deliberately
lets anything that is not a `UsherPortError` propagate — a bug in a handler
is not an upstream failure. So a corrupted `enrich` key would take the worker
process down instead of parking one job. Every key is converted here, once.

**The three source-scoped kinds key on a source's own `external_id`**
(`usher.domain.jobs.Job`), which is not a `MediaItem.id` and carries no
source. A handler therefore has to find *which* configured source addresses
that string, and it does so through an injected `SourceResolver` rather than
by holding one source: a household with two servers has two adapters, and
binding a worker to one of them would silently drop the other's jobs. The
resolver is a local lookup against `media_items`, not a network call, so its
cost is one indexed read per job against an upstream measured at 1-5 s per
request.

**A job for work that has since become impossible completes rather than
parks.** An item the source no longer has, or one no configured source
addresses, is not poison — parking it fills the review list with things that
are simply gone, and PRD 08 reserves parking for work a human has to look
at.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from usher.domain.jobs import Job
from usher.domain.source import MediaItem, Source
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import MediaItemRepository, WatchStateRepository
from usher.ports.source import SourceAdapter, WatchStateUpdate
from usher.services.curation import CurationService
from usher.services.derive import DeriveService
from usher.services.enrich import EnrichService
from usher.services.index import IndexService
from usher.services.jobs import Handler
from usher.services.matching import MatchService
from usher.services.watch_sync import WatchStateSyncService


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """A configured source and the adapter that talks to it.

    A pair rather than an adapter alone because every call below needs both:
    the adapter to ask the upstream, and the `Source` to scope a repository
    read to `source_id` and to name the server in a log line.
    """

    source: Source
    adapter: SourceAdapter


# `external_id` -> which source addresses it, or `None`. Built by a
# composition root, which is the only layer that may construct an adapter
# (`usher.adapters.factory`, PRD 01's layering rule 2).
SourceResolver = Callable[[str], Awaitable[SourceBinding | None]]


def enrich_handler(service: EnrichService) -> Handler:
    """`enrich` jobs key on a `Title.id`."""

    async def handle(job: Job) -> None:
        await service.enrich(_title_id(job))

    return handle


def index_handler(service: IndexService) -> Handler:
    """`index` jobs key on a `Title.id`, exactly as `enrich` does.

    Deliberately not a variation on `enrich_handler`: `_title_id` is shared,
    so the `ValueError` -> `PortDataMalformed` conversion happens in one place
    for both kinds.

    **A worker holds this handler only if an embedder was built.**
    `composition.build_worker` registers `JobKind.INDEX` under `embedder is
    not None`, the way it registers `ENRICH` under `provider is not None`, and
    `run_once` claims only the kinds it has handlers for -- so a deployment
    without the embedding extra leaves index jobs for one that can run them
    rather than parking work whose only problem is that it was offered to the
    wrong process.
    """

    async def handle(job: Job) -> None:
        await service.index(_title_id(job))

    return handle


def derive_handler(service: DeriveService) -> Handler:
    """`derive` jobs key on a `Title.id`, exactly as `enrich` and `index` do.

    Deliberately not a variation on either: `_title_id` is shared, so the
    `ValueError` -> `PortDataMalformed` conversion happens in one place for
    all three kinds. Do not write a third converter -- an unparseable key
    raises a `ValueError`, which is not a `UsherPortError`, and `JobWorker`
    lets those propagate, so one corrupted key would take the worker process
    down instead of parking its own job.

    **A worker holds this handler only if a metadata provider was built.**
    `composition.build_worker` registers `JobKind.DERIVE` under `provider is
    not None` -- the `ENRICH` arm rather than the `INDEX` one -- because
    `DeriveService` holds a `MetadataProvider` for `to_derivation`, and a
    deployment with no key has no cached TMDb payloads to derive from at all.
    """

    async def handle(job: Job) -> None:
        await service.derive(_title_id(job))

    return handle


def curate_handler(service: CurationService) -> Handler:
    """`curate` jobs key on a `User.id`, and that is the whole dedup story.

    **The household comes off the key, never off the composition root.**
    `watch_history_handler` below binds a `user_id` at construction because
    M4 has one user and a walk's job key is a source's `external_id` with no
    household in it; curate is the opposite shape. `(kind, key)` is unique,
    so keying on the household is what makes a second request while a
    generation is pending write no second row and buy no second completion --
    PRD 06's *"one modest completion per user per day"*. A handler that
    curated the root's default user instead would dedup identically, park
    identically, and put one household's generation on another's screen.

    **Nothing is caught here.** PRD 06's *"failure is non-fatal to the screen
    and fatal to the job"* is two promises kept in two places:
    `CurationService.generate` keeps the screen's half by never reaching
    `replace_for_user` on a failure, and this function keeps the job's half
    by letting the exception through. `JobWorker` parks `PortDataMalformed`
    and backs everything else off, and it can only do that with an exception
    it is allowed to see; a handler that absorbed one would `complete()` the
    job, delete its row and lose the generation silently.

    **A worker holds this handler only if an `LLMClient` was built.**
    `composition.build_worker` registers `JobKind.CURATE` under
    `client is not None`, the way it registers `INDEX` under
    `embedder is not None`, and `run_once` claims only the kinds it has
    handlers for -- so a deployment with `USHER_LLM_ENABLED=false` leaves
    curate jobs for one that can run them rather than parking work whose only
    problem is the process it was offered to.
    """

    async def handle(job: Job) -> None:
        await service.generate(_user_id(job))

    return handle


def match_handler(
    matcher: MatchService, media_items: MediaItemRepository, resolve: SourceResolver
) -> Handler:
    """`match` jobs key on a source's own `external_id`, and are the only
    caller of the remote-search tier.

    PRD 03: "the TMDb search tier is queued, not inline" — it is one network
    call per unmatched item, and a first full walk against an unbootstrapped
    catalog produces those in the hundreds of thousands, so running them
    inside the walk would make the walk's duration a function of TMDb's rate
    limit rather than of the source's.

    The item is re-read from the *source*, not from `media_items`: the ladder
    needs a name, a year and a provider-id map, and `MediaItem` carries none
    of the three — it is a file, not a description.
    """

    async def handle(job: Job) -> None:
        binding = await resolve(job.key)
        if binding is None:
            logger.debug("match job {key} names no configured source; nothing to do", key=job.key)
            return
        item = await binding.adapter.get_item(job.key)
        if item is None:
            logger.debug(
                "match job {key} names an item {source} no longer has",
                key=job.key,
                source=binding.source.name,
            )
            return
        outcome = await matcher.match_remote(item)
        if outcome.title_id is None:
            # Still unmatched. It stays in the review queue (PRD 02:
            # "unmatched items are never dropped") and the job completes --
            # re-running the same search on a backoff would spend the rate
            # limit re-deriving the same answer.
            return
        stored = await media_items.get_by_external_id(binding.source.id, job.key)
        if stored is None:
            # The walk that enqueued this has not committed its row yet, or
            # the row was deleted. Nothing to attach to; the next walk
            # re-enqueues.
            return
        await media_items.attach_title(
            stored.id, title_id=outcome.title_id, episode_id=outcome.episode_id
        )
        logger.info(
            "remote search matched {key} on {source} to {title_id} ({method})",
            key=job.key,
            source=binding.source.name,
            title_id=outcome.title_id,
            method=outcome.method.value,
        )

    return handle


def watch_history_handler(
    service: WatchStateSyncService, resolve: SourceResolver, *, user_id: uuid.UUID
) -> Handler:
    """`watch_history` jobs key on a source's own `external_id`.

    The expensive half of ADR-0014: a walk cannot report `play_count` or
    `last_played_at` on the one server measured, so it enqueues one of these
    per played item whose count it could not determine, at background
    priority, and this asks the single-item route.

    `user_id` is bound at construction because M4 has one user (PRD 01's
    authentication seam). Mapping a source's own user ids onto Usher's is
    M5's, and a job key carrying one would settle that question here.
    """

    async def handle(job: Job) -> None:
        binding = await resolve(job.key)
        if binding is None:
            logger.debug(
                "watch-history job {key} names no configured source; nothing to do", key=job.key
            )
            return
        await service.backfill_one(
            binding.source, binding.adapter, external_id=job.key, user_id=user_id
        )

    return handle


def watch_writeback_handler(
    watch_states: WatchStateRepository,
    media_items: MediaItemRepository,
    resolve: SourceResolver,
    *,
    user_id: uuid.UUID,
) -> Handler:
    """`watch_writeback` jobs key on a source's own `external_id`, carry no
    payload, and push whatever the household's row holds **now**.

    PRD 03's outbound half, as a queued job. `WatchWriteService` writes
    locally, commits, publishes and enqueues one of these per source *copy*;
    this is the only place in `src/` where a client's watch write reaches a
    server.

    **The absent payload is the design, not an economy.** `(kind, key)` is
    unique, so five `PUT`s during one minute of playback coalesce into one
    row -- and because the state is re-read here rather than replayed, the
    write that lands is the newest and a retry after a backoff is idempotent
    by construction. A job carrying the state it was enqueued with would have
    neither property: the queue would hold five stale positions and a backoff
    would eventually push an old one over a newer one.

    **`user_id` is bound at construction**, exactly as `watch_history_handler`
    binds it and for the same reason -- M4 has one user (PRD 01's
    authentication seam), and the key is a source's `external_id` with no
    household in it. Mapping a source's own user ids onto Usher's would settle
    that question here.

    **Two ways for the work to have become impossible, and both complete
    rather than park.** No configured source addresses the key (the household
    removed that server, or the copy was never ours), and the source no longer
    has the item. Parking either fills the review list with things that are
    simply gone, and a parked job needs a human to release it. The second
    costs one `get_item` per write-back, which is the honest price of the
    branch: `EmbySession.ok` raises `PortUnavailable` for **every** status at
    or above 400, so a push at an item Emby has deleted is retried five times
    and then parked -- which is exactly what `WatchWriteService` promises does
    not happen when it enqueues a retracted copy on purpose.

    **A household with no row for this target sends nothing**, rather than
    sending zeroes. `WatchStateUpdate` has no "leave it alone" spelling, so a
    push assembled from an absent row would report position 0 and
    `Played: false` -- and on Emby that body is applied verbatim, so it would
    erase the source's own state on behalf of a household that never wrote
    one.

    **Nothing is caught here**, for the reason `curate_handler`'s docstring
    gives: `JobWorker` parks `PortDataMalformed` and backs everything else
    off, and it can only do that with an exception it is allowed to see. A
    handler that absorbed one would `complete()` the job, delete its row and
    lose the write silently -- which is the failure PRD 03's "best effort"
    is most often misread as licensing.

    🔴 **Marking played diverges by one field on the round trip, and the
    divergence is in live Emby rather than in this code.** `POST
    /PlayedItems` clears the resume position as it marks the item played
    (measured against 4.9.5.0), while the local write keeps
    `position_seconds`. So after a successful write-back the source holds `0`
    and Usher holds N, and the next walk can merge the zero back. Named here
    so a live run *observes* it rather than discovering it, and so a later
    reader does not read the difference as a bug in the merge. Nothing here
    chases it: the local rule is M3's own finding and the source's rule is the
    source's.
    """

    async def handle(job: Job) -> None:
        binding = await resolve(job.key)
        if binding is None:
            logger.debug(
                "write-back job {key} names no configured source; nothing to do", key=job.key
            )
            return
        copy = await media_items.get_by_external_id(binding.source.id, job.key)
        if copy is None:
            logger.debug(
                "write-back job {key} names no copy on {source}",
                key=job.key,
                source=binding.source.name,
            )
            return
        state = await _local_watch_state(watch_states, user_id, copy)
        if state is None:
            logger.debug(
                "write-back job {key} has no local watch state to send",
                key=job.key,
                source=binding.source.name,
            )
            return
        if await binding.adapter.get_item(job.key) is None:
            logger.debug(
                "write-back job {key} names an item {source} no longer has",
                key=job.key,
                source=binding.source.name,
            )
            return
        await binding.adapter.push_watch_state(
            job.key,
            WatchStateUpdate(position_seconds=state.position_seconds, played=state.played),
        )
        logger.info(
            "wrote watch state back to {source} for {key}",
            key=job.key,
            source=binding.source.name,
        )

    return handle


async def _local_watch_state(
    watch_states: WatchStateRepository, user_id: uuid.UUID, copy: MediaItem
) -> WatchState | None:
    """The household's row for whatever this copy is matched to.

    An episode's `media_items` row holds its series' `title_id` **and** its
    `episode_id`, and `watch_states` permits exactly one
    (`num_nonnulls(title_id, episode_id) = 1`), so the pair collapses here
    with the episode winning -- the same rule `watch_sync._watch_target`
    applies to the inbound direction, for the same reason. Reading the title's
    row for an episode's copy would push one series' progress onto every one
    of its 999,927 episode files.

    An unmatched copy is matched to nothing and there is no row to read, which
    is a real state rather than a defensive one: `MediaItem.title_id` is
    deliberately nullable and the review queue is where those sit.
    """
    if copy.episode_id is not None:
        return await watch_states.get_for_episode(user_id, copy.episode_id)
    if copy.title_id is not None:
        return await watch_states.get_for_title(user_id, copy.title_id)
    return None


def _title_id(job: Job) -> uuid.UUID:
    """`job.key` as a `Title.id`, or `PortDataMalformed`."""
    return _uuid_key(job, "a title id")


def _user_id(job: Job) -> uuid.UUID:
    """`job.key` as a `User.id`, or `PortDataMalformed`.

    A second *name*, not a second converter: `_uuid_key` below is the one
    place a `ValueError` becomes a `UsherPortError`, and what differs is the
    sentence an operator reads out of `jobs.last_error`. "job key is not a
    title id" is a wrong statement about a household, and a wrong sentence in
    that column is what sends somebody to look at the wrong table.
    """
    return _uuid_key(job, "a user id")


def _uuid_key(job: Job, expected: str) -> uuid.UUID:
    """`job.key` as a UUID, or `PortDataMalformed`.

    A `ValueError` from `uuid.UUID` is not a `UsherPortError`, and
    `JobWorker` lets those propagate deliberately — so an unparseable key
    would kill the worker rather than park its one job. Every **UUID-keyed**
    kind's key passes through here -- `enrich`, `index` and `derive` via
    `_title_id`, `curate` via `_user_id` -- so there is one conversion and one
    raise rather than four chances for one of them to raise the wrong type.
    `match` and `watch_history` never reach it: their key is a source's own
    `external_id`, an opaque string that is handed to the adapter as it
    stands, which is the module docstring's three-category split arriving at
    the converter that only serves one of the three.
    """
    try:
        return uuid.UUID(job.key)
    except ValueError as exc:
        raise PortDataMalformed(
            f"{job.kind.value} job key is not {expected}", detail=job.key
        ) from exc


__all__ = [
    "SourceBinding",
    "SourceResolver",
    "curate_handler",
    "derive_handler",
    "enrich_handler",
    "index_handler",
    "match_handler",
    "watch_history_handler",
    "watch_writeback_handler",
]
