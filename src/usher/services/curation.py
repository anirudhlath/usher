"""One generation: assemble, call once, validate, replace, and record."""

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.trace import Span
from pydantic import AwareDatetime

from usher.domain.curation import CuratedRow, LLMPurpose
from usher.domain.ids import new_id
from usher.ports.errors import PortDataMalformed, UsherPortError
from usher.ports.llm import LLMClient, LLMUsage
from usher.ports.repository import (
    CuratedRowRepository,
    LLMCallRepository,
    TitleRepository,
    WatchStateRepository,
)
from usher.services.curation_pool import CandidatePoolService
from usher.services.curation_prompt import build_prompt, history_lines
from usher.services.curation_validate import (
    DEFAULT_MIN_CARDS,
    ITEM_IDS_KEY,
    REASON_KEY,
    ROWS_KEY,
    TITLE_KEY,
    CurationKept,
    CurationOutcome,
    CurationRejected,
    DropReason,
    validate_curation,
)
from usher.services.llm_ledger import LLMLedger

_tracer = trace.get_tracer("usher.curation")
_meter = metrics.get_meter("usher.curation")

# There is no `usher.llm.*` metric: PRD 10 puts spend on the datasource that
# can answer it exactly.
_rows_kept = _meter.create_counter(
    "usher.curation.rows", unit="1", description="Curated rows kept by validation"
)
# Labelled `reason`, whose vocabulary is closed (`DropReason`) precisely so this stays a
# usable dimension: `not_in_pool` and `unparseable` produce the identical empty screen
# and have opposite fixes.
_rows_dropped = _meter.create_counter(
    "usher.curation.dropped", unit="1", description="Curated rows and cards dropped, by reason"
)

#: What tells a row-unit `DropReason` from a card-unit one, and the reason
#: `_measure` publishes two roll-ups rather than one. Derived from the label
#: rather than held as a second list, so a sixth reason cannot be added to the
#: vocabulary and forgotten here.
_ROW_UNIT_PREFIX = "row_"

#: How many finished titles the prompt describes.
HISTORY_SIZE = 25


@dataclass(frozen=True, slots=True)
class CurationReport:
    """What one successful generation did, for the caller that has to say so.

    There is no failure arm and no empty `rows`: a generation that produced
    nothing raises, so a caller cannot iterate an empty success by accident.

    Everything `usher curate` prints is here rather than re-derived -- a CLI
    that recomputed the pool size would be computing a second pool, and one
    that summed `dropped` from the rows it was handed could not see the rows
    that are missing.

    `dropped` is `CurationKept.dropped` unchanged, already read-only. This is
    the one construction site, so re-wrapping would only be a second place to
    keep the same promise.
    """

    generation_id: uuid.UUID
    pool_size: int
    rows: tuple[CuratedRow, ...]
    dropped: Mapping[DropReason, int]
    usage: LLMUsage


class CurationService:
    """One completion per household per run, and the only writer of `curated_rows`.

    The client is required, never `LLMClient | None`. A deployment with
    `USHER_LLM_ENABLED=false` has no client and the composition root does not
    build this service at all, so a non-optional parameter makes "no client, no
    curation" a fact `mypy` enforces at the one place that can know it, rather
    than a `self._client is None` branch unreachable in `src/`.
    """

    def __init__(
        self,
        *,
        pool: CandidatePoolService,
        watch_states: WatchStateRepository,
        titles: TitleRepository,
        client: LLMClient,
        rows: CuratedRowRepository,
        ledger: LLMCallRepository,
        commit: Callable[[], Awaitable[None]],
        model: str,
        min_cards: int = DEFAULT_MIN_CARDS,
        now: Callable[[], AwareDatetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        # The other half of PRD 06 step 1's *"assemble context"*. The pool is
        # what the model may choose *from*; this is what it chooses *for*, and
        # a prompt without it produces shelves about the catalog rather than
        # about the household.
        self._watch_states = watch_states
        self._titles = titles
        self._client = client
        self._rows = rows
        # The ledger rule lives in `services/llm_ledger.py`, not here. Spelled out
        # at each call site it was a convention rather than a structure, and
        # dropping one of its commits was invisible.
        self._spend = LLMLedger(
            ledger=ledger,
            commit=commit,
            model=model,
            purpose=LLMPurpose.CURATION,
            now=now,
            clock=clock,
        )
        self._min_cards = min_cards
        self._now = now
        self._clock = clock

    async def generate(self, user_id: uuid.UUID) -> CurationReport:
        """One generation for one household.

        Raises `UsherPortError`. Deliberately re-raises rather than absorbing,
        because the exception type is the whole of what `JobWorker` has to work
        with: `PortDataMalformed` parks immediately and everything else backs
        off. `except Exception` here would be a blindfold -- a bug in this
        service is not an upstream failure, and the queue must not learn about
        one as though it were.
        """
        with _tracer.start_as_current_span("curation.generate") as span:
            # An internal identifier only: no name, no heading, no prompt.
            span.set_attribute("usher.user_id", str(user_id))
            generation_id = new_id()
            span.set_attribute("usher.curation.generation_id", str(generation_id))

            candidates = await self._pool.for_user(user_id)
            span.set_attribute("usher.curation.pool", len(candidates))
            if len(candidates) < self._min_cards:
                # Before the client, and therefore before the ledger: a completion
                # bought for a household with nothing to recommend is a charge with
                # a guaranteed empty answer, and nothing was attempted for the
                # ledger to hold a row about.
                span.set_attribute("usher.failed", True)
                raise PortDataMalformed(_nothing_to_curate(len(candidates), self._min_cards))
            # 1-based, and this map is the whole security boundary. The validator
            # does no arithmetic on it, so which handles were sent is a fact this
            # service owns, and the `start=1` is the single thing that has to
            # agree with the prompt's rendering.
            handles = {index: title.id for index, title in enumerate(candidates, start=1)}

            prompt = build_prompt(
                candidates, await self._history(user_id), min_cards=self._min_cards
            )
            schema = _schema(len(candidates), min_cards=self._min_cards)

            started = self._clock()
            try:
                payload, usage = await self._client.complete_json(
                    prompt, schema, purpose=LLMPurpose.CURATION
                )
            except UsherPortError as exc:
                span.set_attribute("usher.failed", True)
                await self._settle(
                    generation_id,
                    started,
                    usage=None,
                    # Never a bare `str(exc)`: it is `""` for an exception raised
                    # with no arguments, `LLMCall` refuses a failed call with a
                    # blank error, and the row lost would be the one this ledger
                    # exists for.
                    error=str(exc) or type(exc).__name__,
                )
                logger.warning(
                    "curation for {user} could not reach the model: {error}",
                    user=user_id,
                    error=str(exc) or type(exc).__name__,
                )
                raise

            outcome = validate_curation(
                payload,
                handles=handles,
                user_id=user_id,
                generation_id=generation_id,
                # What answered, not what was asked. PRD 10 groups spend by
                # model and `curated_rows.model_name` is how "these rows were
                # written by a model we no longer run" stays a query.
                model_name=usage.model,
                generated_at=self._now(),
                min_cards=self._min_cards,
            )
            self._measure(span, outcome)

            if isinstance(outcome, CurationRejected):
                # The call worked, the money is spent, and the generation produced
                # nothing -- the one place those two are allowed to disagree.
                span.set_attribute("usher.failed", True)
                await self._settle(generation_id, started, usage=usage, error=outcome.error)
                logger.warning(
                    "curation for {user} produced nothing usable: {error}",
                    user=user_id,
                    error=outcome.error,
                )
                raise PortDataMalformed(outcome.error)

            # `replace_for_user` runs *before* `_settle` so that one commit covers
            # both writes and PRD 10's `llm_calls JOIN curated_rows USING
            # (generation_id)` never sees a screen with no cost attributed to it.
            await self._rows.replace_for_user(user_id, outcome.rows)
            await self._settle(generation_id, started, usage=usage, error=None)
            return CurationReport(
                generation_id=generation_id,
                pool_size=len(candidates),
                rows=outcome.rows,
                dropped=outcome.dropped,
                usage=usage,
            )

    # ------------------------------------------------------------- assemble

    async def _history(self, user_id: uuid.UUID) -> list[str]:
        """The two reads behind the prompt's history.

        The read is at this layer and the rendering is not: this method is the
        only thing here that touches a port, and splitting it is what lets the
        numbering, `described` and `_engagement` be reached without standing up
        the whole service graph to assert one substring.

        Two reads rather than one join, because `list_recent` answers in
        recency order and `list_by_ids` is one `IN (...)` promising no order at
        all -- so the order the prompt claims is `recent`'s, restored by
        `history_lines` walking `recent` and using the catalog only as a
        lookup.
        """
        recent = await self._watch_states.list_recent(user_id, limit=HISTORY_SIZE)
        if not recent:
            # No second read for a household that has finished nothing. A cold
            # start is the normal state, not an edge case, and it is
            # `_COLD_START`'s branch of the prompt rather than an empty one.
            return []
        catalog = {
            title.id: title
            for title in await self._titles.list_by_ids([entry.title_id for entry in recent])
        }
        return history_lines(recent, catalog)

    # -------------------------------------------------------------- ledger

    async def _settle(
        self,
        generation_id: uuid.UUID,
        started: float,
        *,
        usage: LLMUsage | None,
        error: str | None,
    ) -> None:
        """Close out one attempted completion, through the one ledger."""
        await self._spend.settle(started, usage=usage, error=error, generation_id=generation_id)

    # ----------------------------------------------------------- telemetry

    def _measure(self, span: Span, outcome: CurationOutcome) -> None:
        """The two metrics and the span's counts, on both arms of the union.

        Recorded for a *rejected* generation too, and that is the point: the
        run this pair exists for is the one that dropped everything, and a
        service that reported only successes would leave the panel empty
        exactly when an operator goes looking.

        Two roll-ups, not one. Two of the five `DropReason` members count rows
        and three count cards, so summing across the whole label is
        meaningless: three cards lost from a kept row plus two rows entire is
        neither five cards nor five rows. The split is derived from the `row_`
        prefix rather than from a second list, so a sixth reason cannot be
        added to one and forgotten in the other.
        """
        kept = len(outcome.rows) if isinstance(outcome, CurationKept) else 0
        _rows_kept.add(kept)
        span.set_attribute("usher.curation.rows", kept)
        rows_lost = 0
        cards_lost = 0
        for reason, count in outcome.dropped.items():
            # Zeros included. `add(0)` creates the series, and a reason absent
            # from the export is indistinguishable from a reason nobody counts.
            _rows_dropped.add(count, {"reason": reason.value})
            span.set_attribute(f"usher.curation.dropped.{reason.value}", count)
            if reason.value.startswith(_ROW_UNIT_PREFIX):
                rows_lost += count
            else:
                cards_lost += count
        span.set_attribute("usher.curation.dropped_rows", rows_lost)
        span.set_attribute("usher.curation.dropped_cards", cards_lost)


def _nothing_to_curate(found: int, min_cards: int) -> str:
    """What `generate` raises when the pool cannot fill one row.

    Two sentences from one guard, because they are two diagnoses. A pool of
    zero is *"there is nothing here"*, most often a deployment that has not
    finished a sync or a bootstrap. A pool of four under a floor of five is
    *"there is not enough here"*, and the operator's next question is how much
    is missing, so that arm carries the two numbers.

    No id, no credential, no host: `usher curate` renders this as its entire
    message, and a household id is one an operator has no way to look up on a
    deployment that has exactly one. `found` and `min_cards` are counts of
    things, and neither is looked up anywhere.
    """
    if not found:
        return "the candidate pool is empty; there is nothing to curate"
    return (
        f"the candidate pool holds {found} candidates and a row needs at least "
        f"{min_cards}; there is nothing to curate"
    )


def _schema(pool_size: int, *, min_cards: int) -> dict[str, Any]:
    """The `json_schema` sent with the request."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [ROWS_KEY],
        "properties": {
            ROWS_KEY: {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [TITLE_KEY, REASON_KEY, ITEM_IDS_KEY],
                    "properties": {
                        TITLE_KEY: {"type": "string"},
                        REASON_KEY: {"type": "string"},
                        ITEM_IDS_KEY: {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1, "maximum": pool_size},
                            # A hint, not a floor: `minItems` under guided decoding
                            # forces a model with fewer good answers to pad rather
                            # than to narrow.
                            "description": (
                                f"candidate numbers, at least {min_cards} of them, none repeated"
                            ),
                        },
                    },
                },
            }
        },
    }


__all__ = [
    "HISTORY_SIZE",
    "CurationReport",
    "CurationService",
]
