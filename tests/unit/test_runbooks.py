"""`docs/runbooks/` is four operator-facing documents and an index, and the
index is the part a rename breaks silently.

Four checks and a control, and each exists because the cheap version of it
passes over nothing at all.

**1. The index and the directory are one set.** `docs/runbooks/README.md`'s
links are resolved and compared against the `.md` files beside it. A runbook
index that points at a file somebody renamed is worse than no index — an
operator following a dead link in a disaster is worse off than one who had to
`ls` — and this is the same shape as the PRD link check that
`.claude/rules/prd-maintenance.md` prescribes, for the same reason.

🔴 **`set() == set()` is `True`, which is the whole design of the first case.**
A scan over a `docs/runbooks/` that does not exist globs nothing, compares the
empty set against the empty set, and reports success — so the index is read
**before** the directory is scanned, and `Path.read_text()`'s
`FileNotFoundError` names the missing path. A missing directory is a failure
that names a file, not a pass. `test_the_index_is_read_before_the_directory_is_scanned`
pins that ordering against a temporary directory holding nothing, because a
plant that did not land looks exactly like a check that passed.

**2. Every command a runbook names is a command the CLI advertises.** Extracted
from the prose and checked against `build_parser()`'s subparser choices — read
off the parser, **never** off a `grep -c "add_parser("`, which counts its own
comment (measured on this milestone, in K3's review). The floor of six
occurrences is the premise: an extraction that matched nothing would otherwise
report every runbook clean.

⚠️ **Subcommands only, deliberately, and the sibling that goes further says
why it can.** `test_backup_manifest.py::test_every_rebuild_step_is_a_command_the_cli_really_accepts`
hands whole command strings to `parse_args` and so catches a bad flag too — it
can, because a manifest entry is a bare command with no shell around it. A
runbook's are pasteable lines: `uv run usher restore … --dry-run 2>&1 | tee
/var/tmp/restore-report.txt` is three of them, and `shlex.split` of that
reaches argparse as `2>&1`. What is checkable here without inventing a shell
parser is the first non-flag token, plus whatever flags precede it — and both
are checked, so `uv run usher --output /var/tmp/x.gz backup` fails on the flag
and on the "subcommand" `/var/tmp/x.gz` rather than passing.

🔴 **That last sentence was written before it was true, and the plant is what
said so.** The first spelling matched the flags and the subcommand in one
regex, and `uv run usher --output /var/tmp/x.gz backup` matched *none of it*:
`[a-z]` does not match `/`, so the occurrence was skipped and the case stayed
green over a defect written to be caught. `_INVOCATION` carries the mechanism.
The lesson is the standing one — a plant that did not land looks exactly like a
check that passed — arriving as a docstring that described a guard the code did
not have.

**2b. And every link in every runbook resolves.** The link check
`.claude/rules/prd-maintenance.md` prescribes is scoped to `docs/prd/**` plus
`CLAUDE.md` and `README.md` — deliberately, and that exclusion is a correction
rather than a convenience — so **nothing in this repository walks
`docs/runbooks/`**. Measured 2026-08-26: 21 `.md` links across the five files,
17 of them to a sibling runbook, 2 to PRD 08 and 2 to ADR-0038 — and an ADR
rename is exactly the event that would break the last pair silently.

**3. PRD 08 points at the index, and the link resolves.** Asserted here rather
than left to the link check in `.claude/rules/prd-maintenance.md`: that check
is a heredoc an operator runs by hand, it is not in `scripts/` and no test
invokes it, so *"the PRD link check prints OK"* is a claim about a command
somebody remembered to type. This case is the same obligation with a runner.

**4. No runbook puts a key on a command line.** `--new-key` is a tripwire
argument that exists so that typing it is a refusal rather than a leak
(`cli.py`'s `_rotate_secret_parser`); `--new-key-env` takes the *name* of an
exported variable. A runbook is the most-pasted text this project ships, so the
one place the wrong spelling must never appear is in one of these files.
"""

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
#: A floor rather than an equality, on `test_docs_currency.py`'s precedent: an
#: equality is a line the next runbook edits, which is how a count stops being
#: a measurement and becomes a number people bump until green.
RUNBOOKS_THE_SPEC_ASKS_FOR = 4

#: `.claude/rules/prd-maintenance.md`'s own pattern, unchanged. `[^)#]` on the
#: first character is what keeps a bare `#anchor` out; the `.md` suffix is what
#: keeps `https://…` links and image paths out.
_MARKDOWN_LINK = re.compile(r"\]\(([^)#][^)]*\.md)\)")

#: Every `uv run usher …` and the rest of its line. Stopping at a backtick as
#: well as at a newline is what makes one pattern serve both a fenced block and
#: an inline `` `uv run usher work` `` in a sentence; stopping at `&`, `|` and
#: `;` is what makes a *chain* two invocations rather than one. Measured: with
#: the shell separators left in, `uv run usher index --backfill && uv run usher
#: work` consumed the whole line as a single tail and `re.findall` resumed past
#: the second command, so `docs/runbooks/disaster-recovery.md` reported 24
#: invocations where it has 25 and the trailing `work` was checked by nothing.
#:
#: ⚠️ **This was `uv run usher((?:\s+--[a-z…]*)*)\s+([a-z…]*)` — a flag group
#: and a subcommand in one regex — and the plant walked straight through it.**
#: `uv run usher --output /var/tmp/x.gz backup` matches *nothing*: the flag
#: group takes `--output`, the subcommand group then meets `/var`, `[a-z]` does
#: not match `/`, and backtracking to zero flags meets `--output`. A defect
#: written to be caught was silently skipped instead, and the case stayed green
#: — which is the one failure a check like this must not have. The tail is now
#: captured whole and walked in `_invocations`, where "no subcommand here" is a
#: value the assertions can see rather than an occurrence the regex declines to
#: report.
_INVOCATION = re.compile(r"uv run usher\b([^\n`&|;]*)")

#: Measured 2026-08-26: the three K5/K8 runbooks alone carry 25 invocations of
#: 9 distinct subcommands, and all four carry 35. Six is a floor on the
#: premise, not a description of the corpus — a number that tracked the corpus
#: would be a line the next runbook edits.
INVOCATIONS_AT_LEAST = 6


def _subcommands() -> set[str]:
    """The subcommands `build_parser()` advertises, read off the parser.

    ⚠️ **Not `grep -c "add_parser("`.** That spelling counts its own comment —
    caught in this milestone's K3 review — and it cannot see a subparser added
    by any other route. Twenty commands as of 2026-08-26, which this function
    does not assert: the number is the CLI's business and the runbooks' only
    obligation is to name commands that are in it.
    """
    subparsers = next(
        action
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices)


def _global_flags() -> set[str]:
    """Every top-level option string. `--traceback`, `-h` and `--help` today."""
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
    """Kills a renamed runbook leaving a dead row in the index, and a new
    runbook nobody added to it — the same defect wearing two faces.

    **The read comes first and that is the case's design.** A scan of a
    `docs/runbooks/` that does not exist answers the empty set, and the empty
    set equals the empty set: the check would report success over a directory
    with nothing in it. Reading the index first turns that into a
    `FileNotFoundError` naming `docs/runbooks/README.md`, which is a failure
    an operator can act on rather than a green tick over a hole.
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
    """The obligation the index case scopes away, taken separately.

    The case above compares only the links that land *inside*
    `docs/runbooks/`, because the index legitimately points at
    `../prd/08-operations.md` and that is not a runbook. This one covers the
    rest — and it covers a real gap rather than a hypothetical: the link check
    `.claude/rules/prd-maintenance.md` prescribes is scoped to `docs/prd/**`
    plus `CLAUDE.md` and `README.md`, so **no check in this repository walks
    `docs/runbooks/` at all**. Twenty-one links across the five files, 17 to a
    sibling runbook and 2 to an ADR — and an ADR rename is exactly the event
    that would break those silently.

    The floor is the premise, on the same reasoning as every other scan here.
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
    """Kills a runbook telling an operator to run something that does not
    exist — which is how `usher rotate-secret` would have read in any document
    written a week before it landed.

    The floor is the premise. An extraction that matched nothing would report
    every runbook in this directory clean, which is the failure every scan in
    this repository carries a non-emptiness control against.
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
    """PRD 08 asks for four runbooks; this is the line that lets a reader find
    them.

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
    """`--new-key` is a tripwire, not an argument.

    `cli.py` declares it so that typing it is a refusal rather than a leak: it
    was an unambiguous *prefix* of `--new-key-env` until `allow_abbrev=False`
    landed on 2026-08-26, so the operator's key arrived in `argv` — in shell
    history and in `ps` output for every user on the box — through the very
    flag that exists to keep it out. A runbook is the most-pasted text this
    project ships, so the spelling must not appear on a command line in one.

    Scoped to `uv run usher …` lines rather than to the whole file, because
    `docs/runbooks/rotation.md` names `--new-key` in prose precisely to say it
    is refused, and a check that could not tell those apart would be asking a
    document to stop warning about the thing it warns about.
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
