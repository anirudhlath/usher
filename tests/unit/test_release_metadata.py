"""The version number, and the three places it is written down.

**A guard, not a discovery.** All three sources already agreed when this file
was written (measured 2026-09-07: `0.1.0` from each). Nothing here found a bug;
it exists so that a later edit to one of them cannot pass unnoticed.
"""

import importlib.metadata
import pathlib
import tomllib

import usher

#: What `usher.__init__` answers when the package is not installed. Both cases
#: below refuse to run against it, because an uninstalled tree makes every
#: comparison here compare the fallback to itself.
_UNINSTALLED = "0.0.0+unknown"


def test_the_three_version_sources_agree() -> None:
    """`pyproject.toml`, the installed distribution's metadata, and
    `usher.__version__`.

    ⚠️ **A red here is not a bug in the code.** The build backend is hatchling
    with the version static in `[project]`, so it is copied into the
    distribution's `METADATA` at install time and `pyproject.toml` is never
    read at runtime -- a wheel does not contain one. `uv sync` installs this
    project editable, so the `dist-info` is a snapshot: bumping
    `pyproject.toml` without re-running `uv sync` leaves a running process
    reporting the old number, and that is what this case catches.
    """
    declared = tomllib.loads((pathlib.Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]["version"]
    installed = importlib.metadata.version("usher")

    # The control. Without it an uninstalled tree makes `__version__` and
    # `installed` both the fallback and the comparison vacuous.
    assert usher.__version__ != _UNINSTALLED, (
        "the package is not installed, so this case is comparing the fallback to itself"
    )
    assert declared != _UNINSTALLED

    assert (declared, installed) == (usher.__version__, usher.__version__)
