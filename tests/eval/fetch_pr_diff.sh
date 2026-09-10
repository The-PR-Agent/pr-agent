#!/usr/bin/env bash
# Usage: tests/eval/fetch_pr_diff.sh owner/repo PR_NUMBER out.diff
set -euo pipefail
gh pr diff "$2" --repo "$1" > "$3"
echo "wrote $(wc -l < "$3") lines to $3"
