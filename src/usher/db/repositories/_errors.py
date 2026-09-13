"""Turning Postgres's own structured error fields into port errors."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.ports.errors import RepositoryConflict

# : **SQLSTATE class, not exception class.** Most repositories in this package : catch
# `IntegrityError`, which is right for a table whose only refusals are : constraints and
# wrong for one whose column can refuse a *value*: SQLAlchemy's : asyncpg dialect leaves
# those as a bare `DBAPIError`, so neither of the two : obvious `except` clauses catches
# them and a raw SQLAlchemy exception crosses : the port boundary -- the one thing
ROW_REFUSED_SQLSTATE_CLASSES = frozenset({"22", "23"})


def is_row_refusal(exc: DBAPIError) -> bool:
    """Whether the backing store refused *this row* rather than the connection or the statement.

    see `ROW_REFUSED_SQLSTATE_CLASSES` for the measurements behind the two SQLSTATE
    classes.

    `IntegrityError` is honoured directly as well as by its SQLSTATE, and that
    is not redundancy for its own sake: the sqlstate is read off the same
    best-effort `exc.orig.__cause__` chain `constraint_name` documents, so a
    layer of it not being what is expected must degrade to the answer every
    sibling repository already gives rather than to letting an integrity
    violation through untranslated.
    """
    if isinstance(exc, IntegrityError):
        return True
    sqlstate = getattr(getattr(exc.orig, "__cause__", None), "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate[:2] in ROW_REFUSED_SQLSTATE_CLASSES


def constraint_name(exc: DBAPIError) -> str | None:
    """The Postgres constraint name straight from asyncpg's own structured error fields.

    not parsed out of the exception message text, which is dialect- and locale-dependent
    and was never meant to be machine-read.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)


@asynccontextmanager
async def refusals_as_conflict(session: AsyncSession, message: str) -> AsyncIterator[None]:
    """Runs a repository's own statements so that a refused *row* reaches the caller as a.

    `RepositoryConflict` and nothing else is disturbed.
    """
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
