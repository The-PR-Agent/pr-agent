#!/usr/bin/env bash
# Fail if docker/mosaico/ has drifted, either way, from the bundle mirrored at
# gitlab.eclipse.org/eclipse-research-labs/mosaico-project/mosaico-extra/qodo-pr-agent.
# Usage: [--github-dir DIR] [--gitlab-dir DIR] [--ref BRANCH]. Exit: 0 ok, 1 drift, 2 error.
set -uo pipefail

GITHUB_DIR="docker/mosaico"
GITLAB_DIR=""
REF="main"
MIRROR_URL="https://gitlab.eclipse.org/eclipse-research-labs/mosaico-project/mosaico-extra/qodo-pr-agent.git"

# Mirror-only by design. Filtered from BOTH lists, else a copy here reads as GH-ONLY.
MIRROR_ONLY=(".gitignore")

die() { echo "error: $*" >&2; exit 2; }

needval() {
  [[ $# -ge 2 && -n "${2:-}" && "$2" != -* ]] || die "option $1 requires a value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --github-dir) needval "$@"; GITHUB_DIR="$2"; shift 2 ;;
    --gitlab-dir) needval "$@"; GITLAB_DIR="$2"; shift 2 ;;
    --ref)        needval "$@"; REF="$2";        shift 2 ;;
    -h|--help)    sed -n '2,4p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -d "$GITHUB_DIR" ]] || die "github dir not found: $GITHUB_DIR"

SCRATCH=""
cleanup() { [[ -n "$SCRATCH" ]] && rm -rf "$SCRATCH"; }
trap cleanup EXIT

if [[ -z "$GITLAB_DIR" ]]; then
  SCRATCH="$(mktemp -d)" || die "mktemp -d failed"
  GITLAB_DIR="$SCRATCH/mirror"
  clone_mirror() {
    git clone --quiet --depth 1 --branch "$REF" "$MIRROR_URL" "$GITLAB_DIR" \
      2>"$SCRATCH/clone.err"
  }
  # Sent on every request so a bad token fails the run; git would never consult a
  # credential helper against a public repo, leaving a stale secret looking healthy.
  if [[ -n "${GITLAB_TOKEN:-}" ]]; then
    auth_basic="$(printf 'oauth2:%s' "$GITLAB_TOKEN" | base64 | tr -d '\n')" \
      || die "could not encode the credential for the Authorization header"
    [[ -n "$auth_basic" ]] \
      || die "credential encoded to an empty string; refusing to send a malformed Authorization header"
    (
      export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
      export GIT_CONFIG_VALUE_0="Authorization: Basic $auth_basic"
      clone_mirror
    )
  else
    clone_mirror
  fi
  clone_status=$?
  if (( clone_status != 0 )); then
    echo "--- clone stderr ---" >&2
    sed -E 's#://[^@]*@#://***@#g; s#glpat-[A-Za-z0-9._-]+#***#g; s#([Aa]uthorization:).*#\1 ***#g' \
      "$SCRATCH/clone.err" >&2
    die "could not clone the mirror at ref '$REF'"
  fi
fi

[[ -d "$GITLAB_DIR" ]] || die "gitlab dir not found: $GITLAB_DIR"

# Must stay a pipeline source: a bash variable cannot hold NUL.
emit_raw() {
  local dir="$1"
  if git -C "$dir" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$dir" ls-files -z
  else
    (cd "$dir" && find . \( -type f -o -type l \) -not -path './.git/*' -print0)
  fi
}

list_files() {
  local dir="$1" n_entries n_lines status
  n_entries="$(emit_raw "$dir" | tr -cd '\0' | wc -c | tr -d ' ')" \
    || die "could not list files in '$dir'"
  n_lines="$(emit_raw "$dir" | tr '\0' '\n' | grep -c '')"; status=$?
  (( status <= 1 )) || die "could not count entries in '$dir'"
  [[ "$n_lines" == "$n_entries" ]] \
    || die "a filename in '$dir' contains a newline; this comparison is line-based and cannot represent it safely"
  emit_raw "$dir" | tr '\0' '\n' | sed 's#^\./##' | LC_ALL=C sort \
    || die "could not list files in '$dir'"
}

drop_allowlisted() {
  local list="$1" f status
  for f in "${MIRROR_ONLY[@]}"; do
    list="$(printf '%s\n' "$list" | grep -vxF "$f")"; status=$?
    (( status <= 1 )) || die "could not filter '$f' out of the file list"
  done
  printf '%s\n' "$list"
}

# die() inside a command substitution kills only the subshell, so propagate every status.
gh_raw="$(list_files "$GITHUB_DIR")" || exit $?
gl_raw="$(list_files "$GITLAB_DIR")" || exit $?
gh_files="$(drop_allowlisted "$gh_raw")" || exit $?
gl_files="$(drop_allowlisted "$gl_raw")" || exit $?

drift=0
report() { printf '  %-8s %s\n' "$1" "$2"; }

echo "Comparing  $GITHUB_DIR  <->  mirror ref '$REF'"
echo

only_gh="$(LC_ALL=C comm -23 <(printf '%s\n' "$gh_files") <(printf '%s\n' "$gl_files"))" \
  || die "could not compare the two file lists (GitHub-only set)"
only_gl="$(LC_ALL=C comm -13 <(printf '%s\n' "$gh_files") <(printf '%s\n' "$gl_files"))" \
  || die "could not compare the two file lists (mirror-only set)"

if [[ -n "$only_gh" ]]; then
  drift=1
  while IFS= read -r f; do [[ -n "$f" ]] && report "GH-ONLY" "$f"; done <<<"$only_gh"
fi
if [[ -n "$only_gl" ]]; then
  drift=1
  while IFS= read -r f; do [[ -n "$f" ]] && report "GL-ONLY" "$f"; done <<<"$only_gl"
fi

shared="$(LC_ALL=C comm -12 <(printf '%s\n' "$gh_files") <(printf '%s\n' "$gl_files"))" \
  || die "could not compare the two file lists (shared set)"
n_total=0
n_same=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  n_total=$((n_total + 1))
  gh_p="$GITHUB_DIR/$f"; gl_p="$GITLAB_DIR/$f"
  if [[ ! -e "$gh_p" && ! -L "$gh_p" ]]; then
    drift=1; report "MISSING" "$f (tracked on GitHub side, absent from the working tree)"; continue
  fi
  if [[ ! -e "$gl_p" && ! -L "$gl_p" ]]; then
    drift=1; report "MISSING" "$f (tracked on the mirror, absent from its working tree)"; continue
  fi
  # Compare targets, not content: diff dereferences and would pass a repointed link.
  if [[ -L "$gh_p" || -L "$gl_p" ]]; then
    if [[ ! -L "$gh_p" || ! -L "$gl_p" ]]; then
      drift=1
      report "DIFFER" "$f (symlink on one side, regular file on the other)"
    elif ! readlink "$gh_p" >/dev/null; then
      die "could not read the symlink '$f' on the GitHub side"
    elif ! readlink "$gl_p" >/dev/null; then
      die "could not read the symlink '$f' on the mirror"
    # printf x keeps a target ending in a newline distinct. Linux-verified; on macOS
    # readlink collapses it, so a local run cannot show why this is here.
    elif [[ "$(readlink "$gh_p"; printf x)" == "$(readlink "$gl_p"; printf x)" ]]; then
      report "same" "$f"
      n_same=$((n_same + 1))
    else
      drift=1
      gh_t="$(readlink "$gh_p"; printf x)"; gh_t="${gh_t%x}"; gh_t="${gh_t%$'\n'}"
      gl_t="$(readlink "$gl_p"; printf x)"; gl_t="${gl_t%x}"; gl_t="${gl_t%$'\n'}"
      report "DIFFER" "$f (symlink target: $(printf '%q' "$gh_t") vs $(printf '%q' "$gl_t"))"
    fi
    continue
  fi
  diff_out="$(diff -u "$gh_p" "$gl_p" --label "github/$GITHUB_DIR/$f" --label "gitlab/$f" 2>&1)"
  d_status=$?
  if (( d_status == 0 )); then
    if [[ -x "$gh_p" && ! -x "$gl_p" ]]; then
      drift=1
      report "DIFFER" "$f (executable here, not on the mirror)"
    elif [[ ! -x "$gh_p" && -x "$gl_p" ]]; then
      drift=1
      report "DIFFER" "$f (executable on the mirror, not here)"
    else
      report "same" "$f"
      n_same=$((n_same + 1))
    fi
  elif (( d_status > 1 )); then
    die "diff failed on '$f' (exit $d_status); cannot tell whether it differs: $diff_out"
  else
    drift=1
    report "DIFFER" "$f"
    printf '%s\n' "$diff_out" | sed 's/^/    /'
  fi
done <<<"$shared"

echo
if [[ $drift == 0 ]]; then
  if [[ $n_total == 0 ]]; then
    die "compared 0 files — the bundle should never be empty; check --github-dir/--gitlab-dir"
  fi
  echo "PARITY OK — $n_same/$n_total files identical."
  exit 0
fi

cat >&2 <<'EOF'
PARITY FAILED — docker/mosaico/ and the GitLab mirror have diverged.

Resolve by direction:
  * Change originated here (the normal case): port it to the mirror. Upstream is canonical,
    so the mirror is brought up to this repo, never the reverse.
  * Change originated on the mirror: port it back here FIRST, then re-sync the mirror, so
    the fix is not stranded outside upstream. The bundle README says never to edit the
    mirror directly, and a GL-ONLY line above means someone did.
EOF
exit 1
