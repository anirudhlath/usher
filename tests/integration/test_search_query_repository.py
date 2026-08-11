"""`PostgresSearchQueryRepository` against the real database.

The shared contract runs here unchanged, and this is the arm where nearly all
of it is load-bearing rather than structural -- `tests/fakes/
search_query_repository.py` enumerates the four things a dict cannot express.
Plus the cases only a real column, a real foreign key and a real transaction
can produce: a `latency_ms` too large for the `integer` column that holds it,
an empty `query` the table's own CHECK refuses, a `user_id` and a
`clicked_title_id` naming no row, and the SAVEPOINT that lets a caller keep
using its session after a refused write.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.contract.search_query_repository_contract import (
    SearchQueryLedger,
    SearchQueryRepositoryContract,
    StoredSearchQuery,
    search_query_record,
)
from usher.db.repositories.search_query import PostgresSearchQueryRepository
from usher.domain.ids import new_id
from usher.ports.errors import RepositoryConflict
from usher.ports.search import SearchMode

_READ_ONE = "SELECT * FROM search_queries WHERE id = CAST(:id AS uuid)"


class PostgresSearchQueryLedger(SearchQueryLedger):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, query_id: uuid.UUID) -> StoredSearchQuery | None:
        result = await self._session.execute(text(_READ_ONE), {"id": query_id})
        row = result.one_or_none()
        if row is None:
            return None
        mapping = row._mapping
        return StoredSearchQuery(
            id=mapping["id"],
            at=mapping["at"],
            user_id=mapping["user_id"],
            query=mapping["query"],
            mode=SearchMode(mapping["mode"]),
            result_count=mapping["result_count"],
            latency_ms=mapping["latency_ms"],
            clicked_title_id=mapping["clicked_title_id"],
            played=mapping["played"],
        )

    async def count(self) -> int:
        found = await self._session.execute(text("SELECT count(*) FROM search_queries"))
        return int(found.scalar_one())


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = new_id()
    await session.execute(
        text("INSERT INTO users (id, name) VALUES (CAST(:id AS uuid), :name)"),
        {"id": user_id, "name": f"viewer-{user_id}"},
    )
    return user_id


async def _seed_title(session: AsyncSession) -> uuid.UUID:
    title_id = new_id()
    await session.execute(
        text(
            "INSERT INTO titles (id, kind, name, sort_name) "
            "VALUES (CAST(:id AS uuid), 'movie', 'An Invented Title', 'An Invented Title')"
        ),
        {"id": title_id},
    )
    return title_id


class TestPostgresSearchQueryRepository(SearchQueryRepositoryContract):
    @pytest.fixture(autouse=True)
    def _bind(self, session: AsyncSession) -> None:
        self._session = session

    @pytest.fixture
    def repository(self, session: AsyncSession) -> PostgresSearchQueryRepository:
        return PostgresSearchQueryRepository(session)

    @pytest.fixture
    def ledger(self, session: AsyncSession) -> PostgresSearchQueryLedger:
        # The same session, so what the contract writes and what it reads
        # back are in the transaction this test owns.
        return PostgresSearchQueryLedger(session)

    @pytest.fixture
    async def user_id(self, session: AsyncSession) -> uuid.UUID:
        return await _seed_user(session)

    async def add_title(self) -> uuid.UUID:
        return await _seed_title(self._session)

    async def test_a_query_naming_no_household_is_a_port_error(
        self,
        repository: PostgresSearchQueryRepository,
        ledger: PostgresSearchQueryLedger,
    ) -> None:
        """`fk_search_queries_user_id_users`, reached through the repository
        rather than through raw SQL.

        The wrong implementation this kills: a `record()` that catches only
        the numeric-overflow shape and lets an ordinary foreign-key violation
        cross the port boundary raw.
        """
        stray = search_query_record(user_id=new_id())

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(stray)

        assert raised.value.constraint == "fk_search_queries_user_id_users"
        assert await ledger.count() == 0

    async def test_an_empty_query_is_refused_by_the_table(
        self,
        repository: PostgresSearchQueryRepository,
        ledger: PostgresSearchQueryLedger,
        user_id: uuid.UUID,
    ) -> None:
        """`ck_search_queries_query_not_empty`, reached through the
        repository. An analytics row carrying no query text answers no
        question a dashboard could ask of it."""
        blank = search_query_record(user_id=user_id, query="")

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(blank)

        assert raised.value.constraint == "ck_search_queries_query_not_empty"
        assert await ledger.count() == 0

    async def test_a_latency_the_column_cannot_hold_is_a_port_error(
        self,
        repository: PostgresSearchQueryRepository,
        ledger: PostgresSearchQueryLedger,
        user_id: uuid.UUID,
    ) -> None:
        """**The case the whole error contract rests on**, and Postgres-only
        because a Python `int` has no ceiling to hit.

        `latency_ms` is `integer`, so `2**31` overflows it -- reachable
        because `SearchQueryRecord.latency_ms` is a plain, unbounded `int`,
        the identical shape `curated_rows."position"` and
        `genome_tags.tag_id` measured
        (`.claude/rules/db-and-sql.md`). It is refused **client-side**, by
        asyncpg's own binary encoder, before a byte reaches Postgres --
        `sqlalchemy.exc.DBAPIError`, `exc.orig.__cause__` an
        `asyncpg.exceptions.DataError`, SQLSTATE `22000`, and there is no
        constraint to name: this is the column's declared width refusing a
        value, not a named constraint firing.

        **The exception this must catch is not the obvious one.** An
        implementation catching `IntegrityError` alone -- which is most
        sibling repositories' house style, and was this table's precedent
        before the measurement -- lets a raw SQLAlchemy exception cross the
        port boundary, and the only way a caller could then handle it is to
        import `sqlalchemy` itself, the one thing ADR-0009 forbids.
        """
        too_large = search_query_record(user_id=user_id, latency_ms=2**31)

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record(too_large)

        assert raised.value.constraint is None
        assert await ledger.count() == 0

    async def test_attributing_to_a_title_that_does_not_exist_is_a_port_error(
        self,
        repository: PostgresSearchQueryRepository,
        ledger: PostgresSearchQueryLedger,
        user_id: uuid.UUID,
    ) -> None:
        """`fk_search_queries_clicked_title_id_titles`, reached through
        `record_outcome`. A stale or forged title id from a client must not
        silently attribute a search to nothing storable."""
        record = search_query_record(user_id=user_id)
        await repository.record(record)

        with pytest.raises(RepositoryConflict) as raised:
            await repository.record_outcome(record.id, clicked_title_id=new_id(), played=True)

        assert raised.value.constraint == "fk_search_queries_clicked_title_id_titles"
        stored = await ledger.get(record.id)
        assert stored is not None
        assert stored.clicked_title_id is None, (
            "the refused attribution must not have partially landed"
        )

    async def test_a_refused_query_leaves_the_session_usable(
        self,
        repository: PostgresSearchQueryRepository,
        ledger: PostgresSearchQueryLedger,
        user_id: uuid.UUID,
    ) -> None:
        """**The SAVEPOINT.** The wrong implementation this kills: a
        `record()` with no nested transaction, whose refused `INSERT` aborts
        the caller's whole transaction so the very next statement raises
        `PendingRollbackError` -- turning a failed analytics write into a
        lost request.

        Three assertions, in the order the damage would arrive: the earlier
        row is still there, the refused row is not, and a subsequent
        unrelated `record()` on the same session both succeeds and is
        visible.
        """
        earlier = search_query_record(user_id=user_id)
        await repository.record(earlier)

        with pytest.raises(RepositoryConflict):
            await repository.record(search_query_record(user_id=user_id, latency_ms=2**31))

        assert await ledger.get(earlier.id) is not None
        assert await ledger.count() == 1

        later = search_query_record(user_id=user_id)
        await repository.record(later)
        assert await ledger.get(later.id) is not None
        assert await ledger.count() == 2

    async def test_search_queries_carries_no_updated_at_trigger(
        self, session: AsyncSession
    ) -> None:
        """M1's second ruling: outcome columns are updated in place on the
        row `record()` wrote, and first write wins, so no row is ever
        touched more than twice in its whole life -- `llm_calls`' shape
        rather than `watch_states`'. Mechanically required as well as
        argued: `test_migration_creates_the_updated_at_triggers` asserts the
        trigger set exactly, so a trigger here would be a failing case in
        another file; this is the same fact from the side that would notice
        it first.
        """
        triggers = (
            await session.execute(
                text(
                    "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = 'search_queries' AND NOT t.tgisinternal"
                )
            )
        ).all()

        assert triggers == []

    async def test_a_failure_that_is_not_the_rows_fault_is_not_reported_as_one(
        self,
        repository: PostgresSearchQueryRepository,
        ledger: PostgresSearchQueryLedger,
        session: AsyncSession,
        user_id: uuid.UUID,
    ) -> None:
        """The other side of the error contract: a dropped connection, a
        statement timeout or a missing table must not arrive at a caller as
        "this row is not storable" -- the one distinction ADR-0009 requires a
        caller be able to make, since a bad row is a bug in the analytics
        write and a transport that is gone is something a retry fixes.

        SQLSTATE `42P01` (undefined table) is class 42, outside the `22`/`23`
        classes this repository's SAVEPOINT translates, and deterministic
        where a timeout would not be. Captured by hand rather than with
        `pytest.raises(DBAPIError)`, because under the mutation this case
        exists to kill `record()` raises `RepositoryConflict` -- a
        `UsherPortError` and therefore not a `DBAPIError` -- so
        `pytest.raises` would decline it and fail the case before a single
        assertion runs.
        """
        raised: Exception | None = None
        await session.execute(
            text("ALTER TABLE search_queries RENAME TO search_queries_moved_away")
        )
        try:
            await repository.record(search_query_record(user_id=user_id))
        # Deliberately wide: which exception this is *is* the assertion below.
        except Exception as exc:
            raised = exc
        finally:
            await session.execute(
                text("ALTER TABLE search_queries_moved_away RENAME TO search_queries")
            )

        assert raised is not None, "a write against a table that is not there did not raise"
        assert not isinstance(raised, RepositoryConflict), (
            f"an undefined table reached the caller as {type(raised).__name__}, which tells a "
            "caller the row was wrong when the schema is what is missing"
        )
        assert isinstance(raised, DBAPIError)
        cause = getattr(raised.orig, "__cause__", None)
        assert getattr(cause, "sqlstate", None) == "42P01"
        assert await ledger.count() == 0
