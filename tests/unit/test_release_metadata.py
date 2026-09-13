"""The version number, and the three places it is written down."""

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
    """`pyproject.toml`, the installed distribution's metadata, and `usher.__version__`.

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
    """`0.x`.

    per ADR-0047, and the message names the record so a bump is a decision rather than
    an edit.

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


def test_the_security_policy_supports_the_version_that_ships() -> None:
    """`SECURITY.md`'s supported line names the running minor series.

    **This is the assertion that stops the table becoming a lie.** A security
    policy is correct on the day it is written and wrong on the next release,
    and nothing re-reads it -- a claim whose whole purpose is that it agrees
    with something else needs an assertion on the agreement.

    The control is `assert rows`: a table parse that matched nothing and a
    table that is right are otherwise the same green.
    """
    policy = (pathlib.Path(__file__).parents[2] / "SECURITY.md").read_text()
    rows = re.findall(r"^\|\s*([0-9][^|\s]*)\s*\|\s*(\S+)\s*\|", policy, re.MULTILINE)

    assert rows, "no version rows parsed out of SECURITY.md, so the check below is vacuous"

    supported = [version for version, mark in rows if mark == "✅"]
    assert len(supported) == 1, f"exactly one supported series, got {supported}"

    major, minor, *_ = importlib.metadata.version("usher").split(".")
    assert supported[0] == f"{major}.{minor}.x"


def test_the_readme_backup_section_names_every_precious_table() -> None:
    """Read back out of the manifest, never transcribed.

    **A hand-copied list is the exact drift this section exists to warn
    about.** PRD 08's own prose list of precious tables is missing
    `row_provider_settings` and `search_queries` — M9 shipped the first and M10
    the second, and neither edit reached the paragraph. The manifest is the one
    definition, so the README agrees with it by assertion rather than by
    somebody remembering.

    The control is `assert precious`: an empty manifest would make the loop
    below vacuous and this case would pass against a README naming nothing.
    """
    from usher.db.backup_manifest import BackupClass, tables_of

    precious = sorted(tables_of(BackupClass.PRECIOUS))
    assert precious, "the manifest reports no precious tables, so the loop below is vacuous"

    readme = (pathlib.Path(__file__).parents[2] / "README.md").read_text()
    section = readme.split("\n## Backup\n", 1)
    assert len(section) == 2, "the README has no Backup section"
    body = section[1].split("\n## ", 1)[0]

    missing = [table for table in precious if f"`{table}`" not in body]
    assert not missing, f"README's Backup section does not name {missing}"
