"""One generation: assemble, call once, validate, replace, and record.

**Everything expensive in this module is arranged around one fact -- the call
costs money and the screen does not.** So a failure here is non-fatal to the
*screen* (PRD 08's degradation table: *"previous curated rows persist"*) and
fatal to the *job* (`JobWorker` classifies the exception, and a handler that
absorbed it would complete the job and lose the work silently). The two
promises are kept by the same shape: `replace_for_user` is reached on exactly
one path, and every other path raises after writing a ledger row.

## The ledger is written on every path that attempted a completion

`llm_calls` is one row per *attempt*, `ok` is the discriminator, and
**`ok` is not "the HTTP call returned 200"**: a call that answered perfectly
and validated to zero rows is `ok = false` with a reason
([ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md)'s
rule 3, which exists because that run happened). A ledger holding only the
successes understates spend by exactly the failures, which are the rows an
operator most wants to see.

Two traps on that path, both of which lose the row the ledger exists for:

- **The model is constructed inside an `except` handler.** `str(exc)` is `""`
  for an exception raised with no arguments and `LLMCall` refuses a failed call
  with a blank error, so this module owes `str(exc) or type(exc).__name__` --
  never a bare `str(exc)`. `LLMCallRepository.record`'s docstring names the
  same obligation from the other side.
- **`record()` itself can fail.** `cost_usd` is `NUMERIC(12, 8)` with no
  ceiling on the domain model, so a per-token price entered into a per-Mtok
  field is a `RepositoryConflict` from a *validly constructed* `LLMCall`. That
  failure never changes the outcome of the generation: on the success path the
  household keeps the screen it just earned, and on a failure path the upstream
  error still propagates rather than being replaced by a repository one that
  `JobWorker` would classify differently. It is logged loudly and swallowed,
  because the money is spent either way, the cause is a misconfigured price
  rather than anything a retry fixes, and raising would buy a second completion
  to write the same unwritable row -- five times, on the queue's backoff.

**The one path that records nothing is the one that attempted nothing.** An
empty candidate pool raises before the client is touched, so the rule this
module implements is *record on every path that **attempted** a call* --
narrower than "every path", and wider than "every path that got an answer",
because the upstream-failure path got no answer either and writes its row all
the same.

It is **not** an argument from `LLMCall.model`'s `min_length=1`. That field has
a perfectly honest value for a call nobody made -- `self._model`, the model
this deployment asked for, which is exactly what the upstream-failure path
writes for a call that also completed nothing. What excludes the empty pool is
that it is not an event of the LLM subsystem at all: an empty catalog is an
operator's problem, no completion was attempted, none was billed, and a row
saying otherwise is spend an operator has to explain away.

## Two failures, two exception types, and the choice is `JobWorker`'s policy

`PortDataMalformed` parks immediately; every other `UsherPortError` backs off
and parks at the attempt ceiling. So an upstream failure is **re-raised
unchanged** -- a 429 has a `retry_after` and a truncated completion is already
malformed -- and a generation that validated to nothing raises
`PortDataMalformed`, because the three things that produce it (a prompt the
model cannot follow, a schema key that moved, a pool that cannot answer the
question) are permanent properties of this request and five more completions
reach the same answer at five times the price. Parking is also the only thing
that makes rule 3 *visible*: the failure it names is one whose screen looks
deliberate.

## What goes on the wire, and what may not

The prompt carries the household's watch history and 200 candidate titles, so
it is the single most sensitive body this project sends anywhere. Three
consequences:

- **No identifier is rendered into it.** Candidates are addressed by a 1-based
  integer index and the map back is held here, which is ADR-0028's rule 1 and
  the reason a hallucinated handle is *unrepresentable* rather than merely
  rejected. Measured: a UUID handle costs 3.1x the prompt tokens, 3.0x the
  output tokens and 3.2x the latency, and is the least accurate of the three
  spellings.
- **Nothing the model wrote is echoed** into an exception message, a log line
  or a span attribute -- PRD 08's "a rejected request never echoes the body it
  rejected", where the body is a completion written over a household's viewing.
  `CurationRejected.error` is already numbers and label names only, which is
  why it is safe to carry into both `llm_calls.error` and the raise.
- **The prompt is never a span attribute.** `HTTPXClientInstrumentor` already
  records URLs; a prompt on a span would put a household's viewing history in
  whatever collects traces.

## Three modules, and the two that are pure

This one orchestrates: it reads ports, spends money, writes rows, records
spend and raises. Everything either side of the call is a pure function
elsewhere -- `curation_prompt` builds the body, `curation_validate` reads the
answer -- and neither takes a port, a clock or a session.

That is a testability boundary rather than a tidiness one. Both are
*artefacts*: a prompt whose only real consumer is a language model, and a
tally whose only real consumer is a dashboard. `.claude/rules/testing-
discipline.md` records what happens to an artefact reachable only through an
orchestrator -- a sweep that walked this module's control flow was blind to
sixteen live prompt mutants, because nothing observes a prompt unless a case
opts in by name, and opting in cost a household, four fakes, a pool service, a
taste service and a scripted client per substring. Given a module, the artefact
has a consumer inside the process.

`min_cards` is the one number that crosses all three and may not differ
between them -- a prompt asking for four cards under a validator demanding five
drops every row and reports `row_too_short` -- so it is one parameter, threaded
from here into `build_prompt`, `_schema` and `validate_curation`.
"""

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from opentelemetry import metrics, trace
from opentelemetry.trace import Span
from pydantic import AwareDatetime

from usher.domain.curation import CuratedRow, LLMCall, LLMPurpose
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

_tracer = trace.get_tracer("usher.curation")
_meter = metrics.get_meter("usher.curation")

# **Boundary call 7: there is no `usher.llm.*` metric.** PRD 10 puts spend on
# the datasource that can answer it exactly -- *"LLM spend ... the catalog *is*
# the record"* -- and `llm_calls` is that record. These two are not about
# money: they answer the question no `llm_calls` row can, which is whether the
# validator is eating the output. A call that returned 200 and produced nothing
# usable looks like a healthy call from the wire's side, and the 108/108 run is
# what that looks like in production.
#
# **Counters rather than histograms, and the pair is the point.** One
# generation per household per night is far too sparse a population for a
# distribution to say anything, and what an operator reads is the *ratio* --
# dropped against kept, which two counters give and two histograms do not.
# "How many rows did this generation produce" is a span attribute, where it is
# attached to the generation that produced it.
_rows_kept = _meter.create_counter(
    "usher.curation.rows", unit="1", description="Curated rows kept by validation"
)
# Labelled `reason`, whose vocabulary is closed (`DropReason`) precisely so
# this stays a usable dimension: `not_in_pool` and `unparseable` produce the
# identical empty screen and have opposite fixes. **Every reason is recorded
# every time, zeros included** -- a reason absent from the export is
# indistinguishable from a reason nobody counts, which is the validator's own
# subject one level up. Two of the five count rows and three count cards, so
# summing across the label is meaningless; the `row_` prefix says so.
_rows_dropped = _meter.create_counter(
    "usher.curation.dropped", unit="1", description="Curated rows and cards dropped, by reason"
)

#: How many finished titles the prompt describes. **Deliberately not
#: `WatchStateRepository.list_recent`'s own default of 20**, for two reasons
#: that point the same way: a constant equal to a default is a constant no
#: case can prove is read, and the bound belongs to the *prompt* rather than to
#: a persistence port -- nothing else knows what a prompt costs. Half of
#: `TasteService._WINDOW`'s 50, which is a centroid's window: an average is
#: happy to have a long tail, and a list somebody reads is not.
#:
#: **Stays here rather than in `curation_prompt`**, unlike the row budget and
#: the heading width: it is the `limit` of a port read, and the read is what
#: this layer owns. `curation_prompt` never sees it -- it renders whatever
#: history it is handed.
#:
#: **What it costs, measured 2026-08-07 against the shipped prompt:** three
#: history lines add **55 prompt tokens** over a cold start (4,304 -> 4,359 at
#: pool 200), so a line is ~18 tokens and a household that has actually
#: finished 25 films pays **~460**. That is ~10% on top of a 200-candidate
#: pool's ~4,080 and it is the number to spend first if a prompt ever has to
#: shrink -- a candidate costs ~20.4 and buys a title the model may recommend,
#: a history line costs ~18 and buys context it may not use. One model, one
#: tokenizer, one evening (`gemma-4-26b-a4b`); what transfers is the ratio,
#: not the tokens.
HISTORY_SIZE = 25


@dataclass(frozen=True, slots=True)
class CurationReport:
    """What one successful generation did, for the caller that has to say so.

    **There is no failure arm and no empty `rows`**, which is
    `CurationKept`/`CurationRejected`'s asymmetry arriving one layer up: a
    generation that produced nothing raises, so a caller cannot iterate an
    empty success by accident.

    Everything `usher curate` prints is here rather than re-derived: the pool
    it was chosen from, the rows kept, the drops **by reason**, and the usage.
    A CLI that recomputed the pool size would be computing a second pool, and
    one that summed `dropped` from the rows it was handed could not see the
    rows that are missing.

    `dropped` is `CurationKept.dropped` unchanged, which `_tally` already
    hands over read-only -- `frozen=True` does not reach through a `Mapping`
    field, so the validator wraps it rather than leaving a caller able to edit
    the only record of what a generation lost. Not re-wrapped here: this is
    the one construction site, so a second proxy would copy an already-frozen
    map and be a second place to keep the promise.
    """

    generation_id: uuid.UUID
    pool_size: int
    rows: tuple[CuratedRow, ...]
    dropped: Mapping[DropReason, int]
    usage: LLMUsage


class CurationService:
    """One completion per household per run, and the only writer of
    `curated_rows`.

    **The client is required, never `LLMClient | None`.** A deployment with
    `USHER_LLM_ENABLED=false` has no client, and `composition.llm_client`
    already answers `(None, no-op)` with a warning -- so the composition root
    does not build this service at all, exactly as `build_worker` registers
    `JobKind.INDEX` only when an embedder exists. Spelling the parameter
    non-optional makes "no client, no curation" a fact `mypy` enforces at the
    one place that can know it, instead of a `self._client is None` branch that
    is unreachable in `src/` and can only be reached in a test by constructing
    the state the composition root refuses to build.
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
        self._ledger = ledger
        # `services/` may depend only on `domain/` and `ports/` (ADR-0009) and
        # a session is neither. One commit per generation, after both writes:
        # PRD 10's dashboard 5 is `llm_calls JOIN curated_rows USING
        # (generation_id)`, so a commit between them is a window in which a
        # screen exists with no cost attributed to it.
        self._commit = commit
        # **The model this deployment *asked* for**, which is the only honest
        # value for `llm_calls.model` when no response came back to read one
        # from. `LLMUsage.model` -- what actually answered -- wins whenever
        # there is one. Required rather than defaulted: it is
        # `settings.llm_model`, the same string the client was built with, and
        # a default here would be a second value that silently disagrees.
        self._model = model
        self._min_cards = min_cards
        self._now = now
        # Injected for the reason `OpenAICompatibleClient` injects its own: the
        # latency of a *failed* call is the number this ledger cannot get from
        # an `LLMUsage` that was never returned, and a 120-second timeout is
        # the most expensive thing this service can do.
        self._clock = clock

    async def generate(self, user_id: uuid.UUID) -> CurationReport:
        """One generation for one household. Raises `UsherPortError`.

        Deliberately re-raises rather than absorbing, and the exception type is
        the whole of what `JobWorker` has to work with: `PortDataMalformed`
        parks immediately and everything else backs off. `except Exception`
        anywhere in here would be a blindfold -- a bug in this service is not
        an upstream failure, and the queue must not learn about one as though
        it were.
        """
        with _tracer.start_as_current_span("curation.generate") as span:
            # An internal identifier, exactly as `enrich.title` carries
            # `usher.title_id`. Nothing here carries a name, a heading or a
            # prompt.
            span.set_attribute("usher.user_id", str(user_id))
            generation_id = new_id()
            span.set_attribute("usher.curation.generation_id", str(generation_id))

            candidates = await self._pool.for_user(user_id)
            span.set_attribute("usher.curation.pool", len(candidates))
            if not candidates:
                # **Before the client, and therefore before the ledger.** A
                # completion bought for a household with nothing to recommend
                # is a charge with a guaranteed empty answer, and nothing was
                # attempted here for the ledger to hold a row about. Malformed
                # rather than unavailable: an empty catalog is an operator's
                # problem and does not improve on a backoff schedule.
                span.set_attribute("usher.failed", True)
                raise PortDataMalformed(
                    "the candidate pool is empty; there is nothing to curate",
                    detail=str(user_id),
                )
            # **1-based, and this map is the whole security boundary.** The
            # validator does no arithmetic on it, so which handles were sent is
            # a fact this service owns -- and `enumerate(..., start=1)` is the
            # single line that has to agree with the prompt's rendering.
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
                    # **Never a bare `str(exc)`.** It is `""` for an exception
                    # raised with no arguments, `LLMCall` refuses a failed call
                    # with a blank error, and the row lost would be the one
                    # this ledger exists for.
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
                # **The 108/108 case.** The call worked, the money is spent,
                # and the generation produced nothing -- which is the one place
                # in this milestone where those two are allowed to disagree.
                # `replace_for_user` is not reached, so last night's screen
                # stands (PRD 08), and the usage is recorded in full because a
                # failure with zeroed tokens is indistinguishable from a call
                # that never happened.
                span.set_attribute("usher.failed", True)
                await self._settle(generation_id, started, usage=usage, error=outcome.error)
                logger.warning(
                    "curation for {user} produced nothing usable: {error}",
                    user=user_id,
                    error=outcome.error,
                )
                raise PortDataMalformed(outcome.error)

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
        """The two reads behind the prompt's history, rendered by
        `curation_prompt.history_lines`.

        **The read is at this layer and the rendering is not**, which is the
        seam: this method is the only thing here that touches a port, and
        splitting it is what lets the numbering, `described` and `_engagement`
        be reached without seeding a household, four fakes, a pool service, a
        taste service and a scripted client to assert one substring.

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
        """Close out one attempted completion: write its `llm_calls` row, then
        commit.

        **One function because it is one rule.** *Record on every path that
        attempted a call, and commit what you recorded* holds on all three of
        `generate`'s exits, and it used to be spelled three times -- which made
        the rule a convention rather than a structure, and made deleting one of
        the three commits invisible. It is the *rejected* arm that cannot
        afford that: the call worked, the money is spent, `replace_for_user`
        is never reached, and this row is the only record the spend happened at
        all -- so an uncommitted one is rolled back by `JobWorker`'s own
        failed-job transaction and the ledger loses exactly the failure
        [ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md)'s
        rule 3 exists to make visible. Same `_row` -> `_row` + `_cards` split
        `curation_validate` made, for the same reason.

        **The clock is read here**, so `elapsed_ms` is a delta from `started`
        on every path and no caller can hand over an absolute reading. It is
        the *fallback* latency -- `_ledger_row` prefers whatever the adapter
        measured whenever an `LLMUsage` came back -- and the path with no usage
        is the one it exists for, where a 120-second timeout has no other
        record.

        Not the commit boundary for `curated_rows`: the success path calls
        `replace_for_user` **before** this, so one commit covers both writes
        and PRD 10's `llm_calls JOIN curated_rows USING (generation_id)` never
        sees a screen with no cost attributed to it.
        """
        await self._record(
            self._ledger_row(
                generation_id,
                usage=usage,
                elapsed_ms=_ms(self._clock() - started),
                error=error,
            )
        )
        await self._commit()

    def _ledger_row(
        self,
        generation_id: uuid.UUID,
        *,
        usage: LLMUsage | None,
        elapsed_ms: int,
        error: str | None,
    ) -> LLMCall:
        """One `llm_calls` row.

        **`ok` is derived from `error` rather than passed beside it.** The two
        must agree -- `LLMCall._ok_and_error_must_agree` and
        `ck_llm_calls_ok_error_agree` both refuse a disagreement -- so a
        signature taking both would be one that can be handed a contradiction,
        on the path least able to afford a `ValidationError`.

        `usage is None` is the upstream-failure path and nothing else: there is
        no answer to bill, so the tokens are zero, the cost is zero, the model
        is the one this deployment asked for, and the latency is what this
        service measured, which for a timeout is the whole of it.
        """
        return LLMCall(
            id=new_id(),
            at=self._now(),
            model=usage.model if usage is not None else self._model,
            purpose=LLMPurpose.CURATION,
            tokens_in=usage.tokens_in if usage is not None else 0,
            tokens_out=usage.tokens_out if usage is not None else 0,
            cost_usd=usage.cost_usd if usage is not None else Decimal(0),
            latency_ms=usage.latency_ms if usage is not None else elapsed_ms,
            ok=error is None,
            error=error,
            generation_id=generation_id,
        )

    async def _record(self, call: LLMCall) -> None:
        """Append to the ledger, and **never change the outcome of the
        generation by doing so.**

        The reachable failure is a `cost_usd` the column cannot hold, which
        `PostgresLLMCallRepository` translates to `RepositoryConflict` behind a
        SAVEPOINT so the caller keeps a usable session. Swallowed rather than
        raised for three reasons that point the same way: the completion is
        already paid for, the cause is a configured price rather than anything
        a retry changes, and raising here would either cost the household the
        screen it just earned or replace the upstream failure `JobWorker` needs
        to classify with a repository error it would classify differently.

        `UsherPortError` and not `Exception`: a `ValidationError` from
        `LLMCall` or a `TypeError` in this module is a bug, and a bug in a
        service is not an upstream failure.
        """
        try:
            await self._ledger.record(call)
        except UsherPortError as exc:
            logger.error(
                "the cost ledger refused a {purpose} row for generation {generation}; "
                "spend for this call is unrecorded: {error}",
                purpose=call.purpose.value,
                generation=call.generation_id,
                error=str(exc) or type(exc).__name__,
            )

    # ----------------------------------------------------------- telemetry

    def _measure(self, span: Span, outcome: CurationOutcome) -> None:
        """The two metrics and the span's counts, on both arms of the union.

        Recorded for a *rejected* generation too, and that is the point: the
        run this pair exists for is the one that dropped everything, and a
        service that measured only successes would leave the panel empty
        exactly when an operator goes looking.
        """
        kept = len(outcome.rows) if isinstance(outcome, CurationKept) else 0
        _rows_kept.add(kept)
        span.set_attribute("usher.curation.rows", kept)
        total = 0
        for reason, count in outcome.dropped.items():
            # Zeros included. `add(0)` creates the series, and a reason absent
            # from the export is indistinguishable from a reason nobody counts.
            _rows_dropped.add(count, {"reason": reason.value})
            span.set_attribute(f"usher.curation.dropped.{reason.value}", count)
            total += count
        span.set_attribute("usher.curation.dropped", total)


def _schema(pool_size: int, *, min_cards: int) -> dict[str, Any]:
    """The `json_schema` sent with the request.

    **An optimisation, never the contract.** `response_format: {"type":
    "json_schema", ..., "strict": true}` is a guarantee about *shape* from one
    provider version; every failure ADR-0028 is about is one of *denotation*.
    So the bound below is stated twice on purpose -- here, where a provider
    that honours it makes an out-of-pool handle harder to emit, and in
    `validate_curation`, where it is checked whatever the provider did.

    **`item_ids` items are `integer`, and that is what makes
    `validate_curation`'s coercion the primary path rather than a fallback.**
    Corrected 2026-08-07. `curation_validate` keys its map on `str(index)`, so
    a provider honouring `strict: true` hands back JSON `int`s and `_handle`'s
    `int` branch runs on **100% of cards on every generation** -- not only on
    the `json_object` arm ADR-0028's 108/108 run measured. Verified live over
    405 identifiers in 20 generations: all `int`, none out of pool. Deleting
    the coercion drops every card of every generation against this schema.
    The types are deliberately *not* aligned by asking for strings instead:
    that moves the coercion rather than removing it, gives up `minimum` /
    `maximum` -- which guided decoding was measured to enforce -- and asks a
    model to quote a number.

    ✅ **`strict: true` was measured to hold the *numeric* bound, not only the
    shape** (2026-08-07): with `maximum: 5` against a prompt begging for 1-200,
    **zero** integers above 5 appeared in 2,048 output tokens. What the model
    did instead is the argument for the `description` hint below -- it looped
    `1,2,3,4,3,1,2,3,4...` to the ceiling, `finish_reason == "length"`, and the
    adapter's truncation guard refused it. An unsatisfiable *value* bound
    produces a degenerate loop, so a bound stated here must be one the pool can
    actually satisfy.

    Written against the validator's own four exported key constants: a schema
    saying `ids` and a reader saying `item_ids` is a generation that drops 100%
    of a correct answer.

    `additionalProperties: false` and a `required` naming every property,
    because that is what `strict: true` demands -- an optional `reason` would
    make the whole schema non-strict, and the validator already treats a blank
    one as a row with no subtitle.

    **No `enum` of the pool**, which is the tempting version: 200 inlined
    members in every request, honoured by a subset of providers, and it would
    make the validator look redundant on the deployment it was tested against
    while being the only defence on every other. ADR-0028 refuses it by name.
    """
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
                            # A *hint*, not a floor: `minItems` under guided
                            # decoding forces a model with fewer good answers
                            # to pad rather than to narrow, and the measured
                            # behaviour of the hostile pool was that it
                            # narrowed. The floor is `min_cards`, enforced
                            # where a short row can be discarded whole.
                            #
                            # ✅ **Vindicated live, 2026-08-07, by the failure
                            # of its opposite.** An unsatisfiable *value*
                            # bound in this same subschema made the model loop
                            # to the token ceiling. With the floor left as
                            # this description, the two starved arms -- pool 8
                            # and pool 5 -- **narrowed** instead, returning
                            # rows of 2-3 cards that `row_too_short`
                            # discarded whole. Narrowing is legible and
                            # counted; a loop is a full-price non-answer that
                            # only the truncation guard catches.
                            "description": (
                                f"candidate numbers, at least {min_cards} of them, none repeated"
                            ),
                        },
                    },
                },
            }
        },
    }


def _ms(seconds: float) -> int:
    """`latency_ms`, which is `ge=0` on the model and `>= 0` in the column."""
    return max(0, int(seconds * 1000))


__all__ = [
    "HISTORY_SIZE",
    "CurationReport",
    "CurationService",
]
