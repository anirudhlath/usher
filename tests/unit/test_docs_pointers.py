"""Every pointer outside the dated records lands on something that exists."""

import pathlib
import re
import shutil
import subprocess
import urllib.parse

import pytest

_ROOT = pathlib.Path(__file__).parents[2]

# Records of a past state: read as history, never edited to match the tree.
_DATED_RECORDS = ("docs/plans/", "docs/specs/", "docs/evals/")

# The prose hook refuses a citation by this same pattern, so it has to spell it.
_SPELLS_THE_PATTERN = frozenset(
    {".claude/hooks/guard_prose.py", "tests/unit/test_docs_pointers.py"}
)

# The decision register is deleted, so any mention of one points at nothing.
_ADR_CITATION = re.compile(r"\bADRs?\b|prd/decisions|decisions/\d{4}-")

# A floor rather than an equality, on `test_docs_currency.py`'s precedent.
_LINKS_FLOOR = 50

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"`[^`\n]*`")
_INLINE_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REFERENCE_LINK = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?", re.MULTILINE)
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)
_HEADING_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_ANCHOR = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"")


def _tracked() -> list[str]:
    """Every file git tracks, minus the dated records."""
    git = shutil.which("git")
    assert git, "git is not on PATH -- this check cannot run"
    # S603: a fixed argv from `shutil.which` and literals, as `test_console.py` does.
    listed = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z"], cwd=_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [path for path in listed.split("\0") if path and not path.startswith(_DATED_RECORDS)]


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
    targets = _INLINE_LINK.findall(prose) + _REFERENCE_LINK.findall(prose)
    return [target for target in targets if not _SCHEME.match(target)]


def _anchors(markdown: str) -> set[str]:
    """The fragment ids GitHub renders for a document's headings."""
    found = set(_HTML_ANCHOR.findall(markdown))
    seen: dict[str, int] = {}
    for heading in _HEADING.findall(_FENCE.sub("", markdown)):
        text = _HEADING_LINK.sub(r"\1", heading).strip().lower()
        slug = "".join(ch for ch in text if ch.isalnum() or ch in " -_").replace(" ", "-")
        found.add(f"{slug}-{seen[slug]}" if slug in seen else slug)
        seen[slug] = seen.get(slug, 0) + 1
    return found


def _resolve(source: str, target: str) -> tuple[pathlib.Path, str]:
    """The file a link from `source` reaches, and its fragment."""
    path, _, fragment = target.partition("#")
    fragment = urllib.parse.unquote(fragment)
    if not path:
        return _ROOT / source, fragment
    base = _ROOT if path.startswith("/") else (_ROOT / source).parent
    return (base / urllib.parse.unquote(path.lstrip("/"))).resolve(), fragment


def test_every_relative_link_in_the_shipped_markdown_resolves() -> None:
    """Kills a link into a file, or a heading, that was deleted or renamed."""
    checked: list[str] = []
    anchored: list[str] = []
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
            if not resolved.exists():
                missing_files.append(where)
            elif fragment and resolved.suffix == ".md":
                anchored.append(where)
                if fragment not in _anchors(resolved.read_text(encoding="utf-8")):
                    missing_anchors.append(where)

    assert len(checked) >= _LINKS_FLOOR, (
        f"the link scan found only {len(checked)} links, so it is not reading the tree"
    )
    assert any(where.startswith("README.md: ") for where in checked), (
        "the link scan read nothing from README.md, so it is not reading the repo root"
    )
    assert any(where.startswith("docs/prd/") for where in checked), (
        "the link scan read nothing under docs/prd/, so the dated-record exclusion "
        "has swallowed the documents whose links have to resolve"
    )
    assert anchored, "no link carried a fragment, so the heading check compared nothing"
    assert not missing_anchors, f"links into headings that do not exist: {missing_anchors}"
    assert not missing_files, f"links into files that do not exist: {missing_files}"


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
            if _ADR_CITATION.search(line):
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
    "line",
    [
        '"(ADR-0015; only for a library the operator really did remove)"',
        "the vocabulary the ADR uses",
        "ADRs record contested calls",
        "| Why | [`docs/prd/decisions/`](docs/prd/decisions/) |",
        "[the call](../prd/decisions/0001-abc-over-protocol.md)",
        "[the call](decisions/0001-abc-over-protocol.md)",
    ],
)
def test_the_citation_pattern_sees_every_spelling_the_tree_has_used(line: str) -> None:
    assert _ADR_CITATION.search(line), f"a citation the scan cannot see: {line!r}"


@pytest.mark.parametrize("line", ["CADRES", "LADR-7", "decisions/ for review"])
def test_the_citation_pattern_refuses_a_word_that_only_contains_the_letters(line: str) -> None:
    assert not _ADR_CITATION.search(line), f"not a citation, and the scan flags it: {line!r}"


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
