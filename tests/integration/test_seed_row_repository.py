"""`PostgresSeedRowRepository` against the real database."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.seed_row_repository_contract import (
    BUILT_AT,
    CLOSE,
    LATER,
    NAMED,
    RECIPE_A,
    RECIPE_B,
    RECOMMENDED,
    SeedRowRepositoryContract,
    SeedRowSeeder,
    built_row,
    card,
    cards_of,
)
from usher.db.repositories._errors import constraint_name
from usher.db.repositories.seed_row import PostgresSeedRowRepository
from usher.domain.ids import new_id
from usher.domain.seed_rows import MIN_SEED_ROW_CARDS, SEED_ROW_CARDS, SeedRowOutcome
from usher.ports.errors import RepositoryConflict


class PostgresSeedRowSeeder(SeedRowSeeder):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def title(self) -> uuid.UUID:
        title_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) "
                "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title')"
            ),
            {"id": title_id},
        )
        return title_id

    async def statements_during(self, work: Callable[[], Awaitable[object]]) -> int:
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            statements.append(statement)

        bind = self._session.get_bind()
        event.listen(bind, "before_cursor_execute", record)
        try:
            await work()
        finally:
            event.remove(bind, "before_cursor_execute", record)
        return len(statements)


class TestPostgresSeedRowRepository(SeedRowRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresSeedRowRepository:
        return PostgresSeedRowRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresSeedRowSeeder:
        # The same session, so the seeded titles and the saved records share the
        # transaction this test owns and rolls back.
        return PostgresSeedRowSeeder(session)


async def _ctid(session: AsyncSession, seed_id: uuid.UUID) -> str:
    """Where the seed's row version sits: an update moves it, and a skipped one does not."""
    found = await session.execute(
        text("SELECT ctid::text FROM seed_rows WHERE seed_title_id = CAST(:id AS uuid)"),
        {"id": seed_id},
    )
    return str(found.scalar_one())


async def test_deleting_the_seed_title_deletes_its_record(session: AsyncSession) -> None:
    """`ON DELETE CASCADE`, like `title_embeddings` and `title_neighbors`.

    A stored row is fully re-derivable from the seed plus a model, so it should die with
    its seed rather than block the delete or sit attached to nothing. Kills a migration
    written with `RESTRICT`, and one with no `ondelete`, which Postgres reads as
    `NO ACTION` and refuses the same way.
    """
    seeder = PostgresSeedRowSeeder(session)
    repository = PostgresSeedRowRepository(session)
    seed_id, kept_id = await seeder.title(), await seeder.title()
    await repository.save(built_row(seed_id))
    await repository.save(built_row(kept_id))
    assert set(await repository.list_for([seed_id, kept_id])) == {seed_id, kept_id}

    await session.execute(text("DELETE FROM titles WHERE id = CAST(:id AS uuid)"), {"id": seed_id})

    assert set(await repository.list_for([seed_id, kept_id])) == {kept_id}


async def test_a_seed_naming_no_title_is_a_port_error_and_leaves_the_session_usable(
    session: AsyncSession,
) -> None:
    """`fk_seed_rows_seed_title_id_titles`, translated rather than raised raw.

    Postgres-only: the fake has no `titles` table. The follow-up read is the other half
    of the claim -- a refusal that left the transaction aborted would fail it.
    """
    repository = PostgresSeedRowRepository(session)
    orphan = new_id()

    with pytest.raises(RepositoryConflict) as raised:
        await repository.save(built_row(orphan))

    assert raised.value.constraint == "fk_seed_rows_seed_title_id_titles"
    assert await repository.list_for([orphan]) == {}


@pytest.mark.parametrize(
    ("cards", "constraint"),
    [
        (MIN_SEED_ROW_CARDS - 1, "ck_seed_rows_card_count"),
        (SEED_ROW_CARDS + 1, "ck_seed_rows_card_count"),
    ],
)
async def test_a_record_outside_the_card_count_is_a_port_error(
    session: AsyncSession, cards: int, constraint: str
) -> None:
    """The CHECK reached through the repository, past the model's own bounds.

    `model_construct` is needed because `SeedRow` refuses these first. This case owns the
    translation, not the constraint, which the raw-`INSERT` cases below own.
    """
    seeder = PostgresSeedRowSeeder(session)
    repository = PostgresSeedRowRepository(session)
    seed_id = await seeder.title()
    valid = built_row(seed_id)
    out_of_bounds = valid.model_construct(**{**valid.__dict__, "cards": cards_of(cards)})

    with pytest.raises(RepositoryConflict) as raised:
        await repository.save(out_of_bounds)

    assert raised.value.constraint == constraint
    assert await repository.list_for([seed_id]) == {}


async def test_saving_a_record_that_equals_the_stored_one_writes_nothing(
    session: AsyncSession,
) -> None:
    """The `IS DISTINCT FROM` guard: an unchanged record is not a new row version.

    The scheduler's tick rewrites nothing for a seed whose attempt it repeats exactly,
    and a bare `DO UPDATE` would churn a version a tick. Read off `ctid`, which an
    `UPDATE` always moves; the last step proves the probe can see one.
    """
    seeder = PostgresSeedRowSeeder(session)
    repository = PostgresSeedRowRepository(session)
    seed_id = await seeder.title()
    row = built_row(seed_id, cards=cards_of(9))
    await repository.save(row)
    placed = await _ctid(session, seed_id)

    await repository.save(row)
    assert await _ctid(session, seed_id) == placed

    await repository.save(row.evolve(attempted_at=LATER, attempt_outcome=SeedRowOutcome.TOO_FEW))
    assert await _ctid(session, seed_id) != placed, "the premise: the probe sees an update"


async def test_the_sources_are_stored_as_sorted_names_joined_by_a_plus(
    session: AsyncSession,
) -> None:
    """The encoding the migration, the model and every reader of the raw column share.

    Read raw, because the round trip passes for any reversible encoding.
    """
    seeder = PostgresSeedRowSeeder(session)
    repository = PostgresSeedRowRepository(session)
    seed_id = await seeder.title()
    mixed = [NAMED | CLOSE, RECOMMENDED, NAMED | RECOMMENDED | CLOSE, NAMED]
    cards = tuple(card(sources) for sources in mixed) + cards_of(MIN_SEED_ROW_CARDS - len(mixed))

    await repository.save(built_row(seed_id, cards=cards))

    found = await session.execute(
        text("SELECT card_sources FROM seed_rows WHERE seed_title_id = CAST(:id AS uuid)"),
        {"id": seed_id},
    )
    stored = found.scalar_one()
    assert stored[: len(mixed)] == [
        "close+named",
        "recommended",
        "close+named+recommended",
        "named",
    ]
    assert stored[len(mixed) :] == ["named"] * (MIN_SEED_ROW_CARDS - len(mixed))


_RAW_INSERT = text(
    "INSERT INTO seed_rows (seed_title_id, card_title_ids, card_sources, built_at, "
    "built_recipe, attempted_at, attempt_outcome, attempt_recipe) "
    "VALUES (CAST(:seed_title_id AS uuid), CAST(:card_title_ids AS uuid[]), "
    "CAST(:card_sources AS text[]), :built_at, :built_recipe, :attempted_at, "
    ":attempt_outcome, :attempt_recipe)"
)


def _raw_row(seed_id: uuid.UUID, **overrides: Any) -> dict[str, Any]:
    """A valid record's columns, as the `INSERT` above takes them, with some replaced."""
    row: dict[str, Any] = {
        "seed_title_id": seed_id,
        "card_title_ids": [new_id() for _ in range(MIN_SEED_ROW_CARDS)],
        "card_sources": ["named"] * MIN_SEED_ROW_CARDS,
        "built_at": BUILT_AT,
        "built_recipe": RECIPE_A,
        "attempted_at": BUILT_AT,
        "attempt_outcome": "built",
        "attempt_recipe": RECIPE_A,
    }
    row.update(overrides)
    return row


def _ids(count: int) -> list[uuid.UUID]:
    return [new_id() for _ in range(count)]


async def test_the_raw_row_the_refusals_below_are_built_on_is_accepted(
    session: AsyncSession,
) -> None:
    """The control: with nothing replaced the `INSERT` succeeds, so each refusal is its own."""
    seed_id = await PostgresSeedRowSeeder(session).title()

    await session.execute(_RAW_INSERT, _raw_row(seed_id))

    found = await session.execute(text("SELECT count(*) FROM seed_rows"))
    assert found.scalar_one() == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        pytest.param(
            {"card_title_ids": _ids(7), "card_sources": ["named"] * 7},
            "ck_seed_rows_card_count",
            id="seven cards",
        ),
        pytest.param(
            {"card_title_ids": _ids(21), "card_sources": ["named"] * 21},
            "ck_seed_rows_card_count",
            id="twenty-one cards",
        ),
        pytest.param(
            {"card_sources": ["named"] * 7},
            "ck_seed_rows_sources_match_cards",
            id="one source short",
        ),
        pytest.param(
            {"card_sources": ["named"] * 9},
            "ck_seed_rows_sources_match_cards",
            id="one source over",
        ),
        pytest.param(
            {"built_at": None, "built_recipe": None},
            "ck_seed_rows_built_iff_cards",
            id="cards without a build",
        ),
        pytest.param(
            {"card_title_ids": [], "card_sources": []},
            "ck_seed_rows_built_iff_cards",
            id="a build without cards",
        ),
        pytest.param(
            {"built_recipe": None}, "ck_seed_rows_built_iff_cards", id="a build time, no recipe"
        ),
        pytest.param(
            {"built_at": None}, "ck_seed_rows_built_iff_cards", id="a build recipe, no time"
        ),
        pytest.param({"attempt_recipe": "a" * 31}, "ck_seed_rows_recipe_length", id="attempt 31"),
        pytest.param({"attempt_recipe": "a" * 33}, "ck_seed_rows_recipe_length", id="attempt 33"),
        pytest.param({"built_recipe": "a" * 31}, "ck_seed_rows_recipe_length", id="built 31"),
        pytest.param({"built_recipe": "a" * 33}, "ck_seed_rows_recipe_length", id="built 33"),
    ],
)
async def test_each_check_refuses_the_row_that_breaks_it_and_names_itself(
    session: AsyncSession, overrides: dict[str, Any], constraint: str
) -> None:
    """Raw `INSERT`s, because the repository derives half of what these constrain.

    Each row breaks one rule and is otherwise the control row, so the constraint named is
    the one that fired -- Postgres reports the first it checks, and a row breaking two
    would name whichever sorts earlier.
    """
    seed_id = await PostgresSeedRowSeeder(session).title()

    with pytest.raises(IntegrityError) as raised:
        async with session.begin_nested():
            await session.execute(_RAW_INSERT, _raw_row(seed_id, **overrides))

    assert constraint_name(raised.value) == constraint


async def test_a_record_with_no_cards_is_the_empty_default(session: AsyncSession) -> None:
    """The columns' `'{}'` default, which the three-absences shape is built on."""
    seed_id = await PostgresSeedRowSeeder(session).title()

    await session.execute(
        text(
            "INSERT INTO seed_rows (seed_title_id, attempted_at, attempt_outcome, attempt_recipe) "
            "VALUES (CAST(:id AS uuid), :at, 'too_few', :recipe)"
        ),
        {"id": seed_id, "at": datetime(2026, 11, 2, tzinfo=UTC), "recipe": RECIPE_B},
    )

    found = await PostgresSeedRowRepository(session).list_for([seed_id])
    assert found[seed_id].cards == ()
    assert found[seed_id].attempt_outcome is SeedRowOutcome.TOO_FEW
