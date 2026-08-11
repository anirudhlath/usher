"""The `llm_calls` ledger: one home for *record on every path that attempted a
completion, and commit what you recorded.*

## Why this is a module and not a method on each spender

`.claude/rules/testing-discipline.md` records the measurement this file exists
for. The rule *"record **and** commit"* was once spelled verbatim at three
exits of `CurationService.generate`, and **deleting the commit from one of them
passed all 42 cases** -- the *rejected* arm, where the call succeeded, the money
is spent, `replace_for_user` is never reached and the `llm_calls` row is the
only record the spend happened at all. The repair was two things, and the
second is the one that generalises: *"a rule spelled three times is a rule one
deletion is invisible in ... N copies means N chances for one to go quiet."*

That argument was then made **inside** `CurationService` and not **across** the
two services that spend money. `QueryExpansionService` carried its own
`_settle` / `_ledger_row` / `_record`, identical to curation's but for the
purpose constant and the generation id -- so the count went back from one to
two, and five invariants were each argued and pinned twice:

- `ok` is **derived** from `error`, never passed beside it.
- an error string is `str(exc) or type(exc).__name__`, **never a bare
  `str(exc)`** -- which is `""` for an exception raised with no arguments, and
  `LLMCall` refuses a failed call with a blank error.
- `usage is None` is the upstream-failure path and nothing else.
- `except UsherPortError`, **not** `except Exception`.
- record, **then** commit, as one step.

Both spenders now hold one of these. `tests/unit/test_services_llm_ledger.py`
pins the five behaviourally and, with an `ast` walk rather than a substring
scan, pins that neither service mints a row of its own.

## What stayed different, and why it is a parameter rather than a subclass

`purpose` is fixed for the life of a spender and belongs on the constructor;
`generation_id` varies per call and belongs on `settle`. Curation passes one
because PRD 10's dashboard 5 is `llm_calls JOIN curated_rows USING
(generation_id)`; query expansion writes no `curated_rows` at all, so an id
minted there would be a join key pointing at nothing.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from loguru import logger
from pydantic import AwareDatetime

from usher.domain.curation import LLMCall, LLMPurpose
from usher.domain.ids import new_id
from usher.ports.errors import UsherPortError
from usher.ports.llm import LLMUsage
from usher.ports.repository import LLMCallRepository


class LLMLedger:
    """One attempted completion in, one committed `llm_calls` row out.

    **`model` is the string the client was built with and is not defaulted.**
    It is the only honest value for `llm_calls.model` on the path where no
    response came back to read one from; a default here would be a second value
    that silently disagrees with `Settings.llm_model`.

    **`commit` is a callable and not a session.** `services/` may depend only
    on `domain/` and `ports/` (ADR-0009) and a session is neither. It matters
    most on the query-expansion path, which writes nothing else: an uncommitted
    ledger row is rolled back when the read's session closes, and the money is
    spent with no record at all.

    **`clock` is injected** because the latency of a *failed* call is the one
    number this ledger cannot get from an `LLMUsage` that never came back, and
    a 120-second timeout is the most expensive thing either spender can do.
    """

    def __init__(
        self,
        *,
        ledger: LLMCallRepository,
        commit: Callable[[], Awaitable[None]],
        model: str,
        purpose: LLMPurpose,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ledger = ledger
        self._commit = commit
        self._model = model
        self._purpose = purpose
        self._now = now
        self._clock = clock

    async def settle(
        self,
        started: float,
        *,
        usage: LLMUsage | None,
        error: str | None,
        generation_id: uuid.UUID | None = None,
    ) -> None:
        """Close out one attempted completion: write its `llm_calls` row, then
        commit.

        **The clock is read here**, so `elapsed_ms` is a delta from `started`
        on every path and no caller can hand over an absolute reading. It is
        the *fallback* latency -- `_row` prefers whatever the adapter measured
        whenever an `LLMUsage` came back -- and the path with no usage is the
        one it exists for, where a 120-second timeout has no other record.

        **Not the commit boundary for `curated_rows`.** `CurationService` calls
        `replace_for_user` *before* this, so one commit covers both writes and
        PRD 10's `llm_calls JOIN curated_rows USING (generation_id)` never sees
        a screen with no cost attributed to it. The exception that ordering
        buys is named in `CurationService.generate`'s own docstring.
        """
        await self._record(
            self._row(
                usage=usage,
                elapsed_ms=_ms(self._clock() - started),
                error=error,
                generation_id=generation_id,
            )
        )
        await self._commit()

    def _row(
        self,
        *,
        usage: LLMUsage | None,
        elapsed_ms: int,
        error: str | None,
        generation_id: uuid.UUID | None,
    ) -> LLMCall:
        """One `llm_calls` row.

        **`ok` is derived from `error` rather than passed beside it.** The two
        must agree -- `LLMCall._ok_and_error_must_agree` and
        `ck_llm_calls_ok_error_agree` both refuse a disagreement -- so a
        signature taking both would be one that can be handed a contradiction,
        on the path least able to afford a `ValidationError`.

        `usage is None` is the upstream-failure path and nothing else: there is
        no answer to bill, so the tokens and cost are zero, the model is the
        one this deployment asked for, and the latency is what this ledger
        measured, which for a timeout is the whole of it.
        """
        return LLMCall(
            id=new_id(),
            at=self._now(),
            model=usage.model if usage is not None else self._model,
            purpose=self._purpose,
            tokens_in=usage.tokens_in if usage is not None else 0,
            tokens_out=usage.tokens_out if usage is not None else 0,
            cost_usd=usage.cost_usd if usage is not None else Decimal(0),
            latency_ms=usage.latency_ms if usage is not None else elapsed_ms,
            ok=error is None,
            error=error,
            generation_id=generation_id,
        )

    async def _record(self, call: LLMCall) -> None:
        """Append to the ledger, and **never change the outcome of the caller
        by doing so.**

        The reachable failure is a `cost_usd` the column cannot hold, which
        `PostgresLLMCallRepository` translates to `RepositoryConflict` behind a
        SAVEPOINT so the caller keeps a usable session. Swallowed rather than
        raised for three reasons that point the same way: the completion is
        already paid for, the cause is a configured price rather than anything
        a retry changes, and raising here would either cost the household the
        screen it just earned (or the viewer a search over a bookkeeping
        error) or replace the upstream failure `JobWorker` needs to classify
        with a repository error it would classify differently.

        `UsherPortError` and not `Exception`: a `ValidationError` from
        `LLMCall` or a `TypeError` in this module is a bug, and a bug in a
        service is not an upstream failure.
        """
        try:
            await self._ledger.record(call)
        except UsherPortError as exc:
            # `generation` is omitted rather than rendered `None` on the
            # expansion path: a join key that does not exist reads as a lost
            # one in an operator's grep.
            generation = (
                f" for generation {call.generation_id}" if call.generation_id is not None else ""
            )
            logger.error(
                "the cost ledger refused a {purpose} row{generation}; "
                "spend for this call is unrecorded: {error}",
                purpose=call.purpose.value,
                generation=generation,
                error=str(exc) or type(exc).__name__,
            )


def _ms(seconds: float) -> int:
    """`latency_ms`, which is `ge=0` on the model and `>= 0` in the column."""
    return max(0, int(seconds * 1000))


__all__ = ["LLMLedger"]
