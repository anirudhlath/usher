"""The shared contract against real Postgres, plus the four things a dict
cannot express: a foreign key, a CHECK constraint, a
`CardinalityViolationError`, and a poisoned session.

`FakeEpisodeRepository` keys on the natural key, so every "duplicate inside
one batch" case passes there because a dict cannot hold a key twice. Here the
same batch is a real `ON CONFLICT DO UPDATE command cannot affect row a
second time` unless the staging read is `SELECT DISTINCT ON`.
"""

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import Connection, Engine, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.episode_repository_contract import (
    OTHER_SEEDED_KEYS,
    SEEDED_KEYS,
    EpisodeRepositoryContract,
    EpisodeRepositoryNextUpContract,
    MarkPlayed,
    MarkSeriesPlayed,
    episode,
    season,
    seed_series,
)
from usher.db.repositories.episode import _NEXT_UP, PostgresEpisodeRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.db.repositories.watch_state import PostgresWatchStateRepository
from usher.domain.enums import TitleKind
from usher.domain.episode import Episode
from usher.domain.ids import new_id
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict
from usher.ports.ingest import WatchStateMerge


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


@pytest_asyncio.fixture
async def other_season_id(session: AsyncSession, other_title_id: uuid.UUID) -> uuid.UUID:
    """A second series' season 1, so a batch can carry two shows' S01E01 --
    which is what every page of a real walk carries and what the old
    single-title `resolve` could not express."""
    identifier = new_id()
    await session.execute(
        text("INSERT INTO seasons (id, title_id, season_number) VALUES (:id, :title_id, 1)"),
        {"id": identifier, "title_id": other_title_id},
    )
    return identifier


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
    identifier = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (:id, :name)"),
        {"id": identifier, "name": f"user-{identifier}"},
    )
    return identifier


@pytest_asyncio.fixture
async def series_id(session: AsyncSession) -> uuid.UUID:
    series = Title(kind=TitleKind.SERIES, name="Next Up Series", sort_name="Next Up Series")
    await PostgresTitleRepository(session).add(series)
    return series.id


@pytest_asyncio.fixture
async def other_series_id(session: AsyncSession) -> uuid.UUID:
    series = Title(kind=TitleKind.SERIES, name="Second Series", sort_name="Second Series")
    await PostgresTitleRepository(session).add(series)
    return series.id


@pytest_asyncio.fixture
async def seeded(
    repository: PostgresEpisodeRepository, series_id: uuid.UUID
) -> dict[tuple[int, int], uuid.UUID]:
    return await seed_series(repository, series_id, SEEDED_KEYS)


@pytest_asyncio.fixture
async def other_seeded(
    repository: PostgresEpisodeRepository, other_series_id: uuid.UUID
) -> dict[tuple[int, int], uuid.UUID]:
    return await seed_series(repository, other_series_id, OTHER_SEEDED_KEYS)


def _merge(
    user_id: uuid.UUID,
    *,
    title_id: uuid.UUID | None = None,
    episode_id: uuid.UUID | None = None,
    played: bool,
    last_played_at: datetime | None,
) -> WatchStateMerge:
    return WatchStateMerge(
        user_id=user_id,
        title_id=title_id,
        episode_id=episode_id,
        position_seconds=0 if played else 120,
        played=played,
        runtime_seconds=2700,
        observed_at=datetime(2026, 7, 31, 3, 0, tzinfo=UTC),
        play_count=1 if played else 0,
        last_played_at=last_played_at,
    )


@pytest.fixture
def mark_played(session: AsyncSession, user_id: uuid.UUID) -> MarkPlayed:
    """Real watch state, written through the real repository.

    The seam is the fixture rather than the port: `EpisodeRepository` has no
    write path for watch state and must not grow one just so a contract case
    can arrange its fixture.
    """

    async def _mark(episode_id: uuid.UUID, *, last_played_at: datetime | None = None) -> None:
        await PostgresWatchStateRepository(session).merge_from_source(
            [_merge(user_id, episode_id=episode_id, played=True, last_played_at=last_played_at)]
        )

    return _mark


@pytest.fixture
def mark_in_progress(session: AsyncSession, user_id: uuid.UUID) -> MarkPlayed:
    async def _mark(episode_id: uuid.UUID, *, last_played_at: datetime | None = None) -> None:
        await PostgresWatchStateRepository(session).merge_from_source(
            [_merge(user_id, episode_id=episode_id, played=False, last_played_at=last_played_at)]
        )

    return _mark


@pytest.fixture
def mark_series_played(session: AsyncSession, user_id: uuid.UUID) -> MarkSeriesPlayed:
    """The row Emby writes when a user marks a whole show watched: keyed on
    the series' `title_id`, with no episode at all."""

    async def _mark(series_id: uuid.UUID) -> None:
        await PostgresWatchStateRepository(session).merge_from_source(
            [_merge(user_id, title_id=series_id, played=True, last_played_at=None)]
        )

    return _mark


class TestPostgresEpisodeRepository(EpisodeRepositoryContract, EpisodeRepositoryNextUpContract):
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
    other_title_id: uuid.UUID,
    other_season_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    """Two series in the batch, deliberately: the property is one statement
    for the whole *page*, not one per series. `FakeEpisodeRepository` counts
    calls and so can pin the service's side of this; only here can the
    statement count be real."""
    await repository.upsert_episodes(
        [episode(title_id, season_id, index) for index in range(1, 201)]
        + [episode(other_title_id, other_season_id, index) for index in range(1, 201)]
    )
    statement_counter.clear()
    resolved = await repository.resolve_episodes(
        [(title_id, 1, index) for index in range(1, 201)]
        + [(other_title_id, 1, index) for index in range(1, 201)]
    )
    assert len(resolved) == 400
    assert len(statement_counter) == 1, f"400 lookups cost {len(statement_counter)} statements"


async def test_resolve_seasons_costs_one_statement_for_a_whole_page(
    repository: PostgresEpisodeRepository,
    title_id: uuid.UUID,
    other_title_id: uuid.UUID,
    statement_counter: list[str],
) -> None:
    await repository.upsert_seasons(
        [season(title_id, index) for index in range(1, 21)]
        + [season(other_title_id, index) for index in range(1, 21)]
    )
    statement_counter.clear()
    resolved = await repository.resolve_seasons(
        [(title_id, index) for index in range(1, 21)]
        + [(other_title_id, index) for index in range(1, 21)]
    )
    assert len(resolved) == 40
    assert len(statement_counter) == 1, f"40 lookups cost {len(statement_counter)} statements"


async def test_next_up_costs_one_statement_however_many_series_are_asked_about(
    repository: PostgresEpisodeRepository,
    user_id: uuid.UUID,
    series_id: uuid.UUID,
    other_series_id: uuid.UUID,
    seeded: dict[tuple[int, int], uuid.UUID],
    other_seeded: dict[tuple[int, int], uuid.UUID],
    mark_played: MarkPlayed,
    statement_counter: list[str],
) -> None:
    """`NextUpProvider` asks about every series the household has started, so
    a per-series loop is one round trip per started series -- and it returns
    the identical mapping, which is why nothing about the result can see it.

    `list_for_title` is the method a loop would reach for and it returns the
    whole tree: 20,000 rows for the measured pathological series, four
    million to produce two hundred cards.
    """
    await mark_played(seeded[(1, 1)])
    await mark_played(other_seeded[(1, 1)])
    statement_counter.clear()

    await repository.next_up(user_id, [series_id, other_series_id])

    assert len(statement_counter) == 1, statement_counter


async def test_next_up_reads_the_episode_key_index_and_does_not_scan_episodes(
    session: AsyncSession,
    user_id: uuid.UUID,
    series_id: uuid.UUID,
    seeded: dict[tuple[int, int], uuid.UUID],
) -> None:
    """Scoped to the stage with an ordering to serve, per the standing rule:
    `uq_episodes_title_season_episode` must appear and `Seq Scan on episodes`
    must not. Nothing is asserted about the rest of the plan, because an
    eight-episode fixture seq-scans whatever it is given and an unscoped
    assertion would be a claim about the fixture.

    This is also the case that justifies **not** adding an index in Task 17.
    Both spellings of the comparison return identical rows, so nothing about
    a result can tell them apart.

    **The third assertion is the one with teeth, and the first two are not
    enough -- measured.** A correctly hand-expanded `OR` still names
    `uq_episodes_title_season_episode` (the *mark* side uses it either way)
    and still shows no `Seq Scan` under `enable_seqscan = off`, so that
    mutation survived both of them. What separates the spellings is *where*
    the comparison lands: as an `Index Cond` it bounds the scan, and as a
    `Filter` it reads the whole series and discards. At catalog scale --
    32,409 series, 999,827 episodes, 200 probed -- that is 15.7 ms against
    134.1 ms with a `Seq Scan` over every episode in the library, for the
    identical 200 rows.
    """
    await session.execute(text("SET LOCAL enable_seqscan = off"))
    result = await session.execute(
        text(f"EXPLAIN {_NEXT_UP}"),
        {"user_id": user_id, "title_ids": [series_id]},
    )
    plan = "\n".join(row[0] for row in result)
    assert "uq_episodes_title_season_episode" in plan, plan
    assert "Seq Scan on episodes" not in plan, plan
    assert re.search(r"Index Cond:.*ROW\(season_number, episode_number\)", plan), plan
