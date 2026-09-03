#!/bin/sh
# SessionStart hook: say the one thing a fresh checkout gets wrong.
#
# Plain stdout from a SessionStart hook is added to the session's context, so
# this prints nothing when the worktree is ready and one line when it is not.
# The gate needs the `eval` extra (`ranx`); without it `uv run pytest` aborts
# at collection with EvalDependencyMissing, which reads as a broken suite
# rather than a missing install. CLAUDE.md, "The gate", is the record.
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

if [ ! -x .venv/bin/python ]; then
  echo "This worktree has no .venv yet: run \`uv sync --extra eval\` before the gate (CLAUDE.md, The gate)."
elif ! .venv/bin/python -c 'import ranx' >/dev/null 2>&1; then
  echo "The eval extra is not installed in this worktree's .venv, so \`uv run pytest\` will abort at collection with EvalDependencyMissing. Run \`uv sync --extra eval\` (CLAUDE.md, The gate)."
fi
exit 0
