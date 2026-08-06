"""In-memory `LLMCallRepository`.

**Where this is more forgiving than Postgres, on purpose.** Seven places, each
of which the paired `tests/integration/test_llm_call_repository.py` run is
what actually closes:

- **The stored object *is* the object it was handed, so there is no column
  mapping here to get wrong.** `PostgresLLMCallRepository` builds eleven
  parameters and an `INSERT` naming eleven columns; this appends a reference.
  So a write that dropped `generation_id`, filled `tokens_out` from
  `tokens_in`, wrote `purpose` as a constant or `at` from the wall clock is
  **unexpressible on this arm** -- every field assertion in the contract is
  structural here and load-bearing there. Named first, because a divergence
  that makes a case vacuous is worse than one that makes it strict, and this
  one makes most of the suite vacuous.
- **No `NUMERIC(12, 8)`, so no `numeric field overflow`.** A price entered per
  token instead of per million -- `$36,000` on one 12,000-token call, the
  misconfiguration `m08a` chose precision 12 to catch -- stores happily here
  and is a `RepositoryConflict` there. That is the port's only conflict path
  reachable from a *validly constructed* `LLMCall` (the model bounds
  `cost_usd` with `ge=0` and no ceiling), so
  `test_a_cost_the_column_cannot_hold_is_a_port_error` is Postgres-only and is
  the case the whole error contract rests on.
- **No CHECK constraints.** `ck_llm_calls_ok_error_agree` and the five bound
  checks (`model <> ''`, both token counts, `cost_usd`, `latency_ms`) are
  enforced here only by `LLMCall`'s own pydantic bounds, which fire at a
  different moment with a different exception type -- and not at all for a row
  built with `model_construct`, which is how the integration file reaches
  them. `LLMCall._ok_and_error_must_agree` is a `model_validator(mode=
  "after")` precisely so that construction is possible.
- **No transaction and no SAVEPOINT.** A refused `record()` cannot poison a
  session here because there is none, so the half of the port's promise that
  says the caller keeps a usable session -- which is what lets
  `CurationService` still commit the curated rows it is holding when the
  ledger write is the thing that failed -- is Postgres-only, and so is the
  assertion that a refused row leaves the earlier ones alone.
- **`purpose` is an enum member, not a `VARCHAR(32)`.** It never round-trips
  through a string, so a value written as the member's `name` (`"CURATION"`)
  rather than its `value` (`"curation"`), or a column too narrow for
  `query_expansion`'s fifteen characters, is unexpressible here.
- **Timestamps are stored verbatim.** `timestamptz` normalises to UTC on the
  way out of Postgres, so a row written with a non-UTC offset reads back
  carrying `UTC` there and its original offset here. The two compare equal --
  aware datetimes compare by instant -- so only a case asserting on `tzinfo`
  or `utcoffset()` could tell them apart, and there is deliberately no such
  case in the contract.
- **The duplicate check is a linear scan of a list, not a btree.** Exact and
  O(n), which is the same answer `pk_llm_calls` gives and a different cost; a
  ledger is append-only and this one holds a handful of rows per test, so the
  scan is the honest shape rather than a dict pretending to be an index.

**The duplicate-id refusal is modelled exactly rather than diverged**, name
and all: `FakeTitleRepository` mirrors its three partial unique indexes name
for name for the same reason, so that `RepositoryConflict.constraint` agrees
between the two arms instead of one arm merely also raising. It is the one
place this fake reproduces the database rather than standing in for it,
because "an insert, never an upsert" is a decision a service could otherwise
violate through every unit test in the milestone and only discover against a
real primary key.

**And one divergence in the other direction, which is worth as much as the
seven above: this fake never rounds.** `NUMERIC(12, 8)` rounds a ninth decimal
place -- `$0.0375/Mtok x 101 tokens` is exactly `0.0000037875` and stores as
`0.00000379`, a residual bounded by 5e-9 USD per call -- and a Python
`Decimal` keeps every digit it was given. So a contract case asserting exact
equality on a cost carrying nine decimal places would pass here and fail
there, against a *correct* implementation. Every value in
`MEASURED_COSTS` is therefore at or under scale 8, and that constraint is this
fake's doing rather than the column's.
"""

from usher.domain.curation import LLMCall
from usher.ports.errors import RepositoryConflict
from usher.ports.repository import LLMCallRepository


class FakeLLMCallRepository(LLMCallRepository):
    def __init__(self) -> None:
        #: Every recorded call, in the order it was recorded -- the table, not
        #: a screen. There is no read on this port, so nothing narrows it;
        #: `tests/unit/test_fakes_llm_call_repository.py`'s ledger is what the
        #: contract observes it through.
        self.calls: list[LLMCall] = []

    async def record(self, call: LLMCall) -> None:
        # `pk_llm_calls`, modelled rather than diverged -- see the module
        # docstring. The check is before the append, so a refused call leaves
        # the ledger exactly as it was, which is also what the real one's
        # SAVEPOINT buys on the arm that has a transaction.
        if any(stored.id == call.id for stored in self.calls):
            raise RepositoryConflict(
                f"llm call {call.id} is already in the ledger", constraint="pk_llm_calls"
            )
        # Append, never replace. A store keyed on `generation_id` would look
        # identical against any fixture that mints one generation per call,
        # which is exactly what `test_two_calls_for_one_generation_are_two_
        # rows` exists to make false.
        self.calls.append(call)
