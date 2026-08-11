"""`search_queries` -- one row per answered search, updated at most once by
attribution.

Implements `SearchQueryRepository` (`usher.ports.repository`). Two
statements, both wrapped in the same SAVEPOINT-backed refusal translation
`LLMCallRepository.record` and `CuratedRowRepository.replace_for_user` use, so
a refused analytics write never poisons whatever else the caller's
transaction is holding.

Same session ownership as every other repository here: flushes, never
commits.
"""

import uuid

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import enum_column
from usher.db.repositories._errors import refusals_as_conflict
from usher.ports.repository import SearchQueryRecord, SearchQueryRepository
from usher.ports.search import SearchMode

# **Nine columns named explicitly**, never `INSERT INTO search_queries VALUES
# (...)`, for the reason `llm_calls`' identical comment gives: positional
# values shift silently the moment a column is added, and this table gains
# readers in a later milestone -- a reader is what would find such a shift,
# possibly years later, in a dashboard.
#
# `clicked_title_id` and `played` are written as **literals** (`NULL`,
# `false`) rather than as bind parameters: neither is a fact `record()`'s
# caller (F2) has, and a column with no default (`played` is `NOT NULL` with
# none at all) has to get its first value from somewhere. `record_outcome`
# is the only thing that ever moves them.
#
# `result_count` and `latency_ms` are deliberately left with no explicit
# `bindparam` type, following `curated_rows."position"`'s precedent
# (`db/repositories/curation.py`): an untyped integer bind is exactly what
# lets asyncpg's own binary encoder refuse an out-of-range value client-side,
# which is the behaviour `record`'s docstring documents and
# `test_a_latency_the_column_cannot_hold_is_a_port_error` (integration) pins.
_INSERT_QUERY = text(
    "INSERT INTO search_queries "
    "(id, at, user_id, query, mode, result_count, latency_ms, "
    " clicked_title_id, played) "
    "VALUES (:id, :at, :user_id, :query, :mode, :result_count, :latency_ms, "
    "        NULL, false)"
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
)

# **`clicked_title_id IS NULL` in the `WHERE` is "first write wins"**, not
# merely a scope: a row that already carries a click is left untouched by a
# second call, so a redelivered or duplicated attribution cannot replace a
# real click with a later, less-informative one. Zero rows affected is a
# silent no-op either way -- no row named that `id`, or a row already
# attributed -- because nothing distinguishes the two to a caller and PRD 08's
# redelivery rule already asks for the second to be harmless.
_RECORD_OUTCOME = text(
    "UPDATE search_queries "
    "SET clicked_title_id = :clicked_title_id, played = :played "
    "WHERE id = :id AND clicked_title_id IS NULL"
).bindparams(
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("clicked_title_id", type_=PGUUID(as_uuid=True)),
)


class PostgresSearchQueryRepository(SearchQueryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, record: SearchQueryRecord) -> None:
        async with refusals_as_conflict(
            self._session, "a search query violates search_queries' own bounds"
        ):
            await self._session.execute(_INSERT_QUERY, _parameters(record))

    async def record_outcome(
        self, query_id: uuid.UUID, *, clicked_title_id: uuid.UUID, played: bool
    ) -> None:
        async with refusals_as_conflict(
            self._session, "a search outcome violates search_queries' own bounds"
        ):
            await self._session.execute(
                _RECORD_OUTCOME,
                {"id": query_id, "clicked_title_id": clicked_title_id, "played": played},
            )


def _parameters(record: SearchQueryRecord) -> dict[str, object]:
    """The seven F2 columns, spelled out.

    A `dataclasses.asdict()` would be shorter and would couple the
    statement's parameter names to the record's field names, so a field
    renamed on `SearchQueryRecord` would reach Postgres as an unbound
    parameter rather than as a type error here -- `llm_calls`' identical
    argument.
    """
    return {
        "id": record.id,
        "at": record.at,
        "user_id": record.user_id,
        "query": record.query,
        "mode": record.mode,
        "result_count": record.result_count,
        "latency_ms": record.latency_ms,
    }
