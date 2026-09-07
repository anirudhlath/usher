"""`llm_calls` — one row per *attempted* completion, whether or not it worked.

Implements `LLMCallRepository` (`usher.ports.repository`). Two statements and
no scope -- an insert per attempted completion, and one windowed read over
`at` for the cost-anomaly evaluation. Still the smallest repository in the
package, and most of the decisions in it are about what it declines to do:
there is no filter on `ok`, none on `purpose` or `model`, and no `limit`.

**Not in `curation.py`, and that module says why in its own docstring**: the
two tables share a migration because one service writes both in one
transaction, and they share nothing else — no column, no foreign key, no
lifetime. A module holding both would be one class that replaces and one that
only ever inserts, sharing an import list.

Same session ownership as every other repository: flushes, never commits. The
service commits the ledger entry together with the rows it paid for, which is
what makes PRD 10's "cost per curated row" a join rather than a correlation on
timestamps.
"""

from collections.abc import Sequence

from pydantic import AwareDatetime
from sqlalchemy import DateTime, Numeric, RowMapping, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import enum_column
from usher.db.models.curation import COST_PRECISION, COST_SCALE
from usher.db.repositories._errors import refusals_as_conflict
from usher.domain.curation import LLMCall, LLMPurpose
from usher.ports.repository import LLMCallRepository

# **Eleven columns named explicitly, never `INSERT INTO llm_calls VALUES
# (...)`.** Positional values would still be correct today and would silently
# shift the moment a column is added -- the failure `m08a`'s docstring made
# foreseeable when it said this table gains readers in M10. `_LIST_SINCE`
# below is that reader, and it is `SELECT *` into an `extra="forbid"` model
# for the mirrored reason: a column added to the table without a field on
# `LLMCall` raises on the read rather than being silently dropped, so the two
# statements fail in opposite directions on the same drift.
#
# One row per statement. `record()` has no batch form and the port says why:
# every named call site records exactly once -- one completion per generation,
# one per search -- and a batch would be wrong in kind for the failure path,
# where the row's whole value is that it is written at the moment of failure
# rather than accumulated into something a crash loses.
_INSERT_CALL = text(
    "INSERT INTO llm_calls "
    "(id, at, model, purpose, tokens_in, tokens_out, cost_usd, latency_ms, ok, error,"
    " generation_id) "
    "VALUES (:id, :at, :model, :purpose, :tokens_in, :tokens_out, :cost_usd, :latency_ms,"
    "        :ok, :error, :generation_id)"
).bindparams(
    # Typed rather than cast in the statement text, for `curated_rows`'
    # reason: a `text()` construct carries no type information of its own, and
    # `:id::uuid` is not an option -- SQLAlchemy's bind-parameter regex reads a
    # name followed by `::` as a Postgres cast and skips the bind entirely.
    bindparam("id", type_=PGUUID(as_uuid=True)),
    bindparam("at", type_=DateTime(timezone=True)),
    # The same declaration the column carries, so the member-to-value
    # conversion is one implementation rather than a `.value` spelled by hand
    # here and a `values_callable` spelled there. `enum_column`'s docstring
    # records that SQLAlchemy's default binds a Python enum's `.name`
    # (`"CURATION"`), not its `.value` (`"curation"`), which is the wrong
    # string and the one this schema does not store.
    bindparam("purpose", type_=enum_column(LLMPurpose, length=32)),
    # `NUMERIC(12, 8)` from the same two constants the model and the migration
    # read, because a scale declared three times is a scale that eventually
    # disagrees with itself. What the declaration does *not* buy is a defence
    # against a `float` reaching this parameter: measured on
    # `pgvector/pgvector:pg17`, a Python float is accepted and is
    # value-preserving at this scale (`2e-08` stores `0.00000002`, `1/3`
    # stores `0.33333333`). What loses money is re-scaling on the way in, and
    # `test_a_cost_is_stored_exactly` is what refuses that.
    bindparam("cost_usd", type_=Numeric(COST_PRECISION, COST_SCALE, asdecimal=True)),
    # Nullable, and the `None` is a state rather than an omission: a purpose
    # that produces no rows at all has no generation, and `QueryExpansionService`
    # writes exactly that on every row, so on a deployment that curates and is
    # searched those are the majority of this table.
    bindparam("generation_id", type_=PGUUID(as_uuid=True)),
)

# **`cost_usd` is this table's reason for catching `DBAPIError` and filtering
# on SQLSTATE class rather than catching `IntegrityError` like most of its
# siblings.** The column is `NUMERIC(12, 8)`, so a call above
# `$9,999.99999999` raises `numeric field overflow` -- reachable from a
# *validly constructed* `LLMCall`, since the model bounds that field with
# `ge=0` and no ceiling, and the exact misconfiguration precision 12 was chosen
# to catch (a price scaled *up* by a million on the way in;
# `db/models/curation.py`'s module docstring holds the one copy of that
# argument and of the two limitations it does not cover).
#
# What that exception actually *is*, and why neither obvious `except` clause
# catches it, is measured once in `_errors.ROW_REFUSED_SQLSTATE_CLASSES` --
# together with `curated_rows."position"`, the same shape on an `integer`,
# which is what moved the predicate out of this module and into that one.


# **The reader `m08a` deferred and `m10c` shipped the index for.**
# `SELECT *` rather than eleven names, which is the opposite choice from the
# `INSERT` above and is this schema's house shape for a read: the row is
# validated into an `extra="forbid"` model built 1:1 with the table, so a
# column added to `llm_calls` and not to `LLMCall` raises here instead of
# being quietly dropped from a cost total.
#
# **`at >= :since` is the whole reason `ix_llm_calls_at` exists** -- `m08a`
# wrote the index out beside this predicate by name ("both `WHERE at >=
# :since`"), and `test_the_windowed_read_is_served_by_the_time_index` asserts
# the planner actually chooses it, at a seeded size quoted in that case.
#
# **`COALESCE` rather than `(:until IS NULL OR at < :until)`**, and the
# difference is the plan, not the taste: an `OR` over the ordering column
# cannot become an index condition, so the unbounded call would fall back to
# filtering every row the lower bound returned. `COALESCE(:until,
# 'infinity')` is one comparison the planner can push into the same index
# scan. `CAST(... AS timestamptz)` and never `::timestamptz`, for the reason
# the `INSERT`'s comment gives one screen up: SQLAlchemy's bind-parameter
# regex reads a name followed by `::` as a cast and skips the bind.
#
# Half-open, `[since, until)` -- the port's docstring carries the argument. A
# closed upper bound bills a call landing exactly on midnight to two adjacent
# days, and a nightly curation run at a fixed hour is what produces those
# rows.
#
# **`ORDER BY at` is declared and is not free to delete.** `llm_calls.id` is a
# UUIDv7, so id order agrees with `at` order on every fixture that records its
# rows in the order it wants them back -- the contract's ordering case mints
# in reverse for exactly that reason.
#: The statement as text, so `test_the_windowed_read_is_served_by_the_time_index`
#: can `EXPLAIN` **this** string rather than a transcription of it. A plan
#: assertion against a hand-copied lookalike measures the copy, and the copy is
#: what stops tracking the original -- `_LIST_FOR_USER` one module over is
#: exported for the same reason.
_LIST_SINCE_SQL = (
    "SELECT * FROM llm_calls "
    "WHERE at >= :since AND at < COALESCE(:until, CAST('infinity' AS timestamptz)) "
    "ORDER BY at"
)

_LIST_SINCE = text(_LIST_SINCE_SQL).bindparams(
    # Typed for `_INSERT_CALL`'s reason: a `text()` construct carries no type
    # information of its own, and an untyped `NULL` on `:until` leaves asyncpg
    # unable to resolve the `COALESCE`'s type.
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("until", type_=DateTime(timezone=True)),
)


class PostgresLLMCallRepository(LLMCallRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: LLMCall) -> None:
        # **The SAVEPOINT `refusals_as_conflict` opens buys more here than on
        # any sibling.** `record()` is called from inside an exception handler
        # that is typically still holding curated rows it has to commit, so a
        # refused ledger row that aborted the caller's transaction would turn a
        # failed *call* into a lost *generation* -- and the next statement on
        # that session would raise `PendingRollbackError` with the failure
        # attributed to whatever ran next.
        #
        # `constraint` comes back `None` for the `cost_usd` overflow, which is a
        # declared precision refusing a value rather than a named constraint
        # firing.
        async with refusals_as_conflict(
            self._session, "an llm call violates the ledger's own bounds"
        ):
            await self._session.execute(_INSERT_CALL, _parameters(call))

    async def list_since(
        self, since: AwareDatetime, *, until: AwareDatetime | None = None
    ) -> Sequence[LLMCall]:
        # No `refusals_as_conflict` here and nothing to translate: a `SELECT`
        # with two typed bounds has no constraint to violate and no value the
        # column can refuse, so the only failures reachable are transport ones
        # -- which the port promises to leave alone rather than dress as a
        # conflict (`test_a_failure_that_is_not_the_rows_fault_is_not_reported_
        # as_one` is the case that holds the write to the same rule).
        rows = (
            (await self._session.execute(_LIST_SINCE, {"since": since, "until": until}))
            .mappings()
            .all()
        )
        return [_to_domain(row) for row in rows]


def _to_domain(row: RowMapping) -> LLMCall:
    """One stored ledger entry, whole.

    `dict(row)` with no filtering and no name list, unlike
    `curation._to_domain` -- that one deletes a window label its statement
    adds, and this statement adds nothing to the table's own columns. So every
    column `llm_calls` gains reaches an `extra="forbid"` model and raises,
    which is the drift the `SELECT *` exists to make loud.
    """
    return LLMCall.model_validate(dict(row))


def _parameters(call: LLMCall) -> dict[str, object]:
    """The eleven columns, spelled out.

    A `model_dump()` would be shorter and would couple the statement's
    parameter names to the model's field names, so a field renamed in
    `domain/curation.py` would reach Postgres as an unbound parameter rather
    than as a type error here.
    """
    return {
        "id": call.id,
        # When the completion happened, not when the row was inserted --
        # `llm_calls.at` carries no `server_default` for exactly that reason,
        # so there is nothing for an omitted parameter to fall back to.
        "at": call.at,
        "model": call.model,
        "purpose": call.purpose,
        "tokens_in": call.tokens_in,
        "tokens_out": call.tokens_out,
        # The `Decimal` itself. Never `float(...)`, which is this project's
        # `1 / (60 + rank)` one column over: the value is summed over a month
        # and `$3/Mtok x 1,200 tokens` is exactly `0.0036`, which binary
        # floating point cannot represent.
        "cost_usd": call.cost_usd,
        "latency_ms": call.latency_ms,
        # Written from `call.ok`, never derived from `error is None`. The two
        # agree on every row a validator built -- `LLMCall._ok_and_error_must_
        # agree` and `ck_llm_calls_ok_error_agree` both say so -- so the
        # derived spelling was *predicted* to be an equivalent mutant.
        # **It is not, and the sweep is what corrected that.** It fails two of
        # the three `model_construct` cases, which are precisely the rows where
        # that invariant is suspended: derived, a row carrying `ok = false,
        # error = NULL` becomes a stored *success* and one carrying
        # `ok = true, error = '...'` becomes a stored *failure*, so the CHECK
        # those cases exist to prove is real never fires at all. The third
        # shape is not among them and the asymmetry is worth knowing -- for
        # `error = ''` the derivation happens to agree (`'' is None` is false),
        # so that row is refused either way.
        "ok": call.ok,
        "error": call.error,
        # Never coalesced. `None` is what makes a query-expansion call
        # legible, and a coalesce to the row's own id would give PRD 10's
        # "cost per curated row" join a key that matches nothing while looking
        # populated.
        "generation_id": call.generation_id,
    }
