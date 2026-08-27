"""`search_queries` -- one row per answered search, then attributed by up to
two later calls: a click, and separately a play.

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
from usher.ports.search import SearchMode, SearchSurface, SuggestTier

# **Every column named explicitly**, never `INSERT INTO search_queries VALUES
# (...)`, for the reason `llm_calls`' identical comment gives: positional
# values shift silently the moment a column is added, and this table gains
# readers in a later milestone -- a reader is what would find such a shift,
# possibly years later, in a dashboard. It was nine columns until `m10c` and
# is eleven now, which is that comment paying for itself: the statement below
# had to be edited, loudly, rather than starting to write `tier` into
# `surface`.
#
# `clicked_title_id` and `played` are written as **literals** (`NULL`,
# `false`) rather than as bind parameters: neither is a fact `record()`'s
# caller has, and a column with no default (`played` is `NOT NULL` with none
# at all) has to get its first value from somewhere. `record_outcome` is the
# only thing that ever moves them.
#
# 🔴 **`surface` was the literal `'search'` for exactly one commit and is a
# bind parameter now.** `m10c` landed the column `NOT NULL` with no
# `server_default` -- deliberately, because a default would outlive the
# migration and supply a plausible wrong value to a writer that forgot -- so
# this statement had to name it, and at `m10c` `'search'` was the *true* value
# for every row this method wrote: the one caller was
# `SearchService._record_search`. J2 gives it a second caller
# (`SearchService.suggest`, both tiers), so the literal would now be the
# plausible wrong value the migration refused to install, one layer up. It
# comes off `SearchQueryRecord.surface`, which is required and undefaulted for
# the same reason.
#
# `tier` is a bind parameter beside it rather than a `NULL` literal, and the
# pairing is the point: `SearchQueryRecord` refuses the two combinations that
# are not states -- a `search` row with a tier, a `suggest` row without one --
# so these two binds can never disagree about which index answered.
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
    # Both widths are `SearchQueryRow`'s own -- 8 for `surface` and 6 for
    # `tier` -- read off that model rather than counting the longest member
    # here, because two spellings of one width is how they stop agreeing.
    # Typed for `mode`'s reason and for one more:
    # `tier` binds `None` on every `search` row, and an untyped `NULL` is the
    # shape asyncpg refuses with "could not determine data type of parameter"
    # (`.claude/rules/db-and-sql.md`, and `_RECORD_OUTCOME`'s
    # `clicked_title_id` below is the same trap one statement over).
    bindparam("surface", type_=enum_column(SearchSurface, length=7)),
    bindparam("tier", type_=enum_column(SuggestTier, length=6)),
)

# **Two columns, two different conditions, deliberately not one shared
# guard.** A single `WHERE clicked_title_id IS NULL` was the first cut of
# this statement and it was wrong: F3's own funnel calls `record_outcome`
# *twice* on the same row at two different times --
# `GET /titles/{id}?search_id=…` attributes the click, and
# `POST /titles/{id}/play` reports the play -- and a
# guard keyed on `clicked_title_id` alone silently drops the second call,
# which is the only call in the whole funnel that could ever set `played`.
# Reviewed and corrected before this shipped; see the port docstring for the
# full argument and `tests/contract/search_query_repository_contract.py`'s
# module docstring for the cases that pin it.
#
# `clicked_title_id = COALESCE(clicked_title_id, :clicked_title_id)` is first
# write wins **on that column specifically**: once a click is attributed, a
# later, genuinely different click (someone else's redelivered event, or a
# stale retry naming the wrong result) must not steal credit from the result
# the household actually opened. It is also what lets the *play* writer pass
# `NULL` -- `COALESCE(clicked_title_id, NULL)` is the column unchanged, so a
# play reports `played` and touches nothing else, which is what keeps the two
# writers from collapsing into one that sets both.
#
# `played = played OR :played` is monotonic and moves only toward `True`: a
# call that has not itself observed a play carries `played=False`, and there
# is no route in F3's design that means "actually, undo the play" -- so a
# later `False` is stale information about a fact the row already has,
# never a correction to write over it.
#
# **`AND user_id = :user_id` is a security boundary, not tidiness.** The
# `id` half comes from a client, on `?search_id=`, and UUIDv7 is partially
# time-ordered and therefore partially guessable; without this predicate one
# household writes attribution onto another's row silently, with no error,
# no log line and no metric. It is a predicate rather than a column written:
# `record()` set `user_id` and nothing may move it.
#
# Zero rows affected is still a silent no-op either way -- no row named that
# `id`, a row belonging to somebody else, or a row whose columns already hold
# at least as much as this call would write -- because nothing distinguishes
# those to a caller, and a caller that *could* tell "not yours" from "not
# there" would have a household oracle.
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


def _parameters(record: SearchQueryRecord) -> dict[str, object]:
    """The nine columns a caller supplies, spelled out.

    Seven until `m10c`; `surface` and `tier` are the two the amendment added
    and they arrive here rather than in the statement text, which is what
    stops the INSERT from writing `'search'` onto a keystroke.

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
        "surface": record.surface,
        "tier": record.tier,
    }
