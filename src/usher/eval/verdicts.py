"""What a run can conclude, and what that costs at the shell.

**Its own module, and the reason is an import chain rather than tidiness.**
`runner.py` imports `metrics/ir.py`, which imports `ranx` and raises
`EvalDependencyMissing` when the extra is absent. `usher.cli` needs `Verdict`
and `exit_code_for`, and `usher --help` must work on a deployment that never
installed the extra -- so anything the CLI touches eagerly has to sit on this
side of that import. Nothing here imports anything.
"""

from enum import StrEnum


class Verdict(StrEnum):
    """A run's outcome. `Judgement`'s four members plus the run-level two.

    `SKIPPED` and `BASELINE_INVALID` exit **0**: a surface whose preconditions
    are unmet and a catalog that moved under the baseline are both "this
    measurement did not happen", and blaming a diff for either is how the job
    gets disabled.
    """

    # S105: a verdict, not a credential -- bandit matches the member *name*. The
    # string is what the shell, the ledger and `exit_code_for` publish, so it
    # cannot be spelled around the rule; same call as `bars.Judgement.PASS`.
    PASS = "pass"  # noqa: S105
    FAIL = "fail"
    PENDING = "pending"
    UNBARRED = "unbarred"
    SKIPPED = "skipped"
    BASELINE_INVALID = "baseline-invalid"


# Only a failed bar is a non-zero exit.
#
# `SKIPPED` and `BASELINE_INVALID` are 0 **deliberately**: a red the author
# cannot fix is the red everyone learns to ignore. Both print a loud reason.
_FAILING = frozenset({Verdict.FAIL})


def exit_code_for(verdict: Verdict) -> int:
    """The process exit code for a run's verdict. CI gates on this."""
    return 1 if verdict in _FAILING else 0
