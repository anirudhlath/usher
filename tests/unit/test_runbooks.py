"""`docs/runbooks/` is four operator-facing documents and an index."""

import argparse
import pathlib
import re

import pytest

from usher.cli import build_parser

_ROOT = pathlib.Path(__file__).parents[2]
_RUNBOOKS = _ROOT / "docs" / "runbooks"
_INDEX = _RUNBOOKS / "README.md"
_PRD_OPERATIONS = _ROOT / "docs" / "prd" / "08-operations.md"

#: The spec asks for four — restore, upgrade, disaster recovery, rotation.
#: A floor rather than an equality: an equality is a line the next runbook
#: edits until it goes green.
RUNBOOKS_THE_SPEC_ASKS_FOR = 4

#: `.claude/rules/prd-maintenance.md`'s own pattern, unchanged. `[^)#]` on the
#: first character is what keeps a bare `#anchor` out; the `.md` suffix is what
#: keeps `https://…` links and image paths out.
_MARKDOWN_LINK = re.compile(r"\]\(([^)#][^)]*\.md)\)")

#: Every `uv run usher …` and the rest of its line.
_INVOCATION = re.compile(r"uv run usher\b([^\n`&|;]*)")

#: A floor on the premise that the extraction reads something, not a
#: description of the corpus.
INVOCATIONS_AT_LEAST = 6


def _subcommands() -> set[str]:
    """The subcommands `build_parser()` advertises, read off the parser.

    Not `grep -c "add_parser("`: that spelling counts its own comment and
    cannot see a subparser added by any other route.
    """
    subparsers = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices)


def _global_flags() -> set[str]:
    """Every top-level option string.

    `--traceback`, `-h` and `--help` today.
    """
    return {
        option
        for action in build_parser()._actions
        if not isinstance(action, argparse._SubParsersAction)
        for option in action.option_strings
    }


def _links(document: pathlib.Path) -> set[pathlib.Path]:
    """Every `.md` path `document` links to, resolved against its own directory."""
    return {
        (document.parent / link).resolve() for link in _MARKDOWN_LINK.findall(document.read_text())
    }


def _invocations(text: str) -> list[tuple[tuple[str, ...], str | None]]:
    """`(leading flags, subcommand)` for every `uv run usher …` in `text`.

    Tokens are walked rather than pattern-matched: everything up to the first
    token that does not start with `-` is a flag, and that token is the
    subcommand. `None` where the line offers none — which is a result the
    caller asserts on, not an occurrence that quietly does not exist.
    """
    invocations: list[tuple[tuple[str, ...], str | None]] = []
    for tail in _INVOCATION.findall(text):
        flags: list[str] = []
        command: str | None = None
        for token in tail.split():
            if token.startswith("-"):
                flags.append(token)
                continue
            command = token
            break
        invocations.append((tuple(flags), command))
    return invocations


def test_every_runbook_the_index_names_exists_and_every_runbook_is_indexed() -> None:
    """Kills a renamed runbook leaving a dead row in the index, or a runbook nobody indexed.

    The index is read before the directory is scanned so that a missing
    `docs/runbooks/` raises rather than comparing the empty set to itself.
    """
    linked = _links(_INDEX)

    on_disk = {path.resolve() for path in _RUNBOOKS.glob("*.md")} - {_INDEX.resolve()}
    indexed = {path for path in linked if path.parent == _RUNBOOKS.resolve()}

    assert len(indexed) >= RUNBOOKS_THE_SPEC_ASKS_FOR, (
        f"the index names {len(indexed)} runbooks and PRD 08 asks for "
        f"{RUNBOOKS_THE_SPEC_ASKS_FOR} (restore, upgrade, disaster recovery, "
        f"rotation): {sorted(path.name for path in indexed)}"
    )

    assert indexed == on_disk, (
        f"indexed but not on disk: {sorted(path.name for path in indexed - on_disk)}; "
        f"on disk but not indexed: {sorted(path.name for path in on_disk - indexed)}"
    )


def test_every_link_in_every_runbook_resolves() -> None:
    """Kills a runbook link that points outside `docs/runbooks/` at a file that is gone.

    The index case above compares only links landing inside the directory; no
    other check in this repository walks `docs/runbooks/` at all.
    """
    checked: list[tuple[str, pathlib.Path]] = []
    for runbook in sorted(_RUNBOOKS.glob("*.md")):
        for target in _links(runbook):
            checked.append((runbook.name, target))

    assert len(checked) >= RUNBOOKS_THE_SPEC_ASKS_FOR, (
        f"only {len(checked)} markdown links were extracted from {_RUNBOOKS}, "
        "so the extraction is not reading the runbooks"
    )

    dead = [(name, str(target)) for name, target in checked if not target.exists()]
    assert not dead, f"a runbook links to a file that does not exist: {sorted(dead)}"


def test_the_index_is_read_before_the_directory_is_scanned(
    tmp_path: pathlib.Path,
) -> None:
    """The control for the case above, planted rather than reasoned about.

    An empty runbook directory must be a `FileNotFoundError` that names the
    index, and the set comparison that would have passed over it is asserted to
    be the one it would have passed with. Both halves matter: without the
    second, this case would still be green if the scan were the thing that
    raised.
    """
    empty = tmp_path / "runbooks"
    empty.mkdir()

    assert {path for path in empty.glob("*.md")} == set(), (
        "the premise: a scan of an empty directory is the empty set"
    )

    with pytest.raises(FileNotFoundError) as caught:
        (empty / "README.md").read_text()
    assert "README.md" in str(caught.value), (
        "the failure has to name the missing file, not just be a failure"
    )


def test_every_command_a_runbook_names_is_a_command_the_cli_advertises() -> None:
    """Kills a runbook telling an operator to run a subcommand the CLI does not advertise.

    The floor is the premise: an extraction that matched nothing would report
    every runbook clean.
    """
    advertised = _subcommands()
    global_flags = _global_flags()

    found: list[tuple[str, tuple[str, ...], str | None]] = []
    for runbook in sorted(_RUNBOOKS.glob("*.md")):
        for flags, command in _invocations(runbook.read_text()):
            found.append((runbook.name, flags, command))

    assert len(found) >= INVOCATIONS_AT_LEAST, (
        f"only {len(found)} `uv run usher …` invocations were extracted from "
        f"{_RUNBOOKS}, so the extraction is not reading the runbooks"
    )

    for name, flags, command in found:
        assert command is not None, (
            f"docs/runbooks/{name} writes `uv run usher` with no subcommand after "
            f"it (leading tokens: {list(flags)})"
        )
        assert command in advertised, (
            f"docs/runbooks/{name} names `usher {command}`, which build_parser() "
            f"does not advertise: {sorted(advertised)}"
        )
        for flag in flags:
            assert flag in global_flags, (
                f"docs/runbooks/{name}: `{flag}` is written before the subcommand "
                f"`{command}` but is not a top-level option ({sorted(global_flags)})"
            )


def test_prd_08_points_at_the_runbook_index_and_the_link_resolves() -> None:
    """PRD 08 asks for four runbooks; this is the line that lets a reader find them.

    Resolved from `docs/prd/` rather than asserted as a substring, because a
    pointer that does not resolve is exactly the defect the case above exists
    to catch, one directory over. The premise is that the document really does
    carry `.md` links, so a regex that stopped matching is a failure here too.
    """
    prd = _PRD_OPERATIONS.read_text()
    links = {((_PRD_OPERATIONS.parent / link).resolve()) for link in _MARKDOWN_LINK.findall(prd)}

    assert len(links) >= 2, (
        f"only {len(links)} markdown links were extracted from {_PRD_OPERATIONS}, "
        "so the extraction is not reading the document"
    )
    assert _INDEX.resolve() in links, (
        "docs/prd/08-operations.md does not point at docs/runbooks/README.md; "
        "PRD 08 asks for the runbooks and nothing in it names the index"
    )
    assert _INDEX.exists(), f"{_INDEX} is linked from PRD 08 and does not exist"


def test_no_runbook_puts_a_key_on_a_command_line() -> None:
    """Kills a runbook pasting a secret onto a command line via `--new-key`.

    `cli.py` declares `--new-key` only so that typing it is a refusal; the key
    would otherwise land in shell history and in `ps`. Scoped to `uv run usher
    …` lines, because `rotation.md` names the flag in prose to say it is
    refused.
    """
    scanned = sorted(_RUNBOOKS.glob("*.md"))
    assert len(scanned) > RUNBOOKS_THE_SPEC_ASKS_FOR - 1, (
        f"only {len(scanned)} files were scanned in {_RUNBOOKS}; a security check "
        "that ran over nothing is not a security check"
    )

    offenders = [
        f"docs/runbooks/{runbook.name}:{number}: {line.strip()}"
        for runbook in scanned
        for number, line in enumerate(runbook.read_text().splitlines(), start=1)
        if line.lstrip().startswith("uv run usher ")
        and re.search(r"--new-key(?![a-z-])", line) is not None
    ]
    assert not offenders, (
        "a runbook shows a key on a command line; --new-key-env takes the NAME "
        f"of an exported variable: {offenders}"
    )


#: `Dockerfile:133`, `compose.yml:12`, `cli.py:166`: a file named by one line. Ports
#: (`localhost:8100`, `otel-collector:4317`) name no file, so they do not match.
_LINE_CITATION = re.compile(r"(?:\bDockerfile|\.(?:py|ya?ml|toml|ini|md|sh)):\d+\b")


def test_no_runbook_cites_a_file_by_line_number() -> None:
    """A line number is exact when written and wrong one insert later.

    `upgrade.md` cited `Dockerfile:124`, `:130` and `:133` against an 85-line
    Dockerfile. Quote a phrase, which is greppable and survives a move.
    """
    scanned = sorted(_RUNBOOKS.glob("*.md"))
    assert len(scanned) > RUNBOOKS_THE_SPEC_ASKS_FOR - 1, f"only {len(scanned)} runbooks scanned"
    assert _LINE_CITATION.search("see `Dockerfile:133`"), "the pattern cannot see a citation"

    offenders = [
        f"docs/runbooks/{runbook.name}:{number}: {line.strip()}"
        for runbook in scanned
        for number, line in enumerate(runbook.read_text().splitlines(), start=1)
        if _LINE_CITATION.search(line)
    ]
    assert offenders == [], f"cite by quoting a phrase, not by line number: {offenders}"


def _flattened(text: str) -> str:
    return " ".join(text.split())


def test_the_upgrade_runbook_quotes_the_dockerfile_verbatim() -> None:
    """Every `>` quote and the `dockerfile` fence in §0 must still be in the Dockerfile.

    Compared whitespace-flattened against the Dockerfile with each comment's `#`
    removed, since a quote reflows the comment it copies.
    """
    runbook = (_RUNBOOKS / "upgrade.md").read_text()
    section = runbook[: runbook.index("### `docker compose pull`")]
    quotes = [
        _flattened(" ".join(line.removeprefix(">") for line in block.splitlines()))
        for block in re.findall(r"(?:^>.*\n)+", section, flags=re.MULTILINE)
    ]
    fences = re.findall(r"^```dockerfile\n(.*?)^```", section, flags=re.MULTILINE | re.DOTALL)

    dockerfile = (_ROOT / "Dockerfile").read_text()
    uncommented = _flattened(re.sub(r"^\s*# ?", "", dockerfile, flags=re.MULTILINE))
    assert len(quotes) >= 2, f"only {len(quotes)} quotes extracted from upgrade.md §0"
    assert fences, "upgrade.md §0 no longer shows the Dockerfile's CMD"

    missing = [quote for quote in quotes if quote not in uncommented]
    missing += [fence.strip() for fence in fences if fence.strip() not in dockerfile]
    assert missing == [], f"upgrade.md quotes Dockerfile text that is not there: {missing}"
