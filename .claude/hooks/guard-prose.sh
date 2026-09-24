#!/bin/sh
# PreToolUse hook for Edit|Write: the prose convention, enforced as a ratchet.
# Contract is guard-generated.sh's -- JSON on stdin, exit 2 blocks and hands
# stderr to the model, anything else falls open. The work is in guard_prose.py.
exec python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/guard_prose.py"
