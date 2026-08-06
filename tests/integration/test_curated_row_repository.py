"""`PostgresCuratedRowRepository` against the real database.

The shared contract runs here unchanged, and this is the arm where three of
its cases are real rather than structural — see
`tests/fakes/curated_row_repository.py` for the enumerated list. Chiefly:
`test_a_generation_that_produced_nothing_clears_the_screen` is the only case
in the suite that can see the delete's *scope*, and against a Python list that
scope cannot be got wrong.

Plus the four things a list cannot express: a foreign key, a CHECK constraint,
the SAVEPOINT that makes a failed generation leave the previous screen whole,
and a session that survives being handed a generation the database refuses.

The seeder writes through a raw `INSERT` rather than through the port, because
the port deliberately cannot store two generations for one household: that is
what `replace_for_user` *means*, so a second generation has to arrive from
outside it or the newest-generation read is untestable.
"""

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.curated_row_repository_contract import (
    CuratedRowRepositoryContract,
    CuratedRowSeeder,
    curated_row,
)
from usher.db.repositories.curation import PostgresCuratedRowRepository
from usher.domain.curation import CuratedRow
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict

_SEED_ROW = text(
    "INSERT INTO curated_rows "
    '(id, user_id, slug, title, reason, card_title_ids, "position", '
    " model_name, generation_id, generated_at) "
    "VALUES (:id, :user_id, :slug, :title, :reason, :card_title_ids, :position, "
    "        :model_name, :generation_id, :generated_at)"
).bindparams(
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("user_id", type_=PGUUID(as_uuid=True)),
    bindparam("card_title_ids", type_=ARRAY(PGUUID(as_uuid=True))),
    bindparam("generation_id", type_=PGUUID(as_uuid=True)),
)


class PostgresCuratedRowSeeder(CuratedRowSeeder):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def user(self) -> uuid.UUID:
        user_id = new_id()
        await self._session.execute(
            text("INSERT INTO users (id, name) VALUES (CAST(:id AS uuid), :name)"),
            {"id": user_id, "name": f"viewer-{user_id}"},
        )
        return user_id

    async def generation(self, rows: Sequence[CuratedRow]) -> None:
        for row in rows:
            await self._session.execute(
                _SEED_ROW,
                {
                    "id": row.id,
                    "user_id": row.user_id,
                    "slug": row.slug,
                    "title": row.title,
                    "reason": row.reason,
                    "card_title_ids": list(row.card_title_ids),
                    "position": row.position,
                    "model_name": row.model_name,
                    "generation_id": row.generation_id,
                    "generated_at": row.generated_at,
                },
            )

    async def count(self, user_id: uuid.UUID) -> int:
        found = await self._session.execute(
            text("SELECT count(*) FROM curated_rows WHERE user_id = CAST(:id AS uuid)"),
            {"id": user_id},
        )
        return int(found.scalar_one())


class TestPostgresCuratedRowRepository(CuratedRowRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresCuratedRowRepository:
        return PostgresCuratedRowRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresCuratedRowSeeder:
        # The same session, so the seeded generation and the written one are
        # in the transaction this test owns.
        return PostgresCuratedRowSeeder(session)

    async def test_a_generation_for_a_household_that_does_not_exist_is_a_port_error(
        self, repository: PostgresCuratedRowRepository
    ) -> None:
        """Postgres-only: the fake is a list and has nothing to violate.

        `fk_curated_rows_user_id_users`. A raw `IntegrityError` escaping here
        is the one thing ADR-0009 says must never happen -- the only way a
        caller could handle it is to import sqlalchemy itself.
        """
        orphan = new_id()
        with pytest.raises(RepositoryConflict) as raised:
            await repository.replace_for_user(
                orphan, [curated_row(orphan, position=0, generation_id=new_id())]
            )
        assert raised.value.constraint == "fk_curated_rows_user_id_users"

    async def test_an_empty_shelf_is_refused_by_the_table_and_not_by_luck(
        self,
        repository: PostgresCuratedRowRepository,
        user_id: uuid.UUID,
    ) -> None:
        """`ck_curated_rows_cards_not_empty`, reached through the repository
        rather than through raw SQL.

        Constructed with `model_construct`, because `CuratedRow`'s own
        `min_length=1` refuses it first -- which is exactly why the CHECK
        exists: a heading with no shelf under it is a validator that ran and
        kept nothing, and the row is discarded whole rather than padded from
        the pool. `tests/integration/test_curation_schema.py` owns the
        constraint; this owns the translation, which is the half a caller
        sees.
        """
        valid = curated_row(user_id, position=0, generation_id=new_id())
        empty = valid.model_construct(**{**valid.model_dump(), "card_title_ids": ()})

        with pytest.raises(RepositoryConflict) as raised:
            await repository.replace_for_user(user_id, [empty])
        assert raised.value.constraint == "ck_curated_rows_cards_not_empty"

    async def test_a_generation_that_fails_part_way_leaves_the_previous_screen_whole(
        self,
        repository: PostgresCuratedRowRepository,
        user_id: uuid.UUID,
        seeder: PostgresCuratedRowSeeder,
    ) -> None:
        """**The SAVEPOINT, and the reason `replace_for_user` is one
        transaction rather than two statements.**

        The wrong implementation this kills: a delete and an insert with no
        transaction between them. The delete lands, the insert raises, and the
        household is left with *no* screen -- which is not a legibly short
        generation, it is indistinguishable from a household the LLM has never
        run for. `CurationService` catches the conflict and still has a ledger
        entry to write, so it commits, and the empty screen commits with it.

        **The `DELETE` is what makes this reachable, not a partially-applied
        insert.** asyncpg documents `executemany` as atomic, so whether the
        valid row of this batch ever landed is unobservable -- and that is
        exactly why the SAVEPOINT has to cover *both* statements rather than
        leaning on the insert being all-or-nothing: by the time the second row
        violates `ck_curated_rows_cards_not_empty`, the delete has already
        run. The batch is still written valid-row-first so that an
        implementation which validated only the first row of a generation
        fails here too. Both halves are asserted: the previous generation is
        still on the screen, and the refused generation is not in the table at
        all.

        The last assertion is the other half of the SAVEPOINT's job: without
        it the session is poisoned, and the next unrelated call raises
        `PendingRollbackError` with the failure attributed to whatever ran
        next.
        """
        survivor = [curated_row(user_id, position=0, generation_id=new_id())]
        await repository.replace_for_user(user_id, survivor)

        generation = new_id()
        good = curated_row(user_id, position=0, generation_id=generation)
        broken = good.model_construct(
            **{**good.model_dump(), "id": new_id(), "position": 1, "card_title_ids": ()}
        )
        with pytest.raises(RepositoryConflict):
            await repository.replace_for_user(user_id, [good, broken])

        assert await repository.list_for_user(user_id) == survivor
        assert await seeder.count(user_id) == 1

        # ...and the session is still usable, which is what the SAVEPOINT
        # buys the caller beyond the rollback itself.
        again = [curated_row(user_id, position=0, generation_id=new_id())]
        assert await repository.replace_for_user(user_id, again) == 1
        assert await repository.list_for_user(user_id) == again
