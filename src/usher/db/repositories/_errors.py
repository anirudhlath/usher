"""Turning Postgres's own structured error fields into port errors.

One copy, shared by every repository that catches `IntegrityError`. Its
whole content is a verified observation about where asyncpg actually puts a
constraint name, and two copies of that are two chances to lose it.
"""

from sqlalchemy.exc import IntegrityError


def constraint_name(exc: IntegrityError) -> str | None:
    """The Postgres constraint name straight from asyncpg's own structured
    error fields -- not parsed out of the exception message text, which is
    dialect- and locale-dependent and was never meant to be machine-read.

    SQLAlchemy's asyncpg dialect wraps the driver's own exception in a
    DBAPI2-shaped one (`exc.orig`), but chains the original asyncpg
    exception onto it via `raise translated_error from error` (verified
    directly against `sqlalchemy.dialects.postgresql.asyncpg`'s source) --
    `exc.orig.__cause__` is that original `asyncpg.exceptions.PostgresError`,
    which carries `constraint_name` as a real attribute, populated from the
    `ErrorResponse` protocol field Postgres itself sends. Verified against a
    real unique-violation on both a plain primary key and a partial unique
    index: `exc.orig.constraint_name` does not exist (`exc.orig` is
    SQLAlchemy's wrapper, not the asyncpg exception itself) -- this is not
    a redundant safety check, it is the accessor that actually works.

    Best-effort: `None` if any layer of that chain isn't what's expected,
    rather than raising a second exception while already handling the
    first -- a caller that can't determine which constraint fired still
    gets a `RepositoryConflict`, just without the structured detail.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
