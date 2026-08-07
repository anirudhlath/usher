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

import io
import os
import uuid
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from loguru import logger
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.fakes.collection_repository import FakeCollectionRepository
from tests.fakes.credit_repository import FakeCreditRepository
from tests.fakes.embedding import FakeEmbedder
from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.person_repository import FakePersonRepository
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.taste_repository import FakeTasteRepository
from tests.fakes.title_embedding_repository import FakeTitleEmbeddingRepository
from tests.fakes.title_repository import FakeTitleRepository
from usher.composition import (
    Pipeline,
    build_enrich_service,
    build_pipeline,
    build_worker,
    embedder,
    metadata_provider,
)
from usher.config import Settings
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind
from usher.domain.title import Title
from usher.ports.embedding import Embedder
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobQueue
from usher.ports.repository import TitleRepository
from usher.services.handlers import SourceBinding
from usher.services.rows import ROW_PROVIDERS


def _pipeline_over_fakes(*, titles: TitleRepository, queue: JobQueue) -> Pipeline:
    """A `Pipeline` carrying only the fields `build_enrich_service` reads.

    `cast` rather than a fake per field: every other slot is genuinely unused
    on this path, and filling twelve of them would make the case read as a
    test of `build_pipeline` rather than of one wiring decision.
    """

    async def commit() -> None:
        return None

    unused = cast(Any, None)
    # `build_derive_service` reads three more slots than
    # `build_enrich_service` does, so they are real fakes rather than
    # `unused`: `build_worker` constructs the service eagerly, and a `None`
    # there fails at construction rather than at the one wiring decision
    # under test.
    people = FakePersonRepository()
    titles_store = titles if isinstance(titles, FakeTitleRepository) else FakeTitleRepository()
    return Pipeline(
        sources=unused,
        credentials=unused,
        titles=titles,
        matching=unused,
        media_items=unused,
        episodes=FakeEpisodeRepository(),
        watch_states=unused,
        payloads=FakeRawPayloadStore(),
        runs=unused,
        queue=queue,
        embeddings=FakeTitleEmbeddingRepository(),
        neighbors=unused,
        taste_rows=FakeTasteRepository(),
        people=people,
        credits=FakeCreditRepository(people, titles_store),
        collections=FakeCollectionRepository(),
        adapters=unused,
        matcher=unused,
        ingest=unused,
        reconcile=unused,
        watch=unused,
        search=unused,
        similar=unused,
        taste=unused,
        pool=unused,
        row_providers=ROW_PROVIDERS,
        events=NullEventPublisher(),
        commit=commit,
    )


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

    service = build_enrich_service(pipeline, _settings(), FakeMetadataProvider())
    await service.enrich(title.id)

    assert (await queue.depth())[JobKind.INDEX] == 1


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

    The `ENRICH` half is asserted alongside it, so the two guards cannot
    drift into "one guarded, one not", and `MATCH` is asserted so an
    implementation registering *nothing* cannot pass.
    """
    worker = build_worker(
        _pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue()),
        _settings(),
        provider=None,
        embedder=None,
        resolve=_never_resolves,
        user_id=uuid.uuid4(),
    )

    assert JobKind.INDEX not in worker.registered_kinds
    assert JobKind.ENRICH not in worker.registered_kinds
    assert JobKind.MATCH in worker.registered_kinds


def test_a_worker_with_an_embedder_registers_the_index_handler() -> None:
    """The control that makes the case above evidence rather than a
    tautology: without it, an implementation registering *nothing* passes."""
    worker = build_worker(
        _pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue()),
        _settings(),
        provider=None,
        embedder=FakeEmbedder(),
        resolve=_never_resolves,
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
        _pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue()),
        _settings(),
        provider=None,
        embedder=FakeEmbedder(),
        resolve=_never_resolves,
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
        _pipeline_over_fakes(titles=FakeTitleRepository(), queue=FakeJobQueue()),
        _settings(),
        provider=FakeMetadataProvider(),
        embedder=None,
        resolve=_never_resolves,
        user_id=uuid.uuid4(),
    )

    assert JobKind.DERIVE in worker.registered_kinds
    assert JobKind.INDEX not in worker.registered_kinds


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
            pipeline,
            _settings(),
            provider=None,
            embedder=model,
            resolve=_never_resolves,
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
