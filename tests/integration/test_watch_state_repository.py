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
from datetime import timedelta

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
    """A second household member, so `list_in_progress`' `user_id` predicate
    has something to exclude. On a single-user deployment -- i.e. every
    deployment during development -- a lost `WHERE user_id` is invisible."""
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
    """A real episode, which needs a real series and a real season -- both FKs
    are `ON DELETE CASCADE` and neither is nullable."""
    return await _add_episode(session, episode_series_id, 1)


@pytest_asyncio.fixture
async def episode_ids(session: AsyncSession, episode_series_id: uuid.UUID) -> list[uuid.UUID]:
    """Ten episodes of one series, which is what makes the dedup observable:
    without it a household that watched ten episodes seeds ten identical
    "Because you watched" rows."""
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


async def test_a_client_write_to_an_unknown_title_is_a_port_error_not_an_integrity_error(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID
) -> None:
    with pytest.raises(RepositoryConflict) as caught:
        await repository.set_from_client(write(user_id, new_id()))
    assert caught.value.constraint == "fk_watch_states_title_id_titles"


async def test_a_client_writes_row_refusal_answers_repository_conflict_not_a_raw_dbapierror(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """`WatchState.position_seconds` is `Field(default=0, ge=0)` with no
    ceiling against an `integer` column -- `db-and-sql.md`'s "field bounded
    on fewer sides than the column" shape. `2**31` is refused client-side by
    asyncpg's own binary encoder as an unclassified `DBAPIError`, which
    `except IntegrityError` alone does not catch; `is_row_refusal`, inside
    `refusals_as_conflict`, is what has to.
    """
    with pytest.raises(RepositoryConflict):
        await repository.set_from_client(write(user_id, title_id, position_seconds=2**31))
    assert await repository.get_for_title(user_id, title_id) is None


async def test_a_client_write_conflict_leaves_the_session_usable(
    repository: PostgresWatchStateRepository, user_id: uuid.UUID, title_id: uuid.UUID
) -> None:
    """The SAVEPOINT `refusals_as_conflict` opens is what stops a refused
    client write from poisoning the session for whatever the caller does
    next -- the same property `test_a_caught_conflict_leaves_the_session_
    usable` pins for `merge_from_source`, one method over."""
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
    """The integration fixture is one long transaction with `now()` frozen
    inside it, so "the client write is later than the walk" cannot be shown
    by comparing two SQL-side `now()` reads against each other -- both would
    read the identical instant, which is exactly the trap
    `db-and-sql.md` names for this fixture shape. The walk side is therefore
    a real row backdated with a raw `INSERT` (the trigger only fires
    `BEFORE UPDATE`, so an insert dodges it), the same shape
    `test_the_update_trigger_owns_updated_at` uses for the merge path.

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


async def test_the_in_progress_statement_takes_its_recency_order_from_the_index(
    session: AsyncSession, user_id: uuid.UUID
) -> None:
    """Scoped to the stage that has an ordering to serve, per the standing
    rule: this asserts that `ix_watch_states_user_recent` supplies the
    recency ordering, not that no `Seq Scan` appears anywhere in the plan. An
    unscoped assertion fails on a three-row fixture for a plan that is
    correct at a million rows, and a suite that has to disable an assertion
    has learned nothing.

    `SET LOCAL enable_seqscan = off` is what makes the claim observable at
    all on a near-empty table -- the same technique
    `test_a_fuzzy_lookup_uses_the_trigram_index` uses one file over.
    `enable_bitmapscan = off` goes with it, and not for tidiness: a bitmap
    index scan discards ordering **by construction**, so with it available
    the planner picks one on a small table and the plan carries a full
    `Sort` whatever the index looks like. Measured -- this case passed alone
    and failed inside the full integration run for exactly that reason,
    which is the "assertion about the fixture rather than about the code"
    failure the standing rule names. With both off, the only question left
    is whether the index can supply the order, which is the question.

    **`Presorted Key: last_played_at` is the assertion, and "no Sort node" --
    which is what this case was first written as -- is not achievable.** The
    query orders by `last_played_at DESC NULLS LAST, id DESC` and the index
    carries only the first of those, so Postgres always needs an
    `Incremental Sort` above it for the `id` tiebreak. That node is the index
    *working*: `Presorted Key` is Postgres saying it consumed the index's own
    order and only sorted within ties.

    Which is exactly what separates the right index from the wrong one. An
    index declared `(user_id, played, last_played_at DESC)` without
    `NULLS LAST` cannot supply a `DESC NULLS LAST` order at all, so there is
    no presorted key, the incremental sort becomes a **full** one, and
    Continue Watching sorts the household's entire per-user set on every home
    screen -- while returning byte-identical rows, which is why no
    behavioural case in this file can see it.
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
