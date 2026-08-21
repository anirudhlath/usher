"""The per-column ledger behind ADR-0043 and issue #10.

**Not a test, offline, and it writes nothing.** It opens no database and no
socket. Every fact it prints is derived from three artefacts already in this
repository -- the SQLAlchemy metadata in `usher.db.models`, the source of
`usher.db.repositories` read as an AST, and the pydantic models in
`usher.domain` -- plus an independent replay of `usher/db/migrations/versions/`
used only to cross-check the first of those.

    uv run python scripts/audit_bounded_columns.py                  # the ledger
    uv run python scripts/audit_bounded_columns.py --summary        # the counts
    uv run python scripts/audit_bounded_columns.py --at m08b        # a past head
    uv run python scripts/audit_bounded_columns.py --reading pydantic
    uv run python scripts/audit_bounded_columns.py --check          # exit 1 on drift

**Nothing runs this file.** It is not wired into CI and `--check` is not part
of the gate, so its drift detection is a thing a person runs, not a thing that
fires. F9 owns closing that, because F9's guard is a test and tests do run.

**Why this exists.** *"67 bounded columns, 17 provably safe, 5 already
translated, 45 exposed, 31 through the COPY path"* has been quoted in
`docs/prd/09-roadmap.md`, in issue #10 and in two milestone plans, and until
this file none of the five could be reproduced from anything in the tree. The
17 in particular had no ledger anywhere: no list of which columns they were.
A number that cannot be recomputed is a number that silently goes stale, which
is exactly what happened -- M9 added eight bounded columns and translated five
more tables, and every quotation of the five figures kept the M8 arithmetic.

`alembic upgrade head --sql` is **not** used and must not be built on: offline
it dies at `e5b8f2c40d17_ingest_pipeline.py:107` on `MockConnection`, which is
why the migration cross-check below replays the operations as an AST rather
than asking Alembic to render them.

**The bounding rule this file implements is ADR-0043's, stated once here so
the total and the COPY figure cannot disagree again:**

    A column is BOUNDED when its declared Postgres type refuses at least one
    value that the Python object feeding it can represent.

Consequences of that rule, all of which were contested and are settled in the
ADR rather than here: `bigint` is **in** (a Python `int` is unbounded, so
`media_items.file_size_bytes` refuses values just as `integer` does, and the
COPY figure has always silently counted it); `double precision` is **out**
(IEEE-754 binary64 is exactly a Python `float`, so it refuses nothing -- which
is why `titles.popularity`, an `sa.Float()` and not the `NUMERIC` PRD 09 named,
accepts infinity: a defect of the opposite sign); `halfvec(N)` is **in**, on the
same rule, because a `list[float]` has no fixed length and all three of its
columns leak today; and a CHECK constraint is **out**, because it is not the
declared type and
because it fires server-side as SQLSTATE 23514, which every `except
IntegrityError` in this package already catches. CHECK-bounded columns are
counted separately and printed, so the figure is visible rather than hidden.
"""

import argparse
import ast
import dataclasses
import enum
import pathlib
import re
import sys
import typing
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy import CheckConstraint, Column
from sqlalchemy.dialects import postgresql

# Imported for its side effect as much as for `__all__`: importing the module
# is what registers every table on `Base.metadata`.
import usher.db.models
from usher.db.base import Base

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "src" / "usher"
_REPOSITORIES = _PACKAGE / "db" / "repositories"
_MIGRATIONS = _PACKAGE / "db" / "migrations" / "versions"


def _written_sources() -> list[pathlib.Path]:
    """Every module in the package, because `usher.db.repositories` is not the
    whole of the write surface: `adapters/search/postgres.py` holds a second
    writer of `title_embeddings`, and a scan of the repositories package alone
    reports that column's translation from one of its two writers."""
    return sorted(path for path in _PACKAGE.rglob("*.py") if "migrations" not in path.parts)


# The rendered `type_.compile()` prefixes that the rule above admits. Matched
# on the prefix rather than on the SQLAlchemy class, so `VARCHAR(16)` and
# `NUMERIC(12, 8)` carry their width into the ledger and a reader can check the
# classification against the DDL without holding the type hierarchy in mind.
_BOUNDED_PREFIXES = (
    "VARCHAR(",
    "CHAR(",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "NUMERIC(",
    "HALFVEC(",
    # `VECTOR(` for the reason Rule B admits `HALFVEC(` -- a `list[float]` has
    # no fixed length either. It was missing from both this tuple and
    # `_literal_type`, so a `vector(N)` column added tomorrow would have
    # vanished from the metadata side *and* the replay side and `--check`
    # would have reported zero drift on a column neither one could see.
    "VECTOR(",
)

# The type families this file has decided about. Anything else is a column the
# rule has never been applied to, and it must stop the run rather than be
# silently sorted into "not bounded" -- which is how `VECTOR(` hid.
_UNBOUNDED_PREFIXES = (
    "TEXT",
    "UUID",
    "BOOLEAN",
    "TIMESTAMP",
    "DATE",
    "JSONB",
    "BYTEA",
    "TSVECTOR",
    "FLOAT",
    "DOUBLE PRECISION",
    "REAL",
)

# The three failure shapes a bounded column can produce, which is the
# distinction issue #10 does not make and ADR-0043 turns into a decision.
SHAPE_OVERFLOW = "OverflowError"  # client-side, asyncpg's binary encoder, no SQLSTATE
SHAPE_22001 = "22001"  # server-side during COPY, SQLSTATE on a non-DBAPIError
SHAPE_SQLA = "DBAPIError"  # a SQLAlchemy statement, so a translatable exception


@dataclasses.dataclass(frozen=True, slots=True)
class StagingColumn:
    """One column of one `CREATE TEMP TABLE` DDL, as declared."""

    table: str
    name: str
    sql_type: str


@dataclasses.dataclass(frozen=True, slots=True)
class WriteSite:
    """A repository method that issues at least one write.

    **`destinations` is per TABLE, never per column**, and anything consuming
    this must know it. A method whose SQL names `titles` is credited with every
    bounded column in `titles`, so `bulk.py:apply_ratings` appears against
    `titles.original_language`, which it never writes. Bucket-wise that is
    pessimistic and therefore safe. **For F9 it is not**: F9's unit of work is
    the *site*, so a run parametrised over `(column, writer)` straight off this
    field would wrap sites that cannot refuse that column. Narrow by reading
    each statement's own column list first.
    """

    module: str
    qualname: str
    lineno: int
    destinations: tuple[str, ...]
    staged: tuple[str, ...]
    translation: str


@dataclasses.dataclass(frozen=True, slots=True)
class RefusalPoint:
    """One call in a method that can make Postgres refuse a row, with the rank
    of the translation lexically enclosing it.

    `call` is `""` for a statement the method runs itself and the callee's name
    for one it delegates. `bound_select` marks the case this file deliberately
    does **not** answer -- see `_refusal_points`.
    """

    call: str
    covered: int
    bound_select: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerRow:
    """One bounded destination column, with everything the ADR quotes.

    `writers` carries `WriteSite.destinations`' per-table attribution -- see
    that class. It answers "which methods write this column's table", not
    "which methods write this column".
    """

    table: str
    column: str
    sql_type: str
    bucket: str
    reason: str
    shape: str
    staging: str
    writers: str
    translation: str
    domain: str


# --------------------------------------------------------------------------
# 1. The columns, from the metadata


def _rendered_type(column: Column[Any]) -> str:
    # `postgresql.dialect` is `type[PGDialect]` in SQLAlchemy's own stubs with
    # an untyped `__init__`, so constructing it is a `no-untyped-call`. Ignored
    # narrowly rather than left standing: `scripts/` is outside `mypy src
    # tests`, and a file nothing type-checks that would *also* fail if it were
    # checked is the kind of quiet debt this record argues against.
    return str(column.type.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


class UnknownTypeFamily(RuntimeError):
    """A column type Rule B has never been applied to.

    Loud rather than silent, on both sides of the cross-check: an unrecognised
    family sorted into "not bounded" is invisible to `--check`, because the
    metadata side and the replay side would agree about a column neither of
    them can see.
    """


def _is_bounded(rendered: str) -> bool:
    if rendered.startswith(_BOUNDED_PREFIXES):
        return True
    if rendered.startswith(_UNBOUNDED_PREFIXES) or rendered.endswith("[]"):
        return False
    raise UnknownTypeFamily(
        f"{rendered!r} is neither in _BOUNDED_PREFIXES nor in _UNBOUNDED_PREFIXES. "
        "Apply Rule B to it and add it to one of them; do not let it default."
    )


def bounded_columns() -> list[tuple[str, str, str]]:
    """`(table, column, rendered type)` for every column the rule admits."""
    found: list[tuple[str, str, str]] = []
    for name in sorted(Base.metadata.tables):
        for column in Base.metadata.tables[name].columns:
            rendered = _rendered_type(column)
            if _is_bounded(rendered):
                found.append((name, column.name, rendered))
    return found


def check_bounded_columns() -> dict[tuple[str, str], str]:
    """Columns whose *value* is bounded only by a CHECK, which the rule excludes.

    Deliberately conservative: a CHECK naming a comparison or a `BETWEEN` over
    the column bounds its value; one naming `<> ''` or `IS NOT NULL` does not.
    The point of printing these is that the ADR's "CHECK-only bounds are out"
    is a decision with a number attached rather than an omission.
    """
    comparison = re.compile(r"(BETWEEN|>=|<=|>|<)")
    bounded: dict[tuple[str, str], str] = {}
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        typed = {column.name for column in table.columns if _is_bounded(_rendered_type(column))}
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            body = str(constraint.sqltext)
            # `<>` first, or the `<` inside it reads as an ordering and this
            # count comes back 37 -- almost all of them `name <> ''`, which
            # refuses one value rather than bounding a range.
            if not comparison.search(body.replace("<>", " ")):
                continue
            for column in table.columns:
                if column.name in typed:
                    continue
                if re.search(rf"\b{re.escape(column.name)}\b", body):
                    bounded[(name, column.name)] = body
    return bounded


# --------------------------------------------------------------------------
# 2. The staging DDLs and the write sites, from the repository source

_DDL = re.compile(r"CREATE\s+TEMP\s+TABLE\s+(\w+)\s*\(", re.IGNORECASE)
_INSERT = re.compile(r"INSERT\s+INTO\s+\"?(\w+)\"?", re.IGNORECASE)
# The alias is optional and is not optional in practice: four of this package's
# `UPDATE`s are written `UPDATE titles t SET`, and a pattern without the alias
# arm finds no destination for `stg_ratings` or `stg_credit_names` at all.
_UPDATE = re.compile(r"\bUPDATE\s+\"?(\w+)\"?(?:\s+(?:AS\s+)?\w+)?\s+SET\b", re.IGNORECASE)
# Not used to attribute a destination -- a `DELETE` writes no column, so it
# cannot refuse a value -- but used by `_refusal_points` to tell a statement
# that changes rows from one that reads them. A `DELETE` can still meet a
# foreign key, which is a class-23 refusal an `except` has to be around.
_DELETE = re.compile(r"\bDELETE\s+FROM\s+\"?(\w+)\"?", re.IGNORECASE)
_STAGING_NAME = re.compile(r"\bstg_\w+\b")
#: A SQLAlchemy `text()` bind. `(?<![:\w])` is what keeps it off a Postgres
#: `::` cast; this package spells every cast `CAST(x AS t)` by house rule
#: (`.claude/rules/db-and-sql.md`), so the guard is for a statement that stops
#: following it rather than for one that exists.
_BIND = re.compile(r"(?<![:\w]):[A-Za-z_]\w*")


def _split_ddl_columns(body: str) -> Iterator[tuple[str, str]]:
    """Split a `CREATE TEMP TABLE` column list on top-level commas.

    Written out rather than split on `","` because `varchar(16)` and
    `numeric(12, 8)` both carry a comma or a paren inside one column's type,
    and `text[]` carries brackets -- a naive split silently invents columns.
    """
    depth = 0
    current: list[str] = []
    for char in body:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            yield _one_ddl_column("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        yield _one_ddl_column("".join(current))


def _one_ddl_column(text: str) -> tuple[str, str]:
    name, _, sql_type = text.strip().partition(" ")
    return name.strip().strip('"'), sql_type.strip()


def _ddl_body(source: str, start: int) -> str:
    """The text between the DDL's opening paren and its match."""
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                return source[start + 1 : index]
    raise ValueError("unbalanced CREATE TEMP TABLE")


def staging_ddls() -> dict[str, dict[str, StagingColumn]]:
    """Every `CREATE TEMP TABLE` in the repositories package, parsed."""
    tables: dict[str, dict[str, StagingColumn]] = {}
    for path in _written_sources():
        for statement in _sql_literals(ast.parse(path.read_text())):
            match = _DDL.search(statement)
            if match is None:
                continue
            body = _ddl_body(statement, match.end() - 1)
            tables[match.group(1)] = {
                name: StagingColumn(match.group(1), name, sql_type)
                for name, sql_type in _split_ddl_columns(body)
                if name
            }
    return tables


def _sql_literals(tree: ast.AST) -> Iterator[str]:
    """Every string this subtree contains, with an f-string counted as one.

    The f-string half is load-bearing: `watch_state.py` composes its four
    statements as `f"WITH d AS ({_deduped(target)}) UPDATE watch_states ..."`,
    and a walk that yields each `ast.Constant` separately hands back fragments
    in which the `UPDATE` and the staging table never appear together -- so the
    edge that says `stg_watch_states` feeds `watch_states` is invisible.
    """
    if isinstance(tree, ast.JoinedStr):
        yield "".join(
            part.value
            for part in tree.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        for node in tree.values:
            if not isinstance(node, ast.Constant):
                yield from _sql_literals(node)
        return
    if isinstance(tree, ast.Constant):
        if isinstance(tree.value, str):
            yield tree.value
        return
    for child in ast.iter_child_nodes(tree):
        yield from _sql_literals(child)


def _module_texts(tree: ast.Module) -> dict[str, str]:
    """Every module-level name, resolved to all the SQL it can reach.

    A fixed point rather than one pass, because this package builds statements
    in three layers: `_deduped(target)` returns a fragment, `_update(target)`
    interpolates it, and `_UPDATE_BY_TITLE = _update("title_id")` names the
    result. Only the transitive closure has the `UPDATE` and the `stg_` name in
    one string, which is the pair every staging edge is read off.
    """
    own: dict[str, str] = {}
    references: dict[str, set[str]] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = [node.name]
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [one.id for one in targets if isinstance(one, ast.Name)]
        if not names:
            continue
        text = "\n".join(_sql_literals(node))
        referenced = {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}
        for name in names:
            own[name] = text
            references[name] = referenced - {name}

    resolved = dict(own)
    for _ in range(_FIXED_POINT_ROUNDS):
        moved = False
        for name, referenced in references.items():
            reachable = "\n".join(
                resolved[other] for other in sorted(referenced) if other in resolved
            )
            merged = f"{own[name]}\n{reachable}"
            if merged != resolved[name]:
                resolved[name] = merged
                moved = True
        if not moved:
            return resolved
    raise DegenerateScan(
        "module-name resolution did not converge -- a deeper reference chain than "
        f"{_FIXED_POINT_ROUNDS} rounds means this scan silently truncated"
    )


#: The cap on both fixed points below. **It raises rather than truncates**, and
#: that is not defensive: at four rounds it *already* truncated on
#: `src/usher/composition.py`, which is harmless there only because that module
#: writes nothing. A cap that silently stops is a scan that silently goes
#: partial, which is the class of failure this file exists to make impossible.
_FIXED_POINT_ROUNDS = 12


# The calls that actually reach the database. A function holding SQL is not a
# write site: `watch_state.py`'s `_deduped`, `_update` and `_insert` are string
# builders, and counting them as writers put five "no `except`" verdicts on
# `watch_states` columns that `merge_from_source` does translate.
#
# **Ablated one name at a time, 2026-08-20, and only `execute` moves the
# answer** -- dropping it raises `DegenerateScan` on the `unwritten` bucket,
# and dropping any of the other eight changes no count at all, because
# `_executing_functions` takes a transitive closure and `_rowcount`, `_stage`
# and `_write_result` are all defined in the same module as their callers.
# They stay listed rather than trimmed because `stage_records` and
# `copy_records_to_table` are defined *elsewhere* (`db/staging.py`, asyncpg) and
# would need this list the moment a repository stopped wrapping them, and
# because the list is a statement of what reaches the database rather than a
# minimal working set. The ablation is recorded so nobody re-derives it, and
# `_check_call_lists_are_live` below is what stops the list going stale --
# **which it caught one of on its very first run**: `add_all` was listed here
# and nothing in `usher` calls it, so it had been contributing nothing while
# looking exactly like a name that contributed everything.
_EXECUTING_CALLS = frozenset(
    {
        "execute",
        "add",
        "flush",
        "stage_records",
        "_stage",
        "_rowcount",
        "_write_result",
        "copy_records_to_table",
    }
)


def _check_call_lists_are_live() -> None:
    """Every name in **all three** call lists must be called in the package.

    A hand-maintained name list degrades by the *code* being renamed, not by
    the list being edited -- and a name that matches nothing contributes
    nothing while looking exactly like a name that matches everything.

    **Pointed at one list, this found one dead entry immediately; pointed at
    all three, it found two more.** `_ORM_WRITE_CALLS` carried `add_all` and
    `_ORM_STATEMENT_CALLS` carried `insert`, and nothing in `usher` calls
    either. A guard aimed at one of three sibling lists is a guard that reports
    the state of a third of the surface it appears to cover.
    """
    seen: set[str] = set()
    for path in _written_sources():
        seen |= _called_names(ast.parse(path.read_text()))
    for label, names in (
        ("_EXECUTING_CALLS", _EXECUTING_CALLS),
        ("_ORM_WRITE_CALLS", _ORM_WRITE_CALLS),
        ("_ORM_STATEMENT_CALLS", _ORM_STATEMENT_CALLS),
    ):
        missing = sorted(set(names) - seen)
        if missing:
            raise DegenerateScan(
                f"{label} names nothing this package calls: {missing} -- "
                "the list has gone stale against a rename"
            )


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            called = inner.func
            names.add(
                called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
            )
    return names


def _executing_functions(tree: ast.Module) -> set[str]:
    """The names in this module that reach the database, directly or one hop.

    The hop is not optional: `upsert_seasons` and `upsert_episodes` hold the
    DDL and the statement and delegate the run to a shared `self._upsert`, so
    a direct-only test finds no writer for eight `seasons`/`episodes` columns
    and reports them as written by nothing.
    """
    direct = {
        node.name: _called_names(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    executing = {name for name, calls in direct.items() if calls & _EXECUTING_CALLS}
    for _ in range(_FIXED_POINT_ROUNDS):
        grown = {name for name, calls in direct.items() if calls & executing}
        if grown <= executing:
            return executing
        executing |= grown
    raise DegenerateScan(
        f"the executing-call closure did not converge in {_FIXED_POINT_ROUNDS} rounds"
    )


# Every mapped class, so an ORM write can be attributed to a table without a
# hand-maintained second copy of the mapping the models already declare.
_ROW_TABLES: Mapping[str, str] = {
    name: getattr(usher.db.models, name).__tablename__ for name in usher.db.models.__all__
}
# `add_all` and `insert` were in these two until 2026-08-20 and nothing in
# `usher` calls either -- found by pointing `_check_call_lists_are_live` at all
# three lists instead of only at `_EXECUTING_CALLS`.
_ORM_WRITE_CALLS = frozenset({"add", "flush"})
_ORM_STATEMENT_CALLS = frozenset({"update", "delete", "pg_insert"})


def _constructed_rows(tree: ast.Module) -> dict[str, set[str]]:
    """Per module-level function, the tables whose mapped class it *constructs*,
    followed transitively across calls inside this module.

    🔴 **The blind spot this closes, and the direction it points.**
    `_orm_destinations` used to read only the mapped classes appearing as a
    bare `ast.Name` in the method itself, and
    `PostgresTitleRepository.add` writes `self._session.add(_to_row(title))` --
    so `TitleRow` never appears in it, the method resolved to no destination,
    and `write_sites()` **dropped it silently**. That is worse than a
    pessimistic attribution: a writer the scan cannot resolve makes its table's
    columns *optimistically* `translated`, because a bucket is worst-case over
    the writers the scan can see. Measured before the fix: narrowing
    `title.py:add`'s `except` back to `IntegrityError` produced **no drift and
    no failing case**. This function, `_orm_destinations`' use of it, and the
    `DegenerateScan` in `write_sites()` are the three halves of closing it.

    **Constructed, not merely referenced**, and the narrowness is deliberate:
    `_to_domain(row: TitleRow) -> Title` names the class in an annotation and
    writes nothing, so crediting every mention would attribute `titles` to
    every reader in the module the moment one of them flushed for an unrelated
    reason.
    """
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    own = {
        name: {
            _ROW_TABLES[inner.func.id]
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id in _ROW_TABLES
        }
        for name, node in functions.items()
    }
    calls = {name: _called_names(node) & set(functions) for name, node in functions.items()}
    resolved = {name: set(tables) for name, tables in own.items()}
    for _ in range(_FIXED_POINT_ROUNDS):
        moved = False
        for name, called in calls.items():
            grown = resolved[name] | {one for other in called for one in resolved[other]}
            if grown != resolved[name]:
                resolved[name] = grown
                moved = True
        if not moved:
            return resolved
    raise DegenerateScan(
        "the constructed-row closure did not converge in "
        f"{_FIXED_POINT_ROUNDS} rounds -- the scan truncated rather than finished"
    )


def _orm_destinations(
    node: ast.FunctionDef | ast.AsyncFunctionDef, constructed: Mapping[str, set[str]]
) -> set[str]:
    """Tables this method writes through the ORM rather than through SQL text.

    `flush()` is the marker, and it is a sound one in this package: it appears
    in eight methods and every one of them is a write. A read that merely
    names a mapped class -- `session.get(TitleRow, id)` inside `get()` -- has
    no `flush`, no `add` and no `update()`, so it is not attributed.

    `constructed` is what makes a row built by a helper visible -- see
    `_constructed_rows`, and the finding recorded there.
    """
    referenced: set[str] = set()
    flushed = False
    statement_targets: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Name) and inner.id in _ROW_TABLES:
            referenced.add(_ROW_TABLES[inner.id])
        if not isinstance(inner, ast.Call):
            continue
        called = inner.func
        name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
        if name in constructed:
            referenced |= constructed[name]
        if name in _ORM_WRITE_CALLS:
            flushed = True
        if name in _ORM_STATEMENT_CALLS and inner.args:
            head = inner.args[0]
            if isinstance(head, ast.Name) and head.id in _ROW_TABLES:
                statement_targets.add(_ROW_TABLES[head.id])
    return (referenced if flushed else set()) | statement_targets


#: Ranked weakest-first, and the *order* is the whole content: `except
#: IntegrityError` does not catch a column refusing a **value**, because
#: neither shape `_errors.py` measures is an `IntegrityError`.
#: `refusals_as_conflict` and `except DBAPIError` both reach `is_row_refusal`,
#: so they catch the same set and the ranking only has to put both above
#: `except IntegrityError`; they stay distinct answers because the ledger
#: prints which spelling a site uses.
_TRANSLATION_RANK = (
    "none",
    "except IntegrityError",
    "except DBAPIError",
    "refusals_as_conflict",
)

#: The two calls that reach Postgres on the **raw asyncpg connection**, outside
#: SQLAlchemy's error translation entirely. They are deliberately *not* refusal
#: points: no `except` clause a repository can write catches either shape they
#: raise (a bare `builtins.OverflowError` with no SQLSTATE, or an
#: `asyncpg.exceptions.StringDataRightTruncationError` that is not a
#: `DBAPIError` and carries no `.orig` chain -- both observed, see
#: `tests/integration/test_staging.py`). That is the whole of why ADR-0043's
#: `exposed-copy` bucket is decided by the column's *shape* before any writer's
#: `except` is consulted, and counting a COPY as an untranslated refusal point
#: would report every staged writer in `bulk.py` as `none`.
_COPY_EXECUTION = frozenset({"stage_records", "copy_records_to_table"})

#: A call is a session call only when its receiver is spelled `_session` or
#: `session`. `add` is the reason: `_ORM_WRITE_CALLS` carries it, and a bare
#: attribute match would read `seen.add(...)` on a `set` as an ORM write and
#: put a spurious untranslated refusal point in whatever method holds it.
_SESSION_RECEIVERS = frozenset({"_session", "session"})


def _call_name(node: ast.Call) -> str:
    called = node.func
    return called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")


def _is_local_call(node: ast.Call, local: frozenset[str]) -> bool:
    """Whether this call really is a call into a function of this module.

    🔴 **The receiver check is not tidiness, and its absence was a live defect
    found on 2026-08-20 by the narrowed `SELECT` predicate.** Matching a bare
    attribute name against the module's function names reads
    `credit_names.get(scoped_id, ())` -- a `dict.get` on a caller's mapping --
    as a delegated call into `PostgresPersonRepository.get`, because
    `people.py` happens to define a method by that name. That invented edge
    carried `get`'s rank into `replace_for_titles` and was invisible until a
    second scoring pass disagreed with the first.

    A delegation is `self.<name>(...)` or a bare `<name>(...)` -- the second
    for module-level helpers like `title.py:_to_row`. Anything called on some
    other object is not this module's function, whatever it is spelled.
    """
    called = node.func
    if isinstance(called, ast.Name):
        return called.id in local
    return (
        isinstance(called, ast.Attribute)
        and called.attr in local
        and isinstance(called.value, ast.Name)
        and called.value.id == "self"
    )


def _is_session_call(node: ast.Call, names: frozenset[str]) -> bool:
    called = node.func
    if not isinstance(called, ast.Attribute) or called.attr not in names:
        return False
    owner = called.value
    base = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
    return base in _SESSION_RECEIVERS


def _handler_rank(handlers: Sequence[ast.ExceptHandler]) -> int:
    names: set[str] = set()
    for handler in handlers:
        if handler.type is None:
            continue
        for handled in ast.walk(handler.type):
            if isinstance(handled, ast.Name):
                names.add(handled.id)
    if "DBAPIError" in names:
        return _TRANSLATION_RANK.index("except DBAPIError")
    if "IntegrityError" in names:
        return _TRANSLATION_RANK.index("except IntegrityError")
    return 0


def _statement_text(node: ast.Call, texts: Mapping[str, str]) -> str | None:
    """The SQL an `execute(...)` runs, when the source says what it is.

    `None` means "unreadable", and every caller treats that as a **write** --
    `_rowcount(sql)` takes its statement as a parameter, so inside that helper
    there is nothing to read. Guessing "read" there would launder every
    delegating writer in `bulk.py`.
    """
    if not node.args:
        return None
    head = node.args[0]
    reachable = list(_sql_literals(head))
    reachable += [
        texts[inner.id]
        for inner in ast.walk(head)
        if isinstance(inner, ast.Name) and inner.id in texts
    ]
    return "\n".join(reachable).strip() or None


def _refusal_points(
    node: ast.AST, texts: Mapping[str, str], local: frozenset[str], covered: int = 0
) -> Iterator[RefusalPoint]:
    """Every call in this subtree that can make Postgres refuse a *row*, paired
    with the rank of the translation **lexically enclosing it**.

    The lexical part is the point. The predecessor of this function asked "does
    the name `refusals_as_conflict` appear anywhere in the body", which is
    satisfied by a method that wraps one statement and runs a second outside
    the wrapper.

    **Three exemptions, and they are not co-equal -- one of them is currently
    inert and is labelled as such rather than presented as load-bearing.**

    1. **A COPY** (`_COPY_EXECUTION`) runs on the raw asyncpg connection,
       outside SQLAlchemy's translation, and neither shape it raises is
       catchable by any `except` a repository can write -- both observed, see
       `tests/integration/test_staging.py`. ⚠️ **Measured 2026-08-20: setting
       `_COPY_EXECUTION = frozenset()` moves no count, produces no drift and
       changes no synthetic case.** It is inert because a COPY reaches the
       driver through a bare-name call (`stage_records(...)`) or through a
       receiver that is not the session (`connection.copy_records_to_table`),
       so no *other* predicate here would claim it either. It is kept as a
       declaration of intent for the day a repository reaches the COPY through
       `self._session`, and the honest statement is that it implements nothing
       today.
    2. **A `SELECT` with no caller-supplied bind.** 🔴 **The rule used to be
       "a `SELECT` changes no row, so it cannot be refused for one", and that
       is false.** A `SELECT` carrying a bind raises class 22 routinely --
       `22P02` on a cast of a bad literal, `22003` on an overflowing
       expression, `2201B` on a regex -- and an unwrapped one crosses the port
       boundary exactly as raw as an `INSERT`'s would. Two questions were being
       conflated: *should this statement be wrapped in `refusals_as_conflict`?*
       (no -- a class-22 fault in a computed `SELECT` is a **statement** fault,
       and ADR-0043 question (3) says translating it reports this repository's
       own bug as the caller's row being wrong) and *does this method leak?*
       (yes). This ledger's `translation` column is a proxy for the second, so
       the exemption is now the narrow, true one: **a `SELECT` with no bind
       cannot carry a caller value into a class-22 refusal.**
    3. **A call into a function with no refusal point of its own.**
       `bulk.py:_stage` reaches only `stage_records`.

    ⚠️ **A bind-carrying `SELECT` is marked rather than decided.** There is no
    *uncovered* one at any write site today (measured), and what such a method
    should read is genuinely unresolved -- it does not leak in the way an
    untranslated `INSERT` leaks, and it must not be translated in the way one
    is. `write_sites` raises if one ever appears, so the question arrives as a
    failure rather than as an answer somebody invented here. ADR-0043 records
    it as open.

    ⚠️ **"Writes" is three regexes** -- `_INSERT`, `_UPDATE`, `_DELETE`. A
    `MERGE`, a `SELECT setval(...)`, a `CALL` into a writing procedure or a
    `SELECT ... FOR UPDATE` that later mutates would each be read as a
    bind-free read and exempted, and its method would read `translated` on no
    evidence. There is none of that in this package today; adding one means
    adding it here, and the failure mode is optimistic, which is the direction
    every other guard in this file is arranged against.
    """
    if isinstance(node, ast.AsyncWith | ast.With):
        inner = covered
        for item in node.items:
            if (
                isinstance(item.context_expr, ast.Call)
                and _call_name(item.context_expr) == "refusals_as_conflict"
            ):
                inner = max(inner, _TRANSLATION_RANK.index("refusals_as_conflict"))
            yield from _refusal_points(item.context_expr, texts, local, covered)
        for statement_node in node.body:
            yield from _refusal_points(statement_node, texts, local, inner)
        return
    if isinstance(node, ast.Try | ast.TryStar):
        guarded = max(covered, _handler_rank(node.handlers))
        for guarded_node in node.body:
            yield from _refusal_points(guarded_node, texts, local, guarded)
        # A handler's own statements are not covered by the handler they are
        # in, and neither is a `finally`. `import_run.py:save` runs a
        # `session.rollback()` in its `except`, which is exactly that shape.
        for handler_node in (*node.handlers, *node.orelse, *node.finalbody):
            yield from _refusal_points(handler_node, texts, local, covered)
        return
    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in _COPY_EXECUTION:
            pass
        elif _is_local_call(node, local):
            yield RefusalPoint(name, covered)
        elif _is_session_call(node, _ORM_WRITE_CALLS):
            yield RefusalPoint("", covered)
        elif _is_session_call(node, _EXECUTE_CALL):
            statement = _statement_text(node, texts)
            if statement is None or any(
                pattern.search(statement) for pattern in (_INSERT, _UPDATE, _DELETE)
            ):
                yield RefusalPoint("", covered)
            elif _carries_binds(node, statement):
                yield RefusalPoint("", covered, bound_select=True)
        for child in ast.iter_child_nodes(node):
            yield from _refusal_points(child, texts, local, covered)
        return
    for child in ast.iter_child_nodes(node):
        yield from _refusal_points(child, texts, local, covered)


_EXECUTE_CALL = frozenset({"execute"})


def _carries_binds(node: ast.Call, statement: str) -> bool:
    """Whether this `execute(...)` can carry a value the caller chose.

    Two independent signals, because either alone is missable: a second
    argument (the parameter mapping or sequence SQLAlchemy binds), and a
    `:name` placeholder in the statement itself. `link_crosswalk`'s
    classification query has neither -- it is assembled entirely from this
    module's own constants -- which is what makes exempting it correct rather
    than convenient.
    """
    return len(node.args) > 1 or bool(node.keywords) or bool(_BIND.search(statement))


def _definitions(tree: ast.Module) -> dict[tuple[str, int], ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function in the module, keyed by `(name, lineno)`.

    ⚠️ **Not by bare name, and the escalation is why.** `ast.walk` is flat, so
    a module with two same-named methods on different classes collapses them --
    and **40 modules under `src/` have at least one duplicate**
    (`db/repositories/search.py` has two `count_stale`, `db/repositories/
    sync.py` has two `get`). While the only consumer was
    `_executing_functions`, a collision could merely mis-answer *"does this
    write?"*, and it fails toward "yes", which is the safe direction. Since the
    translation closure follows call edges it could carry a **wrong rank**
    across one, and that fails toward `translated`. Each definition is
    therefore scored on its own, and a call edge resolved by bare name takes
    the `min` over every definition of that name -- conservative, ambiguous
    only where the source is, and failing toward `none`.
    """
    return {
        (node.name, node.lineno): node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _points_of(tree: ast.Module) -> dict[tuple[str, int], list[RefusalPoint]]:
    functions = _definitions(tree)
    texts = _module_texts(tree)
    local = frozenset(name for name, _ in functions)
    return {key: list(_refusal_points(node, texts, local)) for key, node in functions.items()}


def _refusing(points: Mapping[tuple[str, int], list[RefusalPoint]]) -> set[tuple[str, int]]:
    """Which definitions can be refused at all, transitively.

    A delegated point into a function that reaches no statement is dropped
    rather than scored `none` -- that is exemption 3.
    """
    refusing = {key for key, own in points.items() if any(not one.call for one in own)}
    names = {key[0] for key in refusing}
    for _ in range(_FIXED_POINT_ROUNDS):
        grown = {key for key, own in points.items() if any(one.call in names for one in own)}
        if grown <= refusing:
            return refusing
        refusing |= grown
        names = {key[0] for key in refusing}
    raise DegenerateScan(
        f"the refusal-point closure did not converge in {_FIXED_POINT_ROUNDS} rounds"
    )


def _score(
    points: Mapping[tuple[str, int], list[RefusalPoint]],
    refusing: set[tuple[str, int]],
    *,
    strict: bool = True,
) -> dict[tuple[str, int], str]:
    """The translation of every definition, as a fixed point over call edges.

    `strict=False` drops the bind-carrying `SELECT`s, which is the predicate
    this file used until 2026-08-20. `write_sites` runs both and refuses to
    answer where they disagree -- see the second exemption in
    `_refusal_points`. Where they agree there is nothing to settle: a method
    whose own untranslated statement already makes it `none` is not made more
    `none` by a `SELECT` beside it.
    """
    top = len(_TRANSLATION_RANK) - 1
    rank = dict.fromkeys(points, top)
    for _ in range(_FIXED_POINT_ROUNDS):
        by_name: dict[str, int] = {}
        for key in refusing:
            by_name[key[0]] = min(by_name.get(key[0], top), rank[key])
        moved = False
        for key, own in points.items():
            scored = [
                max(one.covered, by_name[one.call]) if one.call else one.covered
                for one in own
                if (strict or not one.bound_select) and (not one.call or one.call in by_name)
            ]
            # **No `else top`.** A definition with no refusal point has no
            # evidence, and answering `refusals_as_conflict` on no evidence is
            # the third instance of this file's recurring asymmetry. It is left
            # at `top` here because most such functions are not write sites and
            # never consulted; `write_sites` refuses to build one that is.
            value = min(scored) if scored else top
            if value != rank[key]:
                rank[key] = value
                moved = True
        if not moved:
            return {key: _TRANSLATION_RANK[value] for key, value in rank.items()}
    raise DegenerateScan(
        f"the translation closure did not converge in {_FIXED_POINT_ROUNDS} rounds -- "
        "a min over a monotone-decreasing lattice cannot cycle, so this is a bug here"
    )


def _translations(tree: ast.Module) -> dict[str, str]:
    """Per-function translation, resolved **across call edges in this module**.

    🔴 **The asymmetry this ends.** `_executing_functions` above already takes a
    transitive closure over exactly these edges to answer *"does this method
    write?"* -- `bulk.py:apply_ratings` is in the executing set **only** because
    `_rowcount` calls `execute`. The predecessor of this function refused to
    traverse the same edge to answer *"does this method translate?"*, so a
    module that moved its translation into a shared helper read as twenty
    untranslated writers. M10's F9 backed a better `bulk.py` out on exactly
    that reading, with a comment instructing the next author to keep it backed
    out; both are gone.

    **The closure is narrower than the execution one, and it has to be.**
    "The callee translates, so the caller translates" over-credits a caller
    that *also* runs a statement of its own outside the helper. So execution
    takes **any** refusal point and translation takes the **weakest**: a
    method's answer is the `min` over its refusal points of
    `max(what encloses the call, what the callee itself does)`. One uncovered
    statement is enough to make the whole method `none`, which is the truth
    -- that statement's refusal is what crosses the port boundary raw.

    Keyed by bare name for callers that want one answer per method; where a
    module has two definitions of a name, this returns the **weakest** of them,
    for `_definitions`' reason. `write_sites` scores each definition on its own
    and does not go through here.
    """
    points = _points_of(tree)
    scored = _score(points, _refusing(points))
    weakest: dict[str, str] = {}
    for (name, _), answer in scored.items():
        current = weakest.get(name)
        if current is None or _TRANSLATION_RANK.index(answer) < _TRANSLATION_RANK.index(current):
            weakest[name] = answer
    return weakest


def write_sites() -> list[WriteSite]:
    """Every repository method that issues an INSERT, an UPDATE or a COPY."""
    sites: list[WriteSite] = []
    for path in _written_sources():
        tree = ast.parse(path.read_text())
        constants = _module_texts(tree)
        executing = _executing_functions(tree)
        constructed = _constructed_rows(tree)
        points = _points_of(tree)
        refusing = _refusing(points)
        translations = _score(points, refusing)
        without_bound_selects = _score(points, refusing, strict=False)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name not in executing:
                continue
            statements = list(_sql_literals(node))
            statements += [
                constants[inner.id]
                for inner in ast.walk(node)
                if isinstance(inner, ast.Name) and inner.id in constants
            ]
            destinations: set[str] = set()
            staged: set[str] = set()
            for statement in statements:
                destinations.update(_INSERT.findall(statement))
                destinations.update(_UPDATE.findall(statement))
                staged.update(_STAGING_NAME.findall(statement))
            destinations = {name for name in destinations if not name.startswith("stg_")}
            destinations |= _orm_destinations(node, constructed)
            destinations &= set(Base.metadata.tables)
            if not destinations:
                # **A write that resolves to no table must fail, not vanish**,
                # and this is the degeneracy class ADR-0043's own testing
                # missed: it covered dead scans and empty maps, never "a writer
                # the scan cannot place". Dropping such a method is the one
                # direction that reads *optimistically* -- a bucket is
                # worst-case over the writers the scan can see, so an
                # unresolvable one launders its table.
                #
                # `flush()` on the session is the marker, because it is
                # unambiguous here in a way `add` is not: it is a write, full
                # stop, so failing to name its table is a scan defect rather
                # than a method that happens to write nothing.
                if any(
                    isinstance(inner, ast.Call) and _is_session_call(inner, frozenset({"flush"}))
                    for inner in ast.walk(node)
                ):
                    raise DegenerateScan(
                        f"{path.name}:{node.name} flushes the session and resolves to no "
                        "destination table -- the write-site scan cannot place a writer it "
                        "can see, and a writer it drops makes its table look translated"
                    )
                continue
            key = (node.name, node.lineno)
            # **A site with no refusal point is a failure, not a translated
            # site**, and this is the mirror of the `flush`-with-no-destination
            # raise above -- the same asymmetry, on the other axis, found by
            # the same review. `_score` answers `refusals_as_conflict` for a
            # definition it found nothing to score, which is the top of the
            # lattice on **no evidence**; harmless for the reads that never
            # reach here, and a laundered writer for anything that does.
            # `_executing_functions` and `_refusal_points` use different
            # predicates (`_EXECUTING_CALLS` also carries the COPY primitives),
            # so a method whose only database access is a COPY is *executing*
            # with zero refusal points. `bulk.py:_stage` is that shape today
            # and is saved from being a counter-example only by resolving no
            # destination -- which is exactly the coincidence this refuses to
            # rely on.
            if key not in refusing:
                raise DegenerateScan(
                    f"{path.name}:{node.qualname if hasattr(node, 'qualname') else node.name} "
                    f"writes {sorted(destinations)} and has no refusal point at all -- "
                    "the translation scan cannot see the statement the destination scan can, "
                    "and a site scored on no evidence reads fully translated"
                )
            # The condition this file deliberately does not answer -- see
            # `_refusal_points`' second exemption. An *uncovered* bind-carrying
            # `SELECT` at a write site is a method that can leak a caller's
            # value as a raw driver exception and that must nonetheless not be
            # wrapped in `refusals_as_conflict`.
            #
            # **Only where counting it changes the verdict.** A method already
            # reading `none` for an untranslated statement of its own has no
            # open question:
            # `media_item.py:mark_unseen_unavailable` runs `_SWEEP_COUNTS` (a
            # bound `SELECT`) and `_SWEEP` (an `UPDATE`) both outside any
            # translation, and the second already decides it. So the ledger is
            # scored both ways and refuses only on a disagreement -- which is
            # exactly the set where somebody would otherwise have had to invent
            # an answer. Empty today.
            if translations[key] != without_bound_selects[key]:
                site = f"{path.name}:{node.name} -> {sorted(destinations)}"
                otherwise = without_bound_selects[key]
                raise DegenerateScan(
                    f"{site} runs a bind-carrying read outside any translation, and that "
                    f"read is the only thing keeping it from reading {otherwise!r}. It can "
                    "leak a caller's value as a raw driver exception on class 22, and "
                    "wrapping it would report a statement fault as a refused row -- "
                    "ADR-0043 records the question as open rather than answered, and this "
                    "is where it has to be settled"
                )
            sites.append(
                WriteSite(
                    module=path.name,
                    qualname=node.name,
                    lineno=node.lineno,
                    destinations=tuple(sorted(destinations)),
                    staged=tuple(sorted(staged)),
                    translation=translations[key],
                )
            )
    return sorted(sites, key=lambda site: (site.module, site.lineno))


def staged_into() -> dict[str, set[str]]:
    """`stg_x -> {destination tables}`, derived from the statements themselves.

    One SQL string literal is one statement, so a literal naming both an
    `INSERT INTO titles` and a `FROM stg_titles` is the edge. Nothing here is
    hand-maintained, which is the point: a staging table renamed or re-pointed
    moves this map without anybody remembering to.
    """
    edges: dict[str, set[str]] = {}
    for path in _written_sources():
        tree = ast.parse(path.read_text())
        for statement in [*_sql_literals(tree), *_module_texts(tree).values()]:
            destinations = {
                name
                for name in _INSERT.findall(statement) + _UPDATE.findall(statement)
                if not name.startswith("stg_")
            }
            for staging in set(_STAGING_NAME.findall(statement)):
                if _DDL.search(statement):
                    edges.setdefault(staging, set())
                    continue
                edges.setdefault(staging, set()).update(destinations)
    return edges


# --------------------------------------------------------------------------
# 3. The domain bounds, from the pydantic models

# Hand-maintained, and the only hand-maintained table in this file, because
# nothing in either tree states it: `usher.domain` deliberately knows nothing
# about `usher.db`. Verified rather than trusted -- `domain_bounds` refuses to
# run if a name here does not resolve, so a renamed model is a crash and not a
# silently empty column in the ledger.
_DOMAIN_FOR_TABLE: Mapping[str, str] = {
    "collections": "usher.domain.collection:Collection",
    "credits": "usher.domain.people:Credit",
    "curated_rows": "usher.domain.curation:CuratedRow",
    "episodes": "usher.domain.episode:Episode",
    "images": "usher.domain.image:Image",
    "import_runs": "usher.domain.bootstrap:ImportRun",
    "jobs": "usher.domain.jobs:Job",
    "llm_calls": "usher.domain.curation:LLMCall",
    "media_items": "usher.domain.source:MediaItem",
    "people": "usher.domain.people:Person",
    "seasons": "usher.domain.episode:Season",
    "sources": "usher.domain.source:Source",
    "sync_runs": "usher.domain.sync:SyncRun",
    "titles": "usher.domain.title:Title",
    "user_taste": "usher.domain.taste:Centroid",
    "users": "usher.domain.watch:User",
    "watch_states": "usher.domain.watch:WatchState",
}


def _load(reference: str) -> type[BaseModel]:
    module_name, _, attribute = reference.partition(":")
    module = __import__(module_name, fromlist=[attribute])
    loaded = getattr(module, attribute)
    if not (isinstance(loaded, type) and issubclass(loaded, BaseModel)):
        raise TypeError(f"{reference} is not a pydantic model")
    return loaded


def _bound_of(annotation: Any, metadata: Sequence[Any]) -> str:
    """How the domain field is bounded, in the vocabulary the ADR uses."""
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        # The **longest member**, not a bare "enum". A closed value set only
        # keeps a column safe if every member fits it, and classifying on the
        # word alone would file a future enum member longer than its column as
        # `safe` while it raised `22001`. The tightest margins in the schema
        # today are `JobKind` (15 into `varchar(32)`) and `SyncRunKind` (11
        # into `varchar(16)`), so this is dormant -- and dormant is exactly
        # what `_fully_bounded`'s substring bug was.
        longest = max((len(str(member.value)) for member in annotation), default=0)
        return f"enum(longest={longest})"
    parts: list[str] = []
    for item in metadata:
        for attribute in ("ge", "gt", "le", "lt", "max_length", "min_length"):
            value = getattr(item, attribute, None)
            if value is not None:
                parts.append(f"{attribute}={value}")
        pattern = getattr(item, "pattern", None)
        if isinstance(pattern, str):
            parts.append(f"pattern={pattern}")
    # `"; "` and not `","`: the one pattern in this package is
    # `^tt\d{7,8}$`, whose own text contains a comma, so a comma-joined
    # bound cannot be parsed back apart.
    return "; ".join(sorted(parts)) if parts else ""


# `usher.domain` declares **no `max_length` at all** -- measured 2026-08-20,
# zero occurrences across all nineteen modules -- so the only thing that can
# bound a `str` above is an anchored `pattern`, and there is exactly one such
# pattern in the package. Its longest match is 10 characters, against a
# `varchar(16)` destination, which is what makes `titles.imdb_id` and
# `episodes.imdb_id` provably safe. Written out as a table rather than computed,
# because deriving a maximum match length from an arbitrary regex is a harder
# problem than this ledger has, and a wrong answer here would silently move a
# column between buckets.
_PATTERN_MAX_LENGTH: Mapping[str, int] = {r"^tt\d{7,8}$": 10}


def domain_bounds() -> dict[tuple[str, str], str]:
    """`(table, column) -> the bound its domain field declares`, or absent."""
    bounds: dict[tuple[str, str], str] = {}
    for table, reference in _DOMAIN_FOR_TABLE.items():
        model = _load(reference)
        for name, info in model.model_fields.items():
            annotation = info.annotation
            args = getattr(annotation, "__args__", ())
            for candidate in (annotation, *args):
                bound = _bound_of(candidate, info.metadata)
                if bound:
                    bounds[(table, name)] = bound
                    break
    return bounds


# --------------------------------------------------------------------------
# 3b. The bound on the path that actually writes
#
# **This section exists because the hand-maintained table it replaced was
# wrong, and wrong in a way the ledger's own self-agreement could not see.**
# Until 2026-08-20 the staged bound was inferred from `_DOMAIN_FOR_TABLE`
# alone, which knows only `usher.domain`. `tmdb_ids` has no domain model, so
# `tmdb_ids.kind` came back unbounded and landed in `exposed-copy` as a
# `22001` -- while `titles.kind`, which is **the same construction one file
# over** (`row.kind.value` off a `TitleKind` on a frozen dataclass), was
# classified `safe` by a two-entry hand table. One rule, two answers, one
# shape. Deriving the bound from the writer's own parameter type is what makes
# that impossible rather than merely noticed.


def _staging_sources() -> dict[str, tuple[str, type[Any]]]:
    """`stg_x -> (writer qualname, the class its rows arrive as)`.

    Every staging writer in this package takes exactly one `Sequence[X]`
    parameter, so `X` is the type whose fields really reach the COPY -- a
    pydantic model on the ingest paths and a frozen dataclass from
    `usher.ports.bulk` on the bulk ones. Resolved through the *repository
    module's own namespace*, so a renamed import is an immediate failure.
    """
    sources: dict[str, tuple[str, type[Any]]] = {}
    for path in _written_sources():
        tree = ast.parse(path.read_text())
        texts = _module_texts(tree)
        # **Imported lazily, and only for a module that really stages.**
        # `_written_sources()` is every module in the package on purpose, and
        # importing all of them eagerly made this scan depend on every optional
        # dependency any of them has: `usher/eval/metrics/ir.py` raises
        # `EvalDependencyMissing` at import time when the `eval` extra is not
        # synced, so the whole ledger crashed on a module that stages nothing.
        #
        # ⚠️ **Not the gate's environment** -- `.github/workflows/ci.yml:63` is
        # `uv sync --frozen --extra eval`, so CI has `ranx` and this crash does
        # not reach it. The environment that lacks it is the **shipped image**:
        # `Dockerfile`'s two syncs are `--no-dev` with no `--extra`, while
        # `COPY src/ ./src/` puts `usher/eval/` in the image regardless. So an
        # operator running this audit against a deployment is exactly who hit
        # it, and a gate-green claim would not have covered them. (This comment
        # said "which is the gate's own environment" for one commit and that
        # was false; the fix is right for a reason it got wrong.)
        #
        # An exclusion list would go stale the moment `usher/eval/` grew a
        # writer; deferring the import to the point a staging table is actually
        # found cannot, because a module with a writer is still imported and
        # still fails loudly if it cannot be. `module` is only read by
        # `_sequence_element`.
        module: Any = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            staged = {
                name
                for statement in [
                    *_sql_literals(node),
                    *(
                        texts[inner.id]
                        for inner in ast.walk(node)
                        if isinstance(inner, ast.Name) and inner.id in texts
                    ),
                ]
                for name in _STAGING_NAME.findall(statement)
                if _DDL.search(statement)
            }
            if not staged:
                continue
            if module is None:
                module = _import_of(path)
            element = _sequence_element(node, module)
            if element is None:
                # **Per table, not in aggregate.** `if element is None:
                # continue` used to be silent here, and the only backstop was
                # `if not staged: raise` in `build_ledger`, which fires when
                # *all sixteen* fail. Dropping one -- `stg_tmdb_ids` -- moved
                # `safe` 18 -> 17 and `exposed-copy` 31 -> 32 with no error at
                # all. All sixteen resolve today; dormant and silent is the
                # combination this whole record argues against.
                raise DegenerateScan(
                    f"{path.name}:{node.name} stages {sorted(staged)} and no "
                    "Sequence[X] parameter of it resolves to a class -- the bound "
                    "for every column of those tables would silently be 'none'"
                )
            for name in staged:
                sources.setdefault(name, (f"{path.name}:{node.name}", element))
    return sources


def _import_of(path: pathlib.Path) -> Any:
    dotted = ".".join(path.relative_to(_PACKAGE.parent).with_suffix("").parts)
    return __import__(dotted, fromlist=["__name__"])


def _sequence_element(
    node: ast.FunctionDef | ast.AsyncFunctionDef, module: Any
) -> type[Any] | None:
    """`rows: Sequence[TmdbId]` -> the `TmdbId` class, resolved in `module`."""
    for argument in node.args.args + node.args.kwonlyargs:
        annotation = argument.annotation
        if annotation is None:
            continue
        text = ast.unparse(annotation)
        match = re.fullmatch(r"Sequence\[(\w+)\]", text)
        if match is None:
            continue
        resolved = getattr(module, match.group(1), None)
        if isinstance(resolved, type):
            return resolved
    return None


def _fields_of(model: type[Any]) -> dict[str, tuple[str, bool]]:
    """`field -> (its bound, whether a validator enforces it at runtime)`.

    The second half of the pair is the whole of reading 3 below, and it is why
    this returns a pair rather than a string: a `pattern` on a pydantic model
    is applied when that model is constructed, and the identical `pattern`
    would be inert on a frozen dataclass, which validates nothing.
    """
    if issubclass(model, BaseModel):
        found: dict[str, tuple[str, bool]] = {}
        for name, info in model.model_fields.items():
            args = getattr(info.annotation, "__args__", ())
            for candidate in (info.annotation, *args):
                bound = _bound_of(candidate, info.metadata)
                if bound:
                    found[name] = (bound, True)
                    break
        return found
    try:
        hints: dict[str, Any] = typing.get_type_hints(model)
    except (NameError, TypeError):  # pragma: no cover - defensive
        return {}
    plain: dict[str, tuple[str, bool]] = {}
    for name, annotation in hints.items():
        for candidate in (annotation, *getattr(annotation, "__args__", ())):
            bound = _bound_of(candidate, ())
            if bound:
                plain[name] = (bound, False)
                break
    return plain


def staged_bounds() -> dict[tuple[str, str], tuple[str, bool, str]]:
    """`(stg_table, column) -> (bound, validated at runtime, where it came from)`.

    Matched by **name**, which is what the `INSERT ... SELECT` matches on too:
    `stg_tmdb_ids.kind` is fed by `TmdbId.kind`. A staging column with no field
    of that name -- `ordinal`, or an `id` minted by `new_id()` -- gets nothing,
    which is the right answer rather than a gap.
    """
    bounds: dict[tuple[str, str], tuple[str, bool, str]] = {}
    for staging, (writer, model) in _staging_sources().items():
        for name, (bound, validated) in _fields_of(model).items():
            bounds[(staging, name)] = (bound, validated, f"{model.__name__} via {writer}")
    return bounds


# --------------------------------------------------------------------------
# 4. The independent cross-check, replayed off the migration files

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)\s+([a-z]+(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?)",
    re.IGNORECASE,
)
_DROP_COLUMN = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+DROP\s+COLUMN\s+(\w+)", re.IGNORECASE)


def _text(node: ast.expr, strings: Mapping[str, str]) -> str | None:
    """A string literal, or a loop variable `_replay_loop` has bound to one."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return strings.get(node.id)
    return None


def _resolve(node: ast.expr, names: Mapping[str, int]) -> Any:
    """A literal, or a module-level/imported integer constant by name.

    Needed rather than fussy: three of this chain's widths are written as
    names, not numbers -- `HALFVEC(GENOME_TAG_COUNT)` in `ffa`,
    `sa.Numeric(COST_PRECISION, COST_SCALE)` in `m08a`, and `HALFVEC(
    _NEW_WIDTH)` in `m09e` -- so a replay that only reads `ast.Constant` is
    short by four columns and would report a disagreement that is its own.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    return None


def _literal_type(node: ast.expr, names: Mapping[str, int]) -> str | None:
    """`sa.String(length=16)` -> `VARCHAR(16)`, and so on for the five families."""
    if not isinstance(node, ast.Call):
        return None
    called = node.func
    name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
    arguments = [resolved for one in node.args if (resolved := _resolve(one, names)) is not None]
    keywords = {
        one.arg: resolved
        for one in node.keywords
        if one.arg and (resolved := _resolve(one.value, names)) is not None
    }
    if name in {"String", "Enum"}:
        length = keywords.get("length")
        return f"VARCHAR({length})" if length else None
    if name == "Integer":
        return "INTEGER"
    if name == "BigInteger":
        return "BIGINT"
    if name == "SmallInteger":
        return "SMALLINT"
    if name == "Numeric":
        precision = keywords.get("precision", arguments[0] if arguments else None)
        scale = keywords.get("scale", arguments[1] if len(arguments) > 1 else None)
        return f"NUMERIC({precision}, {scale})" if precision is not None else None
    if name in {"HALFVEC", "VECTOR"}:
        dimensions = keywords.get("dim", arguments[0] if arguments else None)
        return f"{name}({dimensions})" if dimensions is not None else None
    return None


# The four names this chain writes a width as. Imported rather than
# transcribed: `ffa` and `m08a` reference them directly, so a replay that hard
# codes 1128 or 12 would go stale in exactly the way this file exists to catch.
def _imported_ints(tree: ast.Module) -> dict[str, int]:
    names: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                try:
                    module = __import__(node.module, fromlist=[alias.name])
                    value = getattr(module, alias.name)
                except (ImportError, AttributeError):  # pragma: no cover - defensive
                    continue
                if isinstance(value, int) and not isinstance(value, bool):
                    names[alias.asname or alias.name] = value
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value_node = node.value
            if isinstance(value_node, ast.Constant) and isinstance(value_node.value, int):
                for target in targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = value_node.value
    return names


def _revision_of(tree: ast.Module) -> str | None:
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(one, ast.Name) and one.id == "revision" for one in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _revision_order() -> list[pathlib.Path]:
    """The chain in `down_revision` order, because file order is not it."""
    by_revision: dict[str, pathlib.Path] = {}
    parent: dict[str, str | None] = {}
    for path in _MIGRATIONS.glob("*.py"):
        tree = ast.parse(path.read_text())
        revision: str | None = None
        down: str | None = None
        for node in tree.body:
            if not isinstance(node, ast.Assign | ast.AnnAssign):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {one.id for one in targets if isinstance(one, ast.Name)}
            value = node.value
            literal = value.value if isinstance(value, ast.Constant) else None
            if "revision" in names and isinstance(literal, str):
                revision = literal
            if "down_revision" in names:
                down = literal if isinstance(literal, str) else None
        if revision is None:
            continue
        by_revision[revision] = path
        parent[revision] = down
    children = {down: revision for revision, down in parent.items()}
    ordered: list[pathlib.Path] = []
    cursor: str | None = None
    while cursor in children:
        cursor = children[cursor]
        ordered.append(by_revision[cursor])
    return ordered


def head_revision() -> str:
    """The last revision in the chain, derived rather than written down.

    **This exists because the label was hard-coded as `m09f` and went false the
    moment `m10a` merged**, so a table headed *"m09f / m08b"* was printing
    today's head under a previous head's name -- the same staleness
    `.claude/rules/db-and-sql.md` records for `test_migrations.py`'s `-1`
    assertion, arriving in a *label* instead of an assertion and therefore
    without even a red test to announce it. `--at` still takes an explicit
    revision; only the name of "today" is inferred.
    """
    ordered = _revision_order()
    if not ordered:
        raise DegenerateScan("the migration chain replayed to nothing, so no head exists")
    revision = _revision_of(ast.parse(ordered[-1].read_text()))
    if revision is None:
        raise DegenerateScan(f"the chain's last file {ordered[-1].name} declares no revision")
    return revision


def migration_bounded_columns(stop_after: str | None = None) -> set[tuple[str, str, str]]:
    """The same set as `bounded_columns()`, replayed off the migrations.

    Not a second source of truth -- a **disagreement detector**. The two are
    written by hand in two places and `tests/integration/test_migrations.py`
    compares them against a live Postgres; this compares them offline, which is
    the only comparison available to a design task with no container.

    `stop_after` replays only as far as one revision, which is what makes a
    claim about a *past* head checkable: `--at m08b` is the schema issue #10's
    "67 bounded columns" was counted against, and until this argument existed
    there was no way to score that number without checking out M8's tree.
    """
    chain = _revision_order()
    known = {
        revision
        for path in chain
        if (revision := _revision_of(ast.parse(path.read_text()))) is not None
    }
    if stop_after is not None and stop_after not in known:
        # Quality 2: `--at m09b` used to print the head count and exit 0,
        # because the `break` fires only on a match. A revision that does not
        # exist must not answer as though it did.
        raise ValueError(f"no migration with revision {stop_after!r}; chain is {sorted(known)}")
    schema: dict[str, dict[str, str]] = {}
    for path in chain:
        tree = ast.parse(path.read_text())
        upgrade = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
            ),
            None,
        )
        if upgrade is None:
            continue
        helpers = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name != "upgrade"
        }
        _replay_body(upgrade, schema, _imported_ints(tree), helpers, _module_literals(tree))
        if stop_after is not None and _revision_of(tree) == stop_after:
            break
    return {
        (table, column, sql_type)
        for table, columns in schema.items()
        for column, sql_type in columns.items()
        if _is_bounded(sql_type)
    }


def _in_order(node: ast.AST) -> Iterator[ast.AST]:
    """Depth-first, source order. `ast.walk` is breadth-first, which reorders a
    migration's own statements -- and `m09e` creates nothing and *alters* two
    columns, so an out-of-order replay reads the width the chain started at.

    **`ast.For` is yielded and not descended into**, because `_replay_body`
    unrolls it with its loop variables bound; descending here as well would
    replay every statement in the body a second time with the names unresolved.
    """
    yield node
    if isinstance(node, ast.For):
        return
    for child in ast.iter_child_nodes(node):
        yield from _in_order(child)


def _module_literals(tree: ast.Module) -> dict[str, Any]:
    """Module-level `NAME = <literal>`, for the loop iterables a replay unrolls.

    `m10a` renames six columns as `for old, new, *_ in _RENAMES:
    op.alter_column("titles", old, new_column_name=new)` -- so the column names
    are not in the call at all, they are in a module constant one scope up. A
    replay that reads only `ast.Constant` arguments sees an `alter_column` it
    cannot interpret and silently keeps the pre-rename name.
    """
    literals: dict[str, Any] = {}
    for node in tree.body:
        # `ast.AnnAssign` as well as `ast.Assign`: `m10a` declares
        # `_RENAMES: tuple[tuple[str, str, str, str], ...] = (...)`, and reading
        # only `Assign` skipped it -- which reached `_replay_loop`'s guard as
        # "the iterable does not resolve" rather than as a rename.
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
            value = node.value
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        else:
            continue
        if value is None or len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        try:
            literals[targets[0].id] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
    return literals


def _replay_body(
    node: ast.AST,
    schema: dict[str, dict[str, str]],
    names: Mapping[str, int],
    helpers: Mapping[str, ast.FunctionDef],
    literals: Mapping[str, Any] | None = None,
    strings: Mapping[str, str] | None = None,
) -> None:
    """One `upgrade()`, descending one level into its own module's helpers.

    `m09e` puts both `alter_column`s inside `_resize(width)` and calls it with
    a module constant, so a replay that does not follow the call sees no type
    change at all. One level, no recursion: this chain has exactly one such
    helper and a general inliner would be a worse thing to trust than a
    disagreement the summary prints.
    """
    literals = literals or {}
    strings = strings or {}
    for inner in _in_order(node):
        if isinstance(inner, ast.For):
            _replay_loop(inner, schema, names, helpers, literals, strings)
            continue
        if isinstance(inner, ast.Call):
            called = inner.func
            target = getattr(called, "id", "")
            if target in helpers:
                helper = helpers[target]
                bound = dict(names)
                for parameter, argument in zip(helper.args.args, inner.args, strict=False):
                    value = _resolve(argument, names)
                    if isinstance(value, int) and not isinstance(value, bool):
                        bound[parameter.arg] = value
                _replay_body(helper, schema, bound, {}, literals)
                continue
            _replay_call(inner, schema, names, strings)
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            _replay_sql(inner.value, schema)


def _replay_loop(
    node: ast.For,
    schema: dict[str, dict[str, str]],
    names: Mapping[str, int],
    helpers: Mapping[str, ast.FunctionDef],
    literals: Mapping[str, Any],
    strings: Mapping[str, str],
) -> None:
    """Unroll `for a, b, ... in <module constant>:` and replay the body per item.

    Only over an iterable that resolves to a literal sequence -- which is the
    one shape this chain uses (`m10a`'s `_RENAMES`). Anything else raises,
    rather than replaying the body with the loop variables unbound: a rename
    whose names do not resolve keeps the *old* column silently, and the whole
    argument for this instrument is that a scan which cannot see something must
    say so instead of answering as though there were nothing to see.
    """
    iterable = node.iter
    items: Any = None
    if isinstance(iterable, ast.Name):
        items = literals.get(iterable.id)
    else:
        try:
            items = ast.literal_eval(iterable)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            items = None
    mutating = [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (getattr(call.func, "attr", "") in {"alter_column", "add_column", "drop_column"})
    ]
    if items is None:
        if mutating:
            raise DegenerateScan(
                f"a loop at line {node.lineno} performs {len(mutating)} schema "
                "operation(s) and its iterable does not resolve to a literal, so "
                "every column it touches would keep its pre-loop name and type"
            )
        return
    targets = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
    for item in items:
        values = item if isinstance(item, tuple | list) else (item,)
        bound = dict(strings)
        for target, value in zip(targets, values, strict=False):
            if isinstance(target, ast.Name) and isinstance(value, str):
                bound[target.id] = value
        for statement in node.body:
            _replay_body(statement, schema, names, helpers, literals, bound)


def _replay_call(
    node: ast.Call,
    schema: dict[str, dict[str, str]],
    names: Mapping[str, int],
    strings: Mapping[str, str] | None = None,
) -> None:
    strings = strings or {}
    called = node.func
    name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
    if not node.args:
        return
    table = _text(node.args[0], strings)
    if table is None:
        return
    if name == "create_table":
        columns: dict[str, str] = {}
        for argument in node.args[1:]:
            if not isinstance(argument, ast.Call) or len(argument.args) < 2:
                continue
            head = argument.args[0]
            if not isinstance(head, ast.Constant) or not isinstance(head.value, str):
                continue
            sql_type = _literal_type(argument.args[1], names)
            if sql_type is not None:
                columns[head.value] = sql_type
        schema[table] = columns
    elif name == "drop_table":
        schema.pop(table, None)
    elif name == "add_column" and len(node.args) > 1:
        argument = node.args[1]
        if isinstance(argument, ast.Call) and len(argument.args) > 1:
            head = argument.args[0]
            sql_type = _literal_type(argument.args[1], names)
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and sql_type:
                schema.setdefault(table, {})[head.value] = sql_type
    elif name == "drop_column" and len(node.args) > 1:
        argument = node.args[1]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            schema.get(table, {}).pop(argument.value, None)
    elif name == "alter_column" and len(node.args) > 1:
        # `m09e` moves both vector columns to a new width this way, so a replay
        # blind to `type_=` reports the width the chain started at.
        #
        # **And `m10a` *renames* six columns this way, which is the second
        # keyword.** A replay that reads only `type_=` keeps the old name and
        # never learns the new one, so `--check` reported
        # `only-metadata=[('titles', 'tmdb_vote_count', …)]` against
        # `only-migrations=[('titles', 'vote_count', …)]` -- the metadata and
        # the chain describing the same column under two names. That is the
        # column-set comparison doing exactly what it is for; it is also the
        # reason this branch cannot be left at one keyword, because a rename is
        # the one edit that changes a column's identity without changing
        # anything the rest of this replay looks at.
        column = _text(node.args[1], strings)
        if column is None:
            return
        renamed_node = next(
            (one.value for one in node.keywords if one.arg == "new_column_name"),
            None,
        )
        retyped = next(
            (one.value for one in node.keywords if one.arg == "type_"),
            None,
        )
        if retyped is not None:
            sql_type = _literal_type(retyped, names)
            if sql_type is not None:
                schema.setdefault(table, {})[column] = sql_type
        renamed = None if renamed_node is None else _text(renamed_node, strings)
        if renamed is not None:
            columns = schema.setdefault(table, {})
            # `pop` rather than a read-then-delete: a rename of a column this
            # replay never saw declared (an `op.execute`d `ADD COLUMN` it could
            # not parse, say) must not invent one under the new name.
            existing = columns.pop(column, None)
            if existing is not None:
                columns[renamed] = existing


_SQL_TYPE_RENAMES = {
    "text": "TEXT",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
}


def _replay_sql(statement: str, schema: dict[str, dict[str, str]]) -> None:
    """`op.execute("ALTER TABLE credits ADD COLUMN source varchar(8)")` and its
    inverse -- 45 `op.execute` calls in this chain, and `m09d` puts two bounded
    columns in one, so a replay that only reads `op.add_column` is short by two.
    """
    for table, column, sql_type in _ADD_COLUMN.findall(statement):
        collapsed = re.sub(r"\s+", "", sql_type).upper()
        schema.setdefault(table, {})[column] = _SQL_TYPE_RENAMES.get(sql_type.lower(), collapsed)
    for table, column in _DROP_COLUMN.findall(statement):
        schema.get(table, {}).pop(column, None)


# --------------------------------------------------------------------------
# 5. The classification


def _shape(destination_type: str, staging_type: str | None) -> str:
    """Which of the three failure shapes this column's refusal takes.

    The distinction issue #10 does not make. A staging column **wider** than
    its destination moves the refusal to the `INSERT ... SELECT`, where
    SQLAlchemy is in the stack and an `except` can reach it; a staging column
    that mirrors the destination refuses inside `copy_records_to_table`, on the
    raw asyncpg connection, where nothing in `_errors.py` can see it.
    """
    if staging_type is None:
        return SHAPE_SQLA
    normalised = re.sub(r"\s+", "", staging_type).upper()
    if normalised.startswith("TEXT") and destination_type.startswith("VARCHAR("):
        return SHAPE_SQLA
    if normalised.startswith("BIGINT") and destination_type.startswith("INTEGER"):
        return SHAPE_SQLA
    if normalised.startswith(("INTEGER", "SMALLINT", "BIGINT")):
        return SHAPE_OVERFLOW
    if normalised.startswith(("VARCHAR(", "CHAR(")):
        return SHAPE_22001
    return SHAPE_SQLA


#: The three readings of "the value set is closed", and the ADR publishes all
#: three because choosing between them moves published figures and the choice
#: has to be visible rather than fallen into.
#:
#: - `closure`  -- a bound declared anywhere counts, including a `pattern` on a
#:                 pydantic model that the writing path never constructs.
#: - `path`     -- **the default.** Only the bound on the class the writer
#:                 actually takes counts. An enum still counts on a frozen
#:                 dataclass, because `row.kind.value` closes the set by itself:
#:                 anything that is not an enum member has no `.value`.
#: - `pydantic` -- stricter still: a bound counts only where a validator runs,
#:                 so a frozen dataclass's annotation closes nothing.
READINGS = ("closure", "path", "pydantic")
DEFAULT_READING = "path"


class DegenerateScan(RuntimeError):
    """A scan found nothing, which must fail rather than read as a clean sheet.

    This project's own rule, and the reason this exception exists rather than a
    comment: *a scan that globs nothing passes identically to a scan that
    passes.* Stubbing `write_sites()` to `[]` used to leave every exposed
    bucket empty and exit 0 -- and ADR-0043 specifies F9's guard as "assert the
    `exposed-sqlalchemy` bucket is empty", which a dead scan satisfies
    perfectly. Every derivation this file depends on is checked for emptiness
    before a single column is classified.
    """


def build_ledger(reading: str = DEFAULT_READING, at: str | None = None) -> list[LedgerRow]:
    """The ledger, optionally over the column set of a *past* migration head.

    `at` narrows the columns to what the replay says existed at that revision
    and classifies them with **today's** source. That is the only past-head
    statement this file can honestly make, and it is labelled as such wherever
    it is printed: the writers, the `except` clauses and the source classes are
    all today's, so `--at m08b` answers "how would this rule score M8's schema",
    not "what did M8 measure".
    """
    if reading not in READINGS:
        raise ValueError(f"unknown reading {reading!r}; expected one of {READINGS}")
    _check_call_lists_are_live()
    staging = staging_ddls()
    edges = staged_into()
    sites = write_sites()
    columns = bounded_columns() if at is None else sorted(migration_bounded_columns(at))
    bounds = domain_bounds()
    staged = staged_bounds()

    if not staging:
        raise DegenerateScan("no CREATE TEMP TABLE found -- the staging scan is dead")
    if not sites:
        raise DegenerateScan("no write site found -- the write-site scan is dead")
    if not columns:
        raise DegenerateScan("no bounded column found -- the metadata scan is dead")
    if not bounds:
        raise DegenerateScan("no domain bound found -- the pydantic scan is dead")
    if not staged:
        raise DegenerateScan("no staged bound found -- the source-class scan is dead")
    # **Symmetric, and the asymmetry it replaces was the reviewed Critical in
    # its sibling direction.** Checking only `edges - staging` left
    # `staged_into() -> {}` silent, and it moves 31 columns from `exposed-copy`
    # to `exposed-sqlalchemy` -- the two buckets F9 splits on. Losing one
    # table's destinations moved 8. Both now fail here, in `build_ledger`,
    # which is what F9 imports; `--check` caught them already and nothing runs
    # `--check`.
    unread = sorted(set(edges) - set(staging))
    if unread:
        raise DegenerateScan(f"staging tables read but never declared: {unread}")
    undeclared = sorted(set(staging) - set(edges))
    if undeclared:
        raise DegenerateScan(f"staging tables declared but never read: {undeclared}")
    destinationless = sorted(name for name, targets in edges.items() if not targets)
    if destinationless:
        raise DegenerateScan(
            f"staging tables that feed nothing: {destinationless} -- every staging "
            "table in this package is read by an INSERT or an UPDATE, so an empty "
            "destination set is a dead statement scan, not a table nobody uses"
        )
    # The backstop for the per-table raise inside `_staging_sources`. That one
    # fires when a writer's `Sequence[X]` will not resolve; this one fires when
    # the map arrives short by any other route, because a staging table with no
    # source class silently gives every one of its columns "no bound" and
    # drifts them toward `exposed`.
    unsourced = sorted(set(staging) - set(_staging_sources()))
    if unsourced:
        raise DegenerateScan(f"staging tables with no resolved source class: {unsourced}")

    feeds: dict[tuple[str, str], StagingColumn] = {}
    for staging_table, destinations in edges.items():
        for destination in destinations:
            for staging_column in staging.get(staging_table, {}).values():
                feeds.setdefault((destination, staging_column.name), staging_column)

    rows: list[LedgerRow] = []
    for table, column, sql_type in columns:
        writers = [site for site in sites if table in site.destinations]
        fed = feeds.get((table, column))
        staged_writers = [site for site in writers if any(site.staged)]
        shape = _shape(sql_type, fed.sql_type if fed and staged_writers else None)
        bound = _bound_for(reading, table, column, fed, bounds, staged)
        translations = {site.translation for site in writers}
        bucket, reason = _classify(sql_type, bound, shape, writers, table, column)
        rows.append(
            LedgerRow(
                table=table,
                column=column,
                sql_type=sql_type,
                bucket=bucket,
                reason=reason,
                shape=shape,
                staging=f"{fed.table}.{fed.name} {fed.sql_type}" if fed else "-",
                writers=", ".join(f"{site.module}:{site.qualname}" for site in writers) or "-",
                translation=", ".join(sorted(translations)) or "-",
                domain=bound.text or "-",
            )
        )
    unwritten = [row for row in rows if row.bucket == "unwritten"]
    if unwritten and at is None:
        raise DegenerateScan(
            "columns with no writer at all, which means the write-site scan "
            f"degraded rather than that nothing writes them: {[r.column for r in unwritten]}"
        )
    return rows


@dataclasses.dataclass(frozen=True, slots=True)
class Bound:
    """What closes a column's value set, and where that claim comes from."""

    text: str
    source: str


def _bound_for(
    reading: str,
    table: str,
    column: str,
    fed: StagingColumn | None,
    domain: Mapping[tuple[str, str], str],
    staged: Mapping[tuple[str, str], tuple[str, bool, str]],
) -> Bound:
    """The bound this reading credits, and nothing else.

    The `path` and `pydantic` readings deliberately do **not** fall back to the
    domain model for a staged column. That fallback is what produced the
    original defect: `titles.imdb_id`'s `pattern` lives on `usher.domain.title.
    Title`, and `bulk.py:upsert_titles` takes `ports.bulk.ImdbTitle`, whose
    `imdb_id` is a bare `str` -- so crediting the pattern there asserts a
    validator that the writing path never runs.
    """
    on_path = staged.get((fed.table, fed.name)) if fed else None
    declared = domain.get((table, column), "")
    if reading == "closure":
        if declared:
            return Bound(declared, "declared on the domain model")
        if on_path:
            return Bound(on_path[0], on_path[2])
        return Bound("", "")

    # Staged: the source class is authoritative, **including its silence.**
    # Falling back to the domain model here is what produced the original
    # defect twice over -- `titles.imdb_id`'s `pattern` is on `domain.Title`
    # while `upsert_titles` takes `ports.bulk.ImdbTitle` (a bare `str`), and
    # `jobs.priority`'s `ge=0, le=100` is on `domain.Job` while `enqueue`
    # takes `JobRequest` (a bare `int`). Crediting either asserts a validator
    # the writing path never runs.
    if fed is not None:
        if on_path is None:
            return Bound("", "")
        text, validated, where = on_path
        if reading == "pydantic" and not validated:
            return Bound("", "")
        return Bound(text, where)
    return Bound(declared, "declared on the domain model") if declared else Bound("", "")


# `genome_tags.tag_id` is the one column in this schema bounded by neither its
# own type nor its domain field but by an invariant the *writer* enforces over
# the whole batch: `replace_genome_tags` refuses a vocabulary that is not
# exactly 1..n, so the largest value reaching the driver is the length of the
# sequence handed in. Named here because a per-column scan cannot see a
# batch-level check, and leaving it out would put a demonstrably safe column in
# the exposed bucket. `.claude/rules/db-and-sql.md` holds the measurement.
_BATCH_BOUNDED: Mapping[tuple[str, str], str] = {
    ("genome_tags", "tag_id"): "bounded at the batch by replace_genome_tags' 1..n contiguity check",
}


_TRANSLATING = frozenset({"refusals_as_conflict", "except DBAPIError"})


def _classify(
    sql_type: str,
    bound: Bound,
    shape: str,
    writers: Sequence[WriteSite],
    table: str,
    column: str,
) -> tuple[str, str]:
    """The bucket, worst case over every writer.

    **Pessimistic on purpose.** A column is exposed if *any* path into it lets
    a refusal escape, so one translated writer does not launder a sibling that
    has no `except` -- `titles` has eight writers and only some of them
    translate. The `safe` bucket is the only one that does not depend on a
    writer's `except` at all, which is why it is decided first: a value nothing
    can construct never reaches a driver by any route.
    """
    enum_longest = re.fullmatch(r"enum\(longest=(\d+)\)", bound.text)
    if enum_longest is not None:
        declared = re.search(r"\((\d+)\)", sql_type)
        width = int(declared.group(1)) if declared else 0
        longest = int(enum_longest.group(1))
        if longest > width:
            return "exposed-copy" if shape != SHAPE_SQLA else "exposed-sqlalchemy", (
                f"enum-backed but NOT safe: its longest member is {longest} "
                f"characters and the column holds {width} -- {bound.source}"
            )
        closure = (
            "`.value` off a member closes the set"
            if "via " in bound.source
            else "the pydantic field validates the member"
        )
        return "safe", (
            f"enum-backed, {bound.source}: {closure}; longest member {longest} of {width}"
        )
    if (table, column) in _BATCH_BOUNDED:
        return "safe", _BATCH_BOUNDED[(table, column)]
    if _fully_bounded(sql_type, bound.text):
        return "safe", f"bounded on every side the column is ({bound.text}), {bound.source}"
    if shape in {SHAPE_OVERFLOW, SHAPE_22001}:
        return "exposed-copy", f"refused inside copy_records_to_table as {shape}"
    if not writers:
        return "unwritten", "no writer in src/ carries a value into this column"
    untranslated = [site for site in writers if site.translation not in _TRANSLATING]
    if not untranslated:
        return "translated", "every writer catches on the SQLSTATE class, not on IntegrityError"
    named = "; ".join(
        f"{site.module}:{site.qualname} ({site.translation})" for site in untranslated
    )
    return "exposed-sqlalchemy", f"untranslated writers: {named}"


def _fully_bounded(sql_type: str, domain: str) -> bool:
    """`_errors.py`'s own rule, run forwards: safe when the field is bounded on
    every side the column is, and exposed when it is bounded on fewer."""
    if not domain:
        return False
    declared_parts = dict(part.partition("=")[::2] for part in domain.split("; "))
    if sql_type.startswith(("INTEGER", "SMALLINT", "BIGINT", "NUMERIC(")):
        above = {"le", "lt"} & declared_parts.keys()
        below = {"ge", "gt"} & declared_parts.keys()
        return bool(above and below)
    if sql_type.startswith(("VARCHAR(", "CHAR(")):
        declared = re.search(r"\((\d+)\)", sql_type)
        if declared is None:
            return False
        width = int(declared.group(1))
        # Parsed rather than substring-matched. `f"max_length={width}" in
        # domain` reads `max_length=160` as satisfying `VARCHAR(16)`, and
        # `f"pattern={p}" in domain` matches any pattern with `p` as a prefix.
        # Both are dormant today only because `usher.domain` declares zero
        # `max_length` and exactly one pattern -- which is precisely the state
        # question (5) debates changing.
        length = declared_parts.get("max_length", "")
        if length.isdigit() and int(length) <= width:
            return True
        pattern = declared_parts.get("pattern")
        if pattern is not None and _PATTERN_MAX_LENGTH.get(pattern, width + 1) <= width:
            return True
    return False


# --------------------------------------------------------------------------
# 6. Rendering


_HEADINGS = (
    "table",
    "column",
    "type",
    "bucket",
    "shape",
    "staging column",
    "domain bound",
    "writer (per table -- see WriteSite)",
    "translation",
    "reason",
)


def render(rows: Sequence[LedgerRow]) -> str:
    body = [
        (
            row.table,
            row.column,
            row.sql_type,
            row.bucket,
            row.shape,
            row.staging,
            row.domain,
            row.writers,
            row.translation,
            row.reason,
        )
        for row in rows
    ]
    if not body:
        raise DegenerateScan("nothing to render -- an empty ledger is a dead scan, not a result")
    widths = [
        max(len(_HEADINGS[index]), *(len(line[index]) for line in body))
        for index in range(len(_HEADINGS))
    ]
    lines = [
        "| "
        + " | ".join(head.ljust(width) for head, width in zip(_HEADINGS, widths, strict=True))
        + " |",
        "|" + "|".join("-" * (width + 2) for width in widths) + "|",
    ]
    lines += [
        "| "
        + " | ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True))
        + " |"
        for line in body
    ]
    return "\n".join(lines)


def staging_shape() -> tuple[list[str], list[str]]:
    """Two lists the ADR's decisions (2) and (4) each need a number for.

    The first is every staging column declared **wider** than the destination
    column it feeds -- the shape `id_crosswalk.imdb_id` is the roadmap's own
    worked example of, and the shape the candidate fix proposes generalising.
    The second is every **bounded staging column with no destination column at
    all**: a join key or an ordering that is in none of the 79 because it is in
    no destination table, and therefore in nobody's count.
    """
    staging = staging_ddls()
    edges = staged_into()
    wider: list[str] = []
    orphans: list[str] = []
    for staging_table in sorted(staging):
        destinations = edges.get(staging_table, set())
        for column in staging[staging_table].values():
            normalised = re.sub(r"\s+", "", column.sql_type).upper()
            matched = [
                Base.metadata.tables[destination].columns[column.name]
                for destination in sorted(destinations)
                if destination in Base.metadata.tables
                and column.name in Base.metadata.tables[destination].columns
            ]
            if not matched:
                if normalised.startswith(("INTEGER", "SMALLINT", "BIGINT", "VARCHAR(")):
                    orphans.append(f"{staging_table}.{column.name} {column.sql_type}")
                continue
            for destination_column in matched:
                rendered = _rendered_type(destination_column)
                if _is_bounded(rendered) and _shape(rendered, column.sql_type) == SHAPE_SQLA:
                    wider.append(
                        f"{staging_table}.{column.name} {column.sql_type} -> "
                        f"{destination_column.table.name}.{destination_column.name} {rendered}"
                    )
    return wider, orphans


def counts(rows: Sequence[LedgerRow]) -> dict[str, int]:
    """The bucket census. One function, so `--summary` and `--check` cannot
    read the same ledger and disagree about what it says."""
    counted: dict[str, int] = {bucket: 0 for bucket in BUCKETS}
    for row in rows:
        counted[row.bucket] = counted.get(row.bucket, 0) + 1
    return counted


BUCKETS = ("safe", "translated", "exposed-copy", "exposed-sqlalchemy")

#: The figures ADR-0043 publishes, keyed by reading. **This is the drift check
#: with teeth**: `--check` compares the live ledger against it, so a change to
#: the schema, to a writer's `except`, or to a source class that moves a column
#: between buckets fails the run and names the document that has gone stale.
#: A cross-check that only compared the metadata against the migrations could
#: not see any of that -- every `shape`, `staging`, `writer` and `translation`
#: cell F9 consumes had no drift check at all.
#:
#: **Moved by M10's F9 on 2026-08-20, which is what makes the count a decision
#: rather than an observation.** The `exposed-sqlalchemy` bucket went 20 -> 1
#: under the adopted reading. **Twenty writing sites took a translation, all
#: twenty of them** -- eleven replacing an `except IntegrityError` and nine
#: where there was no `except` at all. The bucket is 1 rather than 0 because of
#: one **column**, `jobs.attempts`, whose four remaining writers
#: (`jobs.py:claim`/`fail`/`touch`/`requeue_running`) are deliberately not
#: among the twenty: its only writer of that column computes the value
#: server-side as `attempts = attempts + 1`, so translating it would report a
#: *statement* fault as a refused row, which is the misuse `_errors.py:66-75`
#: exists to warn about. ADR-0043's scope section carries the evidence.
#: (This comment said "nineteen of the twenty writing sites took ... and
#: `jobs.attempts` did not", which counted a column as a site and was wrong on
#: both halves.)
#:
#: 🔴 **Every reading gained one `translated` on 2026-08-21, and the cause is
#: `m10a`/ADR-0042 rather than anything F9 did.** That revision split
#: `titles.vote_count` -- one `integer` column with two writers -- into
#: `tmdb_vote_count` and `imdb_num_votes`, and the ledger separates them for
#: exactly the reason the record split them: `imdb_num_votes` is fed by
#: `bulk.py:apply_ratings` through `stg_ratings.imdb_num_votes integer`, so it
#: is **exposed-copy**, and `tmdb_vote_count` is reached only through the ORM,
#: so it is **translated**. The old column was `exposed-copy` because a COPY
#: writer touched it at all -- worst case over its writers, which is what a
#: dual-written column costs. So `exposed-copy` holds station (the IMDb half
#: inherits the slot), `translated` gains the TMDb half, and the bounded total
#: goes 79 -> 80. **The provenance split is legible in this instrument without
#: anyone having taught it about ADR-0042**, which is a corroboration of that
#: record rather than drift against it.
PUBLISHED: Mapping[str, Mapping[str, int]] = {
    "closure": {"safe": 20, "translated": 29, "exposed-copy": 30, "exposed-sqlalchemy": 1},
    "path": {"safe": 18, "translated": 30, "exposed-copy": 31, "exposed-sqlalchemy": 1},
    "pydantic": {"safe": 14, "translated": 30, "exposed-copy": 34, "exposed-sqlalchemy": 2},
}

#: Same, at M8's head, which is what the roadmap's corrections are scored
#: against. `17` appears in none of them, and that is the finding.
#:
#: ⚠️ **These moved with F9 too, and the reason is worth knowing before
#: reading them as history.** `--at m08b` is *"that revision's columns,
#: classified with **today's** source"* -- so translating a writer today
#: changes what this rule says about M8's schema. The `translated` column here
#: went 5 -> 23 without a line of M8-era code changing. What is still
#: comparable across the two heads is the *column set*, which is what the
#: roadmap's `67` is scored on; the buckets are a statement about today's
#: writers and always were.
#:
#: 🔴 **And these moved on 2026-08-21 with no M8-era column changing, which is
#: the warning above paying out.** `m10a` redirected `bulk.py:apply_ratings`
#: off `vote_count` and onto `imdb_num_votes` -- a column that does not exist
#: at `m08b` -- so M8's `titles.vote_count` lost its only COPY writer and is
#: scored **translated** today where it was **exposed-copy** yesterday. Hence
#: `exposed-copy` -1 and `translated` +1 on every reading, with the bounded
#: total unchanged at that head. Read as: *a redirect in today's source
#: reclassified a column M8 shipped*, which is the property this block's
#: warning describes and not a discovery about M8.
PUBLISHED_AT_M08B: Mapping[str, Mapping[str, int]] = {
    "closure": {"safe": 18, "translated": 23, "exposed-copy": 29, "exposed-sqlalchemy": 1},
    "path": {"safe": 16, "translated": 24, "exposed-copy": 30, "exposed-sqlalchemy": 1},
    "pydantic": {"safe": 12, "translated": 24, "exposed-copy": 33, "exposed-sqlalchemy": 2},
}


def readings_table() -> str:
    """Every reading's arithmetic at both heads, printed together.

    Printed rather than chosen-and-hidden because the choice between them moves
    published figures: `safe` runs 20/18/14 today and 18/16/12 at `m08b`. A
    reader who cannot see the other two columns cannot tell a decision from an
    accident.
    """
    head = head_revision()
    lines = [f"reading            safe  transl  copy  sqla  exposed  ({head} / m08b)"]
    for reading in READINGS:
        for label, at in ((head, None), ("m08b", "m08b")):
            got = counts(build_ledger(reading, at=at))
            exposed = got["exposed-copy"] + got["exposed-sqlalchemy"]
            marker = "*" if reading == DEFAULT_READING else " "
            lines.append(
                f"{marker}{reading:<10} {label}  {got['safe']:>4}  {got['translated']:>6}  "
                f"{got['exposed-copy']:>4}  {got['exposed-sqlalchemy']:>4}  {exposed:>7}"
            )
    lines.append(f"  (* = the reading ADR-0043 adopts: {DEFAULT_READING})")
    return "\n".join(lines)


def summary(
    rows: Sequence[LedgerRow], reading: str = DEFAULT_READING, at: str | None = None
) -> str:
    """The counts. Under `at`, the sections that read the **live** metadata are
    withheld rather than printed wrong.

    `staging_shape()` and `check_bounded_columns()` both read
    `Base.metadata`, which is today's schema whatever `--at` says. Printing
    them beside a replayed ledger produced one column with two widths in a
    single run -- `title_embeddings.embedding` as `HALFVEC(384)` in the table
    and `HALFVEC(1024)` in the summary. The counts were unaffected, and "one
    rule, two answers" is the defect this file exists to eliminate, so the
    honest move is to print neither rather than the wrong one.
    """
    counted = counts(rows)
    families: dict[str, int] = {}
    for row in rows:
        family = re.sub(r"\(.*", "", row.sql_type)
        families[family] = families.get(family, 0) + 1
    copy_shapes: dict[str, int] = {}
    narrow_staged = 0
    for row in rows:
        if row.shape in {SHAPE_OVERFLOW, SHAPE_22001}:
            narrow_staged += 1
        if row.bucket == "exposed-copy":
            copy_shapes[row.shape] = copy_shapes.get(row.shape, 0) + 1
    safe_narrow = narrow_staged - counted["exposed-copy"]

    lines = [
        f"bounded columns (ADR-0043's rule, reading={reading}): {len(rows)}",
        "  by type family: " + ", ".join(f"{k} {v}" for k, v in sorted(families.items())),
        "  by bucket:      " + ", ".join(f"{k} {v}" for k, v in sorted(counted.items())),
        f"  narrow-staged bounded destination columns: {narrow_staged}"
        f", of which {safe_narrow} provably safe"
        f" -> {narrow_staged} - {safe_narrow} = {counted['exposed-copy']} exposed at the COPY",
        "  COPY-path failure shapes: "
        + (", ".join(f"{k} {v}" for k, v in sorted(copy_shapes.items())) or "none"),
    ]
    if at is not None:
        lines += [
            "",
            f"** the CHECK-only and staging-width sections are WITHHELD under --at {at} **",
            "   Both read the live `Base.metadata`, which is today's schema whatever",
            "   --at says, so printing them beside a replayed ledger would put one",
            "   column at two widths in one run. The bucket counts above are unaffected:",
            "   they are that revision's columns classified with today's source, which",
            "   is what the header says.",
            "",
            readings_table(),
        ]
        return "\n".join(lines)

    check_only = check_bounded_columns()
    wider, orphans = staging_shape()
    metadata_set = {(table, column, sql_type) for table, column, sql_type in bounded_columns()}
    replayed = migration_bounded_columns()
    lines += [
        f"CHECK-only value bounds (excluded by the rule): {len(check_only)}",
        *(f"    {table}.{column}: {body}" for (table, column), body in sorted(check_only.items())),
        f"staging tables: {len(staging_ddls())}",
        f"  staging columns wider than their destination: {len(wider)}",
        *(f"    {one}" for one in wider),
        f"  bounded staging columns with no destination column: {len(orphans)}",
        *(f"    {one}" for one in orphans),
        f"write sites: {len(write_sites())}",
        "",
        f"migration cross-check (replayed off {len(_revision_order())} revisions, no database):",
        f"  metadata says {len(metadata_set)}, the migrations say {len(replayed)}",
    ]
    only_metadata = sorted(metadata_set - replayed)
    only_migrations = sorted(replayed - metadata_set)
    lines.append(f"  in the metadata and not in the replay: {only_metadata or 'none'}")
    lines.append(f"  in the replay and not in the metadata: {only_migrations or 'none'}")
    lines.append(
        "  NOT fully independent, and the exception is named rather than glossed: "
        f"{len(_TAUTOLOGOUS)} of the {len(replayed)} widths are written in the "
        "migration as an imported name that this replay resolves against the *live* "
        "package, so they cannot disagree with the metadata by construction --"
    )
    lines += [f"    {table}.{column}" for table, column in sorted(_TAUTOLOGOUS)]
    lines.append(
        "    (the same mechanism is why `--at m08b` prints user_taste.centroid at "
        "today's width rather than the 384 m09e widened it from)"
    )
    lines.append("")
    lines.append(readings_table())
    return "\n".join(lines)


#: The three columns whose width the migration chain writes as an imported
#: constant -- `HALFVEC(GENOME_TAG_COUNT)`, `HALFVEC(EMBEDDING_DIMENSIONS)` and
#: `sa.Numeric(COST_PRECISION, COST_SCALE)`. Resolving those against the live
#: package is what lets the replay run at all, and it also means their
#: agreement with the metadata is a tautology rather than a check. Named here
#: so "79 from each, zero drift" is read as 76 agreements and 3 tautologies.
_TAUTOLOGOUS = (
    ("genome_scores", "relevance"),
    ("user_taste", "centroid"),
    ("llm_calls", "cost_usd"),
)


def _drift(reading: str) -> list[str]:
    """Everything `--check` compares. Empty means no drift.

    Three comparisons, not one. The column set against the migration replay is
    the weakest of them and was the only one this file had: it cannot see a
    writer losing its `except`, a source class losing a bound, or a staging
    DDL widening -- every cell F9 actually consumes.
    """
    complaints: list[str] = []
    metadata_set = set(bounded_columns())
    replayed = migration_bounded_columns()
    if metadata_set != replayed:
        complaints.append(
            f"metadata/migration drift: only-metadata={sorted(metadata_set - replayed)} "
            f"only-migrations={sorted(replayed - metadata_set)}"
        )
    for label, published, at in (
        (head_revision(), PUBLISHED, None),
        ("m08b", PUBLISHED_AT_M08B, "m08b"),
    ):
        for name in READINGS:
            got = counts(build_ledger(name, at=at))
            want = dict(published[name])
            if {k: got[k] for k in want} != want:
                complaints.append(
                    f"bucket drift at {label} under reading={name}: "
                    f"ADR-0043 publishes {want}, the ledger says "
                    f"{ {k: got[k] for k in want} }"
                )
    if reading not in READINGS:  # pragma: no cover - argparse constrains this
        complaints.append(f"unknown reading {reading!r}")
    return complaints


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_bounded_columns",
        description=(
            "The per-column bounded-column ledger behind ADR-0043 and issue #10. "
            "Offline: no database, no socket, nothing written. See the module "
            "docstring for the bounding rule and the three readings of it."
        ),
    )
    parser.add_argument("--summary", action="store_true", help="print the counts only")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 on any drift from the figures ADR-0043 publishes, at both heads",
    )
    parser.add_argument(
        "--at",
        metavar="REVISION",
        help=(
            "score a past migration head (e.g. m08b): that revision's columns, "
            "classified with today's writers and source classes"
        ),
    )
    parser.add_argument(
        "--reading",
        choices=READINGS,
        default=DEFAULT_READING,
        help=f"which reading of 'the value set is closed' to apply (default: {DEFAULT_READING})",
    )
    arguments = parser.parse_args(argv)

    if arguments.check:
        complaints = _drift(arguments.reading)
        for complaint in complaints:
            print(complaint)
        print("no drift" if not complaints else f"{len(complaints)} drift(s)")
        return 1 if complaints else 0

    rows = build_ledger(arguments.reading, at=arguments.at)
    if not arguments.summary:
        print(render(rows))
        print()
    if arguments.at:
        print(f"** {arguments.at}'s columns, classified with TODAY's source **")
    print(summary(rows, arguments.reading, at=arguments.at))
    return 0


if __name__ == "__main__":
    sys.exit(main())
