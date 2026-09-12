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


# **The one read on this port, and it is an aggregate rather than a row.**
# `SearchQueryRetention.last_done()` is built on it (ADR-0046: a job answers
# "when were you last done" from the artefact it maintains), and
# `ix_search_queries_at` -- `m10c`'s, added for the `DELETE` below -- makes it
# an Index Only Scan of the leftmost leaf. Re-measured 2026-09-07 on `usher_j2`
# at 14,978 rows: `Heap Fetches: 1`, **4 buffers**, median **0.041 ms** over
# seven samples. Constant in the table's size, which is what makes it a
# steady-state number rather than a small-table one.
#
# ⚠️ **This comment carried `Heap Fetches: 0`, 3 buffers and 0.072 ms until
# 2026-09-07**, which is the figure `400eea3` had already corrected on
# `SearchQueryRepository.oldest` -- the same measurement, written down twice,
# and only one copy was updated. The port's docstring is the one that carries
# the reasoning (including why the fetch count is 1 and why the obvious
# explanation is false); this is a pointer to it rather than a second home for
# it.
_OLDEST_AT = text("SELECT min(at) FROM search_queries")

# 🔴 **`<`, not `<=`**, and the port says why: a row answered at exactly the
# cutoff is inside the window, which is the boundary PRD 10's own statement
# draws (`at < now() - interval '90 days'`). The two spellings are one
# character and both read as correct.
#
# **`:before` is a bound value and never `now()` in the statement**, for two
# reasons this project has already paid for. `now()` is
# `transaction_timestamp()` and `clock_timestamp()` is the instant a statement
# runs (`.claude/rules/db-and-sql.md`), so a cutoff computed inside a chunked
# loop is either frozen to the wrong transaction or moving under the loop --
# and a deletion boundary that moves is a different bug from either. The
# service computes it once per run from the clock this project injects, which
# is also what makes an 89/90/91-day case deterministic rather than flaky at
# midnight.
#
# **The subquery is what carries the `LIMIT`**: `DELETE ... LIMIT` is not
# PostgreSQL syntax, and the pattern is a self-`IN` on the primary key. The
# inner `ORDER BY at` is not cosmetic -- it is what lets the planner walk
# `ix_search_queries_at` for exactly `:limit` leaf entries instead of
# collecting every expired row and discarding all but the limit, and it makes
# the chunks oldest-first, so an interrupted run has removed the rows furthest
# past the window rather than an arbitrary sample of them.
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
        # Aware by the column's own type: `search_queries.at` is `TIMESTAMP
        # WITH TIME ZONE` (`m09a`), and asyncpg hands a `timestamptz` back
        # with a `tzinfo`. Asserted rather than trusted -- the port obliges
        # awareness because `Scheduler._due_now` subtracts this from an aware
        # `now`, and a naive value raises `TypeError` at the tick instead of
        # answering wrongly. A future `TIMESTAMP` column, or a driver change,
        # is a red here rather than a scheduler that stops running every job
        # registered after this one.
        if not isinstance(answered, datetime) or answered.tzinfo is None:
            # `PortDataMalformed` rather than a bare `AssertionError`: this
            # crosses a port boundary, ADR-0009 forbids a raw exception doing
            # that, and the family is the right one -- the store answered
            # something this port cannot use. `Scheduler._due_now` catches it,
            # counts the job failed and does not run it, which is the outcome
            # this guard exists for.
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
