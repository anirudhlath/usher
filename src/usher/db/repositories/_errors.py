"""Turning Postgres's own structured error fields into port errors.

One copy, shared by every repository that catches `IntegrityError`. Its
content is two verified observations -- where asyncpg actually puts a
constraint name, and which SQLSTATE classes mean "this row is not storable as
given" -- and two copies of either is two chances to lose it.
"""

from sqlalchemy.exc import DBAPIError, IntegrityError

#: **SQLSTATE class, not exception class.** Most repositories in this package
#: catch `IntegrityError`, which is right for a table whose only refusals are
#: constraints and wrong for one whose column can refuse a *value*: SQLAlchemy's
#: asyncpg dialect leaves those as a bare `DBAPIError`, so neither of the two
#: obvious `except` clauses catches them and a raw SQLAlchemy exception crosses
#: the port boundary -- the one thing ADR-0009 says must never happen.
#:
#: **This comment is the one copy of that measurement**, and the consolidation
#: is itself a finding. The narrative was shipped at near-full length in seven
#: places -- two of them sixty-seven lines apart in *this* file -- and that is
#: precisely the mechanism that produced the defect it describes: one copy, on
#: the port, drifted to `sqlalchemy.exc.DataError` while six identical copies
#: stayed right, and the port is the surface the next three tasks read.
#: Everything that catches with this keeps its own table's fact beside its own
#: `except` and points here for the mechanism.
#:
#: Measured twice on `pgvector/pgvector:pg17`, in M8, on two tables and from
#: two directions, which is why this lives here rather than beside either:
#:
#: - `llm_calls.cost_usd` is `NUMERIC(12, 8)`, so a call above
#:   `$9,999.99999999` raises `numeric field overflow` server-side --
#:   `sqlalchemy.exc.DBAPIError`, cause `asyncpg.exceptions.
#:   NumericValueOutOfRangeError`, SQLSTATE `22003`. It is **not**
#:   `sqlalchemy.exc.DataError`, which is the other guess and is a `DBAPIError`
#:   *subclass*, so an `except` naming it catches nothing here.
#: - `curated_rows."position"` is `integer`, and `2**31` is refused
#:   **client-side** by asyncpg's own binary encoder before a byte is sent --
#:   `sqlalchemy.exc.DBAPIError`, cause `asyncpg.exceptions.DataError`, SQLSTATE
#:   `22000`. Note that is the *driver's* class of that name and not
#:   SQLAlchemy's; the two are unrelated types and both appear in this comment,
#:   so both are module-qualified here and everywhere they are named.
#:
#: Both are reachable from a *validly constructed* domain model: `LLMCall.
#: cost_usd` and `CuratedRow.position` are both bounded below and not above.
#: The second is the more general lesson -- the trap is not "a bounded
#: NUMERIC", it is any column narrower than the field feeding it, which
#: includes every `Integer` in this schema.
#:
#: Catching `DBAPIError` whole would be too much: it is also a dropped
#: connection, a statement timeout and an undefined table, none of which is
#: the caller's row being wrong and all of which a caller must be able to tell
#: apart from one. `22` (data exception) and `23` (integrity constraint
#: violation) are the two classes that mean the row; everything else
#: propagates untranslated.
#:
#: **Bounded, because "class 22 means the row" is not true of class 22 in
#: general.** It also carries statement-level faults -- `22012`
#: division_by_zero, `2201B` invalid_regular_expression, `22P02` on a literal
#: cast -- which are bugs in the *statement* rather than in the row a caller
#: handed in. The claim holds for **a parameterised statement with no
#: server-side expressions**, which is every caller today: both are a bare
#: `INSERT` of bound values, so the only thing class 22 can be about is a bound
#: value. A repository whose statement computes something would report its own
#: bug to the caller as a refused row, and needs a narrower predicate than this
#: one rather than a wider `except`.
ROW_REFUSED_SQLSTATE_CLASSES = frozenset({"22", "23"})


def is_row_refusal(exc: DBAPIError) -> bool:
    """Whether the backing store refused *this row* rather than the connection
    or the statement -- see `ROW_REFUSED_SQLSTATE_CLASSES` for the measurements behind the
    two SQLSTATE classes.

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

    **Typed `DBAPIError` rather than `IntegrityError`, widened by M8 Task
    10.** `IntegrityError` is a subclass, so every existing caller is
    unaffected; what the wider type admits is a refusal that is not an
    integrity violation at all -- `ROW_REFUSED_SQLSTATE_CLASSES` above holds
    the two measured shapes and is the only copy of them. The chain this reads
    is the same one either way, and it correctly answers `None` for both, since
    a column refusing a *value* is not a named constraint firing. Narrowing
    this back would force a second copy of the accessor, which is what this
    module exists to prevent.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
