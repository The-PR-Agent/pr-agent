#!/usr/bin/env bash
# Fail if docker/mosaico/ has drifted from the MOSAICO deployment bundle published at
# gitlab.eclipse.org/eclipse-research-labs/mosaico-project/mosaico-extra/qodo-pr-agent.
#
# That GitLab repo is a *mirror*, not a fork: it holds the same deployment assets at its
# root, with zero Python. Upstream here is canonical and edits flow GitHub -> GitLab, but
# drift is detected in BOTH directions on purpose — a consortium member editing the mirror
# directly is exactly the case that silently strands a fix outside this repo.
#
#   GitHub  docker/mosaico/<f>   <->   GitLab  <f>   (bundle lives at the mirror's root)
#
# Usage:
#   mosaico_bundle_parity.sh                      # fetch the mirror, compare against HEAD
#   mosaico_bundle_parity.sh --gitlab-dir DIR     # compare against an already-fetched copy
#   mosaico_bundle_parity.sh --github-dir DIR     # override the local side
#   mosaico_bundle_parity.sh --ref BRANCH         # mirror ref to compare (default: main)
#
# Auth: none. The mirror is a public project, so the clone is anonymous and every run really
# compares — including fork PRs, which used to be exempted from this check for the sole
# reason that forks receive no secrets. Set GITLAB_TOKEN to a token with read_repository
# only if the mirror is ever made private; unset or empty takes the anonymous path.
#
# A token that IS supplied must work. It is sent on every request and a rejected one fails
# the run — there is no fallback to anonymous, which on a public mirror would "succeed" and
# leave a stale or misconfigured secret looking healthy indefinitely.
#
# There is deliberately no way to exit 0 without having compared the two trees. A clone that
# fails is exit 2, never a skip: "could not verify" must not be able to read as "in parity".
#
# Exit: 0 parity, 1 drift, 2 usage/fetch error.
set -uo pipefail

GITHUB_DIR="docker/mosaico"
GITLAB_DIR=""
REF="main"
MIRROR_URL="https://gitlab.eclipse.org/eclipse-research-labs/mosaico-project/mosaico-extra/qodo-pr-agent.git"

# Tracked on the mirror but deliberately absent from docker/mosaico/. The mirror is a
# standalone repo, so it carries its own .gitignore (it ignores the .env a deployer creates
# from pr-agent.env.example); upstream's root .gitignore already covers that path here.
# Anything NOT listed here must exist on both sides — that is what catches a new file added
# to one side only. Filtered from BOTH lists: an allowlisted name is "not part of the
# comparison", so its presence on either side is equally uninteresting. Filtering only the
# mirror would make an upstream copy of .gitignore report as GH-ONLY on identical trees.
MIRROR_ONLY=(".gitignore")

die() { echo "error: $*" >&2; exit 2; }

# Options take a value, so a missing one is a usage error (exit 2). Relying on bash's
# ${2:?} here would abort with exit 1, which this script defines as "drift found" — a
# mistyped flag would be indistinguishable from a real divergence.
needval() { [[ $# -ge 2 && -n "${2:-}" ]] || die "option $1 requires a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --github-dir) needval "$@"; GITHUB_DIR="$2"; shift 2 ;;
    --gitlab-dir) needval "$@"; GITLAB_DIR="$2"; shift 2 ;;
    --ref)        needval "$@"; REF="$2";        shift 2 ;;
    # Header comment block only; the line after it is `set -uo pipefail`, not documentation.
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -d "$GITHUB_DIR" ]] || die "github dir not found: $GITHUB_DIR"

SCRATCH=""
cleanup() { [[ -n "$SCRATCH" ]] && rm -rf "$SCRATCH"; }
trap cleanup EXIT

# --- obtain the mirror side -------------------------------------------------------------
if [[ -z "$GITLAB_DIR" ]]; then
  SCRATCH="$(mktemp -d)" || die "mktemp -d failed"
  GITLAB_DIR="$SCRATCH/mirror"
  clone_mirror() {
    git clone --quiet --depth 1 --branch "$REF" "$MIRROR_URL" "$GITLAB_DIR" \
      2>"$SCRATCH/clone.err"
  }
  # The mirror is a public project, so the default path carries no credentials at all and
  # needs no secret configured anywhere. An empty GITLAB_TOKEN counts as absent rather than
  # being passed through — an empty password would fail the very clone that succeeds
  # anonymously.
  #
  # A token that IS supplied has to be exercised, not merely offered. Git only consults a
  # credential helper after a 401, and a public project never returns one, so a wrong or
  # expired token would go unused and unnoticed: the clone would succeed anonymously and the
  # operator would keep believing authentication works. That is the silent-green failure
  # this check exists to avoid, wearing a different hat. Sending the header on every request
  # forces the server to judge it — GitLab answers 401 to bad credentials even on a public
  # project (verified) — so a broken token fails the run instead of degrading quietly.
  # There is deliberately no fallback to anonymous here: whoever supplied a credential is
  # entitled to be told it is broken.
  #
  # The header travels in GIT_CONFIG_* rather than `git -c`, keeping the secret out of argv
  # where any local process could read it off the process list. `printf` is a bash builtin,
  # so the token never becomes a process argument there either.
  if [[ -n "${GITLAB_TOKEN:-}" ]]; then
    (
      export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
      export GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'oauth2:%s' "$GITLAB_TOKEN" | base64 | tr -d '\n')"
      clone_mirror
    )
  else
    clone_mirror
  fi
  clone_status=$?
  if (( clone_status != 0 )); then
    echo "--- clone stderr ---" >&2
    # Defensive: no credential should ever appear here, but strip anything credential-shaped
    # anyway rather than trust that and echo a secret into a public log. The Authorization
    # rule matters most — that header carries the base64 of the token, which is reversible.
    sed -E 's#://[^@]*@#://***@#g; s#glpat-[A-Za-z0-9._-]+#***#g; s#([Aa]uthorization:).*#\1 ***#g' \
      "$SCRATCH/clone.err" >&2
    # Not a skip. Failing to reach the mirror means parity is unknown, and unknown is an
    # error here - the whole point of dropping the old SKIP branch.
    die "could not clone the mirror at ref '$REF'"
  fi
fi

[[ -d "$GITLAB_DIR" ]] || die "gitlab dir not found: $GITLAB_DIR"

# --- build both file sets ---------------------------------------------------------------
# Ask git what is tracked (authoritative, and `git -C <dir> ls-files` already emits paths
# relative to <dir>, so no prefix stripping is needed — and no path-spelling like
# "./docker/mosaico" or a trailing slash can desynchronise the two lists). Fall back to a
# plain listing so the script also works against an exported, non-git directory.
# Symlinks are included in the fallback: the bundle has none today, but a symlink appearing
# on one side only is drift, and -type f alone would silently ignore it.
#
# Both backends are read NUL-delimited. Plain `git ls-files` renders a non-ASCII or
# quote-bearing name as a C-style quoted literal ("caf\303\251.md"), which is not a path
# that exists on disk — the existence check below would then miss it and report false drift
# on identical trees. `-z` emits raw bytes and sidesteps the quoting entirely. Note that
# core.quotePath=off is NOT sufficient: it unquotes non-ASCII but still escapes " and \.
# Emits raw NUL-delimited names. Must stay a pipeline source: a bash variable cannot hold
# NUL, so capturing this with $(...) would silently discard every delimiter.
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
  # pipefail carries an emit_raw failure out to each pipeline below; turn it into exit 2.
  # A listing that failed partway - an unreadable subdirectory under the `find` fallback,
  # say - still prints the entries it did reach, and every name missing from that short list
  # then reads as a one-sided file, i.e. drift. Unchecked, an operational error reports as
  # exit 1, the code reserved for a genuine divergence.
  n_entries="$(emit_raw "$dir" | tr -cd '\0' | wc -c | tr -d ' ')" \
    || die "could not list files in '$dir'"
  # `grep -c ''` exits 1 on empty input. That is an empty listing, not a failure: a real
  # emit_raw error already became exit 2 on the line above, and an empty bundle is caught by
  # the "compared 0 files" guard at the end, which words it far better. Above 1 is grep
  # itself failing, which must not pass for a count of zero - hence the range test rather
  # than a blanket `|| true`.
  n_lines="$(emit_raw "$dir" | tr '\0' '\n' | grep -c '')"; status=$?
  (( status <= 1 )) || die "could not count entries in '$dir'"
  # Everything downstream (comm, the read loops) is line-based, so a filename containing a
  # literal newline cannot be compared correctly. Refuse rather than emit a confident wrong
  # verdict; the bundle has no such name, so this is a guard, not a workflow.
  [[ "$n_lines" == "$n_entries" ]] \
    || die "a filename in '$dir' contains a newline; this comparison is line-based and cannot represent it safely"
  emit_raw "$dir" | tr '\0' '\n' | sed 's#^\./##' | LC_ALL=C sort \
    || die "could not list files in '$dir'"
}

drop_allowlisted() {
  local list="$1" f status
  for f in "${MIRROR_ONLY[@]}"; do
    # grep exits 1 when it selects nothing, which here means the allowlisted name was the
    # only entry left - a legitimate empty list. Above 1 is grep failing, and swallowing
    # that would hand back a truncated list whose missing names then read as one-sided.
    list="$(printf '%s\n' "$list" | grep -vxF "$f")"; status=$?
    (( status <= 1 )) || die "could not filter '$f' out of the file list"
  done
  printf '%s\n' "$list"
}

# Both helpers run in a command substitution, so a die() inside one only kills that
# subshell. Without propagating the status here the script would carry on with an empty file
# list and report the resulting phantom differences as drift (exit 1) instead of the real
# error. Every call below needs the `|| exit $?`, not just the list_files pair.
gh_raw="$(list_files "$GITHUB_DIR")" || exit $?
gl_raw="$(list_files "$GITLAB_DIR")" || exit $?
gh_files="$(drop_allowlisted "$gh_raw")" || exit $?
gl_files="$(drop_allowlisted "$gl_raw")" || exit $?

drift=0
report() { printf '  %-8s %s\n' "$1" "$2"; }

echo "Comparing  $GITHUB_DIR  <->  mirror ref '$REF'"
echo

only_gh="$(LC_ALL=C comm -23 <(printf '%s\n' "$gh_files") <(printf '%s\n' "$gl_files"))"
only_gl="$(LC_ALL=C comm -13 <(printf '%s\n' "$gh_files") <(printf '%s\n' "$gl_files"))"

if [[ -n "$only_gh" ]]; then
  drift=1
  while IFS= read -r f; do [[ -n "$f" ]] && report "GH-ONLY" "$f"; done <<<"$only_gh"
fi
if [[ -n "$only_gl" ]]; then
  drift=1
  while IFS= read -r f; do [[ -n "$f" ]] && report "GL-ONLY" "$f"; done <<<"$only_gl"
fi

# --- byte-compare everything present on both sides ---------------------------------------
shared="$(LC_ALL=C comm -12 <(printf '%s\n' "$gh_files") <(printf '%s\n' "$gl_files"))"
n_total=0
n_same=0
while IFS= read -r f; do
  [[ -z "$f" ]] && continue
  n_total=$((n_total + 1))
  gh_p="$GITHUB_DIR/$f"; gl_p="$GITLAB_DIR/$f"
  # A file git still tracks but that is gone from the working tree (an uncommitted delete)
  # is neither "same" nor a content difference; diff would just emit a bare "No such file"
  # to stderr in the middle of the report. Name the real condition instead.
  # -e follows the link, so a dangling symlink would read as absent; -L catches the entry
  # itself, which is what is actually being compared.
  if [[ ! -e "$gh_p" && ! -L "$gh_p" ]]; then
    drift=1; report "MISSING" "$f (tracked on GitHub side, absent from the working tree)"; continue
  fi
  if [[ ! -e "$gl_p" && ! -L "$gl_p" ]]; then
    drift=1; report "MISSING" "$f (tracked on the mirror, absent from its working tree)"; continue
  fi
  # Symlinks are compared by target, not by the bytes they resolve to. `diff` dereferences,
  # so a link repointed on the mirror would read as identical whenever both targets happen
  # to hold the same content - drift the mirror is supposed to surface, silently passing.
  if [[ -L "$gh_p" || -L "$gl_p" ]]; then
    if [[ ! -L "$gh_p" || ! -L "$gl_p" ]]; then
      drift=1
      report "DIFFER" "$f (symlink on one side, regular file on the other)"
    # The trailing `printf x` keeps a target string that ends in a newline distinguishable:
    # command substitution strips trailing newlines, so 'a' and 'a\n' would otherwise be
    # reported as the same link. Verified on Linux, the CI platform, where dropping the
    # `printf x` does make the two compare equal. It is untestable on macOS, whose readlink
    # collapses the trailing newline before the shell ever sees it - so do not "simplify"
    # this away on the strength of a local run there.
    elif [[ "$(readlink "$gh_p"; printf x)" == "$(readlink "$gl_p"; printf x)" ]]; then
      report "same" "$f"
      n_same=$((n_same + 1))
    else
      drift=1
      report "DIFFER" "$f (symlink target: '$(readlink "$gh_p")' vs '$(readlink "$gl_p")')"
    fi
    continue
  fi
  # diff distinguishes "identical" (0) from "differs" (1) from "diff itself failed" (2+, an
  # unreadable file or an I/O error). Folding 2 in with 1 would book an operational fault as
  # drift and print an empty diff under it, so split the three cases on one status.
  #
  # One invocation, not a `diff -q` probe followed by a `diff -u` to render: two calls means
  # two chances to fail and a second status to drop on the floor. Capturing stderr with the
  # output puts it in the error message instead of discarding it down /dev/null, which is
  # what made this failure mode invisible to begin with. The bundle is a handful of small
  # config files, so holding one diff in a variable costs nothing.
  diff_out="$(diff -u "$gh_p" "$gl_p" --label "github/$GITHUB_DIR/$f" --label "gitlab/$f" 2>&1)"
  d_status=$?
  if (( d_status == 0 )); then
    report "same" "$f"
    n_same=$((n_same + 1))
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
