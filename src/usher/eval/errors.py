"""What the harness refuses to do, and how it says so."""


class EvalRefused(RuntimeError):
    """A precondition the run will not proceed without."""


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
