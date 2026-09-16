"""The harness's refusals, and the verdicts that are not failures."""

import re
import tomllib
import uuid
from pathlib import Path

import pytest

from usher.eval.bars import Judgement, load_bars
from usher.eval.errors import EvalDependencyMissing, EvalRefused
from usher.eval.goldens.suggest import TypoCase
from usher.eval.ledger import ScoreRecord
from usher.eval.metrics.ir import Ranking
from usher.eval.runner import score_surface, verdict_for
from usher.eval.surfaces.suggest import SurfaceRun
from usher.eval.verdicts import Verdict

# tests/unit/test_eval_runner.py -> tests/unit -> tests -> repo root. A fact
# about pyproject.toml has to be read from pyproject.toml, not re-asserted as
# a literal that can drift out from under it.
_ROOT = Path(__file__).resolve().parents[2]


def test_a_missing_extra_is_a_refusal_and_names_a_command_that_actually_installs_it() -> None:
    """The refusal subclasses `EvalRefused` and names an extra that really installs it.

    `pytest.raises(EvalDependencyMissing)` is satisfied by a child of anything, so only
    an `isinstance` on the parent pins the ancestry. The extra the message names is
    checked against `pyproject.toml` rather than trusted, so renaming it cannot leave an
    operator with a command that fails.
    """
    problem = EvalDependencyMissing("ranx")
    assert isinstance(problem, EvalRefused)
    assert "uv sync --extra eval" in str(problem)
    assert "ranx" in str(problem)

    named = re.search(r"--extra ([\w-]+)", str(problem))
    assert named is not None, "the message names no `--extra <name>` command at all"
    with (_ROOT / "pyproject.toml").open("rb") as handle:
        extras = tomllib.load(handle)["project"]["optional-dependencies"]
    assert named.group(1) in extras, (
        f"the message tells an operator to run `uv sync --extra {named.group(1)}`, "
        f"which pyproject.toml's [project.optional-dependencies] does not define: "
        f"{sorted(extras)!r}"
    )


def test_a_refusal_message_survives_construction_and_stays_matchable() -> None:
    """The reason a run was refused survives construction and stays matchable."""
    with pytest.raises(EvalRefused, match="sampling frame"):
        raise EvalRefused("the sampling frame does not reproduce the gate's")


_BARS = Path(__file__).resolve().parents[2] / "docs" / "evals" / "bars.toml"


def _run(hit: bool) -> SurfaceRun:
    case = TypoCase(
        title_id=uuid.UUID(int=7),
        name="Alien",
        band="5-7",
        typo_class="substitution",
        probe="Alein",
    )
    found = (str(case.title_id),) if hit else ()
    return SurfaceRun(
        relevant={case.query_id: str(case.title_id)},
        rankings=(Ranking(case.query_id, found),),
        latencies_ms=(1.0,),
        strata={case.query_id: ("all", "band=5-7", "typo_class=substitution")},
    )


def test_a_pending_bar_reports_the_number_and_does_not_gate() -> None:
    """No bar exists for tier 2's overall recall, so the run reports the number.

    A run that claimed PASS against a bar that does not exist has claimed to face
    something it did not.
    """
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    overall = next(s for s in scores if s.metric == "recall_at_5" and s.stratum == "all")
    assert overall.judgement is Judgement.PENDING
    assert overall.value == 1.0


def test_a_window_bar_fails_a_value_outside_it() -> None:
    """Tier 1's bar is a window, so a value far above it fails as surely as one below."""
    scores = score_surface(_run(hit=True), tier="prefix", bars=load_bars(_BARS))
    overall = next(s for s in scores if s.metric == "recall_at_5" and s.stratum == "all")
    assert overall.judgement is Judgement.FAIL


def test_every_stratum_the_run_produced_gets_a_score_row() -> None:
    """A stratum silently absent from the ledger is a stratum nobody plots."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    strata = {one.stratum for one in scores if one.metric == "recall_at_5"}
    assert strata == {"all", "band=5-7", "typo_class=substitution"}


def test_observations_are_recorded_per_stratum() -> None:
    """A recall of 1.0 over three cases and over three thousand are different facts.

    Without the denominator a trend chart cannot tell them apart.
    """
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    assert all(one.observations >= 1 for one in scores)


def test_latency_is_reported_and_is_not_averaged_with_recall() -> None:
    scores = score_surface(_run(hit=True), tier="prefix", bars=load_bars(_BARS))
    metrics = {one.metric for one in scores}
    assert "latency_p95_ms" in metrics
    assert "recall_at_5" in metrics


def _judged(*judgements: Judgement) -> tuple[ScoreRecord, ...]:
    return tuple(
        ScoreRecord(
            surface="suggest",
            tier="prefix",
            metric=f"m{index}",
            stratum="all",
            value=0.0,
            observations=1,
            judgement=judgement,
            bar_kind=None,
            bar_low=None,
            bar_high=None,
        )
        for index, judgement in enumerate(judgements)
    )


@pytest.mark.parametrize(
    ("judgements", "expected"),
    [
        ((Judgement.PENDING,), Verdict.PENDING),
        ((Judgement.UNBARRED,), Verdict.UNBARRED),
        ((Judgement.PASS,), Verdict.PASS),
        ((Judgement.FAIL,), Verdict.FAIL),
        ((Judgement.PENDING, Judgement.UNBARRED), Verdict.PENDING),
        ((Judgement.PASS, Judgement.PENDING, Judgement.UNBARRED), Verdict.PASS),
        ((Judgement.FAIL, Judgement.PASS, Judgement.PENDING), Verdict.FAIL),
    ],
)
def test_the_verdict_precedence_holds_in_every_combination(
    judgements: tuple[Judgement, ...], expected: Verdict
) -> None:
    """`verdict_for`'s four branches are ordered, and the mixed rows pin the order.

    A run whose only judgements are PENDING must report PENDING, not PASS: this feeds
    `exit_code_for`, so a wrong precedence is a CI job going green on a run that faced
    no bar. Any single-judgement row is also satisfied by an implementation that returns
    whatever it was given.
    """
    assert verdict_for(_judged(*judgements)) is expected


def test_an_empty_run_is_unbarred_rather_than_a_pass() -> None:
    """A surface whose tiers all raised produces no records, and `UNBARRED` says so.

    `PASS` here would be a run claiming to have faced a bar it never reached.
    """
    assert verdict_for(()) is Verdict.UNBARRED
