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
task actually ships against — `ix_titles_tmdb_id`/`ix_titles_imdb_id`/
`ix_titles_tvdb_id` (unique partial indexes, `db/models/title.py`, shipped
in Task 8/9, before this task) are exactly the kind of future column the
amendment was waiting for, except they already exist. `update()` sets
tmdb_id/imdb_id/tvdb_id from the incoming `Title`, so retargeting one to a
value another row already holds raises `IntegrityError` today, not
someday. Left uncaught, that would violate the same "db is driven, not
driving" contract `add()`'s translation exists to uphold — confirmed
directly by first writing this without the fix and watching a raw
`sqlalchemy.exc.IntegrityError` escape `update()`.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.models.title import TitleRow
from usher.domain.enums import EnrichmentState
from usher.domain.title import Title
from usher.ports.errors import RepositoryConflict, RepositoryNotFound
from usher.ports.repository import TitleRepository


def _to_domain(row: TitleRow) -> Title:
    return Title.model_validate({c.name: getattr(row, c.name) for c in TitleRow.__table__.columns})


def _to_row(title: Title) -> TitleRow:
    return TitleRow(**title.model_dump(exclude={"created_at", "updated_at"}))


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
            raise RepositoryConflict(f"title {title.id} already exists") from exc

    async def update(self, title: Title) -> None:
        row = await self._session.get(TitleRow, title.id)
        if row is None:
            raise RepositoryNotFound(f"no existing title {title.id} to update")
        # _to_row(title) raises loudly on any field/column mismatch, the same
        # way add() does -- a setattr loop straight off title.model_dump()
        # would not raise, it would just silently skip a would-be column
        # that no longer has a match, undoing the "loud, not silent" point
        # made below.
        fresh = _to_row(title)
        try:
            async with self._session.begin_nested():
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
            raise RepositoryConflict(f"title {title.id} conflicts with an existing title") from exc

    async def get(self, title_id: uuid.UUID) -> Title | None:
        row = await self._session.get(TitleRow, title_id)
        return _to_domain(row) if row else None

    async def get_by_tmdb_id(self, tmdb_id: int) -> Title | None:
        result = await self._session.execute(select(TitleRow).where(TitleRow.tmdb_id == tmdb_id))
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def get_by_imdb_id(self, imdb_id: str) -> Title | None:
        result = await self._session.execute(select(TitleRow).where(TitleRow.imdb_id == imdb_id))
        row = result.scalar_one_or_none()
        return _to_domain(row) if row else None

    async def count_by_state(self) -> dict[EnrichmentState, int]:
        result = await self._session.execute(
            select(TitleRow.enrichment_state, func.count()).group_by(TitleRow.enrichment_state)
        )
        counts = dict.fromkeys(EnrichmentState, 0)
        counts.update({EnrichmentState(state): count for state, count in result.all()})
        return counts
