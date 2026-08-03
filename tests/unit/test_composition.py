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
from collections.abc import Iterator
from typing import Any, cast

import pytest
from loguru import logger
from pydantic import SecretStr

from tests.fakes.episode_repository import FakeEpisodeRepository
from tests.fakes.job_queue import FakeJobQueue
from tests.fakes.metadata_provider import FakeMetadataProvider
from tests.fakes.raw_payload_store import FakeRawPayloadStore
from tests.fakes.title_repository import FakeTitleRepository
from usher.composition import Pipeline, build_enrich_service, metadata_provider
from usher.config import Settings
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.jobs import JobKind
from usher.domain.title import Title
from usher.ports.events import NullEventPublisher
from usher.ports.jobs import JobQueue
from usher.ports.repository import TitleRepository


def _pipeline_over_fakes(*, titles: TitleRepository, queue: JobQueue) -> Pipeline:
    """A `Pipeline` carrying only the fields `build_enrich_service` reads.

    `cast` rather than a fake per field: every other slot is genuinely unused
    on this path, and filling twelve of them would make the case read as a
    test of `build_pipeline` rather than of one wiring decision.
    """

    async def commit() -> None:
        return None

    unused = cast(Any, None)
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
        adapters=unused,
        matcher=unused,
        ingest=unused,
        reconcile=unused,
        watch=unused,
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
