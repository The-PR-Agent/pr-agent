#!/usr/bin/env bash
# PostToolUse(Write|Edit): a prompt/config TOML under pr_agent/settings/ that is not listed in
# settings_files=[...] in pr_agent/config_loader.py is never loaded into global_settings, and
# fails silently at runtime. Warn as soon as such a file is written.
set -uo pipefail

payload=$(cat)
file_path=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty')
project_dir="${CLAUDE_PROJECT_DIR:-$(printf '%s' "$payload" | jq -r '.cwd // empty')}"

[ -n "$file_path" ] || exit 0

# Normalise to a path relative to pr_agent/, which is how config_loader.py spells its entries.
case "$file_path" in
  */pr_agent/settings/*.toml) rel="settings/${file_path##*/pr_agent/settings/}" ;;
  pr_agent/settings/*.toml)   rel="settings/${file_path#pr_agent/settings/}" ;;
  *) exit 0 ;;
esac

# .secrets.toml entries are already listed and are gitignored; nothing to check.
case "$rel" in */.secrets.toml) exit 0 ;; esac

loader="$project_dir/pr_agent/config_loader.py"
[ -f "$loader" ] || exit 0

if ! grep -qF "\"$rel\"" "$loader"; then
  cat >&2 <<EOF
Settings file not registered: $rel

pr_agent/config_loader.py builds Dynaconf from an explicit settings_files=[...] list. This TOML
is not in it, so get_settings() will never expose its keys and the failure is silent at runtime.

Fix: add "$rel" to the settings_files list in pr_agent/config_loader.py
(see AGENTS.md, "Prompt Building"). Ignore this only if the file is deliberately loaded
elsewhere, such as an eval-only template.
EOF
  exit 2
fi

exit 0
