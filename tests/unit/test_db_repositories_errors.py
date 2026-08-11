"""`usher.db.repositories._errors`, the module three repositories now share.

`is_row_refusal` and `constraint_name` are exercised end to end by every
repository that catches with them, against a real driver, in
`tests/integration/`. What is pinned *here* is `refusals_as_conflict` — the
context manager M8 factored out of `PostgresCuratedRowRepository.
replace_for_user`, `PostgresLLMCallRepository.record` and
`BulkCatalogRepository.replace_genome_tags`, which had shipped the same
five-line pyramid three times.

**The session is a stub, deliberately, and that is a claim about what this file
can and cannot say.** What a stub can show is the part that was copied: that
the body runs inside a SAVEPOINT with autoflush suppressed, in that order; that
a refusal is translated and everything else is not; and that the SAVEPOINT is
unwound *before* the port error is raised, which is what leaves the caller a
usable session. What it cannot show is that a real `AsyncSession`'s SAVEPOINT
actually restores the transaction -- that needs Postgres, and
`tests/integration/test_curated_row_repository.py::
test_a_generation_that_fails_part_way_leaves_the_previous_screen_whole` and
`tests/integration/test_llm_call_repository.py::
test_a_refused_call_leaves_the_earlier_rows_and_the_session_usable` are where
it is said. Neither file is replaced by this one; this one is why they now
describe one implementation instead of three.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from typing import cast

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.repositories._errors import refusals_as_conflict
from usher.ports.errors import RepositoryConflict, UsherPortError


class _DriverError(Exception):
    """asyncpg's own exception, in the two fields `_errors.py` reads off it.

    Both are read through `exc.orig.__cause__` -- SQLAlchemy wraps the driver's
    exception and chains the original onto the wrapper -- so the fake has to be
    two layers deep or it would pin an accessor that does not exist.
    """

    def __init__(self, sqlstate: str | None, constraint_name: str | None = None) -> None:
        super().__init__(sqlstate or "")
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _wrapped(cause: _DriverError) -> Exception:
    wrapper = Exception("the dialect's DBAPI-shaped wrapper")
    wrapper.__cause__ = cause
    return wrapper


def _refusal(sqlstate: str, constraint: str | None = None) -> DBAPIError:
    return DBAPIError(
        "INSERT INTO invented VALUES (1)", {}, _wrapped(_DriverError(sqlstate, constraint))
    )


def _integrity_error(constraint: str) -> IntegrityError:
    """The shape every *other* repository in this package raises.

    No SQLSTATE on the chain at all: `is_row_refusal` honours `IntegrityError`
    by type as well as by SQLSTATE, and this is the case that says the shared
    helper still goes through that predicate rather than a hand-rolled sqlstate
    check that would let a plain integrity violation escape untranslated.
    """
    return IntegrityError(
        "INSERT INTO invented VALUES (1)", {}, _wrapped(_DriverError(None, constraint))
    )


class _RecordingSession:
    """An `AsyncSession` in exactly the two members the helper touches.

    Records the order it was driven in, because the ordering is the thing the
    three copies agreed on and the thing a fourth caller could get wrong:
    `no_autoflush` outside the SAVEPOINT, and the SAVEPOINT unwound before the
    translation runs.
    """

    def __init__(self) -> None:
        self.events: list[str] = []

    @property
    def no_autoflush(self) -> AbstractContextManager[None]:
        return self._no_autoflush()

    @contextmanager
    def _no_autoflush(self) -> Iterator[None]:
        self.events.append("autoflush suppressed")
        try:
            yield
        finally:
            self.events.append("autoflush restored")

    def begin_nested(self) -> AbstractAsyncContextManager[None]:
        return self._savepoint()

    @asynccontextmanager
    async def _savepoint(self) -> AsyncIterator[None]:
        self.events.append("savepoint opened")
        try:
            yield
        except BaseException:
            self.events.append("savepoint rolled back")
            raise
        self.events.append("savepoint released")


def _session() -> tuple[_RecordingSession, AsyncSession]:
    recorded = _RecordingSession()
    return recorded, cast(AsyncSession, recorded)


async def test_the_body_runs_in_a_savepoint_with_autoflush_suppressed() -> None:
    """The order, which is what was copied three times.

    `no_autoflush` is outside: a shared session can be carrying some other
    call's unflushed, invalid row, and a flush of *that* inside this SAVEPOINT
    would be reported to this caller as its own row being refused.
    """
    recorded, session = _session()

    async with refusals_as_conflict(session, "an invented artefact is out of bounds"):
        recorded.events.append("the write")

    assert recorded.events == [
        "autoflush suppressed",
        "savepoint opened",
        "the write",
        "savepoint released",
        "autoflush restored",
    ]


async def test_a_refused_row_is_translated_and_carries_its_constraint() -> None:
    """SQLSTATE class 23, the shape every constraint on these three tables
    produces, and the message is the caller's rather than the helper's -- three
    tables refusing a row for three different reasons say three different
    things to a service."""
    recorded, session = _session()
    refusal = _refusal("23503", "fk_curated_rows_user_id_users")

    with pytest.raises(RepositoryConflict) as raised:
        async with refusals_as_conflict(session, "a curated generation is out of bounds"):
            raise refusal

    assert str(raised.value) == "a curated generation is out of bounds"
    assert raised.value.constraint == "fk_curated_rows_user_id_users"
    assert raised.value.__cause__ is refusal
    # The SAVEPOINT is unwound *before* the port error leaves, which is the
    # whole of what it buys the caller: `CurationService` catches this and
    # still has a ledger entry to write on the same session.
    assert recorded.events == [
        "autoflush suppressed",
        "savepoint opened",
        "savepoint rolled back",
        "autoflush restored",
    ]


async def test_a_value_the_column_cannot_hold_is_translated_without_a_name() -> None:
    """SQLSTATE class 22 -- `curated_rows."position"` at `2**31` and
    `llm_calls.cost_usd` above `$9,999.99999999` -- which is the pair that made
    `except IntegrityError` the wrong clause for these three callers.

    `constraint` is `None` and that is the honest answer: a column's declared
    width refusing a value is not a named constraint firing.
    """
    _, session = _session()

    with pytest.raises(RepositoryConflict) as raised:
        async with refusals_as_conflict(session, "an llm call is out of bounds"):
            raise _refusal("22003")

    assert raised.value.constraint is None


async def test_a_plain_integrity_error_is_still_a_refusal() -> None:
    """No SQLSTATE on the chain at all, which is how a refusal arrives when
    any layer of the best-effort accessor is not what was expected.

    It must still translate: degrading to "propagate" would let an integrity
    violation cross the port boundary raw, which is the one thing ADR-0009
    forbids and the thing every sibling repository's `except IntegrityError`
    already gets right.
    """
    _, session = _session()

    with pytest.raises(RepositoryConflict) as raised:
        async with refusals_as_conflict(session, "a genome tag vocabulary is out of bounds"):
            raise _integrity_error("ck_genome_tags_tag_id_in_vocabulary")

    assert raised.value.constraint == "ck_genome_tags_tag_id_in_vocabulary"


async def test_a_failure_that_is_not_the_rows_fault_is_not_translated() -> None:
    """Class 42 -- an undefined table, standing in for the dropped connection
    and the statement timeout that are not deterministic enough to write.

    Captured rather than `pytest.raises(DBAPIError)`: under the mutation this
    kills the helper raises `RepositoryConflict`, which is not a `DBAPIError`,
    so `pytest.raises` would decline it and the case would fail before the
    discriminating assertion ever ran.
    """
    _, session = _session()
    raised: BaseException | None = None

    try:
        async with refusals_as_conflict(session, "an invented artefact is out of bounds"):
            raise _refusal("42P01")
    # Deliberately wide: which exception this is *is* the assertion below.
    except Exception as exc:
        raised = exc

    assert not isinstance(raised, UsherPortError), (
        f"an undefined table reached the caller as {type(raised).__name__}, which tells a "
        "service the row was wrong when the schema is what is missing"
    )
    assert isinstance(raised, DBAPIError)
