# Diff-scoped code context for pr-agent's `[artifacts]` slot

A starting point, not a finished tool. `graph_bundle_ts.py` builds a review-context bundle for one PR with tree-sitter, and `instructions.txt` tells the model how to read it. Together they are the setup measured in [The-PR-Agent/pr-agent#2499](https://github.com/The-PR-Agent/pr-agent/issues/2499).

The builder, instructions and original hardening tests were imported from [IsmaelMartinez/pr-agent, `review-context-builder`, commit `3dcc022`](https://github.com/IsmaelMartinez/pr-agent/tree/3dcc02287589916e4b4a72897a208ee72ac1df26/scripts/review_context).

The bundle holds the changed symbols (signature plus the body lines the diff does not show), module constants and callees they use, inbound callers (hub callers ranked, under a cap), sibling implementations and sibling config/prompt/doc files, sites still using a pattern the PR removed, similar code and same-pattern sites, tests that exercise the change, and importers. It is capped at about 12k tokens.

With `--generic` every language is handled by one class driven by tree-sitter tags queries, covering the 30 languages detected by its extension list with tree-sitter-language-pack 1.22.1 (Python, JavaScript, TypeScript, Go, Java, C, C++, C#, Rust, Ruby, PHP, Kotlin and more). It has only been scored on Python; Go and TypeScript were smoke-tested. Without the flag, the hand-written Python and JavaScript classes are used and other languages fall back to the generic one.

## Run

```bash
uv run --no-project --with tree-sitter-language-pack==1.22.1 python scripts/review_context/graph_bundle_ts.py \
  --repo "$PWD" --base "$BASE_SHA" --head "$HEAD_SHA" --pr "$PR_NUMBER" \
  --out-dir /tmp/review-context --generic --grammar-cache /tmp/grammars
```

`--repo` is any clone that holds both commits; the head does not need to be checked out, because every file is read from git. The bundle is written to `<out-dir>/pr<N>.G6.md` and a summary JSON goes to stdout. On pr-agent's own PRs a build takes 5 to 30 seconds. tree-sitter-language-pack downloads grammars from GitHub on first use, so prefetch them into `--grammar-cache` for offline or sandboxed runs. The build phase has a `--timeout` (120 s by default, after grammar initialization); caught build errors print a diagnostic to stderr and exit 2. Bundle writes are atomic. Callers must remove stale output before each run, bound dependency and grammar initialization separately, and use the bundle only after a successful exit, as the workflow below does.

Hand the bundle to pr-agent, here for `/review` only:

```bash
export ARTIFACT_PATH=/tmp/review-context/pr$PR_NUMBER.G6.md
export ARTIFACT_INSTRUCTIONS="$(cat scripts/review_context/instructions.txt)"
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
uv run --no-project --with pytest --with tree-sitter-language-pack==1.22.1 pytest scripts/review_context -q
```

The workflow that calls it has to do its part. Under `pull_request_target` the job holds a write token and secrets, so take the builder from the base branch and never run anything from the PR head. Since the builder reads git objects, the head does not need a checkout: fetch it into the base clone and check the SHA. A sketch:

```yaml
      - uses: actions/checkout@<pinned SHA>
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          fetch-depth: 0
          persist-credentials: false
          path: base
      - uses: astral-sh/setup-uv@<pinned SHA>
      - name: Build review context
        id: context
        continue-on-error: true
        env:  # no secrets in this step
          PR: ${{ github.event.pull_request.number }}
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          out="$GITHUB_WORKSPACE/review-context"
          rm -rf "$out"
          git -C base fetch --no-tags --quiet origin "+refs/pull/$PR/head:refs/remotes/pr/head"
          test "$(git -C base rev-parse refs/remotes/pr/head)" = "$HEAD_SHA"
          uv run --no-project --with tree-sitter-language-pack==1.22.1 python base/scripts/review_context/graph_bundle_ts.py \
            --repo base --base "$BASE_SHA" --head "$HEAD_SHA" --pr "$PR" --out-dir "$out" --generic \
            --grammar-cache "$RUNNER_TEMP/grammars"
          if [ -f "$out/pr$PR.G6.md" ] && [ ! -L "$out/pr$PR.G6.md" ]; then
            echo "path=/github/workspace/review-context/pr$PR.G6.md" >> "$GITHUB_OUTPUT"
          fi
```

Then give the pr-agent step `ARTIFACT_PATH: ${{ steps.context.outputs.path }}`, which stays empty, and so leaves the review diff-only, when the build fails. The `/github/workspace` path is how a Docker action such as `pragent/pr-agent:github_action` sees `GITHUB_WORKSPACE`. Comment-triggered runs carry no head SHA in their payload, so read the head and base SHAs from the pulls API there. Pin tree-sitter-language-pack as above, and never save an `actions/cache` of the grammar directory from a PR event, because a later run would load native grammar libraries a PR could have planted.

## Self-review workflow

`.github/workflows/pr-agent-review.yaml` builds this context for its ready PR-open events, authorized `/review` comments, and both manual replay forms. It resolves the PR's base and head revisions before checking out the base into `base/`, leaving replay event files in the workspace root. `/describe`, `/improve` and other comment commands do not build a bundle; the artifact is restricted to `pr_reviewer` when an opened event runs all three tools.

The build step has no token or sponsored key in its environment. It deletes `review-context/` before fetching the public PR ref, verifies the head SHA, and runs only the base builder and reading instructions. The builder retains its 120-second limit; a 150-second outer timeout also bounds dependency installation and grammar loading. Grammar downloads use the runner's temporary directory without an Actions cache. Failed preparation, fetching, building, or regular-file validation leaves the action's artifact path empty and preserves diff-only review. The first deployment also falls back if a PR's base revision predates the builder.

Run the workflow shell tests with `PYTHONPATH=. uv run pytest tests/unittest/test_review_context_workflow.py -q`. They require Bash, Git, jq and GNU timeout (available on the native CI runners); shell cases skip with a reason when a tool is missing, including jq in the minimal Docker test image. The workflow structure check still runs there. They simulate opened/comment events and manual replays with a local Git remote, stub the GitHub API and Python installer, and exercise authorization, stale output removal, SHA mismatch, builder failure and non-regular output rejection. The builder's tests above use real grammars and hostile repositories. These local checks do not validate live GitHub event delivery or the sponsored Docker action, and do not replace the independent security review required before merging.
