"""`llm_calls` — one row per *attempted* completion, whether or not it worked."""

from sqlalchemy import DateTime, Numeric, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from usher.db.base import enum_column
from usher.db.models.curation import COST_PRECISION, COST_SCALE
from usher.db.repositories._errors import refusals_as_conflict
from usher.domain.curation import LLMCall, LLMPurpose
from usher.ports.repository import LLMCallRepository

# **Eleven columns named explicitly, never `INSERT INTO llm_calls VALUES (...)`.**
# Positional values would still be correct today and would shift silently the moment a
# column is added.
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
    # The same declaration the column carries, so the member-to-value conversion is one
    # implementation rather than a `.value` spelled by hand here and a `values_callable`
    # spelled there.
    bindparam("purpose", type_=enum_column(LLMPurpose, length=32)),
    # `NUMERIC(12, 8)` from the same two constants the model and the migration read,
    # because a scale declared three times is a scale that eventually disagrees with
    # itself.
    bindparam("cost_usd", type_=Numeric(COST_PRECISION, COST_SCALE, asdecimal=True)),
    # Nullable, and the `None` is a state rather than an omission: a purpose
    # that produces no rows at all has no generation, and `QueryExpansionService`
    # writes exactly that on every row, so on a deployment that curates and is
    # searched those are the majority of this table.
    bindparam("generation_id", type_=PGUUID(as_uuid=True)),
)

# **`cost_usd` is this table's reason for catching `DBAPIError` and filtering on
# SQLSTATE class rather than catching `IntegrityError` like most of its siblings.** The
# column is `NUMERIC(12, 8)`, so a call above `$9,999.99999999` raises `numeric field
# overflow` -- reachable from a *validly constructed* `LLMCall`, since the model bounds
# that field with `ge=0` and no ceiling, and the exact misconfiguration precision 12 was


class PostgresLLMCallRepository(LLMCallRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, call: LLMCall) -> None:
        # **The SAVEPOINT `refusals_as_conflict` opens buys more here than on any
        # sibling.** `record()` is called from inside an exception handler that is
        # typically still holding curated rows it has to commit, so a refused ledger row
        # that aborted the caller's transaction would turn a failed *call* into a lost
        # *generation* -- and the next statement on that session would raise
        async with refusals_as_conflict(
            self._session, "an llm call violates the ledger's own bounds"
        ):
            await self._session.execute(_INSERT_CALL, _parameters(call))


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
        # Written from `call.ok`, never derived from `error is None`.
        "ok": call.ok,
        "error": call.error,
        # Never coalesced. `None` is what makes a query-expansion call
        # legible, and a coalesce to the row's own id would give PRD 10's
        # "cost per curated row" join a key that matches nothing while looking
        # populated.
        "generation_id": call.generation_id,
    }
