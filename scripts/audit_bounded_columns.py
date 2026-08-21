"""The per-column ledger behind ADR-0041 and issue #10.

**Not a test, offline, and it writes nothing.** It opens no database and no
socket. Every fact it prints is derived from three artefacts already in this
repository -- the SQLAlchemy metadata in `usher.db.models`, the source of
`usher.db.repositories` read as an AST, and the pydantic models in
`usher.domain` -- plus an independent replay of `usher/db/migrations/versions/`
used only to cross-check the first of those.

    uv run python scripts/audit_bounded_columns.py            # the ledger
    uv run python scripts/audit_bounded_columns.py --summary  # the counts only
    uv run python scripts/audit_bounded_columns.py --check    # exit 1 on drift

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
_BOUNDED_PREFIXES = ("VARCHAR(", "CHAR(", "SMALLINT", "INTEGER", "BIGINT", "NUMERIC(", "HALFVEC(")

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
    """A repository method that issues at least one write."""

    module: str
    qualname: str
    lineno: int
    destinations: tuple[str, ...]
    staged: tuple[str, ...]
    translation: str


@dataclasses.dataclass(frozen=True, slots=True)
class LedgerRow:
    """One bounded destination column, with everything the ADR quotes."""

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
    return str(column.type.compile(dialect=postgresql.dialect()))


def _is_bounded(rendered: str) -> bool:
    return rendered.startswith(_BOUNDED_PREFIXES)


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
    for _ in range(4):
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
            break
    return resolved


# The calls that actually reach the database. A function holding SQL is not a
# write site: `watch_state.py`'s `_deduped`, `_update` and `_insert` are string
# builders, and counting them as writers put five "no `except`" verdicts on
# `watch_states` columns that `merge_from_source` does translate.
_EXECUTING_CALLS = frozenset(
    {
        "execute",
        "add",
        "add_all",
        "flush",
        "stage_records",
        "_stage",
        "_rowcount",
        "_write_result",
        "copy_records_to_table",
    }
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
    for _ in range(4):
        grown = {name for name, calls in direct.items() if calls & executing}
        if grown <= executing:
            break
        executing |= grown
    return executing


# Every mapped class, so an ORM write can be attributed to a table without a
# hand-maintained second copy of the mapping the models already declare.
_ROW_TABLES: Mapping[str, str] = {
    name: getattr(usher.db.models, name).__tablename__ for name in usher.db.models.__all__
}
_ORM_WRITE_CALLS = frozenset({"add", "add_all", "flush"})
_ORM_STATEMENT_CALLS = frozenset({"update", "insert", "delete", "pg_insert"})


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
        return "enum"
    parts: list[str] = []
    for item in metadata:
        for attribute in ("ge", "gt", "le", "lt", "max_length", "min_length"):
            value = getattr(item, attribute, None)
            if value is not None:
                parts.append(f"{attribute}={value}")
        pattern = getattr(item, "pattern", None)
        if isinstance(pattern, str):
            parts.append(f"pattern={pattern}")
    return ",".join(sorted(parts)) if parts else ""


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
    if name == "HALFVEC":
        dimensions = keywords.get("dim", arguments[0] if arguments else None)
        return f"HALFVEC({dimensions})" if dimensions is not None else None
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
    schema: dict[str, dict[str, str]] = {}
    for path in _revision_order():
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


def build_ledger() -> list[LedgerRow]:
    staging = staging_ddls()
    edges = staged_into()
    sites = write_sites()
    bounds = domain_bounds()

    feeds: dict[tuple[str, str], StagingColumn] = {}
    for staging_table, destinations in edges.items():
        for destination in destinations:
            for column in staging.get(staging_table, {}).values():
                feeds.setdefault((destination, column.name), column)

    rows: list[LedgerRow] = []
    for table, column, sql_type in bounded_columns():
        writers = [site for site in sites if table in site.destinations]
        fed = feeds.get((table, column))
        staged_writers = [site for site in writers if any(site.staged)]
        shape = _shape(sql_type, fed.sql_type if fed and staged_writers else None)
        domain = bounds.get((table, column), "")
        translations = {site.translation for site in writers}
        bucket, reason = _classify(sql_type, domain, shape, writers, table, column)
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
                domain=domain or "-",
            )
        )
    return rows


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

# One column, and it is the one place `safe` means something weaker than it
# does everywhere else. `titles.kind` is `TitleKind` on `usher.domain.title.
# Title`, which is pydantic and validates; on the *bulk* path the same column
# is fed by `usher.ports.bulk.ImdbTitle.kind`, a **frozen dataclass** field,
# which is a mypy claim and not a runtime one. Named rather than re-bucketed:
# the value set is still closed, and what is weaker is who closes it.
_ANNOTATION_ONLY: Mapping[tuple[str, str], str] = {
    ("titles", "kind"): (
        " -- WEAKER on the bulk path: fed by ports.bulk.ImdbTitle.kind, a frozen "
        "dataclass field, so the enum is enforced by mypy and not at runtime"
    ),
    ("titles", "imdb_id"): (
        " -- WEAKER on the bulk path: fed by ports.bulk.ImdbTitle.imdb_id, a bare "
        "`str` on a frozen dataclass, so the pattern is not applied there at all"
    ),
}


def _classify(
    sql_type: str,
    domain: str,
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
    writer at all, which is why it is decided first: a value the domain refuses
    to construct never reaches a driver by any route.
    """
    note = _ANNOTATION_ONLY.get((table, column), "")
    if domain == "enum":
        return "safe", f"enum-backed: the field's value set is a closed Python enum{note}"
    if (table, column) in _BATCH_BOUNDED:
        return "safe", _BATCH_BOUNDED[(table, column)]
    if _fully_bounded(sql_type, domain):
        return "safe", f"the domain field is bounded on every side the column is ({domain}){note}"
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
    if sql_type.startswith(("INTEGER", "SMALLINT", "BIGINT", "NUMERIC(")):
        return ("le=" in domain or "lt=" in domain) and ("ge=" in domain or "gt=" in domain)
    if sql_type.startswith(("VARCHAR(", "CHAR(")):
        declared = re.search(r"\((\d+)\)", sql_type)
        if declared is None:
            return False
        width = int(declared.group(1))
        if f"max_length={width}" in domain:
            return True
        for pattern, longest in _PATTERN_MAX_LENGTH.items():
            if f"pattern={pattern}" in domain and longest <= width:
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
    "writer",
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


def summary(rows: Sequence[LedgerRow]) -> str:
    counted: dict[str, int] = {}
    for row in rows:
        counted[row.bucket] = counted.get(row.bucket, 0) + 1
    families: dict[str, int] = {}
    for row in rows:
        family = re.sub(r"\(.*", "", row.sql_type)
        families[family] = families.get(family, 0) + 1
    copy_shapes: dict[str, int] = {}
    for row in rows:
        if row.bucket == "exposed-copy":
            copy_shapes[row.shape] = copy_shapes.get(row.shape, 0) + 1

    check_only = check_bounded_columns()
    wider, orphans = staging_shape()
    metadata_set = {(table, column, sql_type) for table, column, sql_type in bounded_columns()}
    replayed = migration_bounded_columns()
    lines = [
        f"bounded columns (ADR-0041's rule): {len(rows)}",
        "  by type family: " + ", ".join(f"{k} {v}" for k, v in sorted(families.items())),
        "  by bucket:      " + ", ".join(f"{k} {v}" for k, v in sorted(counted.items())),
        "  COPY-path failure shapes: "
        + (", ".join(f"{k} {v}" for k, v in sorted(copy_shapes.items())) or "none"),
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
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--summary", action="store_true", help="print the counts only")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the metadata and the migration replay disagree",
    )
    parser.add_argument(
        "--at",
        metavar="REVISION",
        help="count the bounded columns at a past migration head (e.g. m08b), off the replay",
    )
    arguments = parser.parse_args(argv)

    if arguments.at:
        past = migration_bounded_columns(stop_after=arguments.at)
        families: dict[str, int] = {}
        for _, _, sql_type in past:
            family = re.sub(r"\(.*", "", sql_type)
            families[family] = families.get(family, 0) + 1
        print(f"bounded columns at {arguments.at}: {len(past)}")
        print("  by type family: " + ", ".join(f"{k} {v}" for k, v in sorted(families.items())))
        for row in sorted(past):
            print("    " + ".".join(row[:2]) + " " + row[2])
        return 0

    rows = build_ledger()
    if not arguments.summary:
        print(render(rows))
        print()
    print(summary(rows))

    if arguments.check:
        metadata_set = {(table, column, sql_type) for table, column, sql_type in bounded_columns()}
        if metadata_set != migration_bounded_columns():
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
