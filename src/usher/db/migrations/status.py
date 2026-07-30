"""Migration status: compares the code's expected head revision against
what a live database reports, so readiness can fail on a schema mismatch
instead of guessing -- PRD 08: "the app refuses to serve on a schema
mismatch rather than guessing."
"""

from functools import lru_cache

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import usher.db.migrations as _migrations_package


@lru_cache
def code_head_revision() -> str | None:
    """The single migration revision this deployed code expects.

    Computed once per process (the same `@lru_cache`-on-a-zero-arg-function
    pattern as `usher.config.get_settings`) from the `versions/` directory
    shipped alongside this package itself -- not from `alembic.ini`, which
    would need a path that's correct both for a local `uv run` invocation
    (CWD is the repo root) and a container (a wheel or editable install
    whose site-packages location need not match where `alembic.ini` itself
    was `COPY`'d to). `usher.db.migrations.__path__` moves with the package
    under either install method instead, since it always points at the
    directory the real `env.py`/`versions/` files were installed into
    (verified directly).

    Returns `None` if there are zero or more than one head. This schema
    has exactly one today (verified directly), but a branched history
    would make "the" expected revision ambiguous rather than something
    worth silently picking one of.
    """
    (location,) = _migrations_package.__path__
    return ScriptDirectory(location).get_current_head()


async def database_revision(session: AsyncSession) -> str | None:
    """The revision Alembic's own bookkeeping table says this database is
    actually at.

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
