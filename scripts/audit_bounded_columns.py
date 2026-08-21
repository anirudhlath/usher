"""The per-column ledger behind ADR-0041 and issue #10.

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

**The bounding rule this file implements is ADR-0041's, stated once here so
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
# distinction issue #10 does not make and ADR-0041 turns into a decision.
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
_STAGING_NAME = re.compile(r"\bstg_\w+\b")


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


def _orm_destinations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Tables this method writes through the ORM rather than through SQL text.

    `flush()` is the marker, and it is a sound one in this package: it appears
    in eleven methods and every one of them is a write. A read that merely
    names a mapped class -- `session.get(TitleRow, id)` inside `get()` -- has
    no `flush`, no `add` and no `update()`, so it is not attributed.
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
        if name in _ORM_WRITE_CALLS:
            flushed = True
        if name in _ORM_STATEMENT_CALLS and inner.args:
            head = inner.args[0]
            if isinstance(head, ast.Name) and head.id in _ROW_TABLES:
                statement_targets.add(_ROW_TABLES[head.id])
    return (referenced if flushed else set()) | statement_targets


def _translation_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """The error translation that actually wraps this method's statements.

    Four answers, and the difference between the middle two is the whole of
    `_errors.py`'s argument: `except IntegrityError` does not catch a column
    refusing a *value*, because neither measured shape is an `IntegrityError`.
    """
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            called = inner.func
            if isinstance(called, ast.Name) and called.id == "refusals_as_conflict":
                return "refusals_as_conflict"
            if isinstance(called, ast.Attribute) and called.attr == "refusals_as_conflict":
                return "refusals_as_conflict"
        if isinstance(inner, ast.ExceptHandler) and inner.type is not None:
            for handled in ast.walk(inner.type):
                if isinstance(handled, ast.Name):
                    names.add(handled.id)
    if "DBAPIError" in names:
        return "except DBAPIError"
    if "IntegrityError" in names:
        return "except IntegrityError"
    return "none"


def write_sites() -> list[WriteSite]:
    """Every repository method that issues an INSERT, an UPDATE or a COPY."""
    sites: list[WriteSite] = []
    for path in _written_sources():
        tree = ast.parse(path.read_text())
        constants = _module_texts(tree)
        executing = _executing_functions(tree)
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
            destinations |= _orm_destinations(node)
            destinations &= set(Base.metadata.tables)
            if not destinations:
                continue
            sites.append(
                WriteSite(
                    module=path.name,
                    qualname=node.name,
                    lineno=node.lineno,
                    destinations=tuple(sorted(destinations)),
                    staged=tuple(sorted(staged)),
                    translation=_translation_of(node),
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
        module = _import_of(path)
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
        _replay_body(upgrade, schema, _imported_ints(tree), helpers)
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
    columns, so an out-of-order replay reads the width the chain started at."""
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _in_order(child)


def _replay_body(
    node: ast.AST,
    schema: dict[str, dict[str, str]],
    names: Mapping[str, int],
    helpers: Mapping[str, ast.FunctionDef],
) -> None:
    """One `upgrade()`, descending one level into its own module's helpers.

    `m09e` puts both `alter_column`s inside `_resize(width)` and calls it with
    a module constant, so a replay that does not follow the call sees no type
    change at all. One level, no recursion: this chain has exactly one such
    helper and a general inliner would be a worse thing to trust than a
    disagreement the summary prints.
    """
    for inner in _in_order(node):
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
                _replay_body(helper, schema, bound, {})
                continue
            _replay_call(inner, schema, names)
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            _replay_sql(inner.value, schema)


def _replay_call(
    node: ast.Call, schema: dict[str, dict[str, str]], names: Mapping[str, int]
) -> None:
    called = node.func
    name = called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", "")
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return
    table = node.args[0].value
    if not isinstance(table, str):
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
        argument = node.args[1]
        retyped = next(
            (one.value for one in node.keywords if one.arg == "type_"),
            None,
        )
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str) and retyped:
            sql_type = _literal_type(retyped, names)
            if sql_type is not None:
                schema.setdefault(table, {})[argument.value] = sql_type


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
    bucket empty and exit 0 -- and ADR-0041 specifies F9's guard as "assert the
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

#: The figures ADR-0041 publishes, keyed by reading. **This is the drift check
#: with teeth**: `--check` compares the live ledger against it, so a change to
#: the schema, to a writer's `except`, or to a source class that moves a column
#: between buckets fails the run and names the document that has gone stale.
#: A cross-check that only compared the metadata against the migrations could
#: not see any of that -- every `shape`, `staging`, `writer` and `translation`
#: cell F9 consumes had no drift check at all.
PUBLISHED: Mapping[str, Mapping[str, int]] = {
    "closure": {"safe": 20, "translated": 10, "exposed-copy": 30, "exposed-sqlalchemy": 19},
    "path": {"safe": 18, "translated": 10, "exposed-copy": 31, "exposed-sqlalchemy": 20},
    "pydantic": {"safe": 14, "translated": 10, "exposed-copy": 34, "exposed-sqlalchemy": 21},
}

#: Same, at M8's head, which is what the roadmap's corrections are scored
#: against. `17` appears in none of them, and that is the finding.
PUBLISHED_AT_M08B: Mapping[str, Mapping[str, int]] = {
    "closure": {"safe": 18, "translated": 5, "exposed-copy": 30, "exposed-sqlalchemy": 18},
    "path": {"safe": 16, "translated": 5, "exposed-copy": 31, "exposed-sqlalchemy": 19},
    "pydantic": {"safe": 12, "translated": 5, "exposed-copy": 34, "exposed-sqlalchemy": 20},
}


def readings_table() -> str:
    """Every reading's arithmetic at both heads, printed together.

    Printed rather than chosen-and-hidden because the choice between them moves
    published figures: `safe` runs 20/18/14 today and 18/16/12 at `m08b`. A
    reader who cannot see the other two columns cannot tell a decision from an
    accident.
    """
    lines = ["reading            safe  transl  copy  sqla  exposed  (m09f / m08b)"]
    for reading in READINGS:
        for label, at in (("m09f", None), ("m08b", "m08b")):
            got = counts(build_ledger(reading, at=at))
            exposed = got["exposed-copy"] + got["exposed-sqlalchemy"]
            marker = "*" if reading == DEFAULT_READING else " "
            lines.append(
                f"{marker}{reading:<10} {label}  {got['safe']:>4}  {got['translated']:>6}  "
                f"{got['exposed-copy']:>4}  {got['exposed-sqlalchemy']:>4}  {exposed:>7}"
            )
    lines.append(f"  (* = the reading ADR-0041 adopts: {DEFAULT_READING})")
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
        f"bounded columns (ADR-0041's rule, reading={reading}): {len(rows)}",
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
        ("m09f", PUBLISHED, None),
        ("m08b", PUBLISHED_AT_M08B, "m08b"),
    ):
        for name in READINGS:
            got = counts(build_ledger(name, at=at))
            want = dict(published[name])
            if {k: got[k] for k in want} != want:
                complaints.append(
                    f"bucket drift at {label} under reading={name}: "
                    f"ADR-0041 publishes {want}, the ledger says "
                    f"{ {k: got[k] for k in want} }"
                )
    if reading not in READINGS:  # pragma: no cover - argparse constrains this
        complaints.append(f"unknown reading {reading!r}")
    return complaints


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_bounded_columns",
        description=(
            "The per-column bounded-column ledger behind ADR-0041 and issue #10. "
            "Offline: no database, no socket, nothing written. See the module "
            "docstring for the bounding rule and the three readings of it."
        ),
    )
    parser.add_argument("--summary", action="store_true", help="print the counts only")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 on any drift from the figures ADR-0041 publishes, at both heads",
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
