"""The shared contract against real Postgres, plus the four things a dict
cannot express: a foreign key, a CHECK constraint, a
`CardinalityViolationError`, and a poisoned session.

`FakeEpisodeRepository` keys on the natural key, so every "duplicate inside
one batch" case passes there because a dict cannot hold a key twice. Here the
same batch is a real `ON CONFLICT DO UPDATE command cannot affect row a
second time` unless the staging read is `SELECT DISTINCT ON`.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.episode_repository_contract import EpisodeRepositoryContract, episode, season
from usher.db.repositories.episode import PostgresEpisodeRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict


@pytest.fixture
def repository(session: AsyncSession) -> PostgresEpisodeRepository:
    return PostgresEpisodeRepository(session)


@pytest_asyncio.fixture
async def title_id(session: AsyncSession) -> uuid.UUID:
    series = Title(kind=TitleKind.SERIES, name="Contract Series", sort_name="Contract Series")
    await PostgresTitleRepository(session).add(series)
    return series.id


@pytest_asyncio.fixture
async def other_title_id(session: AsyncSession) -> uuid.UUID:
    series = Title(kind=TitleKind.SERIES, name="Other Series", sort_name="Other Series")
    await PostgresTitleRepository(session).add(series)
    return series.id


@pytest_asyncio.fixture
async def season_id(session: AsyncSession, title_id: uuid.UUID) -> uuid.UUID:
    """A real season row: `episodes.season_id` is `NOT NULL` with an
    `ON DELETE CASCADE` FK, so an episode cannot exist without one."""
    identifier = new_id()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": identifier, "title_id": title_id},
    )
    return identifier


class TestPostgresEpisodeRepository(EpisodeRepositoryContract):
    """Every case in `EpisodeRepositoryContract`, against real Postgres."""


async def test_a_title_id_no_title_carries_is_a_port_error(
    repository: PostgresEpisodeRepository,
) -> None:
    """The case the fake's docstring names as its own divergence: a dict has
    no foreign keys, so it can store an episode hung off nothing. Postgres
    raises, and `services/` must not have to import `sqlalchemy.exc` to handle
    it (ADR-0009)."""
    with pytest.raises(RepositoryConflict) as caught:
        await repository.upsert_seasons([season(new_id(), 1)])
    assert caught.value.constraint == "fk_seasons_title_id_titles"


async def test_a_season_id_no_season_carries_is_a_port_error(
    repository: PostgresEpisodeRepository, title_id: uuid.UUID
) -> None:
    """`episodes` has two FKs, not one, and an implementation that declared
    only `title_id`'s would let an episode name a season that never existed --
    which is exactly what happens when a walk sees an episode before its
    season."""
    with pytest.raises(RepositoryConflict) as caught:
        await repository.upsert_episodes([episode(title_id, new_id(), 1)])
    assert caught.value.constraint == "fk_episodes_season_id_seasons"


async def test_a_caught_conflict_leaves_the_session_usable(
    repository: PostgresEpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
) -> None:
    """Postgres aborts the entire transaction on any statement error until a
    ROLLBACK, so without a SAVEPOINT a caught conflict poisons the session for
    the caller's next, unrelated call -- and this repository's caller commits a
    batch of episodes together with its sync-run checkpoint."""
    with pytest.raises(RepositoryConflict):
        await repository.upsert_seasons([season(new_id(), 1)])
    result = await repository.upsert_episodes([episode(title_id, season_id, 1)])
    assert (result.inserted, result.updated) == (1, 0)


async def test_a_failed_batch_writes_none_of_itself(
    repository: PostgresEpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
) -> None:
    """The SAVEPOINT is what makes a batch atomic across its staging DDL, its
    `COPY` and its upsert. Half of a 1,000-episode batch landing would leave
    an ingest run unable to tell what it still owes."""
    with pytest.raises(RepositoryConflict):
        await repository.upsert_episodes(
            [episode(title_id, season_id, 1), episode(title_id, new_id(), 2)]
        )
    # Only the episodes: the `season_id` fixture inserts a real season row of
    # its own, and the SAVEPOINT is not supposed to undo work this call never
    # did.
    _, episodes = await repository.list_for_title(title_id)
    assert episodes == []


async def test_a_negative_runtime_is_a_port_error_not_a_copy_failure(
    repository: PostgresEpisodeRepository, title_id: uuid.UUID, season_id: uuid.UUID
) -> None:
    """`usher.db.staging`'s tables are deliberately unconstrained, so a value
    that violates the *destination's* CHECK survives the `COPY` and fails one
    statement later at the `INSERT ... SELECT` -- which goes through SQLAlchemy
    and is therefore an `IntegrityError` this repository can translate. Had the
    constraint been on the staging table, `copy_records_to_table` would raise
    asyncpg's own `CheckViolationError` straight past the `except`.

    `Episode.runtime_minutes` carries `Field(ge=0)`, so the offending row
    cannot be built through the normal path at all -- `model_construct` is
    pydantic's own "skip validation" constructor and is the deliberate tool
    here, not a stand-in for `.evolve()`. That is also the honest shape: the
    `COPY` path never sees a pydantic model, so the only thing standing
    between a mis-mapped adapter value and this column is the CHECK.
    """
    invalid = Episode.model_construct(
        **{**episode(title_id, season_id, 1).model_dump(), "runtime_minutes": -1}
    )
    with pytest.raises(RepositoryConflict) as caught:
        await repository.upsert_episodes([invalid])
    assert caught.value.constraint == "ck_episodes_runtime_minutes_non_negative"


async def test_the_update_trigger_owns_updated_at(
    session: AsyncSession,
    repository: PostgresEpisodeRepository,
    title_id: uuid.UUID,
    season_id: uuid.UUID,
) -> None:
    """`trg_episodes_set_updated_at` is a `BEFORE UPDATE` trigger assigning
    `now()` unconditionally, and it exists precisely because this path never
    goes through the ORM -- SQLAlchemy's `onupdate=` never fires for an
    `INSERT ... SELECT` off a staging table. Recorded rather than assumed,
    because `FakeEpisodeRepository` carries the incoming model's `updated_at`
    through instead.

    **The starting row is inserted by hand with a backdated `updated_at`, and
    that is not incidental.** `set_updated_at()` assigns `now()`, which is
    `transaction_timestamp()` and therefore *frozen* for the life of a
    transaction -- and this suite's fixture is one long transaction. Two
    updates through the repository read back the identical instant, so
    "the second write is later than the first" is unobservable here and says
    nothing about whether the trigger fired at all. Backdating gives it
    something to move away from. (The trigger cannot be dodged with a plain
    `UPDATE` either, since it fires on that too -- hence a raw `INSERT`, which
    it does not.)
    """
    await session.execute(
        text(
            "INSERT INTO episodes (id, title_id, season_id, season_number, episode_number, "
            "created_at, updated_at) VALUES (:id, :t, :s, 1, 1, :old, :old)"
        ),
        {
            "id": new_id(),
            "t": title_id,
            "s": season_id,
            "old": datetime(2000, 1, 1, tzinfo=UTC),
        },
    )
    await repository.upsert_episodes([episode(title_id, season_id, 1, name="Renamed")])
    stored = (
        await session.execute(
            text("SELECT updated_at FROM episodes WHERE title_id = :t"), {"t": title_id}
        )
    ).scalar_one()
    assert stored > datetime(2020, 1, 1, tzinfo=UTC), (
        "the trigger did not stamp an update the statement never named"
    )


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
    repository: PostgresEpisodeRepository,
    title_id: uuid.UUID,
    season_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """999,827 of the one measured source's 1,126,674 items are episodes, so
    a per-row write here is ~19 minutes of pure repository overhead per full
    walk before a byte of upstream I/O."""
    statement_counter.clear()
    await repository.upsert_episodes([episode(title_id, season_id, index) for index in range(1, 6)])
    small = len(statement_counter)

    statement_counter.clear()
    await repository.upsert_episodes(
        [episode(title_id, season_id, index) for index in range(6, 501)]
    )
    large = len(statement_counter)

    assert small == large, f"{small} statements for 5 episodes, {large} for 495"


async def test_resolve_costs_one_statement_for_a_whole_page(
    repository: PostgresEpisodeRepository,
    title_id: uuid.UUID,
    season_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    await repository.upsert_episodes(
        [episode(title_id, season_id, index) for index in range(1, 201)]
    )
    statement_counter.clear()
    resolved = await repository.resolve(title_id, [(1, index) for index in range(1, 201)])
    assert len(resolved) == 200
    assert len(statement_counter) == 1, f"200 lookups cost {len(statement_counter)} statements"
