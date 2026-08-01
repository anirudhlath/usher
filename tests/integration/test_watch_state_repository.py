"""The shared contract against real Postgres -- the half with teeth.

Every `COALESCE` case in `WatchStateRepositoryContract` passes against the
in-memory fake by accident, because Python's `is not None` guard is naturally
that shape. Only here can it fail, and the natural one-statement spelling of
the merge really does fail it: measured against `pgvector/pgvector:pg17`, a
stored `play_count` of 7 reads back `0`.

Plus the four things a fake cannot express: a duplicate that would raise
rather than being last-wins, a foreign key, a poisoned session, and the
`BEFORE UPDATE` trigger that owns `updated_at`.
"""

import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.watch_state_repository_contract import (
    LATER,
    WALK_AT,
    WatchStateRepositoryContract,
    merge,
)
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import TitleKind
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
async def title_id(session: AsyncSession) -> uuid.UUID:
    title = Title(kind=TitleKind.MOVIE, name="Contract Title", sort_name="Contract Title")
    await PostgresTitleRepository(session).add(title)
    return title.id


@pytest_asyncio.fixture
async def episode_id(session: AsyncSession) -> uuid.UUID:
    """A real episode, which needs a real series and a real season -- both FKs
    are `ON DELETE CASCADE` and neither is nullable."""
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


class TestPostgresWatchStateRepository(WatchStateRepositoryContract):
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
    """Postgres aborts the entire transaction on any statement error until a
    ROLLBACK, so without a SAVEPOINT a caught conflict poisons the session for
    the caller's next, unrelated call -- and this repository's caller commits
    a batch of merges together with its sync-run checkpoint."""
    with pytest.raises(RepositoryConflict):
        await repository.merge_from_source([merge(user_id, new_id())])
    assert await repository.merge_from_source([merge(user_id, title_id)]) == 1


async def test_a_failed_batch_writes_none_of_itself(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """Four statements per batch means four chances to half-apply. The
    SAVEPOINT is what makes the batch atomic across all of them, so a caller
    that retries a corrected batch is not blocked by an `updated_at` the
    failed attempt already wrote."""
    with pytest.raises(RepositoryConflict):
        await repository.merge_from_source([merge(user_id, title_id), merge(user_id, new_id())])
    assert await repository.get_for_title(user_id, title_id) is None


async def test_the_update_trigger_owns_updated_at(
    repository: PostgresWatchStateRepository,
    session: AsyncSession,
    user_id: uuid.UUID,
    title_id: uuid.UUID,
) -> None:
    """`trg_watch_states_set_updated_at` is a `BEFORE UPDATE` trigger that
    assigns `now()` unconditionally, so the merge's own
    `updated_at = d.observed_at` lands on the *insert* path only. Recorded
    rather than assumed, because "latest `updated_at` wins" is the merge's
    conflict rule and every later reader of this column inherits whichever
    meaning it actually has.

    Benign for the rule: after a walk writes a row, `updated_at` is the write
    instant, which is if anything the more honest answer to "was this row
    written after the walk observed it". `FakeWatchStateRepository` stores
    `observed_at` on both paths and says so in its docstring.
    """
    await repository.merge_from_source([merge(user_id, title_id)])
    inserted = (
        await session.execute(
            text("SELECT updated_at FROM watch_states WHERE title_id = :t"),
            {"t": title_id},
        )
    ).scalar_one()
    assert inserted == WALK_AT, "the insert path stores observed_at verbatim"

    await repository.merge_from_source(
        [merge(user_id, title_id, position_seconds=999, observed_at=LATER)]
    )
    updated = (
        await session.execute(
            text("SELECT updated_at FROM watch_states WHERE title_id = :t"),
            {"t": title_id},
        )
    ).scalar_one()
    assert updated != LATER, "the trigger overwrote it with now()"
    assert updated > LATER


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
    """A full watch-state walk covers the same 1,126,674 items the ingest
    walk does, so a per-row merge is the same design defect one port over."""
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
