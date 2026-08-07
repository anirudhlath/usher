"""One handler per `JobKind`: the thin layer between a `Job` and a service.

`JobWorker` knows nothing about TMDb, about media sources or about language
models, and the six services know nothing about the queue's shape. This
module is the only place the two vocabularies meet, which is what keeps
`usher.services.jobs` a generic claim/run/park loop rather than a switch
statement over the pipeline.

**One column, three kinds of identifier.** `Job.key` is a string so that one
column serves every kind without a polymorphic payload, and what it names
depends entirely on the kind: a `Title.id` for `enrich`, `index` and
`derive`; a source's own `external_id` for `match` and `watch_history`; a
`User.id` for `curate`. The conversion is what makes that legible, and it is
why `_uuid_key` takes the *expected* thing as an argument — the failure a
handler writes into `jobs.last_error` has to say which of the three the key
failed to be.

**A key that does not parse is `PortDataMalformed`, never a `ValueError`.**
`uuid.UUID("not-a-uuid")` raises a `ValueError`, and `JobWorker` deliberately
lets anything that is not a `UsherPortError` propagate — a bug in a handler
is not an upstream failure. So a corrupted `enrich` key would take the worker
process down instead of parking one job. Every key is converted here, once.

**The two source-scoped kinds key on a source's own `external_id`**
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
from usher.domain.source import Source
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import MediaItemRepository
from usher.ports.source import SourceAdapter
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
    would kill the worker rather than park its one job. Every kind's key
    passes through here, so there is one conversion and one raise rather than
    four chances for one of them to raise the wrong type.
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
]
