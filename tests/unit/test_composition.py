"""`usher.composition`'s process-level wiring.

Most of this module is exercised through its callers -- the lane supervisor
in `tests/unit/test_api_lanes.py`, the CLI in `tests/unit/test_cli.py`, and
both against real Postgres in `tests/integration/`. What lives here is the
one decision `metadata_provider` makes *for the process*: whether this
deployment has a metadata provider at all.

That decision is a per-process fact and its log line has to be too. It was
`build_worker`'s, which is called once per worker *pass*, so a default
deployment with no TMDb key produced a `WARNING` every `IDLE_SLEEP_SECONDS`
-- ~17,280 a day. The lane's half of that is pinned in
`test_a_missing_tmdb_key_is_not_re_reported_on_every_pass`; this file pins
that the information is still surfaced rather than merely quieted.
"""

import ast
import dataclasses
import inspect
import io
import os
import pathlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, cast

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import Depends
from loguru import logger
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import usher
from tests.fakes.bulk_catalog_repository import FakeBulkCatalogRepository
from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.curated_row_repository import FakeCuratedRowRepository
from tests.fakes.embedding import FakeEmbedder
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.image_repository import FakeImageRepository
from tests.fakes.import_run_repository import FakeImportRunRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.llm_call_repository import FakeLLMCallRepository
from tests.fakes.llm_client import FakeLLMClient, usage
from tests.fakes.media_item_repository import FakeMediaItemRepository
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.row_provider_settings_repository import FakeRowProviderSettingsRepository
from tests.fakes.source_adapter import FakeSourceAdapter
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from tests.fakes.watch_state_repository import FakeWatchStateRepository
from usher.adapters.emby.adapter import EmbyAdapter
from usher.api.app import create_app
from usher.api.deps import get_source_adapter_factory, get_source_gates
from usher.composition import (
    Pipeline,
    SourceGateRegistry,
    SourceRegistry,
    UnitOfWork,
    _worker_handlers,
    build_curation_service,
    build_enrich_service,
    build_pipeline,
    build_search_service,
    build_worker,
    embedder,
    llm_client,
    metadata_provider,
    run_bootstrap,
    unit_of_work,
    worker_concurrency,
    worker_kinds,
)
from usher.config import Settings
from usher.db.base import build_session_factory
from usher.db.repositories.search_query import PostgresSearchQueryRepository
from usher.domain.bootstrap import BootstrapPhase, ImportRun
from usher.domain.curation import LLMPurpose
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind, JobPriority, JobStatus
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.credentials import SourceCredentials
from usher.ports.embedding import Embedder
from usher.ports.errors import PortUnavailable
from usher.ports.events import ClientEvent, EventPublisher, NullEventPublisher
from usher.ports.ingest import MediaItemUpsert, WatchStateWrite
from usher.ports.jobs import JobQueue, JobRequest
from usher.ports.repository import (
    CrosswalkLinkResult,
    CuratedRowRepository,
    LLMCallRepository,
    MediaItemRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.ports.source import (
    SourceAdapter,
    SourceAdapterFactory,
    SourceItem,
    SourceItemKind,
)
from usher.services.curation_pool import CandidatePoolService
from usher.services.curation_validate import ITEM_IDS_KEY, REASON_KEY, ROWS_KEY, TITLE_KEY
from usher.services.events import DeferredEventPublisher
from usher.services.handlers import SourceBinding
from usher.services.jobs import JobWorker
from usher.services.rows import ROW_PROVIDERS
from usher.services.taste import TasteService

#: The size of the pool `_pipeline_over_fakes` puts on the pipeline, and it is
#: deliberately neither 200 nor the number of candidates any case seeds.
#: `build_curation_service` has to take `pipeline.pool` rather than construct a
#: second `CandidatePoolService` over the same repositories -- a second one
#: would be built at `settings.curation_pool_size`, which is 200, and would
#: answer *identically* on every fixture seeding fewer than 200 candidates. The
#: pool's size is the one thing that tells the two apart, and
#: `_schema(len(candidates))` puts it on the wire where a case can read it.
POOL_SIZE = 6


def _pipeline_over_fakes(
    *,
    titles: TitleRepository,
    queue: JobQueue,
    curated: CuratedRowRepository | None = None,
    ledger: LLMCallRepository | None = None,
    watch_states: WatchStateRepository | None = None,
    media_items: MediaItemRepository | None = None,
    commit: Callable[[], Awaitable[None]] | None = None,
    events: EventPublisher | None = None,
) -> Pipeline:
    """A `Pipeline` carrying the fields `build_worker`'s factories read.

    `cast` rather than a fake per field: every remaining slot is genuinely
    unused on this path, and filling twelve of them would make the case read
    as a test of `build_pipeline` rather than of one wiring decision.

    **Nothing on the curation path is `unused`, unlike the four fields above
    it.** `build_worker` constructs `CurationService` eagerly whenever a
    client exists, and a `None` there constructs perfectly well and fails an
    `AttributeError` deep inside the first generation -- which is exactly the
    shape a `curated=None` on `RowContext` took when it survived 2,743 cases
    one task ago. The four optional arguments exist so a case can hold the
    same objects the pipeline does and read back what the service wrote into
    *them*.
    """
    settled = _Recording() if commit is None else commit

    unused = cast(Any, None)
    # `build_derive_service` reads four more slots than
    # `build_enrich_service` does, so they are real fakes rather than
    # `unused`: `build_worker` constructs the service eagerly, and a `None`
    # there fails at construction rather than at the one wiring decision
    # under test.
    people = FakePersonRepository()
    titles_store = titles if isinstance(titles, FakeTitleRepository) else FakeTitleRepository()
    history = FakeWatchStateRepository() if watch_states is None else watch_states
    embeddings = FakeTitleEmbeddingRepository()
    taste = TasteService(
        watch_states=history,
        embeddings=embeddings,
        titles=titles,
        taste=FakeTasteRepository(history),
        # The shipped default. Curation has to run without one, and
        # `CandidatePoolService` then returns the base order whole.
        embedder=None,
        now=lambda: datetime(2026, 8, 7, tzinfo=UTC),
    )
    return Pipeline(
        sources=unused,
        credentials=unused,
        titles=titles,
        matching=unused,
        media_items=unused if media_items is None else media_items,
        episodes=FakeEpisodeRepository(),
        watch_states=history,
        payloads=FakeRawPayloadStore(),
        bulk=FakeBulkCatalogRepository(),
        import_runs=FakeImportRunRepository(),
        runs=unused,
        queue=queue,
        embeddings=embeddings,
        neighbors=unused,
        taste_rows=FakeTasteRepository(),
        curated_rows=FakeCuratedRowRepository() if curated is None else curated,
        llm_calls=FakeLLMCallRepository() if ledger is None else ledger,
        people=people,
        credits=FakeCreditRepository(people, titles_store),
        collections=FakeCollectionRepository(),
        images=FakeImageRepository(),
        adapters=unused,
        matcher=unused,
        ingest=unused,
        reconcile=unused,
        watch=unused,
        search=unused,
        similar=unused,
        taste=taste,
        pool=CandidatePoolService(
            titles=titles, embeddings=embeddings, taste=taste, size=POOL_SIZE
        ),
        row_providers=ROW_PROVIDERS,
        row_provider_settings=FakeRowProviderSettingsRepository(),
        events=NullEventPublisher() if events is None else events,
        commit=settled,
    )


#: `json_schema`'s own nesting key, spelled once rather than four times in one
#: subscript chain.
_PROPERTIES = "properties"

#: One shelf of five handles -- `DEFAULT_MIN_CARDS` exactly, so a validator
#: floor moving up is a failure here rather than a silently shorter screen --
#: every one of them inside `POOL_SIZE`. What this response is *not* is
#: interesting: nothing here exercises `validate_curation`, which has its own
#: file and 60 cases; these cases need a generation that survives so the write
#: has somewhere to land.
_ROWS = {
    ROWS_KEY: [
        {
            TITLE_KEY: "Slow-burn science fiction",
            REASON_KEY: "Quiet, long, and mostly about one person and a machine.",
            ITEM_IDS_KEY: [1, 2, 3, 4, 5],
        }
    ]
}


async def _candidates(titles: FakeTitleRepository, *, count: int) -> list[Title]:
    """`count` unwatched, enriched films, seeded **worst first**.

    The pool ranks on `vote_count` descending, so an ascending seed makes pool
    order the reverse of the order `new_id()` minted these in -- the UUIDv7
    trap that cost M7 five untested orderings, avoided here for the same
    reason `tests/unit/test_services_curation.py` avoids it: with a best-first
    fixture a 1-based handle map, a 0-based one and "insertion order" all
    agree, and ADR-0028's whole scheme rests on which one was sent.
    """
    seeded = []
    for index in range(count):
        one = Title(
            id=new_id(),
            kind=TitleKind.MOVIE,
            name=f"Candidate {index}",
            sort_name=f"candidate {index}",
            year=2019,
            vote_count=index,
            enrichment_state=EnrichmentState.ENRICHED,
        )
        await titles.add(one)
        seeded.append(one)
    return seeded


class _Recording:
    """The pipeline's `commit`, counted.

    `CurationService` owes **one** commit per generation covering both of its
    writes: PRD 10's dashboard 5 is `llm_calls JOIN curated_rows USING
    (generation_id)`, so a commit between them is a window in which a screen
    exists with no cost attributed to it. A wiring that handed the service
    some other callable -- `session.commit` captured elsewhere, a no-op --
    would leave this at zero.
    """

    def __init__(self) -> None:
        self.commits = 0

    async def __call__(self) -> None:
        self.commits += 1


def _settings(tmdb_api_key: SecretStr | None = None, **rest: object) -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher"),
        secret_key=SecretStr("0" * 32),
        tmdb_api_key=tmdb_api_key,
        **rest,  # type: ignore[arg-type]
    )


@pytest.fixture
def warnings() -> Iterator[io.StringIO]:
    sink = io.StringIO()
    logger.remove()
    logger.add(sink, level="WARNING")
    yield sink
    logger.remove()


async def test_a_missing_tmdb_key_is_reported_where_the_decision_is_made(
    warnings: io.StringIO,
) -> None:
    """PRD 08's "TMDb key missing" degradation is a *narrowed* deployment,
    not a silent one: an operator whose enrich queue never drains has to be
    able to see why. Once per process is where that belongs -- this function
    is called exactly once by each of the three composition roots (`usher
    work`, `usher push`, and `create_app`'s lifespan)."""
    settings = _settings()
    assert settings.tmdb_api_key is None

    provider, aclose = await metadata_provider(settings)
    await aclose()

    assert provider is None
    logged = warnings.getvalue()
    assert "TMDb" in logged
    assert logged.count("enrich") == 1, f"reported more than once: {logged}"


async def test_a_configured_tmdb_key_says_nothing(warnings: io.StringIO) -> None:
    """The other half. A warning every correctly-configured deployment sees
    is a warning nobody reads -- the same rule `EmbyAdapter.verify` follows
    for its administrator probe."""
    settings = _settings(SecretStr("0" * 32))

    provider, aclose = await metadata_provider(settings)
    await aclose()

    assert provider is not None
    assert warnings.getvalue() == ""


async def test_the_enrich_service_enqueues_into_the_pipelines_own_queue() -> None:
    """Behavioural, never an identity check on a private attribute.

    An `EnrichService` built with a queue of its own passes every case in
    `tests/unit/test_services_enrich.py` and enqueues into an object nothing
    else reads -- so a running deployment enriches titles, indexes none of
    them, and reports no error anywhere. The only way to see that is to drive
    an enrichment through `build_enrich_service` and then read the *pipeline's*
    queue.

    Assembled over fakes rather than over a session: this is a wiring claim,
    and `build_enrich_service` takes a `Pipeline` whose fields are ports.
    """
    titles = FakeTitleRepository()
    title = Title(
        kind=TitleKind.MOVIE,
        tmdb_id=90000550,
        name="The Quiet Vacuum",
        sort_name="quiet vacuum, the",
        enrichment_state=EnrichmentState.STUB,
    )
    await titles.add(title)
    queue = FakeJobQueue()
    pipeline = _pipeline_over_fakes(titles=titles, queue=queue)

    service = build_enrich_service(
        pipeline, _settings(), FakeMetadataProvider(), events=NullEventPublisher()
    )
    await service.enrich(title.id)

    assert (await queue.depth())[JobKind.INDEX] == 1


async def test_the_worker_offers_an_enrichments_frame_after_the_jobs_own_commit() -> None:
    """[ADR-0033](../../docs/prd/decisions/0033-an-event-is-a-statement-about-committed-state.md)
    through the real wiring, which is the half `tests/unit/test_services_jobs.py`
    cannot make.

    That file pins `JobWorker`'s buffer against a handler written for it;
    this one asserts the *decision* `build_worker` makes -- that the
    `EnrichService` it constructs is handed `worker.events` rather than
    `pipeline.events`. The two are indistinguishable from inside either
    module: a worker that buffers correctly and a factory that hands the
    bare bus past it publishes exactly as it does today, and every case in
    both files stays green.

    Driven through `run_once` rather than by reading a private attribute off
    the service, for the reason
    `test_the_enrich_service_enqueues_into_the_pipelines_own_queue` states
    above it: the claim is about what a running deployment does.
    """
    log: list[str] = []

    class _Bus(NullEventPublisher):
        async def publish(self, event: ClientEvent) -> None:
            log.append("publish")

    async def _commit() -> None:
        log.append("commit")

    titles = FakeTitleRepository()
    title = Title(
        kind=TitleKind.MOVIE,
        tmdb_id=90000550,
        name="The Quiet Vacuum",
        sort_name="quiet vacuum, the",
        enrichment_state=EnrichmentState.STUB,
    )
    await titles.add(title)
    queue = FakeJobQueue()
    await queue.enqueue(
        [JobRequest(kind=JobKind.ENRICH, key=str(title.id), priority=JobPriority.DEMAND)]
    )
    pipeline = _pipeline_over_fakes(titles=titles, queue=queue, commit=_commit, events=_Bus())

    worker = build_worker(
        _work_for(pipeline),
        _settings(),
        provider=FakeMetadataProvider(),
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    assert await worker.run_once() == 1

    assert log.count("publish") == 1, f"the enrichment published {log.count('publish')} frames"
    assert log[-1] == "publish", f"the frame was offered mid-job: {log}"
    assert log[-2] == "commit", f"nothing was committed between the handler and the frame: {log}"


def test_only_the_worker_defers_and_the_push_and_reconcile_lanes_do_not() -> None:
    """The push and reconcile lanes publish as they go, and that is a
    decision rather than an omission.

    Neither is a job: each commits its own subject before it publishes
    (`push.py:170` and `:275`, `reconcile.py:245`), so both already satisfy
    ADR-0033's stronger form with no buffer at all -- and a `sync.progress`
    frame held behind a 1,127-batch walk turns a progress bar into a single
    jump at the end.

    **Structural, because the defect is an absence and no lane's output can
    show it.** "Published as it went" and "published at the end" are the same
    list of frames in the same order; only a second commit boundary
    distinguishes them, and a lane has none to hang the assertion on. So the
    claim asserted is the one that can be: a `DeferredEventPublisher` is
    constructed in exactly one place in `src/`, and no composition root can
    acquire one for a lane by wrapping something.

    ⚠️ **That one place moved in M9's W1, from `services/jobs.py` to
    `composition.py`, and the claim is unchanged.** The buffer used to be
    `JobWorker`'s, wrapped once for the life of the worker; it is now built per
    *scope*, because two concurrent jobs sharing one buffer means the failing
    one's `discard()` empties the surviving one's frames. The construction site
    is therefore inside `build_worker`'s scope factory -- still exactly one, and
    still not reachable by a lane.

    Carries its own premise, because a scan that resolves nothing passes
    exactly like a scan that passes.
    """
    root = pathlib.Path(usher.__file__).parent
    sites = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "DeferredEventPublisher"
    )

    assert sites, "the scan found no construction at all; it would pass over an empty tree"
    assert sites == ["composition.py"], f"a second lane wraps its publisher: {sites}"


async def test_no_embedder_configured_degrades_rather_than_raising(
    warnings: io.StringIO,
) -> None:
    """The same shape `metadata_provider` has, for the same reason: a worker
    refusing to start without a model would take three working lanes down
    with the fourth. PRD 05's catalog-lookup tier -- full-text plus trigram
    over 1.27M titles -- needs no model at all, so "no embedder" is a
    *narrowed* deployment rather than a broken one.

    Reported here, once per process, and not in `build_worker`, which runs
    once per worker *pass* at a 5 s floor -- the ~17,280-lines-a-day shape.
    """
    built, aclose = await embedder(_settings(embedding_enabled=False))
    await aclose()  # the no-op half of the pair, callable unconditionally

    assert built is None
    logged = warnings.getvalue()
    assert logged.count("index jobs") == 1, f"reported more than once: {logged}"


def test_a_worker_without_an_embedder_registers_no_index_handler() -> None:
    """`run_once` claims `list(self._handlers)`, and its docstring says why:
    claiming a kind you cannot run either crashes on the lookup or parks work
    whose only problem is that it was offered to the wrong process -- and a
    job parked that way needs a human to release it.

    Fails: registering `INDEX` unconditionally and letting `IndexService`
    hold `None`. Nothing raises until a job arrives, at which point it parks,
    and the review list fills with work that is perfectly runnable elsewhere.

    The `ENRICH` and `CURATE` halves are asserted alongside it, so the three
    guards cannot drift into "two guarded, one not", and `MATCH` is asserted
    so an implementation registering *nothing* cannot pass. This is the
    default deployment -- no key, no extra, no model -- and its five
    claimable kinds (`match`, `watch_history`, `watch_writeback`, `sync`,
    `bootstrap`) are the whole of what it can do. `bootstrap` joined them in
    M9's E5 for `sync`'s reason: a bulk import needs a writable data
    directory and an outbound client, neither of which is a process resource
    a deployment can lack at build time.
    """
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert worker.registered_kinds == frozenset(
        {
            JobKind.MATCH,
            JobKind.WATCH_HISTORY,
            JobKind.WATCH_WRITEBACK,
            JobKind.SYNC,
            JobKind.BOOTSTRAP,
        }
    )


def test_a_write_back_handler_is_registered_in_every_build() -> None:
    """The kind a client's own press enqueues, so no deployment may lack it.

    M4's rule -- *"a job kind whose handler is a stub is a queue that grows
    forever"* -- and this is the shape it takes when the handler exists but
    the registration is guarded: `run_once` claims `list(self._handlers)`, so
    a `WATCH_WRITEBACK` behind any condition leaves the shipped default
    deployment enqueueing a job on every `PUT /watch/...` that nothing ever
    claims. That is not the benign "leave it for a worker that can run it"
    bargain `INDEX` makes, because there is no such worker: the handler needs
    a TMDb key, an embedder and an LLM endpoint exactly as much as `match`
    does, which is not at all.

    Asserted against the **bare** build -- no provider, no embedder, no
    client -- because that is the configuration every guard would exclude it
    from, with the fully-equipped build beside it as the control that stops
    the case passing against a registration nothing reaches.
    """
    bare = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    equipped = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=FakeMetadataProvider(),
        embedder=FakeEmbedder(),
        client=FakeLLMClient(),
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert JobKind.WATCH_WRITEBACK in bare.registered_kinds
    assert JobKind.WATCH_WRITEBACK in equipped.registered_kinds


async def test_a_write_back_job_reaches_the_source_through_the_pipelines_own_repositories() -> None:
    """The registration end to end: a real job, claimed by a real worker,
    arriving at a real adapter.

    Two things only this shape can say. `mypy` holds the *types* of the two
    repositories `build_worker` hands the handler and says nothing about them
    being the **pipeline's** -- a second `FakeMediaItemRepository` built here
    would type-check and would answer `None` for every key, so the case reads
    the state back through the objects the pipeline holds. And the `user_id`
    is the one the root bound, which is the half `watch_history` already
    depends on: a handler reading some other household's row would push a
    position this household never set.

    `run_once() == 1` is the premise, not the result -- a worker that never
    claimed the kind returns 0 and every later assertion would be about an
    empty ledger.
    """
    source = Source(
        kind=SourceKind.EMBY,
        name="Living Room",
        base_url="https://emby.example",
        credentials_ref="ref",
        device_id="device",
    )
    adapter = FakeSourceAdapter(source)
    adapter.seed(
        SourceItem(external_id="emby-1", name="A Film", kind=SourceItemKind.MOVIE),
        datetime(2026, 8, 11, tzinfo=UTC),
    )
    media_items = FakeMediaItemRepository()
    title_id = new_id()
    await media_items.upsert_many(
        [
            MediaItemUpsert(
                source_id=source.id,
                external_id="emby-1",
                title_id=title_id,
                episode_id=None,
                container=None,
                video_codec=None,
                audio_codec=None,
                width=None,
                height=None,
                hdr_format=None,
                audio_channels=None,
                file_size_bytes=None,
                runtime_seconds=None,
                added_at=None,
                last_seen_at=datetime(2026, 8, 11, tzinfo=UTC),
            )
        ]
    )
    watch_states = FakeWatchStateRepository()
    household = new_id()
    await watch_states.set_from_client(
        WatchStateWrite(
            user_id=household,
            title_id=title_id,
            episode_id=None,
            position_seconds=613,
            played=False,
        )
    )
    queue = FakeJobQueue()
    pipeline = _pipeline_over_fakes(
        titles=FakeTitleRepository(),
        queue=queue,
        watch_states=watch_states,
        media_items=media_items,
    )

    async def resolve(external_id: str) -> SourceBinding | None:
        return SourceBinding(source=source, adapter=adapter)

    worker = build_worker(
        _work_for(pipeline),
        _settings(),
        provider=None,
        embedder=None,
        client=None,
        registry=_ScriptedRegistry(resolve),
        user_id=household,
    )
    await queue.enqueue([JobRequest(kind=JobKind.WATCH_WRITEBACK, key="emby-1", priority=80)])

    assert await worker.run_once() == 1, "the worker never claimed the write-back job"

    assert adapter.recorded("emby-1") == (613, False)
    assert queue.jobs_of(JobKind.WATCH_WRITEBACK) == [], "a successful job kept its row"


def test_every_kind_a_bare_build_registers_is_named_by_the_docstring_that_lists_them() -> None:
    """`JobWorker.registered_kinds`' docstring names which kinds are in every
    build, and that sentence was written deliberately to be falsified here --
    M8's trap 2 in a new location, where updating it silently is the failure
    it exists to prevent.

    Derived from the bare build rather than from a literal list, so a sixth
    unconditional kind cannot be added without the prose moving with it. The
    claim is pinned rather than the prose: a verbatim assertion on the
    sentence would fail every future copy-edit that left the claim intact,
    which is the change-detector this repository has already been bitten by
    once.
    """
    doc = inspect.getdoc(JobWorker.registered_kinds)
    assert doc is not None
    bare = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    assert bare.registered_kinds, "the premise: a bare build registers something"

    unnamed = sorted(kind.name for kind in bare.registered_kinds if kind.name not in doc)
    assert unnamed == [], f"in every build and unmentioned by the docstring: {unnamed}"


def test_a_worker_with_an_embedder_registers_the_index_handler() -> None:
    """The control that makes the case above evidence rather than a
    tautology: without it, an implementation registering *nothing* passes."""
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=FakeEmbedder(),
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert JobKind.INDEX in worker.registered_kinds


def test_a_worker_without_a_provider_registers_no_derive_handler() -> None:
    """`DERIVE` is guarded on the **provider**, the `ENRICH` arm rather than
    the `INDEX` one, and the guard is correct rather than merely consistent:
    `DeriveService` holds a `MetadataProvider` for `to_derivation`, and a
    deployment with no key has no TMDb payloads in `raw_payloads` to derive
    from at all -- they exist only because a key once did.

    Fails: the unguarded registration. Its symptom is a parked job on a
    keyless deployment, and a parked job needs a human to release work whose
    only problem was the process it was offered to. Leaving it pending for a
    worker that has a key is `INDEX`'s bargain, one lane over.
    """
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=FakeEmbedder(),
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert JobKind.DERIVE not in worker.registered_kinds
    # The embedder is present, so this is a guard on the provider rather than
    # on "anything optional" -- without this line the case passes against a
    # `DERIVE` registered under `embedder is not None`.
    assert JobKind.INDEX in worker.registered_kinds


def test_a_worker_with_a_provider_registers_the_derive_handler() -> None:
    """The control that makes the case above evidence rather than a
    tautology: without it, an implementation registering *nothing* passes."""
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=FakeMetadataProvider(),
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert JobKind.DERIVE in worker.registered_kinds
    assert JobKind.INDEX not in worker.registered_kinds


# -- the LLM client, and the curate lane it turns on -----------------------


async def test_no_llm_configured_degrades_rather_than_raising(
    warnings: io.StringIO,
) -> None:
    """The shipped default, and the shape `metadata_provider` and `embedder`
    already have: `(None, no-op)` rather than a raise.

    Off by default is the honest default twice over here. Nine of the ten row
    providers need no model, so `GET /home` is a shorter screen rather than a
    broken one -- that is `embedding_enabled`'s argument. The second is this
    project's only one of its kind: turning it on sends the household's watch
    history to whatever `USHER_LLM_BASE_URL` names, which may be a machine
    the household does not own.

    Reported here, once per process, and **not** in `build_worker`, which
    runs once per worker *pass* at a 5 s floor -- the ~17,280-lines-a-day
    shape this project has already measured for a string.
    """
    settings = _settings()
    assert settings.llm_enabled is False, "the premise: off is the shipped default"

    built, aclose = await llm_client(settings)
    await aclose()  # the no-op half of the pair, callable unconditionally

    assert built is None
    logged = warnings.getvalue()
    assert logged.count("curate jobs") == 1, f"reported more than once: {logged}"


async def test_a_configured_llm_is_built_and_says_nothing(warnings: io.StringIO) -> None:
    """The other half, and the control that makes the case above evidence:
    without it, a factory that answered `(None, warning)` for *every*
    deployment passes.

    A warning every correctly-configured deployment sees is a warning nobody
    reads -- the rule `metadata_provider`'s pair already follows. Nothing here
    opens a socket: the client is an `httpx.AsyncClient` that has not been
    asked for anything.
    """
    built, aclose = await llm_client(_settings(llm_enabled=True))
    try:
        assert built is not None
    finally:
        await aclose()

    assert warnings.getvalue() == ""


async def test_a_credentialled_endpoint_with_no_prices_says_the_ledger_will_read_zero(
    warnings: io.StringIO,
) -> None:
    """`llm_price_*_per_mtok` both default to `Decimal(0)`, and no provider
    reports a cost, so `cost_usd` is computed from those two numbers alone --
    an operator who never set them gets `0.00000000` on every row, which looks
    like a measurement and is an absence.

    **The credential is the gate, and it is what keeps this off the shipped
    deployment.** Zero is the *honest* value for the self-hosted vLLM this
    milestone was verified against, so warning on price alone would be the
    thing `test_a_configured_llm_is_built_and_says_nothing` above exists to
    forbid -- a warning every correctly-configured deployment sees. A hosted
    provider requires an `llm_api_key` and the local endpoint needs none, so
    the credential separates the two populations.
    """
    built, aclose = await llm_client(
        _settings(llm_enabled=True, llm_api_key=SecretStr("sk-" + "0" * 44)),
    )
    try:
        assert built is not None
    finally:
        await aclose()

    logged = warnings.getvalue()
    assert logged.count("cost_usd") == 1, f"reported more than once: {logged}"
    # Never the credential itself, on the one line that exists because a
    # credential is present.
    assert "sk-" not in logged


async def test_a_credentialled_endpoint_with_prices_set_says_nothing(
    warnings: io.StringIO,
) -> None:
    """The control that makes the case above evidence: without it, a warning
    fired for every credentialled deployment would pass just as well."""
    built, aclose = await llm_client(
        _settings(
            llm_enabled=True,
            llm_api_key=SecretStr("sk-" + "0" * 44),
            llm_price_in_per_mtok=Decimal(3),
            llm_price_out_per_mtok=Decimal(15),
        ),
    )
    try:
        assert built is not None
    finally:
        await aclose()

    assert warnings.getvalue() == ""


def test_a_worker_without_an_llm_client_registers_no_curate_handler() -> None:
    """`CURATE` is guarded on the **client**, exactly as `INDEX` is guarded on
    the embedder, and for the identical reason: `run_once` claims
    `list(self._handlers)`, so a worker with no model must not ask for work it
    cannot do. Claiming it either crashes on the lookup or parks a job whose
    only problem is the process it was offered to, and a job parked that way
    needs a human to release it.

    Fails: registering `CURATE` unconditionally. It cannot even be spelled
    without weakening `CurationService`'s `client: LLMClient` to
    `LLMClient | None`, which is the point of that annotation -- "no client,
    no curation" is a `mypy` fact at the one layer that can know it, rather
    than an `if self._client is None` branch unreachable from `src/`.

    The embedder is present, so this is a guard on the client rather than on
    "anything optional": without the `INDEX` line the case passes against a
    `CURATE` registered under `embedder is not None`, and without the `MATCH`
    line it passes against an implementation registering *nothing*.
    """
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=FakeEmbedder(),
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert JobKind.CURATE not in worker.registered_kinds
    assert JobKind.INDEX in worker.registered_kinds
    assert JobKind.MATCH in worker.registered_kinds


def test_a_worker_with_an_llm_client_registers_the_curate_handler() -> None:
    """The control that makes the case above evidence rather than a
    tautology. `INDEX` is asserted absent alongside it so the two guards
    cannot drift into "one client turns both on"."""
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())),
        _settings(),
        provider=None,
        embedder=None,
        client=FakeLLMClient(),
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )

    assert JobKind.CURATE in worker.registered_kinds
    assert JobKind.INDEX not in worker.registered_kinds


async def test_the_worker_runs_a_curate_job_into_the_pipelines_own_curated_rows() -> None:
    """**Behavioural, never an identity check on a private attribute**, and
    driven through `run_once` rather than through a handler this file reached
    for -- so registration, claiming, the key conversion and the write are one
    assertion instead of four hopeful ones.

    A `CurationService` wired to repositories of its own passes every case in
    `tests/unit/test_services_curation.py` -- the screen is written, the
    ledger row is written, nothing raises -- and a running deployment then
    generates a household's shelves into an object nothing serves from. That
    is `test_the_enrich_service_enqueues_into_the_pipelines_own_queue`'s
    defect one milestone over, and `RowContext.curated = None`'s one task
    over, where a `mypy` annotation was the only thing holding it. The only
    way to see it is to read the **pipeline's** repositories back.

    The household is the job's key and nothing else, which is the other half:
    `build_worker` is handed a `user_id` for `watch_history`'s handler, and a
    curate handler that took *that* would dedup correctly, park correctly, and
    write household B's generation onto household A's screen.
    """
    titles = FakeTitleRepository()
    await _candidates(titles, count=POOL_SIZE + 2)
    curated = FakeCuratedRowRepository()
    ledger = FakeLLMCallRepository()
    queue = FakeJobQueue()
    pipeline = _pipeline_over_fakes(titles=titles, queue=queue, curated=curated, ledger=ledger)
    bound = new_id()
    household = new_id()
    assert household != bound, "the premise: the key names a household the root did not bind"
    worker = build_worker(
        _work_for(pipeline),
        _settings(),
        provider=None,
        embedder=None,
        client=FakeLLMClient.returning(_ROWS, usages=[usage(model="test/answered-1")]),
        registry=_no_sources(),
        user_id=bound,
    )
    await queue.enqueue([JobRequest(kind=JobKind.CURATE, key=str(household), priority=20)])

    assert await worker.run_once() == 1, "the worker never claimed the curate job"

    assert [row.user_id for row in curated.rows] == [household]
    assert [call.ok for call in ledger.calls] == [True]
    assert queue.jobs_of(JobKind.CURATE) == [], "a successful job kept its row"


async def test_the_curation_service_is_built_over_the_pipelines_pool_and_commits_once() -> None:
    """The two collaborators a second copy would be invisible against, and the
    one call that has to cover both writes.

    `build_curation_service` must take `pipeline.pool` rather than construct a
    `CandidatePoolService` over the same repositories: a second one would be
    built at `settings.curation_pool_size`, which is 200, and would answer
    *identically* on every fixture seeding fewer than that. The pool's size is
    the only thing that separates them, and `_schema(len(candidates))` puts it
    on the wire where a case can read it -- so the fixture seeds more
    candidates than `POOL_SIZE` admits and asserts on the handle ceiling the
    model was actually sent.

    One commit, because PRD 10's dashboard 5 is `llm_calls JOIN curated_rows
    USING (generation_id)`: a commit between the two writes is a window in
    which a screen exists with no cost attributed to it. A wiring that handed
    the service some other callable would leave this at zero, and the count is
    what tells "the pipeline's commit" from "a commit".
    """
    titles = FakeTitleRepository()
    seeded = await _candidates(titles, count=POOL_SIZE + 2)
    assert len(seeded) > POOL_SIZE, "the premise: more candidates exist than the pool admits"
    ledger = FakeLLMCallRepository()
    commits = _Recording()
    pipeline = _pipeline_over_fakes(
        titles=titles, queue=FakeJobQueue(), ledger=ledger, commit=commits
    )
    client = FakeLLMClient.returning(_ROWS, usages=[usage(model="test/answered-1")])

    await build_curation_service(pipeline, _settings(), client).generate(new_id())

    assert len(client.calls) == 1, "one generation is one billed call"
    handles = client.calls[0].schema[_PROPERTIES][ROWS_KEY]["items"][_PROPERTIES][ITEM_IDS_KEY]
    assert handles["items"]["maximum"] == POOL_SIZE, (
        "the model was offered a pool this factory built rather than the pipeline's"
    )
    assert len(ledger.calls) == 1, "one generation is one ledger row"
    assert commits.commits == 1, "the screen and its cost must land in one transaction"


async def test_a_curate_job_for_an_empty_catalog_parks_and_buys_nothing() -> None:
    """PRD 08's operator rule -- every command works against an empty database
    -- and the milestone's cost argument, at the layer that spends the money.

    A generation for a household with nothing to recommend is a charge with a
    guaranteed empty answer, so `CurationService` raises **before** the client
    is touched, and it is the one failure path that writes no `llm_calls` row
    at all: nothing was attempted for a ledger to hold a row about.
    `PortDataMalformed` rather than `PortUnavailable`, so `JobWorker` parks it
    instead of spending four more completions reaching the same answer -- an
    empty catalog is an operator's problem and does not improve on a backoff
    schedule.

    Asserted on the diagnostics rather than on the verdict: an implementation
    that called the model and *then* found nothing usable parks with the same
    status, and `client.calls == []` is what tells the two apart.
    """
    ledger = FakeLLMCallRepository()
    client = FakeLLMClient.returning(_ROWS)
    queue = FakeJobQueue()
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=FakeTitleRepository(), queue=queue, ledger=ledger)),
        _settings(),
        provider=None,
        embedder=None,
        client=client,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    await queue.enqueue([JobRequest(kind=JobKind.CURATE, key=str(new_id()), priority=20)])

    assert await worker.run_once() == 1

    assert [job.status for job in queue.jobs_of(JobKind.CURATE)] == [JobStatus.PARKED]
    assert client.calls == [], "an empty catalog bought a completion"
    assert ledger.calls == [], "a ledger row for a call that was never attempted"


async def test_a_curate_job_that_could_not_reach_the_model_backs_off_and_still_bills() -> None:
    """The other side of the classification, and the control that makes the
    case above about `PortDataMalformed` rather than about "curation fails".

    An endpoint that refused the connection is `PortUnavailable`, which
    `JobWorker` backs off rather than parks -- it may well answer on the next
    attempt, unlike an empty catalog. And the ledger still gets its row:
    `llm_calls` is one row per *attempt*, so a call that never got an answer
    is a row with zeroed tokens, `ok = false`, and **the model this deployment
    asked for**. That last field is why this case reads it: on the success
    path the honest value is whatever answered, and this is the only path
    where `settings.llm_model` is the sole truthful answer -- so a wiring that
    defaulted the model, or passed the neighbouring `embedding_model`, is
    green on every case that scripts a response.
    """
    titles = FakeTitleRepository()
    await _candidates(titles, count=POOL_SIZE)
    ledger = FakeLLMCallRepository()
    queue = FakeJobQueue()
    settings = _settings(llm_model="test/asked-for-this-one")
    assert settings.llm_model != settings.embedding_model, "the premise: the two fields differ"
    worker = build_worker(
        _work_for(_pipeline_over_fakes(titles=titles, queue=queue, ledger=ledger)),
        settings,
        provider=None,
        embedder=None,
        client=FakeLLMClient.returning(PortUnavailable("the endpoint refused the connection")),
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    await queue.enqueue([JobRequest(kind=JobKind.CURATE, key=str(new_id()), priority=20)])

    assert await worker.run_once() == 1

    assert [job.status for job in queue.jobs_of(JobKind.CURATE)] == [JobStatus.PENDING]
    assert [(call.ok, call.model) for call in ledger.calls] == [(False, "test/asked-for-this-one")]


async def test_the_model_is_loaded_once_across_three_worker_passes() -> None:
    """**The measured failure this factory exists to prevent.**

    `build_worker` runs once per worker *pass* -- `lanes._run_worker` rebuilds
    it every turn of a loop whose floor is 5.0 s. A per-pass `logger.warning`
    there was measured at ~17,280 lines a day; a per-pass *model load* is
    4.84 s cold / 0.13 s warm and 65 MB of ONNX, so the lane would spend more
    time loading than working, forever, with nothing in the logs saying so.

    Three passes, not one: a single pass cannot tell "once" from "per pass"
    -- the same shape
    `test_the_worker_lane_requeues_abandoned_claims_once_not_every_pass`
    needed. Counted through a *loading* embedder rather than read off the
    source, so the case fails against any spelling that builds one here.
    """
    loads: list[int] = []

    class _Loading(FakeEmbedder):
        def __init__(self) -> None:
            super().__init__()
            loads.append(1)

    model = _Loading()
    pipeline = _pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())
    for _ in range(3):
        build_worker(
            _work_for(pipeline),
            _settings(),
            provider=None,
            embedder=model,
            client=None,
            registry=_no_sources(),
            user_id=uuid.uuid4(),
        )

    assert loads == [1], "the model was loaded per worker pass, not per process"


async def test_the_factory_sets_hf_hub_offline_before_importing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured: warm cache, no network, flag unset -> `RuntimeError: Cannot
    send a request, as the client has been closed`, from huggingface_hub
    reusing a closed client on the retry path. The message names neither the
    network nor the cache. Reproduced two independent ways, and it is also
    the only setting under which a genuine cache miss produces a
    comprehensible `OSError`.

    `_load_embedder` is replaced rather than left to import a real model:
    this case is about the environment variable, and no test in this
    repository downloads 65 MB or makes a network request.
    """
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setattr("usher.composition._load_embedder", lambda _: FakeEmbedder())

    built, aclose = await embedder(_settings(embedding_enabled=True))
    try:
        assert os.environ["HF_HUB_OFFLINE"] == "1"
    finally:
        await aclose()
    assert built is not None


async def test_an_operators_own_hf_hub_offline_value_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`setdefault`, not assignment.

    An operator warming the cache for the first time runs one command with
    `HF_HUB_OFFLINE=0`, and a container may set its own; either must survive.
    Written as its own case because the mutation to `os.environ[...] = "1"`
    survives the case above -- the plan predicted that and said to add this
    rather than record it as untested.
    """
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setattr("usher.composition._load_embedder", lambda _: FakeEmbedder())

    _, aclose = await embedder(_settings(embedding_enabled=True, embedding_offline=True))
    await aclose()

    assert os.environ["HF_HUB_OFFLINE"] == "0"


async def test_an_embedder_that_cannot_load_degrades_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch, warnings: io.StringIO
) -> None:
    """A missing extra, a missing model file, a cache miss under
    `HF_HUB_OFFLINE=1`: all three are `ImportError`/`OSError` at *build* time
    in a process whose other three lanes are fine.

    Fails: letting it propagate out of `create_app`'s lifespan, which turns
    "the embedding extra is not installed" into a server that will not boot
    -- the degradation-into-outage trade PRD 08 forbids.

    Both arms, because they are different exception hierarchies and catching
    one is the natural half-fix: an absent package raises `ImportError` and a
    cache miss raises `OSError`.
    """
    for failure in (ImportError("no module named fastembed"), OSError("model not in cache")):
        monkeypatch.setattr("usher.composition._load_embedder", _raising(failure), raising=True)

        built, aclose = await embedder(_settings(embedding_enabled=True))
        await aclose()

        assert built is None
    assert "index jobs will not be claimed" in warnings.getvalue()


def _raising(exc: Exception) -> Callable[[Settings], Embedder]:
    def _load(_: Settings) -> Embedder:
        raise exc

    return _load


async def _never_resolves(_: str) -> SourceBinding | None:
    return None


def _work_for(pipeline: Pipeline) -> UnitOfWork:
    """A `UnitOfWork` handing back one already-built pipeline.

    `build_worker` opens a scope per claim and per job since M9's W1, so it
    takes a factory rather than a pipeline. Against fakes there is no session
    to open and no `AsyncSession` to keep two coroutines off, so every scope
    here is the same object -- which is exactly what
    `tests/integration/test_services_jobs.py` must **not** do, and does not:
    the property that concurrent jobs get *different sessions* is only
    expressible against a real engine, and it is asserted there.
    """

    @asynccontextmanager
    async def _open() -> AsyncIterator[Pipeline]:
        yield pipeline

    return _open


class _ScriptedRegistry(SourceRegistry):
    """A `SourceRegistry` whose resolver is given rather than looked up.

    The real one reads `sources` and `media_items` and then builds an adapter
    through `open_adapter`, which needs a source row, a credential row and a
    factory -- three things none of the cases below are about. Subclassing
    rather than passing a bare callable keeps `build_worker`'s signature
    honest about what it takes: the registry is a process-lifetime resource
    with an adapter cache, and a `Callable` argument would have let a caller
    hand it something rebuilt per job.
    """

    def __init__(self, resolve: Callable[[str], Awaitable[SourceBinding | None]]) -> None:
        super().__init__()
        self._scripted = resolve

    def bound(self, pipeline: Pipeline) -> Callable[[str], Awaitable[SourceBinding | None]]:
        return self._scripted


def _no_sources() -> SourceRegistry:
    """The shipped default deployment's answer: nothing addresses that id."""
    return _ScriptedRegistry(_never_resolves)


async def test_the_pool_and_the_screen_read_one_taste_service() -> None:
    """`build_pipeline` wires `Pipeline.taste` and `Pipeline.pool.taste` to the
    **same object**, and until now that was a comment rather than a check.

    Two `TasteService` instances over one session are not merely wasteful.
    `centroid()` *writes*: it reads `user_taste`, and on a miss recomputes and
    `put`s -- including the written refusal for a household below
    `_MIN_TITLES`. Two of them in one generation means two reads and up to two
    writes of one row, and the second is computed against a watermark the first
    may already have moved, which is the self-certifying staleness that
    method's own docstring exists to rule out.

    A real `AsyncSession` over a real engine, because the claim is about
    identity in the wiring rather than about a fake: `create_async_engine` does
    not connect, and nothing here issues a statement, so this stays in the unit
    suite where a wiring assertion belongs.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    try:
        pipeline = build_pipeline(AsyncSession(engine), _settings())

        assert pipeline.pool.taste is pipeline.taste
    finally:
        await engine.dispose()


async def test_a_pipeline_with_no_llm_client_gives_search_nothing_to_expand_with() -> None:
    """**The shipped default, at the wiring layer.** `USHER_LLM_ENABLED` is
    `false`, so `llm_client` answers `(None, no-op)` and no caller has one to
    pass -- and a `build_pipeline` that built an expander anyway would need a
    client it does not have. What this pins is that the *absence* survives:
    with no `llm=`, `SearchService` holds no expander and every search on every
    default deployment embeds the query exactly as typed.

    Reaching `_expander` is deliberate. A wiring assertion has nothing else to
    look at -- the behavioural half needs a real `PostgresSearchIndex` -- and
    this is the same shape as `pipeline.pool.taste is pipeline.taste` above.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    try:
        pipeline = build_pipeline(AsyncSession(engine), _settings())

        assert pipeline.search._expander is None
    finally:
        await engine.dispose()


async def test_an_expansion_is_billed_to_the_pipelines_own_ledger_and_model() -> None:
    """Three wirings, and each is a different way for the spend to go missing.

    A ledger that is **not** `pipeline.llm_calls` writes into a repository over
    another session, so the row never reaches the transaction the search
    commits. A commit that is not the session's leaves the row to be rolled
    back when the read closes -- and a search writes nothing else, so there is
    no second write to carry it. And a `model` that is not `settings.llm_model`
    is a `llm_calls.model` disagreeing with the string the client was built
    with, on exactly the path where no response came back to read one from,
    which is the column PRD 10 groups spend by.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    client = FakeLLMClient()
    try:
        session = AsyncSession(engine)
        pipeline = build_pipeline(session, _expanding(llm_model="wired/asked-1"), llm=client)

        expander = pipeline.search._expander
        assert expander is not None
        assert expander._client is client
        # Through `_spend`, which is where the three of them live since the
        # ledger rule became `services/llm_ledger.py`'s rather than each
        # spender's. The assertion is the same one -- these are still the
        # objects `build_pipeline` is on the hook for wiring.
        assert expander._spend._ledger is pipeline.llm_calls
        assert expander._spend._commit == session.commit
        assert expander._spend._model == "wired/asked-1"
        assert expander._spend._purpose is LLMPurpose.QUERY_EXPANSION
    finally:
        await engine.dispose()


async def test_a_client_is_necessary_and_not_sufficient_for_an_expander() -> None:
    """**The second switch, at the wiring layer, and the state it is for is the
    ordinary M8 deployment.** `USHER_LLM_ENABLED=true` with
    `USHER_QUERY_EXPANSION_ENABLED=false` is a household that wants curated
    rows and does not want its searches rewritten -- which is what PRD 05's
    2026-08-07 measurement (MRR 0.733 -> 0.373) makes the default rather than
    an eccentric choice.

    The distinction this case exists for is that the client is **present**
    here. `test_a_pipeline_with_no_llm_client_gives_search_nothing_to_expand_with`
    above reaches the same `None` through the `llm is None` arm, so it is
    satisfied by a `build_pipeline` that ignores the setting entirely; only a
    fixture holding a real client can tell the two arms apart.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    settings = _settings(llm_enabled=True)
    assert settings.query_expansion_enabled is False, "the premise: off even with the LLM on"
    try:
        pipeline = build_pipeline(AsyncSession(engine), settings, llm=FakeLLMClient())

        assert pipeline.search._expander is None
    finally:
        await engine.dispose()


async def test_a_switch_on_with_no_client_to_hand_still_builds_no_expander() -> None:
    """The mirror of the case above, and the configuration it is about is
    ordinary rather than contrived.

    `unit_of_work` -- what `usher.api.lanes` and `usher work` build every unit
    of work through -- calls `build_pipeline` with **no `llm`**, because a lane
    has no use for a completion client. On a deployment with both switches on,
    that is `query_expansion_enabled=True` arriving beside `llm is None`, which
    is exactly the state a `build_pipeline` that consulted only the setting
    would construct a `QueryExpansionService(client=None)` for: a service whose
    first `complete_json` is an `AttributeError` inside a search.

    Found 2026-08-07 by this task's sweep. Dropping the `llm is None` disjunct
    survived all 2,892 unit cases -- because the only case reaching that arm
    had the setting off, so the mutant answered `None` for the other reason.
    It is caught by `mypy` (`client` narrows to `LLMClient` only through the
    `is None` test), so the *gate* was never open; the **suite** was, and
    "mypy holds it" is a claim about one tool rather than about the wiring.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    settings = _expanding()
    assert settings.query_expansion_enabled is True, "the premise: the switch really is on"
    try:
        pipeline = build_pipeline(AsyncSession(engine), settings)

        assert pipeline.search._expander is None
    finally:
        await engine.dispose()


async def test_both_search_roots_write_search_queries_over_this_sessions_commit() -> None:
    """PRD 10's `search_queries`, wired on the two roots that build a
    `SearchService`, and the *commit* is the half that has to be this
    session's.

    Three wirings, three ways for the analytics to go missing, and each is
    silent:

    - **No analytics at all.** Every search answers correctly, both histograms
      record, and the table PRD 10 turns ADR-0002's Meilisearch gate into a
      live measurement with stays empty forever. There is no error and no log
      line, which is why this is a wiring assertion rather than a behavioural
      one.
    - **A repository over another session**, so the row never reaches the
      transaction the search commits.
    - **A commit that is not this session's**, which leaves the row to be
      rolled back when the read closes -- and a search writes nothing else, so
      there is no second write to carry it. `cli._session_for` disposes its
      engine without committing, so on that root the loss is total.

    **Both roots, because `build_pipeline` delegating to `build_search_service`
    is a fact about today's code rather than a guarantee.** `usher search`
    reaches this through `build_pipeline` and `api/deps.get_search_service`
    reaches it directly; a `build_pipeline` that re-assembled a `SearchService`
    of its own would return a working one and record nothing.
    """
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    try:
        session = AsyncSession(engine)
        direct = build_search_service(session, _settings())
        through_the_pipeline = build_pipeline(session, _settings()).search

        for service in (direct, through_the_pipeline):
            analytics = service._analytics
            assert analytics is not None
            assert isinstance(analytics.queries, PostgresSearchQueryRepository)
            assert analytics.queries._session is session
            assert analytics.commit == session.commit
    finally:
        await engine.dispose()


def _expanding(**rest: object) -> Settings:
    """The only configuration that buys a rewrite: both switches on.

    A helper rather than a literal pair at each call site because `config.py`
    refuses `query_expansion_enabled` without `llm_enabled`, so the two always
    travel together and a case that set one would fail on validation rather
    than on its own subject.
    """
    return _settings(
        llm_enabled=True,
        query_expansion_enabled=True,
        **rest,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# `run_bootstrap` -- one dispatch, two roots (M9's E5).
# ---------------------------------------------------------------------------


class _JournallingCatalog(FakeBulkCatalogRepository):
    """`FakeBulkCatalogRepository` that writes down when the load window opens
    and closes and when the crosswalk is linked.

    The window's two edges are recorded separately rather than as one entry,
    because *"the window wraps both IMDb passes"* and *"the window wraps each
    pass"* differ only in where the closes fall.
    """

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
        return self._journalled_window()

    @asynccontextmanager
    async def _journalled_window(self) -> AsyncIterator[None]:
        self._journal.append("window-open")
        async with super().bulk_load_window():
            yield
        self._journal.append("window-close")

    async def link_crosswalk(self) -> CrosswalkLinkResult:
        self._journal.append("link-crosswalk")
        return await super().link_crosswalk()


class _JournallingRuns(FakeImportRunRepository):
    """Every dataset this run touched, in the order it touched them.

    The transport in these cases refuses every request, so `BulkDataset.
    revision()` raises `PortUnavailable` and `BootstrapService.
    import_dataset` records a `FAILED` run rather than downloading 335 MiB.
    A dataset that gets as far as `start()` therefore writes **twice** -- the
    started row and the failed one -- and consecutive repeats are collapsed,
    because this case is about the order of the phases and not about how many
    writes each one makes.
    """

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    def _note(self, entry: str) -> None:
        if not self._journal or self._journal[-1] != entry:
            self._journal.append(entry)

    async def start(self, dataset: str, revision: str) -> ImportRun:
        self._note(dataset)
        return await super().start(dataset, revision)

    async def save(self, run: ImportRun) -> None:
        self._note(run.dataset)
        await super().save(run)


def _offline_client(*_: object, **__: object) -> httpx.AsyncClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("the bootstrap dispatch reached the network")

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse))


async def _journal_of_a_full_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, *, through_the_worker: bool
) -> list[str]:
    """`--phase all` driven either the way `usher bootstrap` drives it or the
    way the `bootstrap` job handler does, over the same fakes."""
    journal: list[str] = []
    catalog = _JournallingCatalog(journal)
    runs = _JournallingRuns(journal)
    settings = _settings(bulk_data_dir=tmp_path)
    monkeypatch.setattr(usher.composition, "bulk_client", _offline_client)

    if not through_the_worker:
        # Byte for byte what `cli._bootstrap` calls, minus the engine it owns
        # and with the report sink pointed at the same journal -- which is
        # what makes the three catalog-dependent phases visible at all, since
        # each of them refuses an empty catalog with a sentence rather than
        # touching a dataset.
        await run_bootstrap(
            catalog,
            runs,
            _nothing,
            settings,
            BootstrapPhase.ALL,
            report=journal.append,
            events=NullEventPublisher(),
        )
        return journal

    queue = FakeJobQueue()
    pipeline = _pipeline_over_fakes(titles=FakeTitleRepository(), queue=queue)
    monkeypatch.setattr(usher.composition, "_log_bootstrap_line", journal.append)
    worker = build_worker(
        _work_for(dataclasses.replace(pipeline, bulk=catalog, import_runs=runs)),
        settings,
        provider=None,
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.BOOTSTRAP, key=BootstrapPhase.ALL.value, priority=JobPriority.DEMAND
            )
        ]
    )
    assert await worker.run_once() == 1, "the premise: the worker claimed the bootstrap job"
    return journal


async def _nothing() -> None:
    return None


#: Which phase each journal entry belongs to. The three catalog-dependent
#: phases contribute a refusal *sentence* rather than a dataset name against
#: an empty catalog, which is what makes all six visible in one run.
_PHASE_OF = (
    (BootstrapPhase.IMDB, ("window-", "imdb.title.")),
    (BootstrapPhase.CREDIT_NAMES, ("credit-names", "imdb.credit_names")),
    (BootstrapPhase.ALIASES, ("aliases", "imdb.title.akas")),
    (BootstrapPhase.TMDB_IDS, ("tmdb.ids.",)),
    (BootstrapPhase.CROSSWALK, ("wikidata.crosswalk", "link-crosswalk")),
    (BootstrapPhase.MOVIELENS, ("movielens",)),
)


def _phases_in(journal: list[str]) -> list[BootstrapPhase]:
    """The journal's entries collapsed to the phase each belongs to, in first
    -sighting order, with an entry nothing claims raising rather than being
    silently dropped -- a mapping that fell through would turn a reordered
    phase into a missing one, which reads as a shorter list rather than as a
    wrong one."""
    seen: list[BootstrapPhase] = []
    for entry in journal:
        phase = next(
            (one for one, prefixes in _PHASE_OF if entry.startswith(prefixes)),
            None,
        )
        assert phase is not None, f"no phase claims the journal entry {entry!r}"
        if phase not in seen:
            seen.append(phase)
    return seen


async def test_the_cli_and_the_handler_run_the_same_phase_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The proof that the extraction landed is **behavioural, not
    structural**: the same phases, in the same order, whichever root drove
    them.

    A structural assertion -- "the handler imports `run_bootstrap`" -- is
    satisfied by a handler that imports it and then does something else, and
    it is satisfied forever by a `run_bootstrap` whose arms have drifted from
    the ones `usher bootstrap` reaches. What cannot be satisfied that way is
    an identical journal of window edges, dataset names and the crosswalk
    link, produced twice over the same fakes.

    Three facts about that journal are asserted by name, because each is a
    measured decision the order alone would not pin:

    - **One window, both IMDb passes inside it.** Wrapping each pass
      separately rebuilds `ix_titles_sort_name` and
      `ix_titles_name_lower_year` between them and pays for the rebuild
      twice -- 35.8 s suspended against 40.2 s kept (11.0% faster) with a
      rebuilt pair ~24% smaller, 97 MB against 127 MB
      (`.claude/rules/bootstrap-and-datasets.md`).
    - **`link-crosswalk` immediately after the crosswalk import.** The import
      stores pairs; the link is what attaches them to `titles`.
    - **Every phase in `BootstrapPhase`'s declared order**, asserted against
      the enum rather than against the other driver. **A parity assertion
      cannot see a permutation** -- both roots call one function, so a
      reordered dispatch reorders both journals identically and they still
      match. Measured: moving the `credit-names` arm in front of the `imdb`
      one survived this case until the order was pinned against the enum, and
      the damage is the one Track 2 named -- `credit-names` joins to `titles`
      on `imdb_id`, so ahead of `imdb` it refuses an empty catalog and the
      phase silently does nothing, while behind a TMDb crawl it defers every
      enriched title to TMDb permanently and 203,969 of the 204,335
      >=100-vote titles never gain a `credit_names` at all. (It stales no
      embedding in either position; this sentence said it staled that tier
      until an audit checked it against `fill_credit_names`' own predicate.)
    """
    through_cli = await _journal_of_a_full_bootstrap(
        monkeypatch, tmp_path, through_the_worker=False
    )
    through_worker = await _journal_of_a_full_bootstrap(
        monkeypatch, tmp_path, through_the_worker=True
    )

    assert through_cli, "the premise: driving the dispatch records something"
    assert through_worker == through_cli

    window = through_cli.index("window-open"), through_cli.index("window-close")
    inside = through_cli[window[0] + 1 : window[1]]
    assert inside == ["imdb.title.basics", "imdb.title.ratings"]
    assert through_cli.count("window-open") == 1
    assert through_cli.count("window-close") == 1

    assert through_cli[through_cli.index("wikidata.crosswalk") + 1] == "link-crosswalk"
    assert _phases_in(through_cli) == [
        one for one in BootstrapPhase if one is not BootstrapPhase.ALL
    ]


async def test_one_client_serves_the_whole_run_and_is_closed_however_it_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """One `httpx.AsyncClient` for seven datasets, closed in a `finally`.

    A client per *phase* would defeat connection reuse across the whole run;
    a client per worker *pass* would be built ~17,280 times a day at the
    lane's 5 s floor, which is the arithmetic `build_worker`'s own docstring
    records for a log line. The `finally` is the other half: the phase that
    raises here is `bulk_load_window` itself, which is the one thing in the
    dispatch outside `import_dataset`'s `except UsherPortError` and therefore
    the only way a bootstrap run ends by raising.
    """
    built: list[httpx.AsyncClient] = []

    def recording(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client = _offline_client()
        built.append(client)
        return client

    monkeypatch.setattr(usher.composition, "bulk_client", recording)
    journal: list[str] = []
    settings = _settings(bulk_data_dir=tmp_path)

    await run_bootstrap(
        _JournallingCatalog(journal),
        _JournallingRuns(journal),
        _nothing,
        settings,
        BootstrapPhase.ALL,
        report=lambda _: None,
        events=NullEventPublisher(),
    )
    assert len(built) == 1
    assert built[0].is_closed

    class _WindowRaises(_JournallingCatalog):
        def bulk_load_window(self) -> AbstractAsyncContextManager[None]:
            raise RuntimeError("the window could not be opened")

    with pytest.raises(RuntimeError, match="the window could not be opened"):
        await run_bootstrap(
            _WindowRaises(journal),
            _JournallingRuns(journal),
            _nothing,
            settings,
            BootstrapPhase.IMDB,
            report=lambda _: None,
            events=NullEventPublisher(),
        )
    assert len(built) == 2
    assert built[1].is_closed


async def test_the_worker_reports_a_phase_to_the_log_and_never_to_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`run_bootstrap` takes a report sink because the two roots want the
    same sentences in different places, and this is the half a default
    argument would have got wrong.

    `usher bootstrap` prints; a worker inside the server process must not,
    because its stdout is a log stream and a bare line in it has no level, no
    timestamp and no trace id. The refusal sentence is the one that always
    renders against an empty catalog, so it is what this case reads back.
    """
    monkeypatch.setattr(usher.composition, "bulk_client", _offline_client)
    sink: list[str] = []
    handle = logger.add(lambda message: sink.append(message.record["message"]), level="INFO")
    try:
        queue = FakeJobQueue()
        pipeline = _pipeline_over_fakes(titles=FakeTitleRepository(), queue=queue)
        worker = build_worker(
            _work_for(
                dataclasses.replace(
                    pipeline,
                    bulk=FakeBulkCatalogRepository(),
                    import_runs=FakeImportRunRepository(),
                )
            ),
            _settings(bulk_data_dir=tmp_path),
            provider=None,
            embedder=None,
            client=None,
            registry=_no_sources(),
            user_id=uuid.uuid4(),
        )
        capsys.readouterr()
        await queue.enqueue(
            [
                JobRequest(
                    kind=JobKind.BOOTSTRAP,
                    key=BootstrapPhase.MOVIELENS.value,
                    priority=JobPriority.DEMAND,
                )
            ]
        )
        assert await worker.run_once() == 1
    finally:
        logger.remove(handle)

    assert capsys.readouterr().out == ""
    assert any("titles is empty" in line for line in sink)


async def test_the_bootstrap_handler_publishes_to_the_bus_and_not_to_the_workers_buffer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The one registration in `build_worker` that is handed `pipeline.events`
    rather than `worker.events`, pinned from **both** sides.

    G2 measured that swapping those two objects at a registration site is
    invisible to every unit case of `JobWorker` -- the handler runs, the job
    completes, the frames arrive, and only *when* they arrive differs. That
    blind spot is why the enrich registration has a composition-level case,
    and it is exactly as wide here with the polarity inverted: this producer
    must **not** be deferred.

    `DeferredEventPublisher`'s own docstring sizes its buffer for *"a handful
    of events at most"*, and a bootstrap raises one per committed batch -- 26
    for `--phase imdb`'s title pass alone at the shipped 50,000 batch size. So
    a deferred bootstrap delivers its whole progress bar as a single jump
    after the run it was describing has finished, which is the `0% to 100%`
    failure `ReconcileService._publish_progress` already names, and
    `discard()` on a failing job would throw away frames naming batches that
    really did commit.

    Both assertions are needed and neither implies the other: `is` the bus
    says the right object was passed, and the `DeferredEventPublisher` check is
    what fails if a later reader "fixes" this registration to match the four
    below it. The second used to be spelled `is not worker.events`; since M9's
    W1 the buffer belongs to the *scope* rather than to the worker, so there is
    no such attribute to compare against and the type is what carries the
    claim -- the buffer is the only `EventPublisher` in `src/` a handler can be
    handed that is not a bus.
    """
    monkeypatch.setattr(usher.composition, "bulk_client", _offline_client)
    seen: list[EventPublisher] = []

    async def record(*args: object, **kwargs: object) -> None:
        publisher = kwargs["events"]
        assert isinstance(publisher, EventPublisher)
        seen.append(publisher)

    monkeypatch.setattr(usher.composition, "run_bootstrap", record)
    queue = FakeJobQueue()
    pipeline = _pipeline_over_fakes(titles=FakeTitleRepository(), queue=queue, events=_Recorder())
    worker = build_worker(
        _work_for(pipeline),
        _settings(bulk_data_dir=tmp_path),
        provider=None,
        embedder=None,
        client=None,
        registry=_no_sources(),
        user_id=uuid.uuid4(),
    )
    await queue.enqueue(
        [
            JobRequest(
                kind=JobKind.BOOTSTRAP, key=BootstrapPhase.IMDB.value, priority=JobPriority.DEMAND
            )
        ]
    )

    assert await worker.run_once() == 1, "the premise: the worker claimed the bootstrap job"
    assert len(seen) == 1, "the handler did not reach run_bootstrap exactly once"
    assert seen[0] is pipeline.events, "the handler was not handed the process bus"
    assert not isinstance(seen[0], DeferredEventPublisher), (
        "the bootstrap frames were handed the scope's deferred buffer, which holds them "
        "until the whole phase completes -- a progress bar that jumps from 0% to 100%"
    )


class _Recorder(NullEventPublisher):
    """Distinguishable from every other publisher by identity, which is all
    the case above needs -- `NullEventPublisher()` instances compare equal to
    nothing but themselves, and an assertion spelled against the *class* would
    pass against the scope buffer's inner publisher as readily as against the
    bus itself."""


def test_every_configuration_registers_exactly_the_kinds_it_claims() -> None:
    """`worker_kinds` and `_worker_handlers` are the one pair of lists in
    `src/` that have to agree, and **both failure directions are quiet.**

    `JobWorker` claims `list(self._concurrency)`, whose keys come from
    `worker_kinds`; the callables come from `_worker_handlers`. They cannot be
    one expression, because the handler map needs a `Pipeline` -- i.e. a
    session -- and the claimable kinds have to be known before any session is
    opened. So:

    - a kind in `worker_kinds` with no handler is a `KeyError` **inside a
      claimed job**, which parks nothing and crashes the worker; and
    - a handler with no entry in `worker_kinds` is work nothing ever claims,
      which is M4's *"a job kind whose handler is a stub is a queue that grows
      forever"* arriving through the registration instead.

    Neither is visible from the outside, which is why this walks all **eight**
    provider/embedder/client configurations rather than the two a case would
    naturally reach for: three independent guards make eight states, and the
    interesting ones are the mixed builds nothing else constructs.
    """
    checked = 0
    for provider in (None, FakeMetadataProvider()):
        for model in (None, FakeEmbedder()):
            for llm in (None, FakeLLMClient()):
                pipeline = _pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue())
                handlers = _worker_handlers(
                    pipeline,
                    _settings(),
                    provider=provider,
                    embedder=model,
                    client=llm,
                    registry=_no_sources(),
                    user_id=uuid.uuid4(),
                    events=DeferredEventPublisher(NullEventPublisher()),
                )
                claimed = worker_kinds(provider=provider, embedder=model, client=llm)
                assert set(handlers) == claimed, (
                    f"provider={provider is not None} embedder={model is not None} "
                    f"client={llm is not None}: claims {sorted(k.value for k in claimed)} "
                    f"and registers {sorted(k.value for k in handlers)}"
                )
                checked += 1

    assert checked == 8, f"the premise: all eight configurations were built -- {checked}"


def test_the_concurrency_table_covers_exactly_the_kinds_a_build_claims() -> None:
    """The third list in the same rule, one layer down.

    `worker_concurrency` resolves `KIND_CONCURRENCY` against the deployment's
    global, and a kind missing from it would be a `KeyError` at *build* time
    rather than inside a job -- loud, but only for the configuration that
    registers it, which for `curate` is the one nobody runs by default.

    The clamp is asserted beside it because it is the half nothing else can
    see: a per-kind constant chosen for a bigger box must not quietly override
    an operator who set `USHER_JOB_CONCURRENCY=2`.
    """
    everything = worker_kinds(
        provider=FakeMetadataProvider(), embedder=FakeEmbedder(), client=FakeLLMClient()
    )
    assert everything == set(JobKind), "the premise: a fully-equipped build claims every kind"

    generous = worker_concurrency(_settings(), everything)
    assert set(generous) == everything
    assert generous[JobKind.ENRICH] == _settings().job_concurrency
    assert generous[JobKind.INDEX] == 1

    pinched = worker_concurrency(_settings(job_concurrency=2), everything)
    assert max(pinched.values()) == 2, (
        f"a per-kind constant outran the configured global: {pinched}"
    )


# ---------------------------------------------------------------------------
# The outbound gate's owner (M10 S3; ADR-0040 §4)


_GATED = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Living Room Emby",
    base_url="https://emby.invalid",
    credentials_ref="ref-1",
    device_id=str(new_id()),
)
_SECOND_SERVER = Source(
    id=new_id(),
    kind=SourceKind.EMBY,
    name="Bedroom Emby",
    base_url="https://bedroom.invalid",
    credentials_ref="ref-2",
    device_id=str(new_id()),
)
_GATE_CREDENTIALS = SourceCredentials(username="usher", password=SecretStr("correct-horse-battery"))


def _emby_gate(adapter: SourceAdapter) -> object:
    assert isinstance(adapter, EmbyAdapter), "the premise: the factory built an Emby adapter"
    return adapter._session._limiter


#: Where each `SourceKind`'s adapter keeps the gate it was handed.
#:
#: **A table rather than one `isinstance`, because the hole this closes is a
#: *new class*.** The realistic second construction site for an adapter is not
#: a second `EmbyAdapter(...)` somewhere -- it is `adapters/jellyfin/adapter.py`
#: arriving as a new branch of `ConfiguredSourceAdapterFactory.build` at the
#: `SourceKind` seam (`factory.py`'s own docstring names it), added by someone
#: who forgets `limiter=`. A per-class scan for "one construction site" cannot
#: see a class that does not exist yet; a table keyed by the **enum** can,
#: because `test_every_source_kind_has_a_gate_reader` goes red the moment the
#: member lands and stays red until this entry is written.
_GATE_READERS: dict[SourceKind, Callable[[SourceAdapter], object]] = {
    SourceKind.EMBY: _emby_gate,
}


def _gate_of(adapter: SourceAdapter, kind: SourceKind = SourceKind.EMBY) -> object:
    """The `SourceGate` one adapter paces its outbound calls through.

    Reached through two private attributes because there is no public
    accessor and inventing one purely so a test could read it would be a
    wider API for a narrower reason -- the argument
    `test_the_deployment_tuning_reaches_the_adapter` already makes one
    module over. Returned as `object` because every assertion about it here
    is an **identity** assertion.
    """
    return _GATE_READERS[kind](adapter)


def _source_of(kind: SourceKind, name: str, ref: str) -> Source:
    return Source(
        id=new_id(),
        kind=kind,
        name=name,
        base_url="https://emby.invalid",
        credentials_ref=ref,
        device_id=str(new_id()),
    )


def test_every_source_kind_has_a_gate_reader() -> None:
    """The premise the parametrisation below rests on, asserted rather than
    left to `list(SourceKind)` quietly covering one member.

    A `SourceKind` with no row in `_GATE_READERS` is a kind whose adapter
    nobody has checked shares a gate -- and since `SourceKind` is the seam
    `factory.py` says a Jellyfin adapter arrives at, that is precisely the
    moment the check is wanted.
    """
    assert set(_GATE_READERS) == set(SourceKind), (
        "a `SourceKind` member has no gate reader, so the parametrised case below silently "
        f"stopped covering it: {sorted(k.value for k in set(SourceKind) - set(_GATE_READERS))}"
    )


def _kind_id(kind: object) -> str:
    return kind.value if isinstance(kind, SourceKind) else repr(kind)


@pytest.mark.parametrize("kind", list(SourceKind), ids=_kind_id)
async def test_two_adapters_for_one_source_share_one_gate_and_two_sources_do_not(
    kind: SourceKind,
) -> None:
    """🔴 **The finding S3 exists for: the obvious placement is per request.**

    `adapter_factory` is called from `build_pipeline`, i.e. **once per unit of
    work** -- the server opens a pipeline per lane task, `usher work`'s worker
    opens a scope per claim and per job, and `api/deps.py` builds one per
    request. So a gate held on the `EmbySession` (per adapter) or on the
    `ConfiguredSourceAdapterFactory` (per factory) is a gate per *request*,
    which is `api/deps.py`'s own recorded defect verbatim: *"a request-scoped
    `TmdbClient` gives every concurrent request a fresh bucket, so N in-flight
    requests get N x 30 rps"*. The gate is therefore owned by the composition
    root and keyed by `source.id`, and every pipeline that root opens hands
    out the same one.

    **The second assertion is the positive control and it is not decoration.**
    An implementation that returned one *global* gate satisfies the first
    assertion and halves the configured rate for every source after the
    first -- a two-server household would run at `rate/2` each with nothing
    saying so. A case carrying only the first assertion would ratify it.

    A real `AsyncSession` factory over a real engine, for the reason
    `test_the_candidate_pool_reads_the_pipelines_own_taste_service` gives:
    `create_async_engine` does not connect and nothing here issues a
    statement, so a wiring assertion stays in the unit suite.

    **Parametrised over `SourceKind`, which is one arm today and grows with the
    enum for free.** The alternative considered and declined was a scan
    asserting that each adapter class has one construction site: that closes a
    narrower hole than it looks, because the realistic second site is a *new
    class* at the `SourceKind` seam (`adapters/jellyfin/adapter.py`, added as a
    `factory.build` branch that forgets `limiter=`), which no per-class scan can
    see. `_GATE_READERS` plus `test_every_source_kind_has_a_gate_reader` does:
    the new member is red until somebody says where its adapter keeps the gate,
    and then this case covers it.
    """
    gated = _source_of(kind, "Living Room", "ref-1")
    second_server = _source_of(kind, "Bedroom", "ref-2")
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    try:
        work = unit_of_work(build_session_factory(engine), _settings(), events=NullEventPublisher())
        async with work() as first:
            one = first.adapters.build(gated, _GATE_CREDENTIALS)
        async with work() as second:
            two = second.adapters.build(gated, _GATE_CREDENTIALS)
            elsewhere = second.adapters.build(second_server, _GATE_CREDENTIALS)
        try:
            assert one is not two, (
                "the premise: two pipelines really did build two adapters -- "
                "`SourceAdapterFactory.build`'s contract is that the caller owns each one"
            )
            gate_a = _gate_of(one, kind)
            gate_b = _gate_of(two, kind)
            gate_c = _gate_of(elsewhere, kind)

            assert gate_a is gate_b, (
                "two pipelines from one composition root gave one source two gates, "
                "so the configured rate is multiplied by however many pipelines are open"
            )
            assert gate_a is not gate_c, (
                "two sources sharing one gate is a limiter that halves itself per source"
            )
        finally:
            await one.aclose()
            await two.aclose()
            await elsewhere.aclose()
    finally:
        await engine.dispose()


async def test_every_composition_root_that_dials_a_source_reaches_one_gate_per_source(
    tmp_path: pathlib.Path,
) -> None:
    """The four roots, driven rather than argued (M10 S3's acceptance).

    Each arm is a **real** composition root, spelled the way its own module
    spells it:

    | root | how it composes | arm below |
    |---|---|---|
    | the server's push lane | `LaneSupervisor` reads `create_app`'s one `UnitOfWork` | 1 + 2 |
    | the server's worker lane | the same `UnitOfWork`, a scope per claim and per job | 1 + 2 |
    | an HTTP request | a real one, resolving `get_source_adapter_factory` off the graph | 1 |
    | `usher work` | `unit_of_work(...)`, a scope per job | 2 |
    | `usher sync` | one `build_pipeline`, sources looped inside it | 3 |

    🔴 **The request arm is a request, and as S3 shipped it, it was not.** That
    row read the same then and the arm below it called
    `adapter_factory(settings, app.state.source_gates)` — which is
    `get_source_adapter_factory`'s *body* re-derived here, so it asserted the
    wiring this file writes rather than the wiring `api/deps.py` has. Measured
    on the shipped tree: planting `get_source_adapter_factory` to
    `return adapter_factory(settings, SourceGateRegistry())` — a fresh registry
    per request, i.e. verbatim the defect `api/deps.py`'s `EnrichService`
    comment records and this task exists to remove — passed `ruff`, `mypy`,
    `lint-imports` and **the whole 5,329-case suite**, because every test that
    names that dependency overrides it and `get_source_gates` was executed by
    no test at all. `.claude/rules/testing-discipline.md` has the general
    form: *a dependency every test overrides is a dependency no test covers*.
    So the arm drives a real request through a probe route and reads the
    factory the graph resolved.

    **What "one process" means here, stated plainly because the flattering
    reading is available.** Within one process, one source has one gate however
    many pipelines, lanes, adapters or requests exist -- that is what these
    arms assert. `usher work` and `usher sync` are *separate processes* from the
    server, so their registries are separate from its by construction, and a
    second `usher work` container is a second registry and therefore twice the
    configured rate. That is a capacity decision an operator makes; nothing in a
    process can reach across to another one, and this case does not pretend
    otherwise.
    """
    settings = _settings(
        push_enabled=False,
        worker_enabled=False,
        image_cache_dir=tmp_path / "images",
        source_requests_per_second=0.4,
    )

    # -- 1. the server: the lanes' unit of work and the request path -------
    app = create_app(settings)
    # The request path, driven rather than re-derived. A probe route on the
    # real app, because a `SourceAdapterFactory` cannot come back over HTTP and
    # what this arm needs is the object the graph built:
    # `get_source_adapter_factory` -> `get_source_gates` -> `app.state`. The
    # same shape `tests/integration/test_pipeline_deps.py` uses for the four
    # other dependencies whose wiring no route can show.
    resolved: list[SourceAdapterFactory] = []

    @app.get("/_probe/adapter-factory")
    def _probe(
        adapters: Annotated[SourceAdapterFactory, Depends(get_source_adapter_factory)],
    ) -> dict[str, bool]:
        resolved.append(adapters)
        return {"resolved": True}

    async with LifespanManager(app) as manager:
        gates = app.state.source_gates
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            probe = await client.get("/_probe/adapter-factory")
        assert probe.status_code == 200, f"the probe route did not run: {probe.text}"
        assert len(resolved) == 1, (
            "the premise: the dependency graph resolved exactly one factory, so the gate "
            "asserted below came through `api/deps.py` rather than from this file"
        )
        through_a_request = resolved[0].build(_GATED, _GATE_CREDENTIALS)
        # The lanes, through the supervisor's own unit of work -- the object
        # `_start_lane` (push) and `_run_worker` both open their pipelines
        # from. Two scopes, because the two lanes never share one.
        async with app.state.lanes._work() as push_scope:
            through_the_push_lane = push_scope.adapters.build(_GATED, _GATE_CREDENTIALS)
        async with app.state.lanes._work() as worker_scope:
            through_the_worker_lane = worker_scope.adapters.build(_GATED, _GATE_CREDENTIALS)
        try:
            server_gate = _gate_of(through_a_request)
            assert _gate_of(through_the_push_lane) is server_gate, (
                "the push lane paces independently of the request path, so an admin "
                "status probe and a reconnect gap-closer spend two budgets"
            )
            assert _gate_of(through_the_worker_lane) is server_gate, (
                "the worker lane paces independently of the push lane -- the >=2 gates "
                "per source ADR-0040 §4 measured before S3"
            )
            assert server_gate is gates.gate(_GATED.id, _GATED.name)
            assert server_gate._rate == 0.4, (
                "the premise: these gates carry the configured rate, so the identity "
                "above is not three unthrottled defaults agreeing by accident"
            )
        finally:
            await through_a_request.aclose()
            await through_the_push_lane.aclose()
            await through_the_worker_lane.aclose()

    # -- 2. `usher work`: one registry for the daemon, a scope per job -----
    engine = create_async_engine("postgresql+asyncpg://usher:usher@127.0.0.1:1/usher")
    try:
        work = unit_of_work(build_session_factory(engine), settings, events=NullEventPublisher())
        async with work() as one_job:
            first = one_job.adapters.build(_GATED, _GATE_CREDENTIALS)
        async with work() as another_job:
            second = another_job.adapters.build(_GATED, _GATE_CREDENTIALS)
        try:
            assert _gate_of(first) is _gate_of(second)
            assert _gate_of(first) is not server_gate, (
                "the premise, and the honest half of the claim: a second process is a "
                "second registry -- these two roots are only ever in one process here "
                "because a test is a process that runs everything"
            )
        finally:
            await first.aclose()
            await second.aclose()

        # -- 3. `usher sync`: one pipeline, the sources looped inside it ----
        sync = build_pipeline(AsyncSession(engine), settings)
        walking = sync.adapters.build(_GATED, _GATE_CREDENTIALS)
        watching = sync.adapters.build(_GATED, _GATE_CREDENTIALS)
        elsewhere = sync.adapters.build(_SECOND_SERVER, _GATE_CREDENTIALS)
        try:
            assert _gate_of(walking) is _gate_of(watching), (
                "`usher sync` walks items and then watch state through one pipeline; "
                "two gates there is one command spending twice its own ceiling"
            )
            assert _gate_of(walking) is not _gate_of(elsewhere), (
                "two sources sharing one gate is a limiter that halves itself per source"
            )
        finally:
            await walking.aclose()
            await watching.aclose()
            await elsewhere.aclose()
    finally:
        await engine.dispose()


#: The three composition roots in `usher.cli`, and the call each one builds its
#: registry with. **Keyed by function rather than by command**, because that is
#: what the assertion below can see: `usher push` with no `--probe` reaches
#: `_run_lanes`, which is a root an operator cannot name (ADR-0040 §4 records
#: the correction).
_CLI_ROOTS: dict[str, str] = {
    "_work": "unit_of_work",  # `usher work`
    "_run_lanes": "unit_of_work",  # bare `usher push`
    "_sync": "build_pipeline",  # `usher sync`
}


def _cli_function(module: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The top-level `def`/`async def` named `name`, or an assertion failure."""
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"`usher.cli` has no top-level `{name}` -- this table names a root that has been "
        "renamed or moved, so the assertions below are about nothing"
    )


def _calls_of(root: ast.AST, callee: str) -> tuple[int, int]:
    """`(in the function's own body, inside a nested definition)`.

    The split *is* the assertion. A builder called from the function's own body
    runs once when the command starts; the identical call moved inside a
    `def`, an `async def` or a `lambda` runs once per invocation of that
    closure -- which for a `@asynccontextmanager`-wrapped `work()` is once per
    scope, i.e. per claim and per job.
    """
    own = nested = 0

    def walk(node: ast.AST, inside: bool) -> None:
        nonlocal own, nested
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == callee
            ):
                if inside:
                    nested += 1
                else:
                    own += 1
            walk(child, inside or isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef))

    walk(root, False)
    return own, nested


def test_the_cli_roots_compose_once_rather_than_per_scope() -> None:
    """🔴 **Rows 4 and 5 of the table above are re-derivations, and the defect
    they miss is the one row 3 already shipped with.**

    The four-roots case drives `usher work` and `usher sync` by calling
    `unit_of_work(...)` and `build_pipeline(...)` *in the test* -- which is not
    what its own header promises (*"a **real** composition root, spelled the way
    its own module spells it"*), and is exactly the shape the spec round found
    and repaired for the request arm. Measured on the shipped tree: replacing
    `cli._work`'s `work = unit_of_work(...)` with an
    `@asynccontextmanager`-wrapped `work()` that calls `unit_of_work(...)` fresh
    on every scope -- **a new `SourceGateRegistry` per claim and per job**,
    verbatim the multiplication ADR-0040 §4 exists to remove -- passes `ruff`,
    `ruff format --check`, `mypy`, `lint-imports` and the whole suite.

    **A source scan rather than a drive, and the choice is argued rather than
    assumed.** `cli._work` opens an engine, a metadata provider, an embedder, an
    LLM client and a worker loop before it reaches the line in question, so
    driving it needs a database; and lifting the construction into a helper the
    four-roots case could call would move the boundary rather than close it --
    the plant would simply go in `_work`'s body, one level above the helper, and
    survive again. What distinguishes the correct spelling from the defect is
    **structural**: the builder is called in the root's own body, not inside a
    closure that runs per scope. That is a claim an AST can settle, in the shape
    `test_get_source_gates_is_the_only_reader_of_app_state_source_gates` uses
    over `api/`.

    **A count is deliberately not asserted, and `_sync` is why.** A *second*
    `build_pipeline` in `usher sync` is an equivalent mutant -- both spellings
    were planted and both survive, correctly, because `_sync` opens one adapter
    per source and hands it to both lanes, so the second registry is never
    reached. Doubling a source's rate takes two registries **and** two adapters;
    what carries `usher sync` is the adapter count, which is asserted here as
    itself rather than through a proxy that would kill a mutant nothing is wrong
    with.
    """
    source = pathlib.Path(usher.__file__).parent / "cli.py"
    module = ast.parse(source.read_text(encoding="utf-8"), str(source))

    for root, builder in _CLI_ROOTS.items():
        own, nested = _calls_of(_cli_function(module, root), builder)
        assert own + nested >= 1, (
            f"the premise: `cli.{root}` does not call `{builder}` at all, so the assertion "
            "below is an absence proved by a scan that found nothing"
        )
        assert nested == 0, (
            f"`cli.{root}` calls `{builder}` inside a nested definition, so the registry is "
            f"built once per invocation of that closure rather than once for the process -- "
            f"{nested} of {own + nested} calls. That is a fresh outbound gate per scope, "
            "which is the defect ADR-0040 §4 exists to remove"
        )

    walk, _ = _calls_of(_cli_function(module, "_sync"), "_open_adapter")
    assert walk == 1, (
        "`usher sync` opens more than one adapter per source, so the reconcile walk and the "
        f"watch lane no longer share one -- {walk} calls to `_open_adapter`"
    )


async def test_a_request_without_the_lifespan_is_refused_rather_than_quietly_ungated() -> None:
    """`api/deps.get_source_gates`' `RuntimeError` arm, which nothing ran.

    🔴 **This dependency was executed by no test in the suite** — not one, in
    either direction. Every case that names `get_source_adapter_factory`
    replaces it through `dependency_overrides` (`test_api_playback.py`,
    `test_api_playback_leaks.py`, and three integration files),
    `test_api_health.py::test_readiness_never_touches_a_source` asserts that
    readiness does **not** resolve it, and the arm above reaches it only with a
    started lifespan. So the code path that decides what happens when the
    registry is missing had never been taken, and its message had never been
    read by anything.

    **It matters because the alternative failure is silent.** `app.state` is
    typed `Any` and raises `AttributeError` for a missing name, so the
    defensive `getattr(..., None)` here is what turns "the lifespan did not
    run" into a sentence naming the attribute and the fix, rather than into a
    500 from an attribute lookup — or, if the default had been a fresh
    `SourceGateRegistry()`, into a process that dials a household's server
    completely unthrottled and reports nothing at all. A limiter's missing
    owner has to be loud.

    Driven as a real request through `httpx.ASGITransport` **without**
    `LifespanManager`, because that is precisely the configuration the message
    is written for: the transport speaks HTTP to the app and never sends it a
    lifespan event, so `create_app`'s startup block never runs.
    """
    settings = _settings(push_enabled=False, worker_enabled=False)
    app = create_app(settings)

    @app.get("/_probe/gates")
    def _probe(gates: Annotated[SourceGateRegistry, Depends(get_source_gates)]) -> dict[str, bool]:
        return {"resolved": True}  # pragma: no cover -- the point is that it does not

    assert not hasattr(app.state, "source_gates"), (
        "the premise: the lifespan really has not run, so the refusal below is the "
        "dependency's own and not an artefact of this case clearing state"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="source_gates is not set") as caught:
            await client.get("/_probe/gates")

    assert "LifespanManager" in str(caught.value), (
        "the message names the attribute and not the remedy, which is the half a "
        "reader needs -- `asgi_lifespan.LifespanManager` is what a bare ASGI "
        "transport is missing"
    )


def test_get_source_gates_is_the_only_reader_of_app_state_source_gates() -> None:
    """The registry reaches a request through the dependency and no other way.

    A second reader — a router doing `request.app.state.source_gates` inline —
    would resolve, pass mypy and behave identically today, and it is exactly
    how the `RuntimeError` above stops being the only answer to a missing
    lifespan. Asserted as a source scan rather than argued, in the shape
    `test_no_outbound_http_call_escapes_a_recorded_decision` uses one package
    over.
    """
    api = pathlib.Path(usher.__file__).parent / "api"
    readers = sorted(
        path.relative_to(api).as_posix()
        for path in api.rglob("*.py")
        if "state.source_gates" in path.read_text(encoding="utf-8")
    )
    assert readers == ["app.py", "deps.py"], (
        "`app.state.source_gates` is written by `create_app`'s lifespan and read by "
        f"`get_source_gates`, and by nothing else: {readers}"
    )
