#!/usr/bin/env bash
# PreToolUse(Bash): AGENTS.md requires PYTHONPATH=. when invoking pytest from the repo root;
# without it collection fails on imports. Deny the bare form and name the corrected command.
set -uo pipefail

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty')

[ -n "$command" ] || exit 0

# Already correct, or exported in the same command: nothing to do.
case "$command" in *PYTHONPATH*) exit 0 ;; esac

# Only fire on an actual invocation (start of the command or after a separator), not on a
# command that merely mentions pytest, such as a grep over test files.
if printf '%s' "$command" | grep -Eq '(^|[;&|]|&&|\|\|)[[:space:]]*(uv run [^;&|]*)?(python[0-9.]* -m )?pytest\b'; then
  cat >&2 <<EOF
pytest invoked without PYTHONPATH=.

This repo requires PYTHONPATH=. when running pytest from the root (AGENTS.md, "Testing
Guidelines"); without it imports of pr_agent/ fail during collection.

Re-run as: PYTHONPATH=. $command
EOF
  exit 2
fi

exit 0
