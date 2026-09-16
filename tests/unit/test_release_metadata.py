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
    """`pyproject.toml`, the installed distribution's metadata, and `usher.__version__` agree.

    A red here is usually a stale `dist-info` rather than a bug: hatchling
    copies the static version into `METADATA` at install time, so bumping
    `pyproject.toml` without re-running `uv sync` leaves the process on the
    old number.
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
    """The declared version stays `0.x`, so a 1.0.0 bump is a decision rather than an edit."""
    declared = tomllib.loads((pathlib.Path(__file__).parents[2] / "pyproject.toml").read_text())[
        "project"
    ]["version"]

    assert declared.startswith("0."), (
        f"the declared version is {declared!r}. The roadmap's 'v1' is a scope name, "
        "not a compatibility promise, so going to 1.0.0 is a deliberate commitment to "
        "a stable API -- make that decision rather than deleting this assertion."
    )


def test_the_changelog_names_the_version_that_ships() -> None:
    """The newest **released** heading is the version this build reports.

    `[Unreleased]` is skipped so the file can carry work in progress. The
    control is `assert headings`: a regex that matched nothing would be green
    too.
    """
    changelog = (pathlib.Path(__file__).parents[2] / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)

    assert headings, "no `## [version]` heading was found, so the comparison below is vacuous"

    released = [h for h in headings if h != "Unreleased"]
    assert released, "the changelog has no released version, only [Unreleased]"

    assert released[0] == importlib.metadata.version("usher")


def test_the_security_policy_supports_the_version_that_ships() -> None:
    """`SECURITY.md`'s supported line names the running minor series.

    Kills a policy table left behind by a release. The control is `assert
    rows`: a parse that matched nothing would be green too.
    """
    policy = (pathlib.Path(__file__).parents[2] / "SECURITY.md").read_text()
    rows = re.findall(r"^\|\s*([0-9][^|\s]*)\s*\|\s*(\S+)\s*\|", policy, re.MULTILINE)

    assert rows, "no version rows parsed out of SECURITY.md, so the check below is vacuous"

    supported = [version for version, mark in rows if mark == "✅"]
    assert len(supported) == 1, f"exactly one supported series, got {supported}"

    major, minor, *_ = importlib.metadata.version("usher").split(".")
    assert supported[0] == f"{major}.{minor}.x"


def test_the_readme_backup_section_names_every_precious_table() -> None:
    """The README's precious-table list is read back out of the manifest, never transcribed.

    Kills a hand-copied list that a new precious table never reached. The
    control is `assert precious`: an empty manifest would make the loop below
    vacuous.
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
