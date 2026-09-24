"""The `llm_calls` ledger: record every attempted completion, and commit it."""

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

    `model` is the string the client was built with and is not defaulted. It is
    the only honest value for `llm_calls.model` on the path where no response
    came back to read one from; a default here would be a second value that
    silently disagrees with `Settings.llm_model`.

    `commit` is a callable and not a session: `services/` may depend only on
    `domain/` and `ports/`, and a session is neither. It matters most on the
    query-expansion path, which writes nothing else -- an uncommitted ledger row
    is rolled back when the read's session closes, and the money is spent with
    no record at all.

    `clock` is injected because the latency of a *failed* call is the one number
    this ledger cannot get from an `LLMUsage` that never came back.
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
        """Close out one attempted completion: write its `llm_calls` row, then commit.

        The clock is read here, so `elapsed_ms` is a delta from `started` on
        every path and no caller can hand over an absolute reading. It is the
        *fallback* latency -- `_row` prefers the adapter's own reading whenever
        an `LLMUsage` came back -- and the path with no usage is the one it
        exists for, where a timeout has no other record.

        Not the commit boundary for `curated_rows`: `CurationService` calls
        `replace_for_user` *before* this, so one commit covers both writes and
        PRD 10's `llm_calls JOIN curated_rows USING (generation_id)` never sees
        a screen with no cost attributed to it.
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

        `ok` is derived from `error` rather than passed beside it. The two must
        agree -- `LLMCall._ok_and_error_must_agree` and
        `ck_llm_calls_ok_error_agree` both refuse a disagreement -- so a
        signature taking both would be one that can be handed a contradiction,
        on the path least able to afford a `ValidationError`.

        `usage is None` is the upstream-failure path and nothing else: there is
        no answer to bill, so the tokens and cost are zero, the model is the one
        this deployment asked for, and the latency is this ledger's own reading.
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
        """Append to the ledger, never changing the caller's outcome by doing so.

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
