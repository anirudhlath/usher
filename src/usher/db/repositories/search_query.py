"""`search_queries` -- one row per answered search, attributed by up to two later calls."""

import uuid
from datetime import datetime
from typing import Any, cast

from pydantic import AwareDatetime
from sqlalchemy import CursorResult, DateTime, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import enum_column
from usher.db.repositories._errors import refusals_as_conflict
from usher.ports.errors import PortDataMalformed
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.search import SearchMode, SearchSurface, SuggestTier

# Every column named explicitly, never `INSERT INTO search_queries VALUES
# (...)`: positional values shift silently the moment a column is added, and
# the reader that finds such a shift is a dashboard, years later.

# `clicked_title_id` and `played` are literals rather than binds because
# neither is a fact `record()`'s caller has. `result_count` and `latency_ms`
# carry no `bindparam` type on purpose: an untyped integer bind is what lets
# asyncpg refuse an out-of-range value client-side.
_INSERT_QUERY = text(
    "INSERT INTO search_queries "
    "(id, at, user_id, query, mode, result_count, latency_ms, "
    " clicked_title_id, played, surface, tier) "
    "VALUES (:id, :at, :user_id, :query, :mode, :result_count, :latency_ms, "
    "        NULL, false, :surface, :tier)"
).bindparams(
    # Typed rather than cast in the statement text -- `:id::uuid` is not an
    # option, `llm_calls`' comment records why: SQLAlchemy's bind-parameter
    # regex reads a name followed by `::` as a Postgres cast and skips the
    # bind entirely.
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("at", type_=DateTime(timezone=True)),
    bindparam("user_id", type_=PGUUID(as_uuid=True)),
    # The same declaration `SearchQueryRow.mode` carries, so the
    # member-to-value conversion is one implementation rather than a `.value`
    # spelled by hand here and a `values_callable` spelled there.
    bindparam("mode", type_=enum_column(SearchMode, length=16)),
    # Both widths are `SearchQueryRow`'s own, read off that model rather than
    # counted by hand here, because two spellings of one width is how they stop
    # agreeing. Typed for `mode`'s reason and for one more: `tier` binds `None`
    # on every search row, and an untyped `NULL` is the shape asyncpg refuses
    # with "could not determine data type of parameter".
    bindparam("surface", type_=enum_column(SearchSurface, length=8)),
    bindparam("tier", type_=enum_column(SuggestTier, length=6)),
)

# **Two columns, two different conditions, deliberately not one shared guard.** The
# funnel calls `record_outcome` *twice* on the same row at two different times --
# `GET /titles/{id}?search_id=…` attributes the click, `POST /titles/{id}/play` reports
# the play -- so a guard keyed on `clicked_title_id` alone would drop the second call.
_RECORD_OUTCOME = text(
    "UPDATE search_queries "
    "SET clicked_title_id = COALESCE(clicked_title_id, :clicked_title_id), "
    "    played = played OR :played "
    "WHERE id = :id AND user_id = :user_id"
).bindparams(
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("user_id", type_=PGUUID(as_uuid=True)),
    # Typed rather than left to the driver: the play writer binds `None`
    # here on every call, and an untyped `NULL` is the shape asyncpg refuses
    # with "could not determine data type of parameter"
    # (`.claude/rules/db-and-sql.md`). Declared, it is a `uuid` NULL and
    # `COALESCE` resolves against the column beside it.
    bindparam("clicked_title_id", type_=PGUUID(as_uuid=True)),
)


# **The one read on this port, and it is an aggregate rather than a row.**
# `SearchQueryRetention.last_done()` answers "when were you last done" from the artefact
# it maintains, and `ix_search_queries_at` makes this an Index Only Scan of the leftmost
# leaf.
_OLDEST_AT = text("SELECT min(at) FROM search_queries")

# **`<`, not `<=`**: a row answered at exactly the cutoff is inside the window, which is
# the boundary PRD 10's own statement draws (`at < now() - interval '90 days'`).
_PRUNE = text(
    "DELETE FROM search_queries WHERE id IN ("
    "  SELECT id FROM search_queries WHERE at < :before ORDER BY at LIMIT :limit"
    ")"
).bindparams(bindparam("before", type_=DateTime(timezone=True)))


class PostgresSearchQueryRepository(SearchQueryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, record: SearchQueryRecord) -> None:
        async with refusals_as_conflict(
            self._session, "a search query violates search_queries' own bounds"
        ):
            await self._session.execute(_INSERT_QUERY, _parameters(record))

    async def record_outcome(
        self,
        query_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        clicked_title_id: uuid.UUID | None,
        played: bool,
    ) -> None:
        async with refusals_as_conflict(
            self._session, "a search outcome violates search_queries' own bounds"
        ):
            await self._session.execute(
                _RECORD_OUTCOME,
                {
                    "id": query_id,
                    "user_id": user_id,
                    "clicked_title_id": clicked_title_id,
                    "played": played,
                },
            )

    async def oldest(self) -> AwareDatetime | None:
        found = await self._session.execute(_OLDEST_AT)
        # `min()` over an empty table is one row holding `NULL`, not no row --
        # so `scalar_one()` rather than `scalar_one_or_none()`, and `None`
        # here means the table is empty rather than that the read found
        # nothing to look at.
        answered = found.scalar_one()
        if answered is None:
            return None
        # Aware by the column's own type: `search_queries.at` is `TIMESTAMP WITH TIME
        # ZONE`, and asyncpg hands a `timestamptz` back with a `tzinfo`.
        if not isinstance(answered, datetime) or answered.tzinfo is None:
            # `PortDataMalformed` rather than a bare `AssertionError`: this crosses a
            # port boundary, where a raw exception must not, and the family is the right
            # one -- the store answered something this port cannot use.
            raise PortDataMalformed(
                "min(search_queries.at) read back without a timezone; the column is "
                "TIMESTAMP WITH TIME ZONE and ScheduledJob.last_done requires an aware value"
            )
        return answered

    async def prune(self, *, before: datetime, limit: int) -> int:
        result = await self._session.execute(_PRUNE, {"before": before, "limit": limit})
        # `rowcount` lives on `CursorResult`, not on the `Result[Any]`
        # `session.execute` is annotated to return -- `bulk.py:_rowcount` and
        # `PostgresCollectionRepository.link_title` both already record the
        # cast. It is the loop's only terminator, so it is the rows actually
        # removed and never the limit that was asked for.
        return int(cast("CursorResult[Any]", result).rowcount)


def _parameters(record: SearchQueryRecord) -> dict[str, object]:
    """The nine columns the INSERT binds, spelled out.

    `surface` is derived by the record rather than bound from a field, which is
    what stops the statement from writing `'search'` onto a keystroke.

    A `dataclasses.asdict()` would couple the statement's parameter names to
    the record's field names, so a renamed field would reach Postgres as an
    unbound parameter rather than as a type error here.
    """
    return {
        "id": record.id,
        "at": record.at,
        "user_id": record.user_id,
        "query": record.query,
        "mode": record.mode,
        "result_count": record.result_count,
        "latency_ms": record.latency_ms,
        "surface": record.surface,
        "tier": record.tier,
    }
