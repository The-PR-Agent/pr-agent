# Diff-scoped code context for pr-agent's `[artifacts]` slot

A starting point, not a finished tool. `graph_bundle_ts.py` builds a review-context bundle for one PR with tree-sitter, and `instructions.txt` tells the model how to read it. Together they are the setup measured in [The-PR-Agent/pr-agent#2499](https://github.com/The-PR-Agent/pr-agent/issues/2499).

The builder, instructions and original hardening tests were imported from [IsmaelMartinez/pr-agent, `review-context-builder`, commit `3dcc022`](https://github.com/IsmaelMartinez/pr-agent/tree/3dcc02287589916e4b4a72897a208ee72ac1df26/scripts/review_context).

The bundle holds the changed symbols (signature plus the body lines the diff does not show), module constants and callees they use, inbound callers (hub callers ranked, under a cap), sibling implementations and sibling config/prompt/doc files, sites still using a pattern the PR removed, similar code and same-pattern sites, tests that exercise the change, and importers. It is capped at about 12k tokens.

Every language is handled by one class driven by tree-sitter tags queries, covering the 30 languages detected by its extension list with tree-sitter-language-pack 1.22.1 (Python, JavaScript, TypeScript, Go, Java, C, C++, C#, Rust, Ruby, PHP, Kotlin and more). It has only been scored on Python; Go and TypeScript were smoke-tested. `--generic` remains accepted for existing workflow invocations and has no effect; tags queries are always used.

## Run

The pinned builder environment targets CPython 3.12 on Linux x86_64. Install its two wheels with hash checking and verify the versioned grammar archive before extracting it (GNU tar and zstd are required):

```bash
uv venv --python 3.12 /tmp/review-context-venv
uv pip install --python /tmp/review-context-venv/bin/python --require-hashes --only-binary :all: \
  -r scripts/review_context/requirements.txt
mkdir -p /tmp/review-context-grammars
curl --fail --location --max-time 60 \
  https://github.com/xberg-io/tree-sitter-language-pack/releases/download/v1.22.1/parsers-linux-x86_64.tar.zst \
  --output /tmp/review-context-grammars/parsers-linux-x86_64.tar.zst
(cd /tmp/review-context-grammars && sha256sum --check "$OLDPWD/scripts/review_context/grammars.sha256" && \
  tar --zstd --extract --file parsers-linux-x86_64.tar.zst --no-same-owner)
/tmp/review-context-venv/bin/python -I scripts/review_context/graph_bundle_ts.py \
  --repo "$PWD" --base "$BASE_SHA" --head "$HEAD_SHA" --pr "$PR_NUMBER" \
  --out-dir /tmp/review-context --generic --grammar-cache /tmp/review-context-grammars
```

`requirements.txt` pins `tree-sitter-language-pack==1.22.1` and `tree-sitter==0.26.0`, including their wheel hashes. `grammars.sha256` pins the Linux x86_64 grammar bundle from the same language-pack release. Updating a dependency or grammar bundle requires updating the checked-in hashes. No native libraries are restored from an Actions cache.

`--repo` is any clone that holds both commits; the head does not need to be checked out, because every file is read from git. The bundle is written to `<out-dir>/pr<N>.G6.md` and a summary JSON goes to stdout. `--grammar-cache` must point directly to the verified libraries. The builder uses a local `LanguageRegistry`, which never downloads missing grammars; it does not use the package's global auto-downloading loaders. Its `--timeout` covers grammar initialization and building (120 s by default); caught errors print a diagnostic to stderr and exit 2. Bundle writes are atomic. Callers must remove stale output before each run and use the bundle only after a successful exit.

Hand the bundle to pr-agent, here for `/review` only:

```bash
export ARTIFACT_PATH=/tmp/review-context/pr$PR_NUMBER.G6.md
export ARTIFACT_INSTRUCTIONS="$(cat scripts/review_context/instructions.txt)"
export ARTIFACTS__TARGET_COMMANDS='["pr_reviewer"]'
# pr-agent 0.50.0 and earlier read the old name
export ARTIFACTS__TARGET_TOOLS='["pr_reviewer"]'
```

In the GitHub Action, set the same three on the action step's `env` (or its `artifact_path` input), and write the bundle inside `GITHUB_WORKSPACE`, because paths outside it are rejected.

## Untrusted PRs

The builder treats the PR as hostile input:

- It never imports or executes the target repo's code; files are parsed with tree-sitter only.
- It reads only regular files tracked at the head commit, as git blobs (`git ls-tree` plus one `git cat-file --batch`). Symlinks, submodules and untracked files never reach the bundle, so a PR cannot pull in a file from outside the repo.
- It skips files over 1 MB and secret-looking names (`.env*`, `secrets.toml`, `.secrets.toml`, `*.pem`, `*.key`, SSH keys, `.netrc`, `.npmrc`, `.pypirc`, `credentials*`, Terraform state). pr-agent also redacts common credential shapes in artifact content.
- It runs git with `--no-ext-diff --no-textconv`, `core.fsmonitor=false`, `core.hooksPath=/dev/null` and `protocol.allow=never`, so no diff driver, monitor or hook program from a poisoned config runs and a partial clone cannot fetch.
- A file nested too deeply to walk is left out, the whole build has a wall-clock limit, and the output is written atomically, so a failure leaves no partial bundle.

`test_hardening.py` checks these against a hostile repository (symlink out of the tree, untracked file, `.env`, `secrets.toml`, a `.pem`, an oversized file, 30,000-deep nesting, and a local config with a textconv driver, an fsmonitor and a hooks path):

```bash
uv sync
uv pip install --python .venv/bin/python --require-hashes --only-binary :all: \
  -r scripts/review_context/requirements.txt
GRAMMAR_CACHE=/tmp/review-context-grammars PYTHONPATH=. uv run pytest scripts/review_context -q
```

Paths in headings and file lists use Markdown code spans with delimiters longer than any embedded backtick run. Control characters are rendered visibly so a path cannot introduce another heading or line.

## Self-review workflow

`.github/workflows/pr-agent-review.yaml` builds context for ready PR-open events, authorized `/review` comments, and both manual replay forms. `/describe`, `/improve` and other comment commands do not build a bundle; the artifact is restricted to `pr_reviewer` when an opened event runs all three tools.

The workflow separates three jobs:

- `prepare_review` authorizes the event and resolves the PR base/head SHAs. It checks out the repository's **default branch** to record the tooling commit and reading instructions. Manual replay event data is uploaded separately here.
- `review_context` has only `contents: read` permission and no secrets. It checks out that recorded default-branch commit, installs the hash-pinned wheels, verifies/extracts the pinned grammar archive, then fetches the PR objects and verifies the head SHA without checking out either the PR head or its target branch. The builder runs only here. A successful build uploads only `review-context/bundle.md`; this job exports no outputs or reading instructions.
- `pr_agent_job` runs on a fresh runner with the sponsored key and posting token. It downloads the bundle as data, verifies the fixed path is a non-empty regular file within the size bound, and sets the Docker-visible `ARTIFACT_PATH` to `/github/workspace/review-context/bundle.md`. Reading instructions come from `prepare_review`, outside the parser job. Parser writes to `GITHUB_ENV` or `GITHUB_OUTPUT` cannot affect this runner.

Failed context preparation, dependency verification, fetching, parsing, upload/download, or artifact validation preserves diff-only review. The first deployment also falls back while the default branch lacks the tooling. The parse step has a 150-second outer limit; dependency setup has its own two-minute limit. The keyed job still requires successful authorization even when the context job fails or is skipped.

Run the workflow shell tests with `PYTHONPATH=. uv run pytest tests/unittest/test_review_context_workflow.py -q`. They require Bash, Git, jq and GNU timeout; shell cases skip with a reason when a tool is missing, including jq in the minimal Docker test image. The workflow structure check still runs there. The tests execute real workflow scripts against a local Git remote with a modified target-branch builder, stub the API/installer, and exercise authorization, replay, SHA mismatch, stale output, checksum failure, invalid artifacts and untrusted artifact contents. Builder tests use verified native grammars and hostile repositories, including path escaping. The `build-and-test` workflow runs them in a dedicated Python 3.12 job on every PR to `main`, using the hash-pinned wheels and verified grammar archive; setup or test failures fail the job. Local tests do not validate live GitHub event delivery or the sponsored Docker action.
