"""Refuse an edit that adds Python prose past the convention.

A **ratchet**: a file already over a cap stays editable, and only a violation
the edit introduces is refused. Without that this guard would refuse every file
in the cleanup that produced it. Violations are keyed by content rather than by
line number, so prose that merely moves is not read as prose that was added.
"""

import ast
import io
import json
import os
import re
import sys
import tokenize

MODULE_DOCSTRING = 3
DEF_DOCSTRING = 20
COMMENT_BLOCK = 5

_HEX = r"(?<![\w/])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{%d}(?![\w])"

FORBIDDEN = (
    (re.compile(r"\b20\d\d-\d\d-\d\d\b"), "a date"),
    (re.compile(_HEX % 7), "a commit sha"),
    (re.compile(_HEX % 40), "a commit sha"),
    (re.compile(r"\bre-?measured\b|\bmeasured\b", re.IGNORECASE), "a measurement narrative"),
    (re.compile(r"\bADR-\d{4}\b|decisions/\d{4}-"), "an ADR citation"),
    (re.compile("\U0001f534"), "a red-circle marker"),
)

SKIP = ("/docs/", "/.claude/", "/web/", "/node_modules/", "/.venv/", "/migrations/versions/")


def _comment_blocks(source: str) -> list[tuple[str, int]]:
    """Each run of consecutive whole-line comments, as (first line, length)."""
    lines = source.splitlines()
    blocks: list[tuple[str, int]] = []
    run: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            run.append(stripped)
        else:
            if run:
                blocks.append((run[0], len(run)))
            run = []
    if run:
        blocks.append((run[0], len(run)))
    return blocks


TRIPLE = re.compile(r"^[A-Za-z]{0,2}('''|\"\"\")")


def _prose(source: str) -> list[str]:
    """Every comment and triple-quoted string in the file, as raw text.

    Matching the token rather than its line is what makes a raw docstring
    visible; comparing `line.lstrip()[:3]` left every one of them unguarded.
    Every triple-quoted string counts, not only docstrings, because a SQL
    statement's own `--` comments are prose too.
    """
    out: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                out.append(token.string)
            elif token.type == tokenize.STRING and TRIPLE.match(token.string):
                out.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def violations(source: str) -> set[str] | None:
    """Every cap breach and forbidden phrase, keyed so two revisions compare."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    found: set[str] = set()

    def measure(node: ast.AST, cap: int, what: str) -> None:
        text = ast.get_docstring(node, clean=False)  # type: ignore[arg-type]
        if text is None:
            return
        length = len(text.splitlines())
        if length > cap:
            found.add(f"{what} docstring is {length} lines (cap {cap})")

    measure(tree, MODULE_DOCSTRING, "the module")
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef):
            measure(node, DEF_DOCSTRING, node.name + "'s")

    for first, length in _comment_blocks(source):
        if length > COMMENT_BLOCK:
            found.add(f"a {length}-line comment block starting {first[:60]!r} (cap {COMMENT_BLOCK})")

    for text in _prose(source):
        for pattern, what in FORBIDDEN:
            for hit in pattern.findall(text):
                found.add(f"{what}: {hit!r}")
    return found


def resolved(call: dict[str, object]) -> tuple[str, str] | None:
    """The file's current and intended contents, or None if this is not ours."""
    tool_input = call.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not raw:
        return None
    path = os.path.normpath(os.path.join(os.environ.get("CLAUDE_PROJECT_DIR") or ".", raw))
    if not path.endswith(".py") or any(part in path for part in SKIP):
        return None

    before = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            before = handle.read()

    if isinstance(tool_input.get("content"), str):
        return before, str(tool_input["content"])
    old = tool_input.get("old_string")
    if not isinstance(old, str) or old not in before:
        return None
    new = tool_input.get("new_string")
    new = new if isinstance(new, str) else ""
    return before, before.replace(old, new, -1 if tool_input.get("replace_all") else 1)


def main() -> int:
    try:
        call = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(call, dict):
        return 0
    pair = resolved(call)
    if pair is None:
        return 0
    was, now = violations(pair[0]), violations(pair[1])
    if was is None or now is None:
        return 0
    added = sorted(now - was)
    if not added:
        return 0
    print(
        "This edit adds prose the convention refuses:\n\n  "
        + "\n  ".join(added)
        + "\n\nCaps: module docstring <= 3 lines, function/class <= 20, comment block <= 5.\n"
        "Forbidden: dates, commit shas, measurement narratives, ADR citations, red-circle\n"
        "markers. Explain why, never what -- the code and the git history tell the rest.\n"
        "Breaches already in the file are tolerated; only what this edit adds is refused.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
