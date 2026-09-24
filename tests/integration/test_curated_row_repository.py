"""`PostgresCuratedRowRepository` against the real database."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.curated_row_repository_contract import (
    LAST_NIGHT,
    CuratedRowRepositoryContract,
    CuratedRowSeeder,
    curated_row,
)
from usher.db.repositories.curation import _LIST_FOR_USER, PostgresCuratedRowRepository
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


def _relations_scanned(node: dict[str, Any]) -> list[str]:
    """Every relation the plan tree touches, one entry per scan node.

    Recursive over `Plans`, where Postgres nests an `InitPlan` and a `SubPlan` as
    well as ordinary children, so a table probed once by an uncorrelated subquery
    and once by the outer scan appears twice.
    """
    found = [node["Relation Name"]] if "Relation Name" in node else []
    for child in node.get("Plans", []):
        found.extend(_relations_scanned(child))
    return found


class TestPostgresCuratedRowRepository(CuratedRowRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresCuratedRowRepository:
        return PostgresCuratedRowRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresCuratedRowSeeder:
        # The same session, so the seeded generation and the written one are
        # in the transaction this test owns.
        return PostgresCuratedRowSeeder(session)

    async def test_the_newest_generation_costs_one_pass_over_the_table(
        self,
        repository: PostgresCuratedRowRepository,
        seeder: PostgresCuratedRowSeeder,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> None:
        """One read of one household's shelves is one look at the table.

        The correlated-subquery spelling probes it twice.
        """
        # One `generation_id` per generation, not per row: a generation is what
        # `replace_for_user` writes in one call, and a comprehension minting one
        # each would seed four generations of one row and make the read's answer
        # a single shelf -- which is a different fixture asserting a different
        # thing.
        last_night, tonight = new_id(), new_id()
        stale = [
            curated_row(user_id, position=index, generation_id=last_night, generated_at=LAST_NIGHT)
            for index in range(2)
        ]
        await seeder.generation(stale)
        fresh = [curated_row(user_id, position=index, generation_id=tonight) for index in range(2)]
        await seeder.generation(fresh)

        explained = await session.execute(
            text("EXPLAIN (FORMAT JSON) " + _LIST_FOR_USER), {"user_id": user_id}
        )
        raw = explained.scalar_one()
        plan = json.loads(raw) if isinstance(raw, str) else raw
        scanned = _relations_scanned(plan[0]["Plan"])

        assert scanned, f"the plan named no relation at all, so nothing was measured: {plan}"
        assert scanned.count("curated_rows") == 1, (
            f"the read probes curated_rows more than once per home build: {scanned}"
        )
        assert await repository.list_for_user(user_id) == fresh

    async def test_a_generation_for_a_household_that_does_not_exist_is_a_port_error(
        self, repository: PostgresCuratedRowRepository
    ) -> None:
        """Postgres-only: the fake is a list with no `fk_curated_rows_user_id_users`.

        A raw `IntegrityError` escaping here would leave a caller no handling short
        of importing sqlalchemy itself.
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
        """`ck_curated_rows_cards_not_empty`, reached through the repository.

        `model_construct` is needed because `CuratedRow`'s own `min_length=1` refuses
        the empty row first; the CHECK is what catches a writer that bypasses the
        model. This case owns the translation, not the constraint itself.
        """
        valid = curated_row(user_id, position=0, generation_id=new_id())
        empty = valid.model_construct(**{**valid.model_dump(), "card_title_ids": ()})

        with pytest.raises(RepositoryConflict) as raised:
            await repository.replace_for_user(user_id, [empty])
        assert raised.value.constraint == "ck_curated_rows_cards_not_empty"

    async def test_one_row_id_twice_in_a_batch_is_a_port_error(
        self, repository: PostgresCuratedRowRepository, user_id: uuid.UUID
    ) -> None:
        """`pk_curated_rows`, translated rather than raised raw.

        Postgres-only: the fake is a list with no primary key, so a batch naming one
        id twice is stored twice there. An id reused across two shelves of one
        generation is a caller-assembly mistake nothing else in this port refuses,
        and the constraint name is what tells the caller the duplicate was its own.
        """
        generation, reused = new_id(), new_id()
        with pytest.raises(RepositoryConflict) as raised:
            await repository.replace_for_user(
                user_id,
                [
                    curated_row(user_id, position=0, generation_id=generation, row_id=reused),
                    curated_row(user_id, position=1, generation_id=generation, row_id=reused),
                ],
            )
        assert raised.value.constraint == "pk_curated_rows"

    async def test_a_position_wider_than_the_column_is_a_port_error(
        self, repository: PostgresCuratedRowRepository, user_id: uuid.UUID
    ) -> None:
        """The refusal that is not a constraint, translated all the same.

        A too-wide position is a driver-level error, not a named constraint, so
        `RepositoryConflict.constraint` is None and nothing raw escapes the port.
        """
        wide = curated_row(user_id, position=2**31, generation_id=new_id())

        with pytest.raises(RepositoryConflict) as raised:
            await repository.replace_for_user(user_id, [wide])

        assert raised.value.constraint is None
        # The session survives it, exactly as it survives a constraint --
        # the SAVEPOINT does not care which kind of refusal it rolled back.
        again = [curated_row(user_id, position=0, generation_id=new_id())]
        assert await repository.replace_for_user(user_id, again) == 1
        assert await repository.list_for_user(user_id) == again

    async def test_a_generation_that_fails_part_way_leaves_the_previous_screen_whole(
        self,
        repository: PostgresCuratedRowRepository,
        user_id: uuid.UUID,
        seeder: PostgresCuratedRowSeeder,
    ) -> None:
        """The SAVEPOINT, and why `replace_for_user` is one transaction.

        A delete-then-insert pair would leave the household with no screen at all
        when the insert half fails.
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
