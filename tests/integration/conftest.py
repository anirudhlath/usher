"""Integration fixtures backed by a real PostgreSQL with pgvector."""

import os
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

import pytest
import pytest_asyncio
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from usher.config import get_settings
from usher.db import models  # noqa: F401  — registers all tables
from usher.db.base import build_engine, build_session_factory

_THIS_DIR = Path(__file__).parent
_ALEMBIC_INI = _THIS_DIR.parent.parent / "alembic.ini"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test collected under tests/integration/ needs Docker (this directory's whole reason.

    to exist -- see the module docstring).

    Marking it here, once, means Task 10's own literal test functions below don't each
    need a hand-applied `@pytest.mark.integration`, and neither will any test a future
    task adds to this directory. `pytest -m integration` / `pytest -m "not integration"`
    then work as a marker-based equivalent of the tests/unit vs tests/integration
    directory split, for tooling (Group F/G's CI) that would rather filter by `-m` than
    by path.

    `pytest_collection_modifyitems` is *not* directory-scoped the way a
    fixture would be -- pytest calls every conftest.py's implementation of
    this hook with the *entire* session's `items`, not just the ones
    collected from this hook's own directory (verified directly: an
    earlier, unguarded `for item in items: item.add_marker(...)` here
    marked all ~194 tests "integration", including every test under
    tests/unit/ -- `-m integration` selected the whole suite and
    `-m "not integration"` selected nothing). The explicit path check below
    is what actually scopes this to tests/integration/.
    """
    for item in items:
        if item.path.is_relative_to(_THIS_DIR):
            item.add_marker(pytest.mark.integration)


def _upgrade_head(database_url: str) -> None:
    """Runs the real migration chain against a freshly-started container.

    see the module docstring.

    `env.py` (deliberately -- see its own docstring) reads the URL from
    `usher.config.get_settings()`, never from `alembic.ini`, so driving it here means
    setting the env vars a real `alembic upgrade head` invocation would have had,
    exactly as far as `Settings` needs: `USHER_DATABASE_URL` and `USHER_SECRET_KEY`
    (both required, neither has a default). Every `USHER_*`/`OTEL_*` variable is saved
    and restored around the call -- the same isolation `tests/conftest.py`'s
    `clean_environment` gives every test, which doesn't help here since this fixture
    (session scope) runs before that one (function scope) ever does for the first test
    that needs it.
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith(("USHER_", "OTEL_"))}
    for key in saved:
        del os.environ[key]
    os.environ["USHER_DATABASE_URL"] = database_url
    os.environ["USHER_SECRET_KEY"] = "0" * 32
    get_settings.cache_clear()
    try:
        upgrade(Config(str(_ALEMBIC_INI)), "head")
    finally:
        for key in list(os.environ):
            if key.startswith(("USHER_", "OTEL_")):
                del os.environ[key]
        os.environ.update(saved)
        get_settings.cache_clear()


def run_alembic(database_url: str, target: str, *, direction: str | None = None) -> None:
    """`alembic upgrade`/`downgrade` against an arbitrary database.

    Exposed beside `_upgrade_head` so a test can drive the chain in *both*
    directions against a throwaway database, which the session-scoped schema
    cannot survive.

    **Pass `direction` whenever the target is a bare revision id.** Left to
    infer, this reads `"base"` and `"-N"` as downgrades and *everything else*
    as an upgrade -- so `run_alembic(url, "fe1d40c8b7a3")` against a database
    already past that revision runs `upgrade`, which is a **silent no-op**,
    and the caller then asserts against the schema it meant to leave. That is
    not hypothetical: it is how
    `test_a_full_down_and_up_cycle_restores_every_index` failed on the first
    run after `ffa` landed, and the failure looked like a broken migration
    rather than a broken harness. Same family as the `-q`/`-qq` and
    `/tmp`-rootdir traps -- the command ran and measured nothing.
    """
    saved = {key: value for key, value in os.environ.items() if key.startswith(("USHER_", "OTEL_"))}
    for key in saved:
        del os.environ[key]
    os.environ["USHER_DATABASE_URL"] = database_url
    os.environ["USHER_SECRET_KEY"] = "0" * 32
    get_settings.cache_clear()
    try:
        going_down = (
            direction == "down"
            if direction is not None
            else (target == "base" or target.startswith("-"))
        )
        if going_down:
            downgrade(Config(str(_ALEMBIC_INI)), target)
        else:
            upgrade(Config(str(_ALEMBIC_INI)), target)
    finally:
        for key in list(os.environ):
            if key.startswith(("USHER_", "OTEL_")):
                del os.environ[key]
        os.environ.update(saved)
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    # `testcontainers.community.postgres`, not `testcontainers.postgres`: the latter is
    # a shim that raises a DeprecationWarning at import time and is the only warning
    # this suite emits.
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "pgvector/pgvector:pg17",
        username="usher",
        password="usher",
        dbname="usher",
    ) as pg:
        url = pg.get_connection_url().replace("psycopg2", "asyncpg")
        _upgrade_head(url)
        yield url


_A_TABLE_NAME = re.compile(r"\A[a-z_][a-z0-9_]*\Z")


def _identifier(name: str) -> str:
    """A table name that may be interpolated into SQL.

    Every caller below names a literal table, so this can never fail in
    practice; it is here so that the `# noqa: S608` on the interpolations is
    backed by a check rather than by a promise.
    """
    if not _A_TABLE_NAME.match(name):
        raise ValueError(f"not a table name: {name!r}")
    return name


async def _restore_the_statistics(conn: AsyncConnection, tables: frozenset[str]) -> None:
    """Put `pg_class` back after a test `ANALYZE`d something.

    `VACUUM` cannot run inside a transaction block, hence the AUTOCOMMIT hop,
    and it has to run **after** the caller's rollback or the seeded tuples are
    not yet dead and the vacuum is a no-op that looks exactly like a vacuum
    that worked. This runs inside `session`'s own teardown, below its
    `rollback()`, which is what makes that ordering structural rather than a
    convention a future fixture can quietly break -- see the guard's docstring.
    """
    names = ", ".join(_identifier(name) for name in sorted(tables))
    await conn.execute(text(f"VACUUM (ANALYZE) {names}"))


async def _tables_pg_class_is_wrong_about(
    conn: AsyncConnection, forgiven: frozenset[str]
) -> dict[str, tuple[int, int]]:
    """Each public table whose `reltuples` disagrees with its `count(*)`.

    against what it really holds.

    Cheap on purpose: one query, and it returns nothing at all unless
    something already looks wrong, so the `count(*)`s below are paid for only
    when there is something to attribute. Tables never analyzed read
    `reltuples = -1`, which is Postgres for "measure the file", and are not a
    lie about anything.
    """
    described = await conn.execute(
        text(
            "SELECT relname FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' AND reltuples > 0"
        )
    )
    wrong: dict[str, tuple[int, int]] = {}
    for table in sorted({str(row[0]) for row in described} - forgiven):
        estimate = await conn.execute(
            text("SELECT reltuples::bigint FROM pg_class WHERE relname = :name"), {"name": table}
        )
        live = await conn.execute(
            text(f"SELECT count(*) FROM {_identifier(table)}")  # noqa: S608 -- checked identifier
        )
        described_as, really_there = int(estimate.scalar_one()), int(live.scalar_one())
        if described_as != really_there:
            wrong[table] = (described_as, really_there)
    return wrong


async def _assert_pg_class_still_describes_this_database(
    conn: AsyncConnection,
    *,
    node_id: str,
    forgiven: frozenset[str],
    expected: frozenset[str],
    inherited: frozenset[str],
) -> None:
    """The property every test in this directory is entitled to assume.

    `pg_class` describes the database it is about to plan against.
    """
    lying = await _tables_pg_class_is_wrong_about(conn, forgiven)
    if lying:
        await _restore_the_statistics(conn, frozenset(lying))
    introduced = {
        table: pair
        for table, pair in lying.items()
        if table not in inherited and table not in expected
    }
    assert not introduced, (
        f"{node_id} left `pg_class` describing rows this database does not have "
        f"(reltuples, count(*)): {introduced}. The catalog update is **in place**, so this "
        "is now every later test's planner too -- and `ANALYZE` is not the only thing that "
        "writes it: a rolled-back `CREATE INDEX` leaves the same lie, measured. Take the "
        "`analyze` fixture instead of executing `ANALYZE` directly, or -- if the leak is a "
        "side effect of what the test exercises -- mark it "
        "`@pytest.mark.leaks_statistics(...)` naming the tables."
    )


@pytest.fixture
def _analyzed_tables() -> set[str]:
    """The tables this test told the planner about.

    shared by reference between `analyze` and `session` so the restore needs no global.
    """
    return set()


class Analyze(Protocol):
    """The `analyze` fixture, as a test signature can name it."""

    async def __call__(self, *tables: str) -> None: ...


@pytest.fixture
def analyze(session: AsyncSession, _analyzed_tables: set[str]) -> Analyze:
    """`ANALYZE`.

    for a test whose subject is a *plan*, with the cleanup the rollback does not do.

    **A test that asserts a plan establishes its own statistics.** Without
    them the planner sizes the relation off an empty `pg_class`, every
    candidate index costs the same to four significant figures, and which one
    the assertion names is decided by nothing the test controls -- that is
    both of #79's CI failures, and `test_the_availability_sweeps_update_uses_
    its_index` failed **10 runs of 10** in isolation for exactly this reason
    before it seeded a population and called this.

    Use this rather than executing `ANALYZE` yourself: it registers the table
    for the `VACUUM (ANALYZE)` that `session` runs after its rollback, which
    is what keeps the statistics from becoming every later test's problem.
    The guard in `session`'s teardown is what makes that non-optional.
    """

    async def run(*tables: str) -> None:
        for table in tables:
            await session.execute(text(f"ANALYZE {_identifier(table)}"))
        _analyzed_tables.update(tables)

    return run


A_DECISIVE_MARGIN = 2.0
"""How much cheaper the asserted plan has to be than the best one without its
index, before "the planner chose it" is a claim about the schema.

Two, because the measured margins are far above it and the number that matters
is the one that separates *decided* from *tied*, not a percentile. On
`pgvector/pgvector:pg17`, 2026-08-31: the availability sweep at 2,000 rows with
50 stale costs **31.72** through `ix_media_items_sweep` and **129.09** through
`uq_media_items_source_external` with the sweep index suspended -- **4.07x**.
The same case at its old fixture size of fifty rows and no `ANALYZE` costs
`0.14..8.16` **both** ways -- measured by suspending each candidate in turn, so
that is a reading rather than an inference from the winner's cost -- which is
the tie this constant exists to fail on. That reading also reproduces the CI
failure byte for byte: `Index Scan using uq_media_items_source_external ...
(cost=0.14..8.16 rows=1 width=7)`, `Index Cond` on `source_id` alone.
"""

_A_ROOT_COST = re.compile(r"\(cost=[0-9.]+\.\.([0-9.]+) ")


def total_cost(plan: str) -> float:
    """`EXPLAIN`'s estimate for the whole plan, off its root node.

    The root is the first line, and its `cost=start..total` is the number to
    compare: `start` is what it takes to return the *first* row, which two
    plans can tie on while differing by orders of magnitude overall.
    """
    match = _A_ROOT_COST.search(plan)
    assert match, f"no root cost in this plan, so `EXPLAIN` did not run with costs:\n{plan}"
    return float(match.group(1))


@asynccontextmanager
async def index_suspended(session: AsyncSession, index: str) -> AsyncIterator[None]:
    """Hide one index from the planner.

    so a plan assertion can measure what the *alternative* costs.

    **"The planner chose the index I meant" is not a property of the schema
    unless the runner-up is materially worse**, and at fixture scale it
    routinely is not: #79's two CI failures are both plans where the chosen
    and the asserted index cost the same to four significant figures, so the
    assertion was reporting a tie-break. Asserting the *margin* is what stops
    a future fixture -- trimmed from two thousand rows to fifty because the
    suite got slow -- from silently restoring the tie under a green test.

    `indisvalid = false` is how Postgres itself marks an index the planner
    must ignore (it is the state a failed `CREATE INDEX CONCURRENTLY` leaves),
    and a plain `UPDATE` on the catalog is transactional: the `SAVEPOINT` here
    takes it back, and the enclosing `session` fixture's rollback would take
    it back again. Verified directly on `pgvector/pgvector:pg17` -- the row
    reads `indisvalid = t` again after the block, on this connection and on a
    fresh one. Nothing writes to the table inside the block, so the index
    being nominally invalid for its duration costs nothing.
    """
    savepoint = await session.begin_nested()
    try:
        await session.execute(
            text(
                "UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:name AS regclass)"
            ),
            {"name": index},
        )
        yield
    finally:
        await savepoint.rollback()


@pytest_asyncio.fixture
async def session(
    postgres_url: str, request: pytest.FixtureRequest, _analyzed_tables: set[str]
) -> AsyncIterator[AsyncSession]:
    # `leaks_statistics(*tables)` says the leak is a known side effect of what
    # the test exercises, so the guard repairs it and stays quiet.
    # `leaks_statistics(*tables, restored_by=...)` says something else owns the
    # repair and the leak has to survive until it runs -- which is a real case
    # exactly once here, and naming the owner is the point of the keyword.
    expected, forgiven = set[str](), set[str]()
    for marker in request.node.iter_markers("leaks_statistics"):
        (forgiven if marker.kwargs.get("restored_by") else expected).update(marker.args)
    engine = build_engine(postgres_url)
    factory = build_session_factory(engine)
    async with engine.connect() as conn:
        await conn.begin()
        inherited = frozenset(await _tables_pg_class_is_wrong_about(conn, frozenset(forgiven)))
        async with factory(bind=conn) as s:
            yield s
        await conn.rollback()
        # Switching isolation level needs the connection to be between transactions,
        # which the rollback above has just made true.
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        if _analyzed_tables:
            await _restore_the_statistics(autocommit, frozenset(_analyzed_tables))
        await _assert_pg_class_still_describes_this_database(
            autocommit,
            node_id=request.node.nodeid,
            forgiven=frozenset(forgiven),
            expected=frozenset(expected),
            inherited=inherited,
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Separately-committing sessions, not the `session` fixture above.

    Anything whose subject is what a *different* session or another task can
    see is only a question at all once something has committed, so these
    sessions are real: a case that seeded through the shared rolled-back
    transaction would be handing a route, or a lane, rows it cannot see.

    Function-scoped like everything else here, so a case owns its engine and
    disposes of it; the shared container is what `postgres_url` carries.
    """
    engine = build_engine(postgres_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def column_set(url: str, table: str) -> set[str]:
    """One table's column names, read off the catalog.

    The reader a column-only migration's `downgrade()` needs: without it the
    only thing that can observe one is the whole-chain `base`/`head` round
    trip, which passes against a no-op downgrade because `base` drops the
    table anyway.
    """
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def index_set(url: str) -> set[str]:
    """Every index name in the public schema.

    `column_set`'s sibling.
    """
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def scratch_database(postgres_url: str, prefix: str) -> tuple[str, str, str]:
    """Create a throwaway database.

    Returns `(admin_url, name, url)`.

    What a case needs when its subject is a migration's effect on rows that
    existed before it ran, or the pre-migration schema as a control arm --
    neither of which the shared, already-migrated database can give.
    """
    admin = postgres_url.rsplit("/", 1)[0]
    scratch = f"{prefix}_{uuid.uuid4().hex[:12]}"
    engine = build_engine(f"{admin}/postgres")
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'CREATE DATABASE "{scratch}"'))
    finally:
        await engine.dispose()
    return admin, scratch, f"{admin}/{scratch}"


async def drop_database(admin: str, scratch: str) -> None:
    """`scratch_database`'s other half, for the `finally` that owns it."""
    engine = build_engine(f"{admin}/postgres")
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'))
    finally:
        await engine.dispose()
