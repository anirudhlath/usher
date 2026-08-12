"""`PostgresPersonRepository` against the real database.

The contract suite runs here unchanged -- that is the point of it -- plus the
cases `FakePersonRepository` documents itself as unable to express:

- **Foreign keys.** A credit naming a `person_id` no row carries is a
  `RepositoryConflict` here and silently fine in a dict.
- **`xmax = 0`.** `test_upsert_reports_inserts_and_updates_separately` is a
  real assertion only here; the fake computes the answer from dict membership.
- **`SELECT DISTINCT ON`.** `test_a_duplicate_person_inside_one_batch_is_tolerated`
  passes in the fake because a dict is structurally last-wins, and passes here
  only if the staging read deduplicates.
- **The join through `episodes`.**
  `test_an_episode_watch_state_reaches_its_series_credits` is a real join here
  and a reproduced one there.
- **CHECK constraints**, which fire at the `INSERT ... SELECT` rather than
  during the `COPY`, because the staging table carries none.
"""

import uuid
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.person_repository_contract import (
    PersonHistorySeeder,
    PersonRepositoryContract,
    person,
)
from usher.db.repositories.people import PostgresPersonRepository
from usher.domain.ids import new_id
from usher.domain.people import CreditKind, Person
from usher.ports.errors import RepositoryConflict


class PostgresPersonHistorySeeder(PersonHistorySeeder):
    """Real rows: `titles`, `seasons`, `episodes`, `credits`, `watch_states`.

    Raw SQL rather than repositories, because every one of those is a
    different port and this file is about `PersonRepository`. The seasons
    exist only because `episodes.season_id` is `NOT NULL` with a CASCADE FK --
    an episode cannot exist without one.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._episode_number = 0

    async def movie(self) -> uuid.UUID:
        title_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) "
                "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Film', 'An Invented Film')"
            ),
            {"id": title_id},
        )
        return title_id

    async def series_with_episodes(self, count: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
        title_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO titles (id, kind, name, sort_name) "
                "VALUES (CAST(:id AS uuid), 'series', 'An Invented Series', 'An Invented Series')"
            ),
            {"id": title_id},
        )
        season_id = new_id()
        await self._session.execute(
            text(
                "INSERT INTO seasons (id, title_id, season_number) "
                "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), 1)"
            ),
            {"id": season_id, "title_id": title_id},
        )
        episode_ids = []
        for _ in range(count):
            self._episode_number += 1
            episode_id = new_id()
            await self._session.execute(
                text(
                    "INSERT INTO episodes "
                    "(id, title_id, season_id, season_number, episode_number) "
                    "VALUES (CAST(:id AS uuid), CAST(:title_id AS uuid), "
                    "CAST(:season_id AS uuid), 1, :number)"
                ),
                {
                    "id": episode_id,
                    "title_id": title_id,
                    "season_id": season_id,
                    "number": self._episode_number,
                },
            )
            episode_ids.append(episode_id)
        return title_id, episode_ids

    async def credit(
        self,
        *,
        person_id: uuid.UUID,
        title_id: uuid.UUID,
        kind: CreditKind = CreditKind.CAST,
        job: str | None = None,
        character: str | None = None,
    ) -> None:
        await self._session.execute(
            text(
                "INSERT INTO credits "
                '(id, person_id, title_id, kind, source, job, "character") '
                "VALUES (CAST(:id AS uuid), CAST(:person_id AS uuid), "
                "CAST(:title_id AS uuid), :kind, 'tmdb', :job, :character)"
            ),
            {
                "id": new_id(),
                "person_id": person_id,
                "title_id": title_id,
                "kind": kind.value,
                "job": job,
                "character": character,
            },
        )

    async def stored(self, person_id: uuid.UUID) -> Person:
        row = (
            await self._session.execute(
                text("SELECT * FROM people WHERE id = CAST(:id AS uuid)"), {"id": person_id}
            )
        ).one()
        return Person.model_validate(dict(row._mapping))

    async def watched(
        self,
        *,
        user_id: uuid.UUID,
        title_id: uuid.UUID | None = None,
        episode_id: uuid.UUID | None = None,
        played: bool = True,
        last_played_at: datetime | None = None,
    ) -> None:
        await self._session.execute(
            text(
                "INSERT INTO watch_states "
                "(id, user_id, title_id, episode_id, played, position_seconds, play_count, "
                " last_played_at, origin, updated_at) "
                "VALUES (CAST(:id AS uuid), CAST(:user_id AS uuid), CAST(:title_id AS uuid), "
                "CAST(:episode_id AS uuid), :played, 0, 0, "
                "CAST(:last_played_at AS timestamptz), 'source', now())"
            ),
            {
                "id": new_id(),
                "user_id": user_id,
                "title_id": title_id,
                "episode_id": episode_id,
                "played": played,
                "last_played_at": last_played_at,
            },
        )


async def _user(session: AsyncSession, *, name: str) -> uuid.UUID:
    user_id = new_id()
    await session.execute(
        text("INSERT INTO users (id, name, is_default) VALUES (CAST(:id AS uuid), :name, false)"),
        {"id": user_id, "name": name},
    )
    return user_id


class TestPostgresPersonRepository(PersonRepositoryContract):
    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresPersonRepository:
        return PostgresPersonRepository(session)

    @pytest.fixture
    def seeder(self, session: AsyncSession) -> PostgresPersonHistorySeeder:
        return PostgresPersonHistorySeeder(session)

    @pytest_asyncio.fixture
    async def user_id(self, session: AsyncSession) -> uuid.UUID:
        return await _user(session, name="A Household Member")

    @pytest_asyncio.fixture
    async def other_user_id(self, session: AsyncSession) -> uuid.UUID:
        return await _user(session, name="Another Household Member")

    async def test_upsert_reports_inserts_and_updates_separately(
        self, repository: PostgresPersonRepository
    ) -> None:
        """`xmax = 0` in `RETURNING` is the only way to tell an insert from an
        update -- rowcount reports their sum.

        The wrong implementation this kills: `RETURNING true`, or returning
        `(len(rows), 0)`. **This is the one property the fake cannot express
        at all**: it computes the split from dict membership, which *is* the
        answer rather than a measurement of it.

        A mixed batch rather than two calls, because the split is only
        interesting when both arms fire in one statement.
        """
        await repository.upsert_many([person(93_000_060, "Already Here")])
        mixed = await repository.upsert_many(
            [person(93_000_060, "Already Here"), person(93_000_061, "Brand New")]
        )
        assert (mixed.inserted, mixed.updated) == (1, 1)

    async def test_a_person_whose_name_violates_the_check_is_a_port_error(
        self, repository: PostgresPersonRepository
    ) -> None:
        """`ck_people_name_not_empty` fires at the `INSERT ... SELECT`, not
        during the `COPY`: the staging table deliberately carries no
        constraints, so a bad value reaches Postgres and fails one statement
        later -- which goes through SQLAlchemy and is therefore translatable.
        `copy_records_to_table` runs on the raw asyncpg connection, outside
        SQLAlchemy's error translation, and would raise
        `asyncpg.exceptions.CheckViolationError` straight through.

        Constructed by bypassing the model's own validation rather than
        through `Person(name="")`, whose `min_length=1` refuses it first --
        which is the whole reason the CHECK exists: the bulk path constructs
        no pydantic model at all.
        """
        empty_named = Person.model_construct(
            id=new_id(),
            tmdb_id=93_000_062,
            name="",
            sort_name="Placeholder",
            known_for_department=None,
        )
        with pytest.raises(RepositoryConflict):
            await repository.upsert_many([empty_named])

    async def test_the_session_survives_a_conflicting_batch(
        self, repository: PostgresPersonRepository
    ) -> None:
        """The SAVEPOINT, asserted rather than assumed. `DeriveService`
        commits a batch of people together with its job checkpoint, so a
        caught conflict must leave the session usable -- without
        `begin_nested()` the next unrelated call raises
        `PendingRollbackError` and the failure is attributed to whatever ran
        next.
        """
        broken = Person.model_construct(
            id=new_id(),
            tmdb_id=93_000_063,
            name="",
            sort_name="Placeholder",
            known_for_department=None,
        )
        with pytest.raises(RepositoryConflict):
            await repository.upsert_many([broken])

        result = await repository.upsert_many([person(93_000_064, "Perfectly Fine")])
        assert (result.inserted, result.updated) == (1, 0)

    async def test_a_batch_of_five_hundred_costs_a_bounded_number_of_statements(
        self, repository: PostgresPersonRepository, session: AsyncSession
    ) -> None:
        """The fake's `calls` counter cannot express this and this case counts
        real statements instead -- `FakeEpisodeRepository` records the same
        split.

        Bounded and independent of batch size: the DDL, the `COPY` (which
        asyncpg issues on the raw connection and SQLAlchemy therefore never
        sees) and one `INSERT ... SELECT`. A per-row ORM write here is the
        ~19 minutes of pure repository overhead `PostgresEpisodeRepository`
        measured one table over.
        """
        from sqlalchemy import event

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
            statements.append(statement)

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            await repository.upsert_many(
                [person(94_000_000 + index, f"Person {index}") for index in range(500)]
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert len(statements) <= 6, statements
