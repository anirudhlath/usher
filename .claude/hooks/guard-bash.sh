#!/usr/bin/env bash
# PreToolUse(Bash): refuse the three command families CLAUDE.md forbids, on any
# spelling — destructive git, `ruff format` over prose, and venv activation.
#
# `.claude/settings.json`'s `deny` list is prefix matching, so it closes exactly
# the spellings someone thought to enumerate. It cannot see a flag inserted
# between the subcommand and its path (`ruff format --line-length 100 docs/`),
# a different launcher (`python -m ruff`, `uvx ruff`, `.venv/bin/ruff`), a
# quoted or absolute path, or `git -C . checkout .`. Every one of those was
# demonstrated against the enumerated list on 2026-09-02. This hook reads the
# whole command instead, so the check is on what the command *does*.
#
# It reads each *statement* separately, which is the part that is easy to get
# wrong: a first version matched against newline-collapsed text anchored on
# `^` and `[;&|]`, and so caught `cd src && git clean -fd` while letting
# `cd src<newline>git clean -fd` through. Seven of its eight git families were
# open that way. Test any change against statement position, not just flags.
#
# Exit 2 returns stderr to the model and blocks the call; exit 0 allows it.
# Deliberately narrow: anything it does not match is settings.json's problem.
# It is not a sandbox — `eval "$(printf ...)"` defeats it by construction.
exec python3 -c '
import json, os, re, shlex, sys

try:
    command = json.load(sys.stdin).get("tool_input", {}).get("command") or ""
except Exception:
    sys.exit(0)          # unparseable stdin is settings.json problem, not ours

# A destructive call is destructive wherever it sits: after a newline, inside
# `if ...; then`, in a loop body. Splitting on the separators AND on the
# keywords that introduce a statement is what makes position stop mattering.
CLAUSE = re.compile(r"[;&|\n]+|\b(?:then|else|elif|do|done|fi)\b|[(){}]")

# Words that can sit in front of the real command without changing it.
PREFIX = {"if", "while", "until", "for", "!", "time", "sudo", "command",
          "builtin", "exec", "env", "nohup", "then", "else", "do"}
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
PROSE = re.compile(r"(?:^|/)(?:docs|\.claude)(?:/|$)")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}


def clauses(text):
    for raw in CLAUSE.split(text):
        try:
            words = shlex.split(raw)
        except ValueError:
            words = raw.split()
        while words and (words[0] in PREFIX or ASSIGNMENT.match(words[0])):
            words = words[1:]
        if words:
            yield words


def git_call(words):
    """The verb and its arguments if this clause runs git, else None."""
    if os.path.basename(words[0]) != "git":
        return None
    rest = words[1:]
    while rest:
        if rest[0] in ("-C", "-c") and len(rest) > 1:
            rest = rest[2:]
        elif rest[0].startswith("-"):
            rest = rest[1:]
        else:
            break
    return (rest[0], rest[1:]) if rest else None


def names_a_path(args):
    root = os.environ.get("CLAUDE_PROJECT_DIR") or "."
    for arg in args:
        if arg.startswith("-"):
            continue
        if os.path.exists(arg) or os.path.exists(os.path.join(root, arg)):
            return True
    return False


def destructive(verb, args):
    if verb in ("restore", "stash", "clean", "checkout-index"):
        return "git " + verb
    if verb == "reset" and "--soft" not in args:
        return "git reset"
    if verb == "switch" and "--discard-changes" in args:
        return "git switch --discard-changes"
    if verb == "worktree" and args[:1] == ["remove"] and {"--force", "-f"} & set(args):
        return "git worktree remove --force"
    if verb == "checkout":
        # A branch switch and a discard are the same verb, and only the
        # filesystem tells them apart -- so ask it, rather than guessing from a
        # slash (`git checkout -b feature/x` is not a discard).
        if "--" in args:
            return "git checkout -- <path>"
        if not ({"-b", "-B", "--orphan"} & set(args)) and names_a_path(args):
            return "git checkout of a path"
    return None


def ruff_formats_prose(words):
    # ruff formats Python fences inside Markdown by default (verified on ruff
    # 0.16.0), and `[tool.ruff] extend-exclude` is bypassed by an explicit path
    # argument. `--check` and `--diff` write nothing, so they stay allowed.
    found = [i for i, w in enumerate(words) if os.path.basename(w) == "ruff"]
    if not found:
        return False
    args = words[found[0] + 1:]
    if "format" not in args or "--check" in args or "--diff" in args:
        return False
    return any(PROSE.search(a) for a in args if not a.startswith("-"))


def activates_a_venv(words):
    return (words[0] in (".", "source") and len(words) > 1
            and re.search(r"(?:venv|env)/bin/activate", words[1]))


def refuse(message):
    print(message, file=sys.stderr)
    sys.exit(2)


def scan(text, depth=0):
    for words in clauses(text):
        call = git_call(words)
        if call and destructive(*call):
            refuse(
                "Blocked: {} discards uncommitted work, not just the thing you "
                "meant to undo.\n"
                "CLAUDE.md, \"Conventions that will bite you\": every plant gets "
                "a `cp` backup and the restore is verified by reading the file "
                "back. A suite green before a plant is green again after a "
                "revert that took twenty unrelated lines with it (M8 Task 10).\n"
                "Undo by restoring your backup, or `git commit` first if you "
                "want a checkpoint. `git reset --soft` is allowed."
                .format(destructive(*call))
            )
        if ruff_formats_prose(words):
            refuse(
                "Blocked: `ruff format` rewrites Python code fences inside "
                "Markdown, and `docs/` and `.claude/` are prose whose fences "
                "other groups transcribe verbatim.\n"
                "`[tool.ruff] extend-exclude` does NOT protect them here -- the "
                "exclude is bypassed by an explicit path argument.\n"
                "Scope the command to real Python (`uv run ruff format src "
                "tests`), or use `--check` if you only wanted to look."
            )
        if activates_a_venv(words):
            refuse(
                "Blocked: this project never activates a venv -- `uv run <cmd>` "
                "instead (CLAUDE.md, \"Conventions that will bite you\").\n"
                "An activation only lasts for the one Bash call anyway, since "
                "each call gets its own shell, so the next command would "
                "silently run outside it."
            )
        # `bash -c "git clean -fd"` hides a statement inside an argument. Only
        # recurse for a real shell launcher: recursing into every quoted string
        # would block `git commit -m "git reset is bad"`.
        if depth < 2 and os.path.basename(words[0]) in SHELLS and "-c" in words:
            index = words.index("-c")
            if index + 1 < len(words):
                scan(words[index + 1], depth + 1)


scan(command)
sys.exit(0)
'
