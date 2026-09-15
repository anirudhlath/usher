"""One handler per `JobKind`: the thin layer between a `Job` and a service."""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from loguru import logger

from usher.domain.bootstrap import BootstrapPhase
from usher.domain.jobs import Job
from usher.domain.source import MediaItem, Source
from usher.domain.sync import SyncRunKind
from usher.domain.watch import WatchState
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import MediaItemRepository, SourceRepository, WatchStateRepository
from usher.ports.source import SourceAdapter, WatchStateUpdate
from usher.services.curation import CurationService
from usher.services.derive import DeriveService
from usher.services.enrich import EnrichService
from usher.services.index import IndexService
from usher.services.jobs import Handler
from usher.services.matching import MatchService
from usher.services.reconcile import ReconcileService
from usher.services.watch_sync import WatchStateSyncService

#: `SyncRunKind` has a third member, `WATCH_STATE`, which is never a lane an
#: operator triggers on its own -- it is the second half of every triggered
#: sync, run by `sync_handler` itself immediately after the item lane. A key
#: naming it is exactly as malformed as one naming no lane at all.
_TRIGGERABLE_SYNC_LANES = frozenset({SyncRunKind.FULL, SyncRunKind.DELTA})

#: One bulk-import phase, run to completion. The alias exists so this module
#: can name the collaborator without importing the composition root, exactly as
#: `AdapterOpener` does for `sync_handler`.
BootstrapRunner = Callable[[BootstrapPhase], Awaitable[None]]

#: The adapter factory a `sync` job's handler is closed over.
AdapterOpener = Callable[[Source], Awaitable[SourceAdapter | None]]


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
    """`enrich` jobs key on a `Title.id`.

    The rung travels with the key, because `EnrichService` enqueues an
    `INDEX` and a `DERIVE` of its own and `DERIVE` is what writes `images`.
    A handler that passed only the key would leave every follow-up at the
    sweep's priority however urgently its own job was claimed -- so a title a
    client opened would get its text at `DEMAND` and its artwork whenever the
    background queue drained, which on this catalog was never. The clamp that
    keeps a bulk `NEW` ingest off the demand rungs lives in `_apply`, not
    here: this is the wire, not the policy.
    """

    async def handle(job: Job) -> None:
        await service.enrich(_title_id(job), priority=job.priority)

    return handle


def index_handler(service: IndexService) -> Handler:
    """`index` jobs key on a `Title.id`, exactly as `enrich` does.

    Deliberately not a variation on `enrich_handler`: `_title_id` is shared,
    so the `ValueError` -> `PortDataMalformed` conversion happens in one place
    for both kinds.

    A worker holds this handler only if an embedder was built.
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

    A worker holds this handler only if a metadata provider was built.
    `composition.build_worker` registers `JobKind.DERIVE` under `provider is
    not None` -- the `ENRICH` arm rather than the `INDEX` one -- because
    `DeriveService` holds a `MetadataProvider` for `to_derivation`, and a
    deployment with no key has no cached TMDb payloads to derive from at all.
    """

    async def handle(job: Job) -> None:
        await service.derive(_title_id(job))

    return handle


def curate_handler(service: CurationService) -> Handler:
    """`curate` jobs key on a `User.id`, and that is the whole dedup story."""

    async def handle(job: Job) -> None:
        await service.generate(_user_id(job))

    return handle


def match_handler(
    matcher: MatchService, media_items: MediaItemRepository, resolve: SourceResolver
) -> Handler:
    """`match` jobs key on a source's own `external_id`.

    The only caller of the remote-search tier. PRD 03: "the TMDb search tier is
    queued, not inline" -- it is one network call per unmatched item, and a
    first full walk against an unbootstrapped catalog produces those in the
    hundreds of thousands, so running them inside the walk would make the walk's
    duration a function of TMDb's rate limit rather than of the source's.

    The item is re-read from the *source*, not from `media_items`: the ladder
    needs a name, a year and a provider-id map, and `MediaItem` carries none of
    the three -- it is a file, not a description.
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

    The expensive half of the watch-state sync: a walk cannot report
    `play_count` or `last_played_at` on every server, so it enqueues one of
    these per played item whose count it could not determine, at background
    priority, and this asks the single-item route.

    `user_id` is bound at construction because there is one user (PRD 01's
    authentication seam). Mapping a source's own user ids onto Usher's is a
    later question, and a job key carrying one would settle it here.
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


def sync_handler(
    sources: SourceRepository,
    reconcile: ReconcileService,
    watch: WatchStateSyncService,
    open_adapter: AdapterOpener,
    *,
    user_id: uuid.UUID,
) -> Handler:
    """`sync` jobs key on `"{source_id}:{lane}"`.

    `POST /admin/sources/{id}/sync` lands here as an enqueue rather than as a
    synchronous walk.
    """

    async def handle(job: Job) -> None:
        source_id, lane = _sync_key(job)
        source = await sources.get(source_id)
        if source is None:
            logger.debug(
                "sync job {key} names a source that no longer exists; nothing to do",
                key=job.key,
            )
            return
        if not source.enabled:
            logger.debug(
                "sync job {key} names a source disabled since it was enqueued; nothing to do",
                key=job.key,
            )
            return
        adapter = await open_adapter(source)
        if adapter is None:
            logger.debug(
                "sync job {key} found no adapter for {source}; nothing to do",
                key=job.key,
                source=source.name,
            )
            return
        try:
            await reconcile.reconcile(source, lane, adapter)
            await watch.sync(source, adapter, user_id=user_id)
        finally:
            await adapter.aclose()

    return handle


def bootstrap_handler(run: BootstrapRunner) -> Handler:
    """`bootstrap` jobs key on a `BootstrapPhase`.

    The thinnest handler in the module, because everything it would otherwise
    hold is a composition-root concern.
    """

    async def handle(job: Job) -> None:
        await run(_bootstrap_phase(job))

    return handle


def watch_writeback_handler(
    watch_states: WatchStateRepository,
    media_items: MediaItemRepository,
    resolve: SourceResolver,
    *,
    user_id: uuid.UUID,
) -> Handler:
    """`watch_writeback` jobs key on a source's own `external_id`.

    They carry no payload and push whatever the household's row holds now.
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


def _bootstrap_phase(job: Job) -> BootstrapPhase:
    """`job.key` as a `BootstrapPhase`, or `PortDataMalformed`.

    A `ValueError` from a `StrEnum` lookup is not a `UsherPortError`, so an
    unparseable key would take the worker down rather than park its one job --
    `_uuid_key`'s argument, arriving at the one key that is not a UUID and not
    an opaque adapter string. It is reachable only from a row somebody wrote
    by hand or from a member deleted between the enqueue and the claim, since
    `POST /admin/bootstrap/{phase}` types the path parameter as this very enum
    and `usher bootstrap --phase` derives its `choices` from it.

    The message names the vocabulary rather than only the offending value: an
    operator reading `jobs.last_error` on a parked row can act on
    *"not one of imdb, credit-names, ..."* and cannot act on *"not a phase"*.
    """
    try:
        return BootstrapPhase(job.key)
    except ValueError as exc:
        offered = ", ".join(phase.value for phase in BootstrapPhase)
        raise PortDataMalformed(
            f"bootstrap job key is not one of {offered}", detail=job.key
        ) from exc


def _sync_key(job: Job) -> tuple[uuid.UUID, SyncRunKind]:
    """`job.key` as `(source id, lane)`, or `PortDataMalformed`.

    `(kind, key)` is unique, so a bare source id would coalesce a requested
    *full* walk into a pending *delta* one and answer 202 for a walk that
    never happens -- the composite is deliberate, not incidental, and is
    documented on `JobKind.SYNC` itself.

    `str.partition` rather than `str.split(":")`, so a source id that turned
    out to embed a colon (none does today; nothing enforces it) would still
    produce exactly two parts rather than three. `SyncRunKind.WATCH_STATE`
    parses as a `SyncRunKind` and is refused anyway: the watch lane is never
    a thing a client asks for on its own, only the second half of every
    triggered sync.
    """
    source_id_part, separator, lane_part = job.key.partition(":")
    try:
        if not separator:
            raise ValueError("missing lane")
        source_id = uuid.UUID(source_id_part)
    except ValueError as exc:
        raise PortDataMalformed(
            'sync job key is not "source id:lane" -- the source id half did not parse',
            detail=job.key,
        ) from exc
    try:
        lane = SyncRunKind(lane_part)
        if lane not in _TRIGGERABLE_SYNC_LANES:
            raise ValueError(f"{lane_part!r} is not a triggerable lane")
    except ValueError as exc:
        raise PortDataMalformed(
            'sync job key is not "source id:lane" -- the lane half is not full or delta',
            detail=job.key,
        ) from exc
    return source_id, lane


async def _local_watch_state(
    watch_states: WatchStateRepository, user_id: uuid.UUID, copy: MediaItem
) -> WatchState | None:
    """The household's row for whatever this copy is matched to.

    An episode's `media_items` row holds its series' `title_id` *and* its
    `episode_id`, and `watch_states` permits exactly one
    (`num_nonnulls(title_id, episode_id) = 1`), so the pair collapses here with
    the episode winning -- the same rule `watch_sync._watch_target` applies to
    the inbound direction. Reading the title's row for an episode's copy would
    push one series' progress onto every one of its episode files.

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

    A `ValueError` from `uuid.UUID` is not a `UsherPortError`, and `JobWorker`
    lets those propagate deliberately -- so an unparseable key would kill the
    worker rather than park its one job. Every UUID-keyed kind's key passes
    through here -- `enrich`, `index` and `derive` via `_title_id`, `curate` via
    `_user_id` -- so there is one conversion and one raise rather than four
    chances for one of them to raise the wrong type. `match` and
    `watch_history` never reach it: their key is a source's own `external_id`,
    an opaque string handed to the adapter as it stands.
    """
    try:
        return uuid.UUID(job.key)
    except ValueError as exc:
        raise PortDataMalformed(
            f"{job.kind.value} job key is not {expected}", detail=job.key
        ) from exc


__all__ = [
    "AdapterOpener",
    "BootstrapRunner",
    "SourceBinding",
    "SourceResolver",
    "bootstrap_handler",
    "curate_handler",
    "derive_handler",
    "enrich_handler",
    "index_handler",
    "match_handler",
    "sync_handler",
    "watch_history_handler",
    "watch_writeback_handler",
]
