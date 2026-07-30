"""Persistence for canonical titles.

Repositories translate between SQLAlchemy rows and domain models. Nothing
above this layer sees a Row type. Implements the TitleRepository port
(usher.ports.repository) so services can depend on the port instead of
this module directly — see ADR-0009.

Session-poisoning decision (binding on every repository after this one —
see the plan's "Open question flagged for Group E" note on Task 10):
`add()` and `update()` each wrap their flush in `session.begin_nested()`,
a SAVEPOINT, rather than calling `session.rollback()` in the `except`
block. Both close the same hole — Postgres aborts the *entire* transaction
on any statement error until a ROLLBACK, so an uncaught `IntegrityError`
leaves the session unusable for anything else. But `TitleRepository`'s own
docstring already says "the caller owns the session and the transaction:
... committing or rolling back is the caller's call" — a full
`session.rollback()` here would discard whatever else the caller had
pending on this session (e.g. three other repository calls earlier in the
same request), which is exactly the ownership that line rules out. A
SAVEPOINT confines the damage to just this call's own statement(s): on
`IntegrityError` it rolls back to the SAVEPOINT, not to the start of the
transaction, so the caller's other pending work survives and the caller
still decides commit vs. rollback for the transaction as a whole. The cost
is one extra round trip per `add`/`update` call — accepted deliberately,
because the alternative silently breaks the port's documented contract.
Verified directly (`tests/integration/test_title_repository.py`): without
the SAVEPOINT, a caught `RepositoryConflict` leaves the *session* raising
`PendingRollbackError` on the next unrelated call, not just the failed one
— proving this isn't a hypothetical concern.

`update()` needs the identical translation, not just `add()`: the plan's
own amendment claiming "nothing in [update's] current body raises
IntegrityError... it can't yet" no longer holds against the schema this
task actually ships against — `ix_titles_tmdb_id_kind`/`ix_titles_imdb_id`/
`ix_titles_tvdb_id` (unique partial indexes, `db/models/title.py`, shipped
in Task 8/9, before this task; `ix_titles_tmdb_id_kind` widened from a
single-column index by ADR-0011) are exactly the kind of future column the
amendment was waiting for, except they already exist. `update()` sets
tmdb_id/imdb_id/tvdb_id from the incoming `Title`, so retargeting one to a
value another row already holds raises `IntegrityError` today, not
someday. Left uncaught, that would violate the same "db is driven, not
driving" contract `add()`'s translation exists to uphold — confirmed
directly by first writing this without the fix and watching a raw
`sqlalchemy.exc.IntegrityError` escape `update()`.

Autoflush is the second way a storage exception can escape this class,
independent of the SAVEPOINT decision above. `session.get()` and
`session.execute()` both flush any pending, unflushed session state before
running — including state this repository never touched, left behind by
some other call sharing the same `AsyncSession` (see `TitleRepository`'s
docstring for the session-wide precondition this is about). Verified
directly: a pending, invalid row staged on the session (not flushed) made
every one of `get`/`get_by_tmdb_id`/`get_by_imdb_id`/`count_by_state` raise
a raw `sqlalchemy.exc.IntegrityError`, and made `update()`'s own
`session.get()` lookup do the same, before the fix below. The four read
methods now run their query inside `self._session.no_autoflush` — a plain
read has nothing to flush on its own account, so suppressing autoflush
entirely is strictly better there than catching and translating an error
that isn't this call's own conflict to report. `update()`'s lookup instead
moved inside its existing SAVEPOINT + `except IntegrityError` (the same
protection its mutate-and-flush already had), because unlike a pure read,
`update()` already has a conflict-shaped exception to raise if the lookup's
autoflush fails.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.title import TitleRow
from usher.domain.enums import EnrichmentState, TitleKind
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


def _to_domain(row: TitleRow) -> Title:
    return Title.model_validate({c.name: getattr(row, c.name) for c in TitleRow.__table__.columns})


# The four ARRAY(Text) columns -- see the module docstring's note on
# ARRAY(Text) always reading back as a list, never a tuple.
_ARRAY_FIELDS = ("genres", "keywords", "spoken_languages", "origin_countries")


def _to_row(title: Title) -> TitleRow:
    # Emits lists for the four ARRAY columns, not the tuples Title actually
    # types them as. update()'s mutate loop setattr()s every column from
    # this onto an already-persistent row loaded from the database, and a
    # loaded row's ARRAY columns are always lists -- never tuples -- on
    # read. SQLAlchemy's attribute-history comparison (what decides whether
    # a column is actually included in the UPDATE's SET clause) uses `==`,
    # and `("a",) != ["a"]` in Python regardless of contents, so emitting
    # tuples here made every update() rewrite all four arrays even when
    # nothing in them changed -- confirmed directly by counting the actual
    # UPDATE statements SQLAlchemy issued for a semantically no-op update()
    # call: before this fix, one was issued touching all four columns;
    # after, none is.
    data = title.model_dump(exclude={"created_at", "updated_at"})
    for field in _ARRAY_FIELDS:
        data[field] = list(data[field])
    return TitleRow(**data)


def _constraint_name(exc: IntegrityError) -> str | None:
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


def _conflict(title_id: uuid.UUID, constraint: str | None) -> RepositoryConflict:
    """Builds an accurate `RepositoryConflict` for `add()`/`update()`
    alike. Deliberately never claims `title_id` itself already exists --
    measured bug this replaced: the message used to read "title {id}
    already exists" unconditionally, which is false whenever the actual
    collision was on a *different* row's tmdb_id/imdb_id/tvdb_id (`id`
    doesn't pre-exist at all in that case; the provider id does). "conflicts
    with an existing title" is true either way -- `title_id`'s own id
    collided, or one of its provider ids did -- and `constraint` carries
    the specific, structured answer for a caller that needs to branch on
    which.
    """
    detail = f" (constraint: {constraint})" if constraint else ""
    return RepositoryConflict(
        f"title {title_id} conflicts with an existing title{detail}", constraint=constraint
    )


class PostgresTitleRepository(TitleRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, title: Title) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(_to_row(title))
                await self._session.flush()
        except IntegrityError as exc:
            # Postgres's own unique-violation on a duplicate id (or a
            # duplicate tmdb_id/imdb_id/tvdb_id), translated so callers
            # depend only on usher.ports.errors -- importing sqlalchemy.exc
            # here would break "db is driven, not driving". The flush runs
            # inside a SAVEPOINT (begin_nested) specifically so this catch
            # only unwinds this call's own insert, not the caller's whole
            # transaction -- see the module docstring.
            raise _conflict(title.id, _constraint_name(exc)) from exc

    async def update(self, title: Title) -> None:
        # _to_row(title) raises loudly on any field/column mismatch, the same
        # way add() does -- a setattr loop straight off title.model_dump()
        # would not raise, it would just silently skip a would-be column
        # that no longer has a match, undoing the "loud, not silent" point
        # made below.
        fresh = _to_row(title)
        try:
            async with self._session.begin_nested():
                # session.get() lives *inside* the try and the SAVEPOINT,
                # not just the flush below -- it autoflushes by default
                # (SQLAlchemy's load_on_pk_identity, no_autoflush=False),
                # so it can just as easily be the statement that surfaces
                # some *other*, unrelated pending row's IntegrityError on
                # this shared session (verified: session.execute()/
                # session.get() both do). Per the ordering trap already
                # documented below, that flush must happen inside the
                # SAVEPOINT or a caught conflict leaves the outer
                # transaction DEACTIVE for the next unrelated call -- the
                # same reasoning as the mutation loop, just one statement
                # earlier. See TitleRepository's docstring for the
                # session-wide precondition this is a backstop for, not a
                # substitute for.
                row = await self._session.get(TitleRow, title.id)
                if row is None:
                    raise RepositoryNotFound(f"no existing title {title.id} to update")
                # The mutation happens *inside* the SAVEPOINT scope, not
                # before it -- verified directly that mutating `row` first
                # and only wrapping flush() leaves the session's outer
                # transaction DEACTIVE after a caught conflict (a second,
                # unrelated call then raises PendingRollbackError instead
                # of succeeding). SQLAlchemy's rollback-to-SAVEPOINT only
                # cleanly reverts attribute changes it watched happen
                # within its own scope.
                for column in TitleRow.__table__.columns:
                    if column.name not in {"id", "created_at", "updated_at"}:
                        setattr(row, column.name, getattr(fresh, column.name))
                await self._session.flush()
        except IntegrityError as exc:
            # Same translation and same SAVEPOINT reasoning as add() -- see
            # the module docstring. Retargeting tmdb_id/imdb_id/tvdb_id to a
            # value another title already holds raises IntegrityError here
            # today (verified), not just in some future schema change.
            raise _conflict(title.id, _constraint_name(exc)) from exc

    async def get(self, title_id: uuid.UUID) -> Title | None:
        # no_autoflush: a plain read has no business flushing anything, and
        # by default it would anyway (session.get() autoflushes) -- so a
        # pre-existing, unflushed, invalid row left on this *shared* session
        # by unrelated code could otherwise fail right here, as a raw
        # sqlalchemy.exc.IntegrityError this method has no way to translate
        # meaningfully (it isn't this read's conflict to report). See
        # TitleRepository's docstring for the session-wide precondition
        # this is a backstop for, not a substitute for.
        with self._session.no_autoflush:
            row = await self._session.get(TitleRow, title_id)
        return _to_domain(row) if row else None

    async def get_by_tmdb_id(self, tmdb_id: int, kind: TitleKind) -> Title | None:
        # tmdb_id's own type is `int`, not `int | None` -- but a caller
        # holding a genuinely optional value (e.g. Title.tmdb_id itself)
        # can still reach this with None if it ever bypasses mypy at the
        # call site (a stray `# type: ignore`, `cast`, ...). Guarded
        # because `TitleRow.tmdb_id == None` compiles to `IS NULL`,
        # matching whichever null-provider-id title Postgres happens to
        # return first -- not "the title with this id", the opposite of
        # what this method promises. Verified: without this,
        # get_by_tmdb_id(None) returns an arbitrary title instead of None.
        #
        # The kind filter is not optional either. Without it this query can
        # match a movie and a series holding the same tmdb_id, and
        # scalar_one_or_none() then raises a raw
        # sqlalchemy.exc.MultipleResultsFound out of the port -- reproduced
        # directly against tmdb_id=550 in both namespaces. ADR-0011.
        if tmdb_id is None:
            return None
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow).where(TitleRow.tmdb_id == tmdb_id, TitleRow.kind == kind)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        # See get_by_tmdb_id's comment -- same IS NULL hazard, same guard.
        if imdb_id is None:
            return None
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow).where(TitleRow.imdb_id == imdb_id)
            )
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        with self._session.no_autoflush:  # see get()'s comment
            result = await self._session.execute(
                select(TitleRow.enrichment_state, func.count()).group_by(TitleRow.enrichment_state)
            )
        counts = dict.fromkeys(EnrichmentState, 0)
        counts.update({EnrichmentState(state): count for state, count in result.all()})
        return counts
