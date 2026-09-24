"""`usher eval`'s argument surface and its exit codes."""

import pytest

from usher.cli import parse_args
from usher.eval.verdicts import Verdict, exit_code_for


def test_quick_is_the_default_and_full_is_opt_in() -> None:
    """A slow default is a command nobody types.

    `--quick` reports numbers, enforces no bar and writes no ledger.
    """
    assert parse_args(["eval"]).full is False
    assert parse_args(["eval", "--full"]).full is True


def test_the_surface_defaults_to_every_surface() -> None:
    assert parse_args(["eval"]).surface is None
    assert parse_args(["eval", "suggest"]).surface == "suggest"


def test_the_seed_defaults_to_the_gates_own() -> None:
    """The default seed is the gate's own, so its numbers stay comparable across runs."""
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
    """`BASELINE_INVALID` exits 0: a moved catalog is not the diff's fault."""
    assert exit_code_for(verdict) == code
