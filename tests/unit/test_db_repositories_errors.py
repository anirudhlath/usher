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

import ast
import pathlib
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


# --------------------------------------------------------------------------
# The re-raise, at every site that widened -- a property, not eleven cases
# --------------------------------------------------------------------------

_SCANNED = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "usher" / "db" / "repositories",
    pathlib.Path(__file__).resolve().parents[2] / "src" / "usher" / "adapters" / "search",
)


def _dbapi_handlers() -> list[tuple[str, str, ast.ExceptHandler]]:
    """Every `except DBAPIError` in the packages that translate, with the
    method it is in."""
    found: list[tuple[str, str, ast.ExceptHandler]] = []
    for directory in _SCANNED:
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for handler in ast.walk(node):
                    if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
                        continue
                    named = {one.id for one in ast.walk(handler.type) if isinstance(one, ast.Name)}
                    if "DBAPIError" in named:
                        found.append((path.name, node.name, handler))
    return found


#: Every `except DBAPIError` in the two packages that translate, named rather
#: than counted.
#:
#: 🔴 **This was `assert len(handlers) >= 11` and a floor one below the true
#: count is a dead-scan guard wearing a narrowing guard's clothes.** There are
#: twelve; narrowing any one site left eleven and passed. At eleven of the
#: twelve the ledger's own drift check backstops it -- but at
#: `jobs.py:enqueue` the two limits **compose**, because that site's widening
#: is unpinned by the ledger for a reachability reason recorded in
#: `.claude/rules/mutation-sweeps.md` (nothing it writes can produce a class-22
#: refusal). Narrowing it moved neither: zero drift complaints, twelve handlers
#: down to eleven, `>= 11` green. Each limit was declared; their composition
#: was not, and a census is what closes it.
#:
#: A named set rather than `== 12`, so a site that is narrowed *and* a new one
#: that is widened cannot cancel out.
WIDENED_SITES = frozenset(
    {
        ("_errors.py", "refusals_as_conflict"),
        ("collection.py", "attach_titles"),
        ("import_run.py", "save"),
        ("jobs.py", "enqueue"),
        ("people.py", "replace_for_titles"),
        ("postgres.py", "index_many"),
        ("search.py", "replace"),
        ("search.py", "upsert_many"),
        ("sync.py", "add"),
        ("sync.py", "save"),
        ("title.py", "add"),
        ("title.py", "update"),
    }
)


def test_the_set_of_widened_sites_is_exactly_what_this_file_names() -> None:
    """A census, not a floor -- see `WIDENED_SITES`.

    Widening a site is a decision (it changes what crosses a port boundary),
    and so is narrowing one. Either without editing this set fails here, which
    is what makes the count a decision rather than an observation -- the same
    thing `PUBLISHED` does for the ledger one directory over.
    """
    found = {(module, method) for module, method, _ in _dbapi_handlers()}
    assert found == set(WIDENED_SITES), (
        f"widened but not named here: {sorted(found - set(WIDENED_SITES))}; "
        f"named here but no longer widened: {sorted(set(WIDENED_SITES) - found)}"
    )


def test_every_widened_except_re_raises_what_is_not_a_row_refusal() -> None:
    """The invariant `except DBAPIError` buys its width with, checked once
    across every site instead of once per site.

    🔴 **It was covered by exactly one behavioural case out of eleven sites**,
    and the ledger cannot see it at all: `_translation_of` reads the `except`
    clause's *type* and never the handler body, so deleting `if not
    is_row_refusal(exc): raise` from `import_run.py:save` left
    `audit_bounded_columns.py --check` reporting no drift and the whole suite
    green. What is lost when it goes is not a missed refusal but the opposite —
    a dropped connection, a statement timeout or an undefined table reported to
    a caller as *its row being wrong*, which is the one distinction
    `ROW_REFUSED_SQLSTATE_CLASSES` exists to preserve and the one a caller
    needs to decide whether a retry can help.

    A structural case rather than eleven integration cases because the
    behaviour needs a transport fault, and no fixture in this project can
    manufacture one against a live database.

    `refusals_as_conflict` is excluded by construction: it *is* the guard, and
    `test_a_failure_that_is_not_the_rows_fault_is_not_translated` above is the
    behavioural case for it.
    """
    handlers = _dbapi_handlers()
    assert {(module, method) for module, method, _ in handlers} == set(WIDENED_SITES), (
        "the handler scan disagrees with WIDENED_SITES -- fix that census first, since "
        "this case cannot be read until it is known which sites it covered"
    )

    unguarded = []
    for module, method, handler in handlers:
        if module == "_errors.py" and method == "refusals_as_conflict":
            continue
        guarded = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "is_row_refusal"
            for node in ast.walk(handler)
        ) and any(isinstance(node, ast.Raise) and node.exc is None for node in ast.walk(handler))
        if not guarded:
            unguarded.append(f"{module}:{method}")

    assert not unguarded, (
        "these `except DBAPIError` handlers catch more than a row refusal and re-raise "
        f"none of it, so a transport fault reaches the caller as a RepositoryConflict: "
        f"{unguarded}"
    )
