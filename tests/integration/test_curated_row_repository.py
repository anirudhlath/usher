"""`PostgresCuratedRowRepository` against the real database.

The shared contract runs here unchanged, and this is the arm where several of
its cases are load-bearing rather than structural — see
`tests/fakes/curated_row_repository.py` for the enumerated list. Chiefly the
delete's *scope*, which against a Python list cannot be got wrong by accident.
**Which two cases see a wrongly-scoped delete, and through which assertion
each one sees it, is stated once in the contract's module docstring**; it is
not restated here, because two of the three copies that fact used to have
drifted in opposite directions.

Plus the four things a list cannot express: a foreign key, a CHECK constraint,
the SAVEPOINT that makes a failed generation leave the previous screen whole,
and a session that survives being handed a generation the database refuses.

The seeder writes through a raw `INSERT` rather than through the port, because
the port deliberately cannot store two generations for one household: that is
what `replace_for_user` *means*, so a second generation has to arrive from
outside it or the newest-generation read is untestable.
"""

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

    Recursive over `Plans`, which is where Postgres nests an `InitPlan` and a
    `SubPlan` as well as ordinary children -- so a table probed once by an
    uncorrelated subquery and once by the outer scan appears twice, which is
    the whole measurement.
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
        """**One read of one household's shelves should be one look at the
        table, and the correlated-subquery spelling is two.**

        `list_for_user` runs on every home build, and the statement it shipped
        asked `curated_rows` for the newest `generation_id` and then asked
        `curated_rows` again for the rows carrying it. The second probe is the
        one that does not have an index to sit on: `ix_curated_rows_user_newest`
        is `(user_id, generated_at DESC)` and the outer scan's second predicate
        is `generation_id`, which the index does not carry, so it is a filter
        over everything the household has ever been given.

        Asserted on the plan rather than on the text, because the claim is
        about what Postgres does: a rewrite that merely moved the subquery into
        a CTE reads differently and probes the same table twice, and a `WITH …
        AS MATERIALIZED` would too.

        Two generations are seeded so the read has something to choose
        between -- with one generation stored, a wrong statement and a right
        one plan the same and answer the same. What the *choice* must be is
        `test_only_the_newest_generation_reaches_the_screen`'s, on both arms;
        this case asserts the cost of making it, and re-reads through the port
        afterwards so a plan measured against a statement nobody executes
        cannot pass.
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

    async def test_one_row_id_twice_in_a_batch_is_a_port_error(
        self, repository: PostgresCuratedRowRepository, user_id: uuid.UUID
    ) -> None:
        """`pk_curated_rows`, and it is here because the enumeration beside the
        `except` clause said "a CHECK or a foreign key" and was wrong by a
        whole class of constraint.

        Postgres-only: the fake is a list and has no primary key, so a batch
        naming one id twice is stored twice there. Reachable as a
        caller-assembly mistake -- an id reused across two shelves of one
        generation, which nothing else in this port refuses, since
        `replace_for_user`'s two `ValueError`s are about the household and the
        generation rather than about the ids.

        Also pins that it is *translated*: a raw `IntegrityError` out of here
        is the one thing ADR-0009 says must never happen, and the constraint
        name is what tells a caller this was its own duplicate rather than a
        conflict with somebody else's row.
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
        """**The refusal that is not a constraint**, and the one that crossed
        this port boundary raw until the `except` clause widened.

        `curated_rows."position"` is `integer` and `CuratedRow.position` is
        `Field(ge=0)` with **no ceiling**, so this row is a *validly
        constructed* domain model -- no `model_construct`, nothing bypassed --
        that the column cannot hold. `LLMCall.cost_usd` against `NUMERIC(12,
        8)` is the sibling shape, found one task earlier and server-side;
        this one is refused **client-side** by asyncpg's own binary encoder
        before a byte is sent, and arrives as a bare
        `sqlalchemy.exc.DBAPIError` with cause `asyncpg.exceptions.DataError`
        and SQLSTATE `22000`. Measured, and it is why `except IntegrityError`
        -- which every other repository in this package still uses, correctly,
        for tables whose refusals are all constraints -- is not what this one
        catches.

        The wrong implementation this kills is therefore the *obvious* one,
        and it was shipped: `except IntegrityError` lets this through
        untranslated, so a caller would have to import sqlalchemy to handle
        it.

        `constraint` is `None` here rather than a name, which is the honest
        answer: a column's declared width refusing a value is not a named
        constraint firing.
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
