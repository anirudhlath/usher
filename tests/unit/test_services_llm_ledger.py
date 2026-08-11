"""`LLMLedger` -- the one home for *record on every path that attempted a call,
and commit what you recorded.*

**Why this module exists at all**, and it is this repository's own finding
applied one level up. `.claude/rules/testing-discipline.md` records that the
rule *"record **and** commit"* was spelled verbatim at three exits of
`CurationService.generate`, that deleting the commit from one of them passed
all 42 cases, and that the repair had to be structural as well as behavioural:
*"a rule spelled three times is a rule one deletion is invisible in ... N copies
means N chances for one to go quiet."*

That argument was then applied **inside** one service and not **across** the
two that spend money. `CurationService` and `QueryExpansionService` each
carried their own `_settle` / `_ledger_row` / `_record`, identical but for the
purpose constant and the generation id, so five invariants -- `ok` derived
rather than passed, `str(exc) or type(exc).__name__`, the `usage is None`
fallback, `except UsherPortError` and not `except Exception`, and
record-then-commit -- were each argued and pinned twice. This file is the one
place they are now pinned once, and `test_no_service_mints_its_own_ledger_row`
is what stops a third copy appearing.
"""

import ast
import datetime as dt
import inspect
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from usher.domain.curation import LLMCall, LLMPurpose
from usher.ports.errors import PortUnavailable
from usher.ports.llm import LLMUsage
from usher.services.llm_ledger import LLMLedger

NOW = dt.datetime(2026, 8, 10, 3, 0, tzinfo=dt.UTC)
ASKED = "configured/model-1"
ANSWERED = "served/model-2"

#: Not zero, for the reason this repository learned twice -- once in
#: `CurationService` and once in `OpenAICompatibleClient`. `time.monotonic`'s
#: epoch is arbitrary, so a fixture starting at `0.0` makes `clock() - started`
#: and a bare `clock()` the identical number, and an absolute reading is
#: invisible on the one field the injected clock exists to measure.
_T0 = 1_000.0
_ELAPSED = 1.5
_ELAPSED_MS = 1_500


class _RecordingLedger:
    """Appends, and refuses on demand the way `PostgresLLMCallRepository` does
    for a `cost_usd` the column cannot hold."""

    def __init__(self, events: list[str], *, refuse_with: Exception | None = None) -> None:
        self.events = events
        self.calls: list[LLMCall] = []
        self._refuse_with = refuse_with

    async def record(self, call: LLMCall) -> None:
        self.events.append("ledger")
        if self._refuse_with is not None:
            raise self._refuse_with
        self.calls.append(call)


def _usage(*, latency_ms: int = 42) -> LLMUsage:
    return LLMUsage(
        model=ANSWERED,
        tokens_in=1_200,
        tokens_out=340,
        cost_usd=Decimal("0.01658700"),
        latency_ms=latency_ms,
    )


def _ledger(
    *,
    purpose: LLMPurpose = LLMPurpose.CURATION,
    refuse_with: Exception | None = None,
    elapsed: float = _ELAPSED,
) -> tuple[LLMLedger, _RecordingLedger, list[str]]:
    events: list[str] = []
    repo = _RecordingLedger(events, refuse_with=refuse_with)

    async def commit() -> None:
        events.append("commit")

    # One reading, not two: `settle` takes `started` from its caller and reads
    # the clock exactly once, so the clock here is the *end* of the window. A
    # two-tick iterator would hand back the same pair whichever way round the
    # subtraction went, which is the fixture shape this repository has twice
    # recorded as unable to see an absolute reading.

    return (
        LLMLedger(
            ledger=repo,  # type: ignore[arg-type]
            commit=commit,
            model=ASKED,
            purpose=purpose,
            now=lambda: NOW,
            clock=lambda: _T0 + elapsed,
        ),
        repo,
        events,
    )


# --- the row ---------------------------------------------------------------


async def test_a_settled_success_derives_ok_from_the_absence_of_an_error() -> None:
    """`ok` is derived rather than passed, so the contradiction
    `LLMCall._ok_and_error_must_agree` and `ck_llm_calls_ok_error_agree` both
    refuse is unspellable on the path least able to afford a
    `ValidationError`."""
    ledger, repo, _events = _ledger()
    await ledger.settle(_T0, usage=_usage(), error=None)
    assert repo.calls[0].ok is True
    assert repo.calls[0].error is None


async def test_a_settled_failure_derives_ok_from_the_presence_of_an_error() -> None:
    ledger, repo, _events = _ledger()
    await ledger.settle(_T0, usage=None, error="the endpoint refused the connection")
    assert repo.calls[0].ok is False
    assert repo.calls[0].error == "the endpoint refused the connection"


async def test_an_upstream_failure_bills_zero_against_the_model_this_deployment_asked_for() -> None:
    """`usage is None` is the upstream-failure path and nothing else: there is
    no answer to bill, and `ANSWERED` is a string that never came back."""
    ledger, repo, _events = _ledger()
    await ledger.settle(_T0, usage=None, error="timed out")
    call = repo.calls[0]
    assert (call.tokens_in, call.tokens_out) == (0, 0)
    assert call.cost_usd == Decimal(0)
    assert call.model == ASKED


async def test_an_upstream_failure_records_the_latency_this_ledger_measured() -> None:
    """The path the injected clock exists for: a 120-second timeout has no
    `LLMUsage` to read a latency from, and it is the most expensive thing
    either service can do."""
    ledger, repo, _events = _ledger()
    await ledger.settle(_T0, usage=None, error="timed out")
    assert repo.calls[0].latency_ms == _ELAPSED_MS


async def test_a_completion_prefers_the_latency_the_adapter_measured() -> None:
    """Kills a ledger that always uses its own clock. The adapter's number is
    the send window; this service's is the send window *plus* validation, and
    PRD 10's latency panel plots the former on every ordinary night."""
    ledger, repo, _events = _ledger()
    await ledger.settle(_T0, usage=_usage(latency_ms=99), error=None)
    assert repo.calls[0].latency_ms == 99
    # The premise: the two numbers are genuinely different, so preferring the
    # wrong one is observable rather than accidentally right.
    assert _ELAPSED_MS != 99


async def test_a_negative_delta_clamps_rather_than_failing_the_model_bound() -> None:
    """`latency_ms` is `ge=0` on the model and `>= 0` in the column."""
    ledger, repo, _events = _ledger(elapsed=-5.0)
    await ledger.settle(_T0, usage=None, error="timed out")
    assert repo.calls[0].latency_ms == 0


async def test_the_purpose_is_the_one_this_ledger_was_built_for() -> None:
    ledger, repo, _events = _ledger(purpose=LLMPurpose.QUERY_EXPANSION)
    await ledger.settle(_T0, usage=_usage(), error=None)
    assert repo.calls[0].purpose is LLMPurpose.QUERY_EXPANSION


async def test_a_generation_id_is_none_unless_the_caller_names_one() -> None:
    """Query expansion produces no `curated_rows` at all, so an id minted for
    it would be a join key pointing at nothing -- and PRD 10's dashboard 5 is
    `llm_calls JOIN curated_rows USING (generation_id)`."""
    ledger, repo, _events = _ledger(purpose=LLMPurpose.QUERY_EXPANSION)
    await ledger.settle(_T0, usage=_usage(), error=None)
    assert repo.calls[0].generation_id is None

    generation = uuid.uuid4()
    ledger, repo, _events = _ledger()
    await ledger.settle(_T0, usage=_usage(), error=None, generation_id=generation)
    assert repo.calls[0].generation_id == generation


# --- the commit ------------------------------------------------------------


async def test_the_row_is_recorded_and_then_committed_in_that_order() -> None:
    """`events.count("ledger") == 1` is satisfied by a service that never
    commits, which is exactly how the deleted commit survived 42 cases."""
    ledger, _repo, events = _ledger()
    await ledger.settle(_T0, usage=_usage(), error=None)
    assert events == ["ledger", "commit"]


async def test_a_refused_ledger_row_never_changes_the_outcome() -> None:
    """The completion is already paid for and the cause is a configured price,
    not anything a retry fixes -- so raising here would either cost the
    household the screen it just earned or replace the upstream failure
    `JobWorker` needs to classify with a repository error it would classify
    differently."""
    ledger, _repo, events = _ledger(refuse_with=PortUnavailable("the ledger is unreachable"))
    await ledger.settle(_T0, usage=_usage(), error=None)
    assert events == ["ledger", "commit"]


async def test_a_bug_in_this_module_is_not_swallowed_as_an_upstream_failure() -> None:
    """`UsherPortError` and not `Exception`: a `TypeError` here is a bug, and a
    bug in a service is not an upstream failure."""
    ledger, _repo, _events = _ledger(refuse_with=TypeError("a bug, not an outage"))
    with pytest.raises(TypeError):
        await ledger.settle(_T0, usage=_usage(), error=None)


# --- the structural half ---------------------------------------------------


def test_no_service_mints_its_own_ledger_row() -> None:
    """The half that stops copy three.

    An `ast` walk rather than a substring scan, for the reason this repository
    recorded twice: prose in a docstring both *trips* a textual guard and
    *answers* one, and both services legitimately discuss `LLMCall` at length
    in theirs. Only a real construction call counts.
    """
    import usher.services.curation as curation
    import usher.services.query_expansion as query_expansion

    for module in (curation, query_expansion):
        source = Path(inspect.getsourcefile(module) or "").read_text()
        minted = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LLMCall"
        ]
        assert not minted, (
            f"{module.__name__} constructs an LLMCall directly; "
            "the row belongs to services.llm_ledger so the rule has one spelling"
        )
