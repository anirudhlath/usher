"""The version number, and the three places it is written down.

**A guard, not a discovery.** All three sources already agreed when this file
was written (measured 2026-09-07: `0.1.0` from each). Nothing here found a bug;
it exists so that a later edit to one of them cannot pass unnoticed.
"""

import importlib.metadata
import pathlib
import re
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


def test_the_declared_version_is_pre_one_point_zero() -> None:
    """`0.x`, per ADR-0047, and the message names the record so a bump is a
    decision rather than an edit.

    This is the whole of what a test can honestly say about R3. The rest of
    that task is prose, and a case grepping the README for the word "Beta"
    would be a change-detector on the sentence the task exists to make
    readable.
    """
    declared = tomllib.loads((pathlib.Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]["version"]

    assert declared.startswith("0."), (
        f"the declared version is {declared!r}. Going to 1.0.0 overturns "
        "docs/prd/decisions/0047-the-release-is-v0-1-0.md, which says the "
        "roadmap's 'v1' is a scope name and not a compatibility promise -- "
        "read it and amend it rather than deleting this assertion."
    )


def test_the_changelog_names_the_version_that_ships() -> None:
    """The newest **released** heading is the version this build reports.

    `[Unreleased]` is skipped, and that is the one design decision here: the
    check is *"the newest released version is the one that ships"*, not *"the
    newest heading is"* -- otherwise the file could never carry work in
    progress.

    The control is `assert headings`. A regex that matched nothing and a
    changelog whose top entry is right are otherwise the same green.
    """
    changelog = (pathlib.Path(__file__).parents[2] / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)

    assert headings, "no `## [version]` heading was found, so the comparison below is vacuous"

    released = [h for h in headings if h != "Unreleased"]
    assert released, "the changelog has no released version, only [Unreleased]"

    assert released[0] == importlib.metadata.version("usher")
