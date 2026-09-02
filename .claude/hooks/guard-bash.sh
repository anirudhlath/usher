#!/usr/bin/env bash
# PreToolUse(Bash): refuse the two command families CLAUDE.md forbids, on any
# spelling.
#
# `.claude/settings.json`'s `deny` list is prefix matching, so it closes exactly
# the spellings someone thought to enumerate. It cannot see a flag inserted
# between the subcommand and its path (`ruff format --line-length 100 docs/`),
# a different launcher (`python -m ruff`, `uvx ruff`, `.venv/bin/ruff`), a
# quoted or absolute path, or `git -C . checkout .`. Every one of those was
# demonstrated against the enumerated list on 2026-09-02. This hook reads the
# whole command instead, so the check is on what the command *does*.
#
# Exit 2 returns stderr to the model and blocks the call; exit 0 allows it.
# Deliberately narrow: it refuses two families and says why. It is not a second
# permission system, and anything it does not match is settings.json's problem.
exec python3 -c '
import json, re, sys

try:
    command = json.load(sys.stdin).get("tool_input", {}).get("command") or ""
except Exception:
    sys.exit(0)          # unparseable stdin is settings.json'"'"'s problem, not ours

text = " ".join(command.split())

# `git`, then any global flags (-C <dir>, -c k=v, --git-dir=...), then the verb.
GIT = r"(?:^|[;&|]|\$\()\s*git(?:\s+(?:-[cC]\s*\S+|--\S+))*\s+"

DESTRUCTIVE = (
    (rf"{GIT}checkout\b[^;&|]*\s--\s", "git checkout -- <path>"),
    (rf"{GIT}checkout-index\b", "git checkout-index"),
    (rf"{GIT}restore\b", "git restore"),
    (rf"{GIT}stash\b", "git stash"),
    (rf"{GIT}reset\b(?![^;&|]*--soft)", "git reset"),
    (rf"{GIT}clean\b", "git clean"),
    (rf"{GIT}switch\b[^;&|]*--discard-changes", "git switch --discard-changes"),
    (rf"{GIT}worktree\s+remove\b[^;&|]*(?:--force|-f)", "git worktree remove --force"),
)

# `git checkout <thing>` is a branch switch or a discard depending on whether
# <thing> names a path, and only the filesystem can tell them apart -- so ask it,
# rather than guessing from a slash (`git checkout -b feature/x` is not a
# discard). Branch-creating flags settle it without a lookup.
def checkout_targets_a_path(cmd: str) -> bool:
    import os, shlex
    for clause in re.split(r"[;&|]+", cmd):
        try:
            words = shlex.split(clause)
        except ValueError:
            continue
        if "git" not in words:
            continue
        words = words[words.index("git") + 1:]
        if "checkout" not in words:
            continue
        rest = words[words.index("checkout") + 1:]
        if {"-b", "-B", "--orphan"} & set(rest):
            continue
        if any(arg != "-" and not arg.startswith("-") and os.path.exists(arg) for arg in rest):
            return True
    return False


if checkout_targets_a_path(text):
    DESTRUCTIVE = ((r"(?s).*", "git checkout of a path"),) + DESTRUCTIVE

for pattern, name in DESTRUCTIVE:
    if re.search(pattern, text):
        print(
            f"Blocked: {name} discards uncommitted work, not just the thing you "
            "meant to undo.\n"
            "CLAUDE.md, \"Conventions that will bite you\": every plant gets a `cp` "
            "backup and the restore is verified by reading the file back. A suite "
            "green before a plant is green again after a revert that took twenty "
            "unrelated lines with it (M8 Task 10).\n"
            "Undo by restoring your backup, or `git commit` first if you want a "
            "checkpoint. `git reset --soft` is allowed.",
            file=sys.stderr,
        )
        sys.exit(2)

# ruff formats Python fences inside Markdown by default (verified on ruff
# 0.16.0), and `[tool.ruff] extend-exclude` is bypassed by an explicit path
# argument. `--check` and `--diff` write nothing, so they stay allowed.
if re.search(r"\bruff\b[^;&|]*\bformat\b", text) and not re.search(
    r"\bformat\b[^;&|]*(?:--check|--diff)", text
):
    if re.search(r"(?:^|[\s\"'"'"'=/])(?:docs|\.claude)(?:[/\s\"'"'"']|$)", text):
        print(
            "Blocked: `ruff format` rewrites Python code fences inside Markdown, "
            "and `docs/` and `.claude/` are prose whose fences other groups "
            "transcribe verbatim.\n"
            "`[tool.ruff] extend-exclude` does NOT protect them here -- the "
            "exclude is bypassed by an explicit path argument.\n"
            "Scope the command to real Python (`uv run ruff format src tests`), or "
            "use `--check` if you only wanted to look.",
            file=sys.stderr,
        )
        sys.exit(2)

if re.search(r"\bsource\s+\S*(?:venv|env)/bin/activate", text) or re.search(r"(?:^|[;&|]\s*)\.\s+\S*(?:venv|env)/bin/activate", text):
    print(
        "Blocked: this project never activates a venv -- `uv run <cmd>` instead "
        "(CLAUDE.md, \"Conventions that will bite you\").\n"
        "An activation only lasts for the one Bash call anyway, since each call "
        "gets its own shell, so the next command would silently run outside it.",
        file=sys.stderr,
    )
    sys.exit(2)

sys.exit(0)
'
