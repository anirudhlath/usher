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

**The one path that records nothing is the one that attempted nothing.** A
candidate pool that cannot fill one row raises before the client is touched, so
the rule this module implements is *record on every path that **attempted** a
call* -- narrower than "every path", and wider than "every path that got an
answer", because the upstream-failure path got no answer either and writes its
row all the same.

It is **not** an argument from `LLMCall.model`'s `min_length=1`. That field has
a perfectly honest value for a call nobody made -- `self._model`, the model
this deployment asked for, which is exactly what the upstream-failure path
writes for a call that also completed nothing. What excludes this path is that
it is not an event of the LLM subsystem at all: a catalog with nothing to
recommend from is an operator's problem, no completion was attempted, none was
billed, and a row saying otherwise is spend an operator has to explain away.

**That guard was `not candidates` until 2026-08-11 and is now
`len(candidates) < min_cards`** (M9 Task G4), which is the same argument one
inequality wider: `curation_validate._row` discards a row of fewer than
`min_cards` *distinct* cards, so a pool below the floor is billed in full for
an answer that is guaranteed to validate to nothing. The raise site carries how
often that is reachable, which is the half worth reading before treating it as
a nightly saving.

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
from here into `build_prompt`, `_schema` and `validate_curation`. Proposed for
deletion in review 2026-08-10 and declined; the argument is recorded beside
`DEFAULT_MIN_CARDS`, which is the paragraph that invites it.

**`_schema` is the one pure function still in this module, and that is a known
seam in the wrong place rather than an oversight.** It builds an artefact whose
only real consumer is a provider's guided decoder -- outside the process,
exactly like the prompt -- so by this module's own argument it belongs beside
`build_prompt`, where a case could reach it without a household, four fakes, a
pool service, a taste service and a scripted client. Reviewed 2026-08-10 and
**not moved**, for two measured reasons rather than for taste:

- **Moving the four wire-key constants with it is a circular import.**
  `curation_prompt` imports `MAX_REASON_CHARS` from `curation_validate`
  deliberately (a reason over it costs the whole row, so the writer takes the
  reader's bound rather than restating it), and `validate_curation` reads
  `ROWS_KEY`, `TITLE_KEY`, `REASON_KEY` and `ITEM_IDS_KEY` in its own body --
  so the two modules would import each other. The keys stay declared in the
  reader, which is the authority on what it reads, and the writer imports them.
- **`curation._schema` is named by module path in ADR-0028, in
  `.claude/rules/curation-and-llm.md`, in two `docs/plans/` files and in two
  test docstrings.** Moving the function without amending all six leaves the
  stale "verified" fact `prd-maintenance.md` calls worse than none, and the
  amendment is a wider commit than the seam is worth on a branch that is ready
  to merge. Recorded here so the next reader inherits the reasoning rather than
  the question.
"""

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

#: What tells a row-unit `DropReason` from a card-unit one, and the reason
#: `_measure` publishes two roll-ups rather than one. **Derived from the label
#: rather than held as a second list**, so a sixth reason cannot be added to
#: the vocabulary and forgotten here -- the comment above already says the
#: prefix is what carries the unit, and this is that sentence made executable.
_ROW_UNIT_PREFIX = "row_"

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
        # **The ledger rule lives in `services/llm_ledger.py`, not here.** It
        # used to be spelled three times in this class, which made it a
        # convention rather than a structure and made deleting one of the three
        # commits invisible; collapsing those three into a `_settle` fixed that
        # *within* this service and left the count at two across the codebase,
        # because `QueryExpansionService` carried a verbatim copy. `LLMLedger`
        # is the same argument applied once more, and
        # `tests/unit/test_services_llm_ledger.py` pins that neither service
        # mints a row of its own.
        #
        # `commit` is a callable and not a session because `services/` may
        # depend only on `domain/` and `ports/` (ADR-0009). One commit per
        # generation, after both writes: PRD 10's dashboard 5 is `llm_calls
        # JOIN curated_rows USING (generation_id)`, so a commit between them is
        # a window in which a screen exists with no cost attributed to it.
        #
        # `model` is the model this deployment *asked* for -- the only honest
        # value for `llm_calls.model` when no response came back to read one
        # from; `LLMUsage.model`, what actually answered, wins whenever there
        # is one. `clock` is reached only on the arm where there is no usage at
        # all, because the ledger prefers `usage.latency_ms` whenever a usage
        # came back, so the adapter's clock is what PRD 10 plots on every
        # *successful* generation and this one is the failed-call fallback.
        # Two different halves of one column, each with its own case
        # (`test_the_latency_is_the_whole_send_and_not_what_was_left_after_it`
        # for the adapter's).
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
            if len(candidates) < self._min_cards:
                # **Before the client, and therefore before the ledger.** A
                # completion bought for a household with nothing to recommend
                # is a charge with a guaranteed empty answer, and nothing was
                # attempted here for the ledger to hold a row about. Malformed
                # rather than unavailable: an empty catalog is an operator's
                # problem and does not improve on a backoff schedule.
                #
                # **The floor and not merely zero, widened 2026-08-11 (M9 Task
                # G4).** `curation_validate._row` discards a row carrying fewer
                # than `min_cards` *distinct* cards and `_cards` de-duplicates
                # by title id, so a pool below the floor cannot produce one
                # surviving row however good the completion is: every row is
                # `row_too_short`, the outcome is `CurationRejected`, and the
                # household is billed in full for a guaranteed-empty answer.
                # That is arithmetic rather than a judgement, which is why it
                # is a guard and not a setting -- and it is the same guard
                # rather than a second one because the empty pool is its `0`.
                #
                # **How often this can fire is worth knowing before reading it
                # as a nightly saving.** The pool is
                # `min(catalog_unwatched, USHER_CURATION_POOL_SIZE)` and
                # ownership is an `ORDER BY` key rather than a filter (M9 Task
                # G3, measured), so a *small library* does not make a small
                # pool: only a catalog whose entire unwatched set is below the
                # floor reaches here. Rare, not nightly. What it is worth is
                # the completion it does not buy on the run where it fires.
                #
                # **No `detail`, and it carried `str(user_id)` until
                # 2026-08-07.** `PortDataMalformed.detail` promises *"enough to
                # find the offending record"*, and an empty pool has no record
                # -- what it carried was a fourth copy of an id every reader of
                # this raise already holds. `Job.key` **is** `str(user_id)` for
                # `JobKind.CURATE` (`handlers._user_id` reads the argument back
                # off it), so a parked job row named the household twice, and
                # the span above carries `usher.user_id` either way. The one
                # reader it was not redundant for is `usher curate`, which
                # renders this raise as its entire message -- and `build_parser`
                # refuses a `--user` flag on the grounds that a household id is
                # *"an id an operator has no way to look up on a deployment
                # that has exactly one"*. So the id was the sentence's only
                # concrete token and the command's own stated reasoning says an
                # operator cannot read it. The widened arm's sentence obeys the
                # same rule and says the two things the empty one has no room
                # for: how many candidates were found, and what the floor is.
                span.set_attribute("usher.failed", True)
                raise PortDataMalformed(_nothing_to_curate(len(candidates), self._min_cards))
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

            # **The one exception to this module's "record on every path that
            # attempted a completion", named rather than left to be
            # discovered.** `replace_for_user` runs *before* `_settle` so that
            # one commit covers both writes and PRD 10's `llm_calls JOIN
            # curated_rows USING (generation_id)` never sees a screen with no
            # cost attributed to it -- and the price of that ordering is that a
            # rows-write failure here loses the ledger row for a completion
            # that was already billed.
            #
            # Left as-is deliberately, because the exception is close to
            # unreachable and reordering would not buy what it looks like it
            # buys. `curated_rows` carries no unique constraint, slugs are
            # positional (`curated-01`...) so two rows cannot collide, and every
            # CHECK on the table is already satisfied by `CuratedRow`'s own
            # validators -- so `RepositoryConflict` has no reachable cause from
            # a validly constructed row. What remains is a dropped connection
            # or a statement timeout, and those stop `_settle` committing too:
            # the ledger lives in the same database as the rows it is the
            # record of, which PRD 08 already declares a total outage.
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
        """Close out one attempted completion, through the one ledger.

        **One function because it is one rule.** *Record on every path that
        attempted a call, and commit what you recorded* holds on all three of
        `generate`'s exits. It used to be spelled three times here -- which
        made the rule a convention rather than a structure, and made deleting
        one of the three commits invisible -- and then twice across the
        codebase, because `QueryExpansionService` carried a verbatim copy. It
        is the *rejected* arm that cannot afford either: the call worked, the
        money is spent, `replace_for_user` is never reached, and that row is
        the only record the spend happened at all, so an uncommitted one is
        rolled back by `JobWorker`'s own failed-job transaction and the ledger
        loses exactly the failure
        [ADR-0028](../../../docs/prd/decisions/0028-the-pool-is-the-contract.md)'s
        rule 3 exists to make visible.

        Kept as a one-line method rather than inlined at the three call sites
        for the same reason it was collapsed in the first place: three spellings
        of `self._spend.settle(...)` is three chances for one to drift in its
        arguments, and `generation_id` is the argument that would drift.
        """
        await self._spend.settle(started, usage=usage, error=error, generation_id=generation_id)

    # ----------------------------------------------------------- telemetry

    def _measure(self, span: Span, outcome: CurationOutcome) -> None:
        """The two metrics and the span's counts, on both arms of the union.

        Recorded for a *rejected* generation too, and that is the point: the
        run this pair exists for is the one that dropped everything, and a
        service that measured only successes would leave the panel empty
        exactly when an operator goes looking.

        **Two roll-ups, not one, and that is the whole of this method's own
        rule.** Two of the five `DropReason` members count *rows* and three
        count *cards*; `curation_validate`'s module docstring, ADR-0028 and
        `_rows_dropped`'s own comment all say that summing across the whole
        label is meaningless -- and this method used to publish exactly that
        sum as `usher.curation.dropped`. A generation losing three cards out of
        a row it kept and two rows entire reported `5`, a number that is
        neither five cards nor five rows and answers no question an operator
        has. The split is derived from the `row_` prefix rather than from a
        second list, so a sixth reason cannot be added to one and forgotten in
        the other.
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
    """What `generate` raises when the pool cannot fill one row, and what
    `usher curate` prints as its whole message.

    **Two sentences from one guard, because they are two diagnoses.** A pool of
    zero is *"there is nothing here"* -- most often a deployment that has not
    finished a sync or a bootstrap -- and it keeps the sentence two cases and
    the comment at the raise site argue about, word for word. A pool of four
    under a floor of five is *"there is not enough here"*, and the operator's
    question is immediately how much is missing, so that arm carries the two
    numbers rather than the word `empty`.

    **No id, no credential, no host**, on the same reasoning that took
    `str(user_id)` off the empty-pool raise on 2026-08-07: `usher curate`
    renders this as its entire message and `build_parser` refuses a `--user`
    flag because a household id is *"an id an operator has no way to look up on
    a deployment that has exactly one"*. `found` and `min_cards` are both
    counts of things, and neither is looked up anywhere.
    """
    if not found:
        return "the candidate pool is empty; there is nothing to curate"
    return (
        f"the candidate pool holds {found} candidates and a row needs at least "
        f"{min_cards}; there is nothing to curate"
    )


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


__all__ = [
    "HISTORY_SIZE",
    "CurationReport",
    "CurationService",
]
