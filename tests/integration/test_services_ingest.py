"""`IngestService` against the real repositories, for the four things its
port fakes structurally cannot express.

The unit suite (`tests/unit/test_services_ingest.py`) runs this service
against dicts. Dicts have no foreign keys, and two of this service's steps
exist *only* to satisfy one: `resolve_seasons` and `resolve_episodes` are
what turn a freshly-minted UUIDv7 into the id the catalog actually stored,
and skipping either writes a `season_id`/`episode_id` naming a row that does
not exist. Measured directly -- deleting both resolves leaves all 24 unit
cases green, and fails here on
`fk_media_items_episode_id_episodes` / `fk_episodes_season_id_seasons`.

Also here rather than there: the second walk of a series the pipeline itself
stubbed (one `titles` table, read through two ports, where the fakes keep two
dicts), and one batch really costing one round trip per stage.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.jobs import PostgresJobQueue
from usher.db.repositories.matching import PostgresTitleMatchRepository
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import EnrichmentState, SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.jobs import JobKind
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.source import SourceItem, SourceItemKind
from usher.services.ingest import IngestService
from usher.services.matching import MatchService

RUN_AT = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)

SERIES = SourceItem(
    external_id="series-1",
    name="Example Series",
    kind=SourceItemKind.SERIES,
    year=2011,
    provider_ids={"tvdb": "121361"},
)
EPISODE = SourceItem(
    external_id="episode-1",
    name="Kissed by Fire",
    kind=SourceItemKind.EPISODE,
    provider_ids={"imdb": "tt2178782"},
    container="mkv",
    series_external_id="series-1",
    season_number=3,
    episode_number=5,
)


@pytest_asyncio.fixture
async def source_id(session: AsyncSession) -> uuid.UUID:
    source = Source(
        kind=SourceKind.EMBY,
        name="Ingest Source",
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    await PostgresSourceRepository(session).add(source)
    return source.id


@pytest_asyncio.fixture
async def media_items(session: AsyncSession) -> AsyncIterator[PostgresMediaItemRepository]:
    yield PostgresMediaItemRepository(session)


@pytest.fixture
def service(session: AsyncSession) -> IngestService:
    titles = PostgresTitleRepository(session)
    matching = PostgresTitleMatchRepository(session)
    queue = PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0)
    return IngestService(
        matcher=MatchService(titles=titles, matching=matching, queue=queue),
        matching=matching,
        media_items=PostgresMediaItemRepository(session),
        episodes=PostgresEpisodeRepository(session),
        queue=queue,
    )


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues, so "one round trip per stage"
    is measured rather than asserted about a fake's call counter.

    Same shape as `tests/integration/test_media_item_repository.py`'s, and
    with the same caveat: `copy_records_to_table` runs on the raw asyncpg
    connection and is invisible here, which is the point -- a `COPY` is one
    command however many records stream through it.
    """
    seen: list[str] = []

    def record(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


async def test_an_episode_is_attached_to_real_season_and_episode_rows(
    service: IngestService,
    media_items: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """The whole point of the two resolves. `episodes.season_id` and
    `media_items.episode_id` are both real foreign keys, so an id the service
    minted but the catalog did not store is an `IntegrityError` here and a
    perfectly happy dict entry in the unit suite."""
    await service.ingest_batch(source_id, [SERIES, EPISODE], observed_at=RUN_AT)
    stored = await media_items.get_by_external_id(source_id, "episode-1")
    assert stored is not None
    assert stored.episode_id is not None
    row = (
        await session.execute(
            text("SELECT season_id, season_number, episode_number FROM episodes WHERE id = :id"),
            {"id": stored.episode_id},
        )
    ).one()
    assert (row.season_number, row.episode_number) == (3, 5)
    seasons = (
        await session.execute(text("SELECT id FROM seasons WHERE id = :id"), {"id": row.season_id})
    ).all()
    assert len(seasons) == 1


async def test_re_ingesting_an_episode_keeps_the_stored_ids(
    service: IngestService,
    media_items: PostgresMediaItemRepository,
    source_id: uuid.UUID,
) -> None:
    """The mutation the unit suite cannot see: skipping either resolve and
    trusting the freshly-minted UUIDv7. On the *second* walk that id names no
    row, so `episodes.season_id` and then `media_items.episode_id` both point
    at nothing. A dict stores that happily; Postgres does not."""
    await service.ingest_batch(source_id, [SERIES, EPISODE], observed_at=RUN_AT)
    first = await media_items.get_by_external_id(source_id, "episode-1")
    await service.ingest_batch(source_id, [SERIES, EPISODE], observed_at=RUN_AT)
    second = await media_items.get_by_external_id(source_id, "episode-1")
    assert first is not None and second is not None
    assert first.episode_id == second.episode_id
    assert first.title_id == second.title_id


async def test_a_second_walk_reuses_the_stub_the_first_walk_created(
    service: IngestService,
    media_items: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """`titles` is one table read through two ports. `TitleRepository.add`
    flushes, so the stub the match stage wrote on walk one is visible to walk
    two's `match_by_provider_ids` -- and if it were not, walk two would try to
    create it again and conflict on `ix_titles_tvdb_id`. The fakes kept two
    dicts and reproduced exactly that failure, which is why they no longer
    do."""
    await service.ingest_batch(source_id, [SERIES], observed_at=RUN_AT)
    first = await media_items.get_by_external_id(source_id, "series-1")
    await service.ingest_batch(source_id, [SERIES], observed_at=RUN_AT)
    second = await media_items.get_by_external_id(source_id, "series-1")
    assert first is not None and second is not None
    assert first.title_id == second.title_id
    count = (
        await session.execute(text("SELECT count(*) FROM titles WHERE tvdb_id = 121361"))
    ).scalar_one()
    assert count == 1


async def test_a_batch_of_episodes_costs_a_bounded_number_of_statements(
    service: IngestService,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """The scale property, measured against real SQL rather than a fake's
    call counter. 200 episodes across two series in one page: the statement
    count must not grow with the page.

    Not an exact number -- the staged `COPY` path issues DDL plus a
    `SAVEPOINT` per upsert, and pinning the total would break on any
    unrelated change to `usher.db.staging`. The property is that 200 episodes
    and 20 cost the same.
    """
    other_series = SourceItem(
        external_id="series-2",
        name="Other Series",
        kind=SourceItemKind.SERIES,
        year=2015,
        provider_ids={"tvdb": "999999"},
    )
    await service.ingest_batch(source_id, [SERIES, other_series], observed_at=RUN_AT)

    def _episodes(count: int, offset: int) -> list[SourceItem]:
        return [
            SourceItem(
                external_id=f"e{offset + index}",
                name=f"Episode {offset + index}",
                kind=SourceItemKind.EPISODE,
                series_external_id="series-1" if index % 2 else "series-2",
                season_number=1,
                episode_number=offset + index,
            )
            for index in range(count)
        ]

    statement_counter.clear()
    await service.ingest_batch(source_id, _episodes(20, 0), observed_at=RUN_AT)
    small = len(statement_counter)
    statement_counter.clear()
    await service.ingest_batch(source_id, _episodes(200, 1000), observed_at=RUN_AT)
    large = len(statement_counter)
    assert small == large, f"{small} statements for 20 episodes, {large} for 200"


async def test_a_walk_enqueues_enrichment_only_for_what_needs_it(
    service: IngestService,
    source_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """`enrichment_states` against real SQL, through the service. An
    already-enriched title must produce no job -- a nightly walk that
    enqueued 1,126,674 of them makes the queue permanently the size of the
    library."""
    enriched = Title(
        kind=TitleKind.SERIES,
        name="Example Series",
        sort_name="Example Series",
        year=2011,
        tvdb_id=121361,
        enrichment_state=EnrichmentState.ENRICHED,
    )
    await PostgresTitleRepository(session).add(enriched)
    await service.ingest_batch(source_id, [SERIES, EPISODE], observed_at=RUN_AT)
    depth = await PostgresJobQueue(session, max_attempts=5, backoff_seconds=30.0).depth()
    assert depth[JobKind.ENRICH] == 0
    assert depth[JobKind.MATCH] == 0
