"""What the harness refuses to do, and how it says so.

Two types, and the split is the one `measure_suggest_tiers.py` already draws:
a *refusal* is "this would produce a plausible number that means nothing",
which is a different event from a crash and from a low score.
"""


class EvalRefused(RuntimeError):
    """A precondition the run will not proceed without.

    An empty catalog, a sampling frame that does not reproduce, a run whose
    every case returned nothing where that is impossible. All of them share
    one property: continuing produces a number a reader would believe.

    Its own class rather than `RuntimeError` so `runner.py` can turn it into
    a *reported verdict* rather than a traceback -- `skipped-with-reason` and
    `baseline-invalid` are both this, caught.
    """


class EvalDependencyMissing(EvalRefused):
    """The `eval` extra is not installed.

    A subclass rather than a sibling because it is the same event -- the run
    will not proceed -- and every handler that wants one wants both.

    **The message names the command.** `usher eval` reaching an operator as
    `ModuleNotFoundError: No module named 'ranx'` tells them a module is
    absent and nothing else: not that it is optional, not which extra carries
    it, not what to type.
    """

    def __init__(self, package: str) -> None:
        super().__init__(
            f"the eval harness needs {package!r}, which ships in the optional "
            f"`eval` extra -- run `uv sync --extra eval`"
        )
        self.package = package
