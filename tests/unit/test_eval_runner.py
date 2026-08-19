"""The harness's refusals, and the verdicts that are not failures."""

import re
import tomllib
import uuid
from pathlib import Path

import pytest

from usher.eval.bars import Judgement, load_bars
from usher.eval.errors import EvalDependencyMissing, EvalRefused
from usher.eval.goldens.suggest import TypoCase
from usher.eval.metrics.ir import Ranking
from usher.eval.runner import score_surface
from usher.eval.surfaces.suggest import SurfaceRun

# tests/unit/test_eval_runner.py -> tests/unit -> tests -> repo root. Same
# derivation as tests/unit/test_eval_contract.py, which reads the same file
# for the same reason: a fact about pyproject.toml has to be read from
# pyproject.toml, not re-asserted as a literal that can drift out from under
# it.
_ROOT = Path(__file__).resolve().parents[2]


def test_a_missing_extra_is_a_refusal_and_names_a_command_that_actually_installs_it() -> None:
    """`EvalDependencyMissing` subclasses `EvalRefused` so every handler that
    wants a refusal gets this one too -- but `pytest.raises(EvalDependencyMissing)`
    says nothing about that, since it is satisfied by a child of anything;
    only an `isinstance` assertion on the parent pins the ancestry (the
    precedent is `tests/unit/test_ports_ingest.py`'s
    `test_the_sweep_refusal_is_a_port_error`).

    And the message is the whole point of this class existing: a bare
    ImportError tells an operator a module is absent, not that it is
    optional, which extra carries it, or what to type. That command is only
    honest if the extra it names is one `uv sync --extra` can actually
    install -- a rename of the extra in `pyproject.toml` must not leave this
    message pointing an operator at a command that fails, so the extra named
    here is checked against `pyproject.toml` itself rather than trusted."""
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
    """`EvalRefused` carries the reason a run could not be measured, and that
    reason has to survive construction and remain something a caller can
    `pytest.raises(..., match=...)` for -- which is what this case pins.

    It does **not** pin that the type is distinct from a scoring error, or
    that no caller can catch the two together in one clause -- there is no
    second type in this module for it to be distinct from yet, so nothing
    here checks that property. It belongs on whichever future case
    introduces a scoring-error type and has to catch the two separately."""
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
    """Spec 14: no bar exists for tier 2's overall recall, so the first run
    reports it. A run that claimed PASS against a bar that does not exist has
    claimed to face something it did not."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    overall = next(s for s in scores if s.metric == "recall_at_5" and s.stratum == "all")
    assert overall.judgement is Judgement.PENDING
    assert overall.value == 1.0


def test_a_window_bar_fails_a_value_outside_it() -> None:
    """Tier 1's 1.9% is a window. This stub run scores 1.0, which is far
    above it -- and 'a tier 1 that scores higher is not the index that was
    measured' is exactly what the window says."""
    scores = score_surface(_run(hit=True), tier="prefix", bars=load_bars(_BARS))
    overall = next(s for s in scores if s.metric == "recall_at_5" and s.stratum == "all")
    assert overall.judgement is Judgement.FAIL


def test_every_stratum_the_run_produced_gets_a_score_row() -> None:
    """A stratum silently absent from the ledger is a stratum nobody plots.
    ADR-0002's 0.0% transposition finding is a stratum, not a headline."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    strata = {one.stratum for one in scores if one.metric == "recall_at_5"}
    assert strata == {"all", "band=5-7", "typo_class=substitution"}


def test_observations_are_recorded_per_stratum() -> None:
    """A recall of 1.0 over three cases and over three thousand are different
    facts. Without the denominator a trend chart cannot tell them apart."""
    scores = score_surface(_run(hit=True), tier="fuzzy", bars=load_bars(_BARS))
    assert all(one.observations >= 1 for one in scores)


def test_latency_is_reported_and_is_not_averaged_with_recall() -> None:
    scores = score_surface(_run(hit=True), tier="prefix", bars=load_bars(_BARS))
    metrics = {one.metric for one in scores}
    assert "latency_p95_ms" in metrics
    assert "recall_at_5" in metrics
