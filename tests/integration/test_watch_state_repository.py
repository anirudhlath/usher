"""The shared contract against real Postgres -- the half with teeth."""

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.watch_state_repository_contract import (
    LATER,
    WALK_AT,
    WatchStateRepositoryContract,
    WatchStateRepositoryInProgressContract,
    merge,
    write,
)
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import TitleKind, WatchStateOrigin
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict


@pytest.fixture
def repository(session: AsyncSession) -> PostgresWatchStateRepository:
    return PostgresWatchStateRepository(session)


@pytest_asyncio.fixture
async def user_id(session: AsyncSession) -> uuid.UUID:
    identifier = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": identifier, "name": f"user-{identifier}"},
    )
    return identifier


@pytest_asyncio.fixture
async def other_user_id(session: AsyncSession) -> uuid.UUID:
    """A second household member.

    so `list_in_progress`' `user_id` predicate has something to exclude.

    On a single-user deployment -- i.e. every deployment during development -- a lost
    `WHERE user_id` is invisible.
    """
    identifier = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": identifier, "name": f"user-{identifier}"},
    )
    return identifier


@pytest_asyncio.fixture
async def title_id(session: AsyncSession) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name="Contract Title", sort_name="Contract Title")
    await PostgresTitleRepository(session).add(title)
    return title.id


@pytest_asyncio.fixture
async def other_title_id(session: AsyncSession) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name="Other Title", sort_name="Other Title")
    await PostgresTitleRepository(session).add(title)
    return title.id


@pytest_asyncio.fixture
async def third_title_id(session: AsyncSession) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name="Third Title", sort_name="Third Title")
    await PostgresTitleRepository(session).add(title)
    return title.id


@pytest_asyncio.fixture
async def episode_series_id(session: AsyncSession) -> uuid.UUID:
    """The series `episode_id` and every id in `episode_ids` hang off.

    Separate from the episodes themselves because `list_recent` rolls an
    episode up to *this* id through `episodes.title_id`, so a case asserting
    the rollup needs to name it.
    """
    series = Title(kind=TitleKind.SERIES, name="Contract Series", sort_name="Contract Series")
    await PostgresTitleRepository(session).add(series)
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": new_id(), "title_id": series.id},
    )
    return series.id


async def _add_episode(session: AsyncSession, series_id: uuid.UUID, number: int) -> uuid.UUID:
    """One real episode of `series_id`'s season 1.

    `episodes.season_id` and `episodes.title_id` are both `NOT NULL` with
    `ON DELETE CASCADE`, so neither can be invented.
    """
    identifier = new_id()
    season = (
        await session.execute(
            text("SELECT id FROM seasons WHERE title_id = :title_id AND season_number = 1"),
            {"title_id": series_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number) "
            "VALUES (:id, :title_id, :season_id, 1, :number)"
        ),
        {"id": identifier, "title_id": series_id, "season_id": season, "number": number},
    )
    return identifier


@pytest_asyncio.fixture
async def episode_id(session: AsyncSession, episode_series_id: uuid.UUID) -> uuid.UUID:
    """A real episode, which needs a real series and a real season.

    both FKs are `ON DELETE CASCADE` and neither is nullable.
    """
    return await _add_episode(session, episode_series_id, 1)


@pytest_asyncio.fixture
async def episode_ids(session: AsyncSession, episode_series_id: uuid.UUID) -> list[uuid.UUID]:
    """Ten episodes of one series, which is what makes the dedup observable.

    without it a household that watched ten episodes seeds ten identical "Because you
    watched" rows.
    """
    return [await _add_episode(session, episode_series_id, number) for number in range(2, 12)]


class TestPostgresWatchStateRepository(
    WatchStateRepositoryContract, WatchStateRepositoryInProgressContract
):
    """Every case in `WatchStateRepositoryContract`, against real Postgres."""


async def test_an_unknown_title_id_is_a_port_error_not_an_integrity_error(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID
) -> None:
    with pytest.raises(RepositoryConflict) as caught:
        await repository.merge_from_source([merge(user_id, new_id())])
    assert caught.value.constraint == "fk_watch_states_title_id_titles"


async def test_a_caught_conflict_leaves_the_session_usable(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """Postgres aborts the entire transaction on any statement error until a ROLLBACK.

    so without a SAVEPOINT a caught conflict poisons the session for the caller's next,
    unrelated call -- and this repository's caller commits a batch of merges together
    with its sync-run checkpoint.
    """
    with pytest.raises(RepositoryConflict):
        await repository.merge_from_source([merge(user_id, new_id())])
    assert await repository.merge_from_source([merge(user_id, title_id)]) == 1


async def test_a_failed_batch_writes_none_of_itself(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """Four statements per batch means four chances to half-apply.

    The SAVEPOINT is what makes the batch atomic across all of them, so a caller that
    retries a corrected batch is not blocked by an `updated_at` the failed attempt
    already wrote.
    """
    with pytest.raises(RepositoryConflict):
        await repository.merge_from_source([merge(user_id, title_id), merge(user_id, new_id())])
    assert await repository.get_for_title(user_id, title_id) is None


async def test_a_client_write_to_an_unknown_title_is_a_port_error_not_an_integrity_error(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID
) -> None:
    with pytest.raises(RepositoryConflict) as caught:
        await repository.set_from_client(write(user_id, new_id()))
    assert caught.value.constraint == "fk_watch_states_title_id_titles"


async def test_a_client_writes_row_refusal_answers_repository_conflict_not_a_raw_dbapierror(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """`WatchState.position_seconds` is `Field(default=0.

    ge=0)` with no ceiling against an `integer` column -- `db-and-sql.md`'s "field
    bounded on fewer sides than the column" shape.

    `2**31` is refused client-side by asyncpg's own binary encoder as an unclassified
    `DBAPIError`, which `except IntegrityError` alone does not catch; `is_row_refusal`,
    inside `refusals_as_conflict`, is what has to.
    """
    with pytest.raises(RepositoryConflict):
        await repository.set_from_client(write(user_id, title_id, position_seconds=2**31))
    assert await repository.get_for_title(user_id, title_id) is None


async def test_a_client_write_conflict_leaves_the_session_usable(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """The SAVEPOINT `refusals_as_conflict` opens is what stops a refused client write from.

    poisoning the session for whatever the caller does next -- the same property
    `test_a_caught_conflict_leaves_the_session_ usable` pins for `merge_from_source`,
    one method over.
    """
    with pytest.raises(RepositoryConflict):
        await repository.set_from_client(write(user_id, title_id, position_seconds=2**31))
    result = await repository.set_from_client(write(user_id, title_id, position_seconds=10))
    assert result.position_seconds == 10


async def test_a_client_write_stamps_a_fresh_updated_at_over_a_backdated_row(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """The integration fixture is one long transaction with `now()` frozen inside it.

    so "the client write is later than the walk" cannot be shown by comparing two SQL-
    side `now()` reads against each other -- both would read the identical instant,
    which is exactly the trap `db-and-sql.md` names for this fixture shape.

    The walk side is therefore a real row backdated with a raw `INSERT` (the trigger
    only fires `BEFORE UPDATE`, so an insert dodges it).

    This is the `ON CONFLICT ... DO UPDATE` path specifically, which nothing
    else in this file drives through the trigger: proof that the exotic
    statement shape still fires it rather than silently bypassing it.

    `origin` is asserted here too and not only in the contract's own DO
    UPDATE case: the row is seeded `origin='source'` directly, by raw SQL
    rather than through `merge_from_source`, so this is a second, differently
    constructed row proving a dropped `origin = 'api'` in that branch's `SET`
    clause is caught regardless of how the pre-existing row got there.
    """
    long_ago = WALK_AT - timedelta(days=365)
    await session.execute(
        text(
            "INSERT INTO watch_states "
            "(id, user_id, title_id, position_seconds, played, play_count, updated_at, origin) "
            "VALUES (:id, :user_id, :title_id, 5, false, 0, :updated_at, 'source')"
        ),
        {"id": new_id(), "user_id": user_id, "title_id": title_id, "updated_at": long_ago},
    )

    result = await repository.set_from_client(write(user_id, title_id, position_seconds=999))

    assert result.updated_at > long_ago
    assert result.position_seconds == 999
    assert result.origin is WatchStateOrigin.API, "the DO UPDATE branch must promote it too"


async def _stored(session: AsyncSession, title_id: uuid.UUID) -> tuple[int, datetime]:
    row = (
        await session.execute(
            text("SELECT position_seconds, updated_at FROM watch_states WHERE title_id = :t"),
            {"t": title_id},
        )
    ).one()
    return row.position_seconds, row.updated_at


async def test_a_source_merge_keeps_its_own_instant_on_the_update_path(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """`watch_states_set_updated_at` keeps `observed_at` on an update whose origin is the source.

    The first merge inserts, which no trigger sees; the second updates the row it left, the
    path the old trigger stamped with the transaction's `now()`.
    """
    await repository.merge_from_source([merge(user_id, title_id)])
    assert await _stored(session, title_id) == (90, WALK_AT), "the premise: the insert path"
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=999, observed_at=LATER)]
    )
    assert await _stored(session, title_id) == (999, LATER)


async def test_a_newer_read_is_not_refused_because_an_older_one_committed_first(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """Two walks overlap over a stored row, and the one that began first commits first.

    Its update keeps the instant that walk began, so the second walk's newer read still
    writes. The old trigger stamped that update with the transaction's `now()` -- later
    than the second walk began -- and refused the newer read, leaving the older position.
    """
    await repository.merge_from_source(
        [merge(user_id, title_id, observed_at=WALK_AT - timedelta(days=1))]
    )
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=5, observed_at=WALK_AT)]
    )
    position, _ = await _stored(session, title_id)
    assert position == 5, "the premise: the older read wrote first, on the update path"
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=999, observed_at=LATER)]
    )
    assert await _stored(session, title_id) == (999, LATER)


async def test_a_later_sighting_by_the_same_walk_writes_at_the_walks_instant(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """A walk sights a stored row in two batches; the later one writes, at the walk's instant.

    The second batch carries the instant the first left, which the merge's `<=` lets
    through. A trigger stamping `now()` on that tie writes the row and then refuses every
    read of it that began before the commit -- the fixed defect, back for any row a walk
    sights twice.
    """
    await repository.merge_from_source(
        [merge(user_id, title_id, observed_at=WALK_AT - timedelta(days=1))]
    )
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=5, observed_at=WALK_AT)]
    )
    position, _ = await _stored(session, title_id)
    assert position == 5, "the premise: the first sighting wrote, on the update path"
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=6, observed_at=WALK_AT)]
    )
    assert await _stored(session, title_id) == (6, WALK_AT)


async def test_an_older_read_committed_after_a_newer_one_is_refused(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """The converse: the newer read commits first, and the older one after it writes nothing.

    The first merge only seeds the row, so both reads land on the update path.
    """
    await repository.merge_from_source(
        [merge(user_id, title_id, observed_at=WALK_AT - timedelta(days=1))]
    )
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=999, observed_at=LATER)]
    )
    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=5, observed_at=WALK_AT)]
    )
    assert await _stored(session, title_id) == (999, LATER)


async def test_a_client_write_over_a_source_row_is_still_stamped_now(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    await repository.merge_from_source([merge(user_id, title_id)])
    await repository.set_from_client(write(user_id, title_id, position_seconds=30))
    now = (await session.execute(text("SELECT now()"))).scalar_one()
    assert await _stored(session, title_id) == (30, now)


@pytest.fixture
def statement_counter() -> Iterator[list[str]]:
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
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """A walk merges its states a batch at a time, as the ingest walk writes items.

    So a per-row merge is the same design defect one port over.
    """
    titles = []
    for index in range(500):
        title = Title(kind=TitleKind.MOVIE, name=f"T{index}", sort_name=f"T{index}")
        await PostgresTitleRepository(session).add(title)
        titles.append(title.id)

    statement_counter.clear()
    await repository.merge_from_source([merge(user_id, one) for one in titles[:5]])
    small = len(statement_counter)

    statement_counter.clear()
    await repository.merge_from_source([merge(user_id, one) for one in titles[5:]])
    large = len(statement_counter)

    assert small == large, f"{small} statements for 5 merges, {large} for 495"


async def test_the_in_progress_statement_takes_its_recency_order_from_the_index(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Scoped to the stage that has an ordering to serve, per the standing rule.

    this asserts that `ix_watch_states_user_recent` supplies the recency ordering, not
    that no `Seq Scan` appears anywhere in the plan.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    await session.execute(text("SET LOCAL enable_bitmapscan = off"))
    result = await session.execute(
        text(
            "EXPLAIN SELECT id FROM watch_states "
            "WHERE user_id = CAST(:user_id AS uuid) AND NOT played AND position_seconds > 0 "
            "ORDER BY last_played_at DESC NULLS LAST, id DESC LIMIT 20"
        ),
        {"user_id": user_id},
    )
    plan = "\n".join(row[0] for row in result)
    assert "ix_watch_states_user_recent" in plan, plan
    assert "Presorted Key: last_played_at" in plan, plan
    assert "Incremental Sort" in plan, plan
