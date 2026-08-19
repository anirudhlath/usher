"""`usher eval`'s argument surface and its exit codes.

The exit code is what CI gates on, so it is pinned here rather than left to
the workflow file -- a job that greps stdout is a job that goes green when a
message is reworded.
"""

import pytest

from usher.cli import parse_args
from usher.eval.verdicts import Verdict, exit_code_for


def test_quick_is_the_default_and_full_is_opt_in() -> None:
    """A slow default is a command nobody types. `--quick` reports numbers,
    enforces no bar and writes no ledger."""
    assert parse_args(["eval"]).full is False
    assert parse_args(["eval", "--full"]).full is True


def test_the_surface_defaults_to_every_surface() -> None:
    assert parse_args(["eval"]).surface is None
    assert parse_args(["eval", "suggest"]).surface == "suggest"


def test_the_seed_defaults_to_the_gates_own() -> None:
    """20260803 is ADR-0002's seed. A different default would make every E1
    number incomparable with the measurement E1 exists to reproduce."""
    from usher.eval.goldens.suggest import GATE_SEED

    assert parse_args(["eval"]).seed == GATE_SEED


@pytest.mark.parametrize(
    ("verdict", "code"),
    [
        (Verdict.PASS, 0),
        (Verdict.PENDING, 0),
        (Verdict.UNBARRED, 0),
        (Verdict.SKIPPED, 0),
        (Verdict.BASELINE_INVALID, 0),
        (Verdict.FAIL, 1),
    ],
)
def test_only_a_failed_bar_is_a_non_zero_exit(verdict: Verdict, code: int) -> None:
    """**`BASELINE_INVALID` exits 0 deliberately.** A catalog that moved under
    the baseline is not the diff's fault, and a red the author cannot fix is
    the red everyone learns to ignore."""
    assert exit_code_for(verdict) == code
