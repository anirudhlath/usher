"""Turning Postgres's own structured error fields into port errors."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.errors import RepositoryConflict

#: **SQLSTATE class, not exception class.** Catching `IntegrityError` is right for a
#: table whose only refusals are constraints and wrong for one whose column can refuse
#: a *value*: SQLAlchemy's asyncpg dialect leaves those a bare `DBAPIError`, so neither
#: obvious `except` clause catches them and a raw exception crosses the port boundary.
ROW_REFUSED_SQLSTATE_CLASSES = frozenset({"22", "23"})


def is_row_refusal(exc: DBAPIError) -> bool:
    """Whether the store refused *this row* rather than the connection or the statement.

    `IntegrityError` is honoured directly as well as by its SQLSTATE, and not as
    redundancy: the sqlstate is read off the same best-effort
    `exc.orig.__cause__` chain `constraint_name` uses, so a chain that is not
    what was expected degrades to the answer every sibling repository gives
    rather than letting an integrity violation through untranslated.
    """
    if isinstance(exc, IntegrityError):
        return True
    sqlstate = getattr(getattr(exc.orig, "__cause__", None), "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate[:2] in ROW_REFUSED_SQLSTATE_CLASSES


def constraint_name(exc: DBAPIError) -> str | None:
    """The Postgres constraint name straight from asyncpg's own structured error fields.

    Not parsed out of the message text, which is dialect- and locale-dependent
    and was never meant to be machine-read.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


@asynccontextmanager
async def refusals_as_conflict(session: AsyncSession, message: str) -> AsyncIterator[None]:
    """Runs a repository's statements so a refused *row* surfaces as `RepositoryConflict`."""
    try:
        with session.no_autoflush:
            async with session.begin_nested():
                yield
    except DBAPIError as exc:
        if not is_row_refusal(exc):
            raise
        # Translated so nothing above this layer imports `sqlalchemy.exc`, and
        # raised outside the SAVEPOINT's own scope, so the rollback has already
        # happened by the time the caller sees this.
        raise RepositoryConflict(message, constraint=constraint_name(exc)) from exc
