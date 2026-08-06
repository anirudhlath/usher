"""`PostgresCreditRepository` against the real database.

The shared contract runs here unchanged, plus the four things a dict cannot
express: a foreign key, a CHECK constraint, the partial unique index doing its
one job, and a poisoned session.

`FakeCreditRepository`'s delete scope is structurally correct -- a dict filter
cannot be derived from the wrong collection by accident -- so
`test_replacing_for_a_title_with_no_new_credits_still_clears_it` is a real
assertion only here.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.credit_repository_contract import CreditRepositoryContract, credit
from tests.contract.person_repository_contract import person
from usher.db.repositories.people import PostgresCreditRepository, PostgresPersonRepository
from usher.db.repositories.title import PostgresTitleRepository
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict

_PEOPLE = {
    "lead_person": 93_000_070,
    "second_person": 93_000_071,
    "third_person": 93_000_072,
    "other_person": 93_000_073,
}


async def _title(session: AsyncSession) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name) "
            "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Film', 'An Invented Film')"
        ),
        {"id": title_id},
    )
    return title_id


class TestPostgresCreditRepository(CreditRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresCreditRepository:
        return PostgresCreditRepository(session)

    @pytest.fixture
    def titles(self, session: AsyncSession) -> PostgresTitleRepository:
        # The same session, so this reads the rows `replace_for_titles` wrote
        # in the transaction this test owns.
        return PostgresTitleRepository(session)

    @pytest_asyncio.fixture
    async def _seeded_people(self, session: AsyncSession) -> dict[str, uuid.UUID]:
        people = PostgresPersonRepository(session)
        await people.upsert_many(
            [person(tmdb_id, name.replace("_", " ").title()) for name, tmdb_id in _PEOPLE.items()]
        )
        resolved = await people.resolve_tmdb_ids(list(_PEOPLE.values()))
        return {name: resolved[tmdb_id] for name, tmdb_id in _PEOPLE.items()}

    @pytest.fixture
    def lead_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["lead_person"]

    @pytest.fixture
    def second_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["second_person"]

    @pytest.fixture
    def third_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["third_person"]

    @pytest.fixture
    def other_person(self, _seeded_people: dict[str, uuid.UUID]) -> uuid.UUID:
        return _seeded_people["other_person"]

    @pytest_asyncio.fixture
    async def title_id(self, session: AsyncSession) -> uuid.UUID:
        return await _title(session)

    @pytest_asyncio.fixture
    async def other_title_id(self, session: AsyncSession) -> uuid.UUID:
        return await _title(session)

    async def test_a_credit_naming_no_title_is_a_port_error(
        self, repository: PostgresCreditRepository, lead_person: uuid.UUID
    ) -> None:
        """Postgres-only: the fake is a dict and has nothing to violate.

        `fk_credits_title_id_titles`. A raw `IntegrityError` escaping here is
        the one thing ADR-0009 says must never happen -- the only way a caller
        could handle it is to import sqlalchemy itself.
        """
        orphan = new_id()
        with pytest.raises(RepositoryConflict):
            await repository.replace_for_titles(
                [orphan], [credit(orphan, lead_person)], credit_names={}
            )

    async def test_a_credit_naming_no_person_is_a_port_error(
        self, repository: PostgresCreditRepository, title_id: uuid.UUID
    ) -> None:
        """`fk_credits_person_id_people`, the other arm.

        This is what `PersonRepository.resolve_tmdb_ids`' "absent means no
        such person" rule protects against: a resolve that minted an id for an
        unknown `tmdb_id` would land here, one statement later, and the fake
        would have accepted it silently.
        """
        with pytest.raises(RepositoryConflict):
            await repository.replace_for_titles(
                [title_id], [credit(title_id, new_id())], credit_names={}
            )

    async def test_a_credit_id_held_by_another_title_is_a_port_error(
        self, repository: PostgresCreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """`ix_credits_tmdb_credit_id` doing the one job it has.

        Uniqueness is **not** what makes the derivation idempotent -- the
        title-scoped delete is that. This index exists so a bug in the
        delete's *scope* raises a `RepositoryConflict` instead of silently
        doubling a title's cast on every pass. Here the second title is
        outside the first's scope, so the delete legitimately does not reach
        it and the index is what speaks.

        The fake has no unique index at all, so this is Postgres-only.
        """
        other = await _title(repository._session)
        await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person, tmdb_credit_id="7" * 24)], credit_names={}
        )
        with pytest.raises(RepositoryConflict):
            await repository.replace_for_titles(
                [other], [credit(other, lead_person, tmdb_credit_id="7" * 24)], credit_names={}
            )

    async def test_a_negative_billing_order_is_a_port_error(
        self, repository: PostgresCreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """`ck_credits_billing_order_non_negative`, which fires at the
        `INSERT ... SELECT` rather than during the `COPY` -- the staging table
        carries no constraints, deliberately, so the violation surfaces one
        statement later where SQLAlchemy can translate it.

        Constructed by bypassing the model, because `Credit`'s own `ge=0`
        refuses it first -- which is exactly why the CHECK exists: the bulk
        path constructs no pydantic model at all.
        """
        valid = credit(title_id, lead_person, billing_order=0)
        broken = valid.model_construct(
            **{**valid.model_dump(), "billing_order": -1},
        )
        with pytest.raises(RepositoryConflict):
            await repository.replace_for_titles([title_id], [broken], credit_names={})

    async def test_the_session_survives_a_conflicting_batch(
        self, repository: PostgresCreditRepository, title_id: uuid.UUID, lead_person: uuid.UUID
    ) -> None:
        """The SAVEPOINT. `DeriveService` commits credits together with its
        job checkpoint, so a caught conflict must leave the session usable --
        without `begin_nested()` the next unrelated call raises
        `PendingRollbackError` and the failure is attributed to whatever ran
        next.
        """
        with pytest.raises(RepositoryConflict):
            await repository.replace_for_titles(
                [title_id], [credit(title_id, new_id())], credit_names={}
            )

        written = await repository.replace_for_titles(
            [title_id], [credit(title_id, lead_person, billing_order=0)], credit_names={}
        )
        assert written == 1
