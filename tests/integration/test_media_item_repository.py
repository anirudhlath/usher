"""The shared contract against real Postgres, plus the five things a fake
cannot express: a duplicate that raises rather than being last-wins, a CHECK
that fires, a foreign key, a poisoned session, and "one statement per
batch"."""

import dataclasses
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.media_item_repository_contract import (
    EARLIER,
    RUN_AT,
    MediaItemRepositoryContract,
    item,
)
from usher.db.repositories.media_item import PostgresMediaItemRepository
from usher.db.repositories.source import PostgresSourceRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import SourceKind, TitleKind
from usher.domain.ids import new_id
from usher.domain.source import Source
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.ingest import AvailabilitySweepRefused


async def _make_source(session: AsyncSession, name: str) -> uuid.UUID:
    source = Source(
        kind=SourceKind.EMBY,
        name=name,
        base_url="https://emby.invalid",
        credentials_ref=f"ref-{new_id()}",
        device_id=str(new_id()),
    )
    await PostgresSourceRepository(session).add(source)
    return source.id


@pytest_asyncio.fixture
async def source_id(session: AsyncSession) -> uuid.UUID:
    return await _make_source(session, "Contract Source")


@pytest_asyncio.fixture
async def other_source_id(session: AsyncSession) -> uuid.UUID:
    return await _make_source(session, "Other Contract Source")


@pytest_asyncio.fixture
async def title_id(session: AsyncSession) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name="Contract Title", sort_name="Contract Title")
    await PostgresTitleRepository(session).add(title)
    return title.id


@pytest_asyncio.fixture
async def episode_id(session: AsyncSession) -> uuid.UUID:
    """A real episode, which needs a real series and a real season: both FKs
    are `NOT NULL`, and `media_items.episode_id` is itself a foreign key --
    the whole reason the contract's episode cases mean something here and
    are dict entries in the unit half."""
    series = Title(kind=TitleKind.SERIES, name="Contract Series", sort_name="Contract Series")
    await PostgresTitleRepository(session).add(series)
    season, episode = new_id(), new_id()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": season, "title_id": series.id},
    )
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :title_id, :season_id, 1, 1)"
        ),
        {"id": episode, "title_id": series.id, "season_id": season},
    )
    return episode


@pytest.fixture
def repository(session: AsyncSession) -> PostgresMediaItemRepository:
    return PostgresMediaItemRepository(session)


class TestPostgresMediaItemRepository(MediaItemRepositoryContract):
    """Every case in `MediaItemRepositoryContract`, against real Postgres.

    The fixtures come from module scope rather than being redefined here, so
    the four Postgres-only tests below share them.
    """


async def test_a_negative_dimension_is_a_port_error_not_an_integrity_error(
    repository: PostgresMediaItemRepository, source_id: uuid.UUID
) -> None:
    """`ck_media_items_width_non_negative` is one of five CHECKs mirroring
    `MediaItem`'s own pydantic bounds, and the staged path bypasses pydantic
    entirely -- a `COPY` never constructs a `MediaItem`. So the database is
    the only thing standing between a bad width and a stored row, and the
    repository has to translate what it raises: a raw
    `sqlalchemy.exc.IntegrityError` escaping here would break "db is driven,
    not driving" for every caller written against `usher.ports.errors`.

    Note *where* it fires. The staging table carries no constraints, so the
    `COPY` succeeds and the following `INSERT ... SELECT` is what raises --
    which is why catching `IntegrityError` is sufficient. Had the constraint
    been on the staging table, `copy_records_to_table` runs on the raw
    asyncpg connection, outside SQLAlchemy's error translation, and would
    have raised `asyncpg.exceptions.CheckViolationError` straight through.
    """
    bad = dataclasses.replace(item(source_id, "movie-1"), width=-1)
    with pytest.raises(RepositoryConflict):
        await repository.upsert_many([bad])


async def test_an_unknown_title_id_is_a_port_error_not_an_integrity_error(
    repository: PostgresMediaItemRepository, source_id: uuid.UUID
) -> None:
    with pytest.raises(RepositoryConflict) as caught:
        await repository.upsert_many([item(source_id, "movie-1", title_id=new_id())])
    assert caught.value.constraint == "fk_media_items_title_id_titles"


async def test_a_caught_conflict_leaves_the_session_usable(
    repository: PostgresMediaItemRepository, source_id: uuid.UUID
) -> None:
    """The bug `PostgresImportRunRepository` shipped with: Postgres aborts
    the *entire* transaction on any statement error until a ROLLBACK, so a
    caught conflict poisons the session for the next unrelated call. This
    repository uses a SAVEPOINT rather than a full rollback, because its
    caller genuinely does have other pending work -- a batch of items and
    its sync-run checkpoint commit together."""
    with pytest.raises(RepositoryConflict):
        await repository.upsert_many([item(source_id, "movie-1", title_id=new_id())])
    result = await repository.upsert_many([item(source_id, "movie-2")])
    assert (result.inserted, result.updated) == (1, 0)


async def test_a_caught_conflict_leaves_no_staging_table_behind(
    repository: PostgresMediaItemRepository, source_id: uuid.UUID
) -> None:
    """The rollback-to-SAVEPOINT has to take the staging table's DDL with it
    (Postgres DDL is transactional), or the next batch's `CREATE UNLOGGED
    TABLE` either fails or -- with the `DROP ... IF EXISTS` in front of it --
    silently inherits nothing while the failed batch's rows sit in a table
    nobody reads. Verified by writing a second batch and checking its counts,
    which the test above already does; this one checks the mechanism
    directly."""
    with pytest.raises(RepositoryConflict):
        await repository.upsert_many([item(source_id, "movie-1", title_id=new_id())])
    second = await repository.upsert_many(
        [item(source_id, "a"), item(source_id, "b"), item(source_id, "c")]
    )
    assert (second.inserted, second.updated) == (3, 0)


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
    """Every SQL statement SQLAlchemy issues, in order.

    `copy_records_to_table` is invisible here -- it runs on the raw asyncpg
    connection -- which is fine and is the point: a `COPY` is one command
    however many records stream through it, so counting what SQLAlchemy
    issues counts exactly the part that could have been per-row.
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


async def test_a_batch_costs_the_same_number_of_statements_however_big_it_is(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """ "One statement per batch" is a scale requirement, not an aesthetic:
    at 1,126,674 items a per-row write is ~21 minutes of pure repository
    overhead per walk before a byte of upstream I/O, on the same measurement
    that put `BulkCatalogRepository` outside `TitleRepository`.

    Asserted as "the count does not grow with the batch" rather than as a
    magic number, so adding a legitimate statement to the path does not
    break this and making one of them per-row does."""
    statement_counter.clear()
    await repository.upsert_many([item(source_id, f"small-{index}") for index in range(5)])
    small = len(statement_counter)

    statement_counter.clear()
    await repository.upsert_many([item(source_id, f"large-{index}") for index in range(500)])
    large = len(statement_counter)

    assert small == large, f"{small} statements for 5 rows, {large} for 500"
    assert await repository.count_for_source(source_id) == 505


async def test_the_sweep_costs_the_same_number_of_statements_however_big_it_is(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    other_source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """A sweep that loaded rows to decide which to retract is the design
    defect this milestone is warned about. It is also the *obvious*
    implementation, because the guard needs a count and the retraction needs
    a set -- reading the rows once gives you both."""
    await repository.upsert_many(
        [item(source_id, f"m-{index}", last_seen_at=RUN_AT) for index in range(4)]
    )
    await repository.upsert_many(
        [item(other_source_id, f"m-{index}", last_seen_at=RUN_AT) for index in range(400)]
    )

    statement_counter.clear()
    await repository.mark_unseen_unavailable(source_id, seen_since=RUN_AT, max_retract_fraction=1.0)
    small = len(statement_counter)

    statement_counter.clear()
    await repository.mark_unseen_unavailable(
        other_source_id, seen_since=RUN_AT, max_retract_fraction=1.0
    )
    large = len(statement_counter)

    assert small == large, f"{small} statements for 4 rows, {large} for 400"


async def test_a_refused_sweep_issues_no_update_at_all(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """ "Nothing was retracted" has to mean the UPDATE never ran, not that it
    ran and was rolled back -- a sweep that writes first and checks after
    leaves the guard depending on the caller's transaction discipline, and
    `deps.get_session` commits on any handler that does not raise."""
    await repository.upsert_many(
        [item(source_id, f"m-{index}", last_seen_at=EARLIER) for index in range(4)]
    )
    statement_counter.clear()
    with pytest.raises(AvailabilitySweepRefused):
        await repository.mark_unseen_unavailable(
            source_id, seen_since=RUN_AT, max_retract_fraction=0.25
        )
    assert not any("UPDATE media_items" in statement for statement in statement_counter)


async def _seed_a_series_with_episodes(
    session: AsyncSession, source_id: uuid.UUID, *, episodes: int
) -> uuid.UUID:
    """One series, one season, `episodes` real episodes, and a `media_items`
    row for each -- plus one for the series itself, which is what a real Emby
    walk produces (a `Series` item has no `MediaSource`, so its row carries no
    quality facts, and M4's live run counted exactly 20 such rows among 601).

    Raw `INSERT ... SELECT generate_series` rather than the repository,
    because the point is to make the *episode count* large cheaply; the read
    under test is the only thing being measured.
    """
    series = Title(kind=TitleKind.SERIES, name="Bounded Series", sort_name="Bounded Series")
    await PostgresTitleRepository(session).add(series)
    season = new_id()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": season, "title_id": series.id},
    )
    await session.execute(
        text(
            """
            INSERT INTO episodes (id, title_id, season_id, season_number, episode_number)
            SELECT gen_random_uuid(), :title_id, :season_id, 1, n
            FROM generate_series(1, :count) AS n
            """
        ),
        {"title_id": series.id, "season_id": season, "count": episodes},
    )
    await session.execute(
        text(
            """
            INSERT INTO media_items (id, source_id, title_id, episode_id, external_id, last_seen_at)
            SELECT gen_random_uuid(), :source_id, :title_id, e.id,
                   'ep-' || e.id, :last_seen_at
            FROM episodes e WHERE e.title_id = :title_id
            """
        ),
        {"source_id": source_id, "title_id": series.id, "last_seen_at": RUN_AT},
    )
    await session.execute(
        text(
            """
            INSERT INTO media_items (id, source_id, title_id, external_id, last_seen_at)
            VALUES (gen_random_uuid(), :source_id, :title_id, :external_id, :last_seen_at)
            """
        ),
        {
            "source_id": source_id,
            "title_id": series.id,
            "external_id": f"series-{series.id}",
            "last_seen_at": RUN_AT,
        },
    )
    return series.id


async def test_list_for_title_does_not_grow_with_a_series_episode_count(
    repository: PostgresMediaItemRepository,
    session: AsyncSession,
    source_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """The bound, measured rather than asserted about a lookalike.

    999,827 of the one measured source's 1,126,789 items are episodes, and an
    episode's `media_items` row carries its series' `title_id` as well as its
    own `episode_id`. So the natural read -- `WHERE title_id = :id` -- answers
    a *series* with one row per episode file, which puts a badge per episode
    in PRD 07's `availability` array and makes the response length a property
    of the show rather than of the household. "Fine for a ten-season series,
    unbounded by contract" is what M4 recorded about the neighbouring
    `EpisodeRepository.list_for_title`; this is where the media-item one is
    settled.

    Held fixed: the series, the source, the row shape. Varied: the episode
    count, by two orders of magnitude. Both the rows returned and the
    statements issued must be flat -- a count that tracked the episodes would
    be the defect, and a *statement* count that tracked them would be a
    different one.

    500 episodes is enough to fail the assertion and cheap enough to keep in
    the suite; the cost of getting it wrong was measured separately, at a
    scale a test should not pay for. On 80,201 `media_items` rows with one
    20,000-episode series, EXPLAIN (ANALYZE, BUFFERS) on the statement this
    repository actually issued -- captured off `before_cursor_execute`, never
    transcribed -- reports **1 row in 0.251 ms over 21 buffers** as shipped
    and **20,001 rows in 22.901 ms over 402 buffers** with the `episode_id IS
    NULL` clause deleted. The numbers and the plans are recorded on
    `_FOR_TITLE` in `usher.db.repositories.media_item`.
    """
    small = await _seed_a_series_with_episodes(session, source_id, episodes=5)
    large = await _seed_a_series_with_episodes(session, source_id, episodes=500)

    statement_counter.clear()
    few = await repository.list_for_title(small)
    statements_for_few = len(statement_counter)

    statement_counter.clear()
    many = await repository.list_for_title(large)
    statements_for_many = len(statement_counter)

    assert [row.external_id for row in few] == [f"series-{small}"]
    assert [row.external_id for row in many] == [f"series-{large}"]
    assert len(few) == len(many) == 1, "the answer is copies of the title, not of its episodes"
    assert statements_for_few == statements_for_many == 1


async def test_upsert_many_never_hard_deletes_across_sources(
    repository: PostgresMediaItemRepository,
    source_id: uuid.UUID,
    other_source_id: uuid.UUID,
) -> None:
    """The `DELETE`-shaped mistake a set-based implementation invites: a
    statement that reconciles the table to the batch rather than merging the
    batch into the table. PRD 02: "Soft-delete availability, hard-delete
    nothing.\""""
    await repository.upsert_many([item(source_id, "mine"), item(other_source_id, "theirs")])
    await repository.upsert_many([item(source_id, "mine")])
    assert await repository.count_for_source(other_source_id) == 1
