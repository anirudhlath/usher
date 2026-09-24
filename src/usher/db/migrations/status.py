"""Migration status: the code's expected head revision against what a database reports."""

from functools import lru_cache

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import usher.db.migrations as _migrations_package


@lru_cache
def code_head_revision() -> str | None:
    """The single migration revision this deployed code expects.

    Computed once per process from the `versions/` directory shipped alongside
    this package -- not from `alembic.ini`, which would need a path correct both
    for a local `uv run` (CWD is the repo root) and a container, where the
    site-packages location need not match where `alembic.ini` was `COPY`'d to.
    `usher.db.migrations.__path__` moves with the package under either.

    `None` when there are zero or more than one head: a branched history makes
    "the" expected revision ambiguous rather than something to silently pick one
    of.
    """
    (location,) = _migrations_package.__path__
    return ScriptDirectory(location).get_current_head()


async def database_revision(session: AsyncSession) -> str | None:
    """The revision Alembic's own bookkeeping table says this database is actually at.

    Raises like any other failed query if the `alembic_version` table
    doesn't exist (a database that predates any migration) or the
    connection fails -- callers already have to handle that class of
    failure for the plain connectivity check, so this reuses the same
    handling rather than adding a second, differently-shaped way to
    report "couldn't tell". Returns `None` only when the table exists but
    is empty (e.g. `alembic stamp base`), which is a distinct, genuine
    "unmigrated" state, not a failure.
    """
    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    return result.scalars().first()
