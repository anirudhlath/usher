"""`llm_calls` — one row per completion, whether or not it worked.

Implements `LLMCallRepository` (`usher.ports.repository`). One statement, no
read, no scope: this is the smallest repository in the package, and every
decision in it is about what it declines to do.

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

from sqlalchemy import DateTime, Numeric, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import enum_column
from usher.db.models.curation import COST_PRECISION, COST_SCALE
from usher.db.repositories._errors import constraint_name, is_row_refusal
from usher.domain.curation import LLMCall, LLMPurpose
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import LLMCallRepository

# **Eleven columns named explicitly, never `INSERT INTO llm_calls VALUES
# (...)`.** Positional values would still be correct today and would silently
# shift the moment a column is added, which is the failure the two future
# indexes in `m08a`'s docstring make foreseeable: this table gains readers in
# M10, and a reader is what would find such a shift, years later, in a
# dashboard.
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
    # that produces no rows at all has no generation, and once Task 20 ships
    # query expansion those are the majority of this table.
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


class PostgresLLMCallRepository(LLMCallRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: LLMCall) -> None:
        try:
            with self._session.no_autoflush:
                # **The SAVEPOINT, and it buys more here than on any sibling.**
                # `record()` is called from inside an exception handler that
                # is typically still holding curated rows it has to commit, so
                # a refused ledger row that aborted the caller's transaction
                # would turn a failed *call* into a lost *generation* -- and
                # the next statement on that session would raise
                # `PendingRollbackError` with the failure attributed to
                # whatever ran next.
                async with self._session.begin_nested():
                    await self._session.execute(_INSERT_CALL, _parameters(call))
        except DBAPIError as exc:
            if not is_row_refusal(exc):
                # A dropped connection or a statement timeout is not this
                # row being wrong, and a caller that cannot tell those apart
                # would retry the one thing a retry cannot fix.
                raise
            raise RepositoryConflict(
                "an llm call violates the ledger's own bounds",
                # `None` for the overflow, which is a declared precision
                # refusing a value rather than a named constraint firing.
                constraint=constraint_name(exc),
            ) from exc


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
