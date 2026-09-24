"""Every pointer outside the dated records and the hash-pinned evals lands on something real."""

import pathlib
import posixpath
import re
import shutil
import subprocess
import urllib.parse
from collections.abc import Iterable

import pytest

_ROOT = pathlib.Path(__file__).parents[2]

# Records of a past state: read as history, never edited to match the tree.
_DATED_RECORDS = ("docs/plans/", "docs/specs/")
_DATED_EVALS = re.compile(r"^docs/evals/[^/]+\.md$")

# Live, not history, and still never edited to fit: every ledger row records the
# sha256 of the bars.toml it ran against, and the ledger is append-only.
_HASH_PINNED = frozenset({"docs/evals/bars.toml", "docs/evals/ledger.jsonl"})

# Both spell a citation pattern: the prose hook a narrower one, this file its own.
_SPELLS_THE_PATTERN = frozenset(
    {".claude/hooks/guard_prose.py", "tests/unit/test_docs_pointers.py"}
)

# The decision register is deleted, so any mention of one points at nothing. The
# numbered arm takes any case and an underscore, because a test name spells it so.
_ADR_CITATION = re.compile(
    r"(?<![A-Za-z])(?i:adr)[-_ ]?\d{4}|prd/decisions|decisions/\d{4}-"
    r"|(?<![A-Za-z])(?i:decisions?[-_ ]register)"
)

# The bare word is an acronym upstreams use too, so it is read only where this
# project wrote the text. Captured payloads and lockfiles are somebody else's.
_ADR_WORD = re.compile(r"\bADRs?\b")
_CAPTURED = re.compile(
    r"^tests/fixtures/.+\.(?:json|jsonl|tsv)$|(?:^|/)(?:uv\.lock|package-lock\.json)$"
)

# A floor rather than an equality: an equality is a number every new link edits,
# and a floor set near the real count still fails a scan that lost most of the tree.
_LINKS_FLOOR = 150

_FENCE = re.compile(r"^[ >]*(`{3,}|~{3,}).*?^[ >]*\1", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_INLINE_LINK = re.compile(
    r"\]\(\s*(?:<([^<>\n]*)>|((?:[^\s()<>]|\([^\s()<>]*\))+))"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)"
)
_REFERENCE_LINK = re.compile(
    r"^ {0,3}\[(?!\^)[^\]]+\]:\s*(?:<([^<>\n]*)>|([^\s<>]+))", re.MULTILINE
)
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)
_HEADING_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_ANCHOR = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"")


def _git_files() -> list[str]:
    """Every file git tracks."""
    git = shutil.which("git")
    assert git, "git is not on PATH -- this check cannot run"
    # S603: a fixed argv from `shutil.which` and literals, as `test_console.py` does.
    listed = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z"], cwd=_ROOT, capture_output=True, check=False
    )
    assert listed.returncode == 0, (
        f"`git ls-files` failed in {_ROOT}, so this is not a git checkout and nothing "
        f"can say which files ship: {listed.stderr.decode(errors='replace').strip()}"
    )
    return [path for path in listed.stdout.decode().split("\0") if path]


def _exempt(path: str) -> bool:
    """Whether a tracked file is a dated record or hash-pinned, so its pointers stay as written."""
    return (
        path.startswith(_DATED_RECORDS)
        or _DATED_EVALS.match(path) is not None
        or path in _HASH_PINNED
    )


def _tracked() -> list[str]:
    """Every file git tracks, minus the exempt ones."""
    return [path for path in _git_files() if not _exempt(path)]


def _cites_an_adr(path: str, line: str) -> bool:
    """Whether one line of a tracked file points into the deleted register."""
    if _ADR_CITATION.search(line):
        return True
    return not _CAPTURED.search(path) and _ADR_WORD.search(line) is not None


def _text(path: str) -> str | None:
    """The file as text, or None for a binary or a tracked file deleted on disk."""
    try:
        data = (_ROOT / path).read_bytes()
    except FileNotFoundError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _relative_links(markdown: str) -> list[str]:
    """Every link target that is a path into this repository, code excluded."""
    prose = _CODE_SPAN.sub("", _FENCE.sub("", markdown))
    pairs = _INLINE_LINK.findall(prose) + _REFERENCE_LINK.findall(prose)
    targets = [angled or bare for angled, bare in pairs]
    return [target for target in targets if target and not _SCHEME.match(target)]


def _anchors(markdown: str) -> set[str]:
    """The fragment ids GitHub renders for a document's headings, as github-slugger counts."""
    found = set(_HTML_ANCHOR.findall(markdown))
    taken: dict[str, int] = {}
    for heading in _HEADING.findall(_FENCE.sub("", markdown)):
        text = _HEADING_LINK.sub(r"\1", heading).strip().lower()
        base = "".join(ch for ch in text if ch.isalnum() or ch in " -_").replace(" ", "-")
        slug = base
        while slug in taken:
            taken[base] += 1
            slug = f"{base}-{taken[base]}"
        taken[slug] = 0
        found.add(slug)
    return found


def _landable(tracked: Iterable[str]) -> frozenset[str]:
    """What a link may land on: a tracked file, or a directory holding one."""
    found = {""}
    for path in tracked:
        while path and path not in found:
            found.add(path)
            path = posixpath.dirname(path)
    return frozenset(found)


def _resolve(source: str, target: str) -> tuple[str | None, str]:
    """The repository path a link from `source` reaches, None above the root, and its fragment."""
    path, _, fragment = target.partition("#")
    fragment = urllib.parse.unquote(fragment)
    if not path:
        return source, fragment
    path = urllib.parse.unquote(path)
    joined = (
        path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(source), path)
    )
    landed = posixpath.normpath(joined)
    if landed == ".." or landed.startswith("../"):
        return None, fragment
    return ("" if landed == "." else landed), fragment


def test_every_relative_link_in_the_shipped_markdown_resolves() -> None:
    """Kills a link into a file, or a heading, that was deleted, renamed or never shipped."""
    landable = _landable(_git_files())
    checked: list[str] = []
    anchored: list[str] = []
    outside: list[str] = []
    missing_files: list[str] = []
    missing_anchors: list[str] = []
    for source in (path for path in _tracked() if path.endswith(".md")):
        markdown = _text(source)
        if markdown is None:
            continue
        for target in _relative_links(markdown):
            where = f"{source}: {target}"
            checked.append(where)
            resolved, fragment = _resolve(source, target)
            if resolved is None:
                outside.append(where)
            elif resolved not in landable:
                missing_files.append(where)
            elif fragment and resolved.endswith(".md"):
                anchored.append(where)
                if fragment not in _anchors(_text(resolved) or ""):
                    missing_anchors.append(where)

    assert any(where.startswith("README.md: ") for where in checked), (
        "the link scan read nothing from README.md, so it is not reading the repo root"
    )
    assert any(where.startswith("docs/prd/") for where in checked), (
        "the link scan read nothing under docs/prd/, so the dated-record exclusion "
        "has swallowed the documents whose links have to resolve"
    )
    assert len(checked) >= _LINKS_FLOOR, (
        f"the link scan found only {len(checked)} links, so it is not reading the tree"
    )
    assert anchored, "no link carried a fragment, so the heading check compared nothing"
    assert not missing_anchors, f"links into headings that do not exist: {missing_anchors}"
    assert not outside, f"links above the repository root, which GitHub cannot follow: {outside}"
    assert not missing_files, f"links into files git does not track: {missing_files}"


def test_nothing_outside_the_dated_records_cites_an_adr() -> None:
    """Kills a pointer into the deleted decision register, in any format the tree ships.

    A citation that survives is a reason nobody can read. State the reason where
    the reference was, or drop the reference.
    """
    scanned: set[str] = set()
    citations: list[str] = []
    for path in _tracked():
        if path in _SPELLS_THE_PATTERN:
            continue
        text = _text(path)
        if text is None:
            continue
        scanned.add(path)
        for number, line in enumerate(text.splitlines(), start=1):
            if _cites_an_adr(path, line):
                citations.append(f"{path}:{number}: {line.strip()[:120]}")

    formats = {pathlib.PurePath(path).suffix for path in scanned}
    assert {".py", ".md", ".toml", ".yml", ".sql", ".example"} <= formats, (
        f"the citation scan read only {sorted(formats)}, so a pointer in a comment, "
        "a help string or a config file would pass unseen"
    )
    assert not citations, "citations of a decision record nobody can read:\n  " + "\n  ".join(
        citations
    )


@pytest.mark.parametrize(
    ("path", "exempt"),
    [
        ("docs/plans/2026-08-13-m10-hardening.md", True),
        ("docs/specs/2026-08-18-usher-quality-evals-design.md", True),
        ("docs/evals/2026-08-19-e1-baseline-window-disagreement.md", True),
        ("docs/evals/bars.toml", True),
        ("docs/evals/ledger.jsonl", True),
        ("docs/evals/bars.toml.orig", False),
        ("docs/evals/notes.toml", False),
        ("docs/evals/runs/2026-09-01.md", False),
        ("docs/evals.md", False),
        ("docs/prd/05-search-and-similarity.md", False),
    ],
)
def test_only_dated_records_and_the_hash_pinned_evals_are_exempt(path: str, exempt: bool) -> None:
    assert _exempt(path) is exempt, f"{path} should read as exempt={exempt}"


def test_the_hash_pinned_evals_are_files_git_tracks() -> None:
    """An exemption naming a renamed file exempts nothing and hides that it does."""
    missing = _HASH_PINNED - set(_git_files())
    assert not missing, f"exempted by name, tracked by nobody: {sorted(missing)}"


@pytest.mark.parametrize(
    "line",
    [
        '"(ADR-0015; only for a library the operator really did remove)"',
        "the vocabulary the ADR uses",
        "ADRs record contested calls",
        "| Why | [`docs/prd/decisions/`](docs/prd/decisions/) |",
        "[the call](../prd/decisions/0001-abc-over-protocol.md)",
        "[the call](decisions/0001-abc-over-protocol.md)",
        "async def test_the_channel_subscribes_with_adr_0004s_own_frame() -> None:",
        "def test_the_panel_sql_never_names_the_column_or_the_table_adr_0016_refused():",
        "the adr-0016 call",
        "the adr 0016 call",
        "Adr0016",
        "# A floor rather than an equality, on `test_decision_register.py`'s precedent.",
        "the Decision Register",
    ],
)
def test_the_citation_pattern_sees_every_spelling_the_tree_has_used(line: str) -> None:
    assert _cites_an_adr("CHANGELOG.md", line), f"a citation the scan cannot see: {line!r}"


@pytest.mark.parametrize(
    "line", ["CADRES", "LADR-7", "LADR-0007", "ADRIFT", "decisions/ for review", "registered"]
)
def test_the_citation_pattern_refuses_a_word_that_only_contains_the_letters(line: str) -> None:
    assert not _cites_an_adr("CHANGELOG.md", line), (
        f"not a citation, and the scan flags it: {line!r}"
    )


@pytest.mark.parametrize(
    ("path", "line", "cites"),
    [
        ("tests/fixtures/tmdb/movie.json", '{"job": "ADR Mixer"}', False),
        ("tests/fixtures/bulk/name.basics.slice.tsv", "sound_department\tADR Editor", False),
        ("uv.lock", 'sdist = { url = "https://example.org/ADR-1.0.tar.gz" }', False),
        ("web/package-lock.json", '"ADR": "1.0.0"', False),
        ("tests/fixtures/tmdb/movie.json", '{"note": "ADR-0004"}', True),
        ("tests/fixtures/emby/README.md", "what the ADR recorded", True),
        ("dashboards/03-pipeline.json", '"description": "the ADR says why"', True),
        ("CHANGELOG.md", "ADRs record contested calls", True),
    ],
)
def test_the_bare_word_is_read_only_in_text_this_project_wrote(
    path: str, line: str, cites: bool
) -> None:
    """TMDb's crew jobs include "ADR Mixer", so a captured payload may carry the word."""
    assert _cites_an_adr(path, line) is cites, f"{path}: {line!r} should read as cites={cites}"


def test_a_link_inside_code_is_not_a_link() -> None:
    """A snippet that builds a link is not a link, and a reference definition is."""
    markdown = (
        "See [the PRD](docs/prd/README.md) and [issue 10][ten].\n"
        "\n"
        "[ten]: https://example.org/10\n"
        "[local]: CLAUDE.md\n"
        "\n"
        "`[not a link](gone.md)` in a code span.\n"
        "\n"
        "```python\n"
        'text = "[not a link](gone-too.md)"\n'
        "```\n"
    )

    assert _relative_links(markdown) == ["docs/prd/README.md", "CLAUDE.md"]


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("## Versioning", "versioning"),
        ("## `usher sync` — the walk", "usher-sync--the-walk"),
        ("## \U0001f534 A marker first", "-a-marker-first"),
        ("### See [PRD 08](docs/prd/08-operations.md)", "see-prd-08"),
    ],
)
def test_a_heading_anchor_is_spelled_the_way_github_renders_it(heading: str, anchor: str) -> None:
    assert anchor in _anchors(heading + "\n")


def test_a_repeated_heading_takes_a_numbered_anchor() -> None:
    assert _anchors("## Setup\n\n## Setup\n\n## Setup\n") == {"setup", "setup-1", "setup-2"}


def test_a_numbered_anchor_already_taken_by_a_heading_is_numbered_again() -> None:
    """github-slugger re-numbers a collision against every slug handed out, not the base."""
    assert _anchors("## Setup\n\n## Setup\n\n## Setup 1\n") == {"setup", "setup-1", "setup-1-1"}


def test_a_link_title_in_any_of_its_spellings_leaves_the_target_readable() -> None:
    markdown = (
        "[a](double.md \"title\") [b](single.md 'title') [c](paren.md (title))\n"
        '[d](<with space.md>) [e](<angled.md> "title")\n'
        "[f]: <reference with space.md>\n"
    )

    assert _relative_links(markdown) == [
        "double.md",
        "single.md",
        "paren.md",
        "with space.md",
        "angled.md",
        "reference with space.md",
    ]


def test_a_fence_indented_in_a_list_or_quoted_in_a_blockquote_is_still_code() -> None:
    markdown = (
        "- a list item\n"
        "\n"
        "    ```markdown\n"
        "    [not a link](gone.md)\n"
        "    ```\n"
        "\n"
        "> ```markdown\n"
        "> [not a link](gone-too.md)\n"
        "> ```\n"
        "\n"
        "[a link](kept.md)\n"
    )

    assert _relative_links(markdown) == ["kept.md"]


def test_a_footnote_definition_is_not_a_reference_link() -> None:
    assert _relative_links("Claim.[^1]\n\n[^1]: see the notes above\n[ref]: kept.md\n") == [
        "kept.md"
    ]


@pytest.mark.parametrize(
    ("source", "target", "lands"),
    [
        ("README.md", "CLAUDE.md", "CLAUDE.md"),
        ("docs/prd/README.md", "../../CLAUDE.md#the-gate", "CLAUDE.md"),
        ("docs/prd/README.md", "/CLAUDE.md", "CLAUDE.md"),
        ("docs/prd/README.md", "./", "docs/prd"),
        ("README.md", "my%20file.md", "my file.md"),
        ("README.md", "../usher/README.md", None),
        ("docs/prd/README.md", "../../../elsewhere.md", None),
    ],
)
def test_a_link_resolves_inside_the_repository_or_not_at_all(
    source: str, target: str, lands: str | None
) -> None:
    """GitHub renders nothing above the repository root, whatever sits there on disk."""
    assert _resolve(source, target)[0] == lands


def test_a_link_lands_only_on_a_tracked_file_or_a_directory_holding_one() -> None:
    landable = _landable(["README.md", "docs/prd/00-overview.md"])

    assert {"README.md", "docs", "docs/prd", "docs/prd/00-overview.md"} <= landable
    assert ".env" not in landable, "an untracked file is not something GitHub can render"
    assert "docs/prd/99-gone.md" not in landable
