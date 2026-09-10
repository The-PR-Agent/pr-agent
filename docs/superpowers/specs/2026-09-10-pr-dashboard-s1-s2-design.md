# PR-Agent Dashboard — S1 (shell + provider read layer) and S2 (usage store) design

Date: 2026-09-10
Status: approved for implementation planning
Branch: `feature/pr-dashboard`

## Problem

PR-Agent is stateless. A run reads a pull request, calls a model, posts a comment, and
forgets everything: there is no database, no review history, and no persisted token or
cost accounting. `TokenHandler` counts tokens for the duration of one request; Langfuse
tracing, when configured, is fire-and-forget to an external service.

The goal is a single local dashboard showing every connected repository, the reviews
PR-Agent has produced, the findings inside them, model consumption and cost, and the
effective configuration — at any time, without opening a pull request.

That is not a UI task on top of an existing system. It requires a persistence layer, an
instrumentation path, a read API, the dashboard itself, and a configuration surface.

## Decomposition (approved)

Five sub-projects, each with its own spec, plan, and implementation cycle. All live in a
new sibling package `pr_dashboard/` so that upstream rebases of this fork stay clean.

| ID | Sub-project | Ships |
| --- | --- | --- |
| S1 | Shell + provider read layer | Browse every repo and review in one place |
| S2 | Usage store + instrumentation | Token and cost consumption views |
| S3 | Run control | Trigger `/review`, `/improve`, `/describe` from the UI with live logs |
| S4 | Configuration editor | Full edit of `.pr_agent.toml`, `configuration.toml`, and prompt TOMLs |
| S5 | Cross-repo findings index | Filterable findings across all repositories |

**This spec covers S1 and S2 only.** S3, S4, and S5 are explicitly out of scope; the
decomposition was approved on that basis.

## Decisions taken

| Decision | Choice | Rationale |
| --- | --- | --- |
| Data source | Reviews and pull requests read live from provider APIs; a local store records only usage | Provider APIs give full history from day one with no schema; usage was never in the comment, so it must be recorded |
| Deployment | Local, single user, no authentication | Provider credentials stay in the existing `.secrets.toml` and environment variables, matching the repository secret rules |
| Configuration scope | Full edit, including prompts and defaults (S4) | User decision, with guardrails recorded below |
| Run triggering | Yes, from the UI (S3) | Makes the dashboard a control panel rather than a mirror |
| Code location | Sibling package `pr_dashboard/` in this repository | This repository is a fork tracking upstream pull requests; code inside `pr_agent/` fights every future merge |
| Frontend stack | Jinja2 + HTMX + Alpine, vendored assets | No build step, no `package.json`, no node toolchain; FastAPI, uvicorn, Jinja2, and pydantic are already dependencies |

### S4 guardrails (recorded now, implemented later)

Full configuration editing was chosen deliberately. Because `AGENTS.md` treats prompt and
configuration files as single sources of truth and forbids reordering or reformatting
them, the S4 spec must implement all of the following:

- Round-trip TOML with `tomlkit` so comments and section order survive a write.
- Show a diff and require explicit confirmation before every write.
- Write a timestamped backup of the previous file content.
- Write to the working tree only. Never stage, never commit.

## Key finding: the accounting already exists

`pr_agent/algo/run_details.py` already collects, per run, everything S2 needs:

- `model_used`, `fallback_used` (sticky, so a fallback cannot be hidden by a later success)
- `prompt_tokens`, `completion_tokens`, `total_tokens`
- `num_ai_calls`, `known_cost_call_count`, `cost_status` (`complete` / `partial` / `unavailable`)
- `total_cost_usd` as a `Decimal`, and `model_costs_usd` per model
- `duration_seconds` from a monotonic start reference

The collector is a `ContextVar` installed by `init_run_details()` at the top of a tool's
`run()`. S2 therefore is a persistence call, not a new accounting system.

**Verified propagation assumption:** `pr_agent/agent/pr_agent.py:291` awaits the tool
directly — `await command2class[action](pr_url, ai_handler=self.ai_handler, args=args).run()`
— with no `asyncio.gather`, `create_task`, `to_thread`, or executor offload between
`_handle_request` and the tool. Coroutines awaited directly share the task context, so a
`ContextVar.set()` inside the tool is visible to `_handle_request` after the await. The
tool's own internal `gather` calls copy the context but reference the same mutable
`RunDetails` object, which is the documented behaviour of that module.

This assumption is load-bearing and is covered by a test (see Testing) so that an
upstream refactor to `gather` fails loudly instead of silently zeroing usage.

## A. Package layout and runtime

```
pr_dashboard/
  __init__.py
  app.py            # FastAPI app and routes
  registry.py       # connected-repo list, reads and writes pr_dashboard.toml
  providers.py      # thin wrapper over pr_agent.git_providers and provider SDKs
  comments.py       # identify and parse PR-Agent comments
  store.py          # sqlite3 schema, migrations, queries
  recorder.py       # the single persist hook
  templates/        # Jinja2 templates and HTMX partials
  static/           # vendored htmx.min.js, alpine.min.js, chart.umd.js
```

One process:

```
uv run uvicorn pr_dashboard.app:app --port 8420
```

A `pr-dashboard` console script is added to `[project.scripts]` in `pyproject.toml`
(approved).

**New dependencies: none.** FastAPI, starlette, uvicorn, Jinja2, and pydantic are already
declared. `sqlite3` is in the standard library. JavaScript assets are vendored under
`pr_dashboard/static/`, not installed through npm. `tomlkit` will be needed by S4 and is
not introduced here.

The dashboard runs under `uv run` like the rest of the project. The system `python3` on
this machine is 3.9, while `pyproject.toml` requires ≥ 3.12; no code path may assume the
system interpreter.

## B. Repository registry and provider read layer

Connected repositories are listed in `~/.pr_dashboard/pr_dashboard.toml`, outside the git
repository so that nothing can be committed by accident:

```toml
[[repo]]
provider = "github"      # github | bitbucket
slug = "samer2373/block_rush"
```

Credentials are **not** stored in this file. `providers.py` resolves tokens through the
existing `get_settings()` accessor, reading `.secrets.toml` and environment variables
unchanged. The add-repository flow validates a slug by fetching it and reports a missing
credential per provider — "no token configured for bitbucket" — rather than presenting a
half-working repository.

Provider access reuses `pr_agent/git_providers/` wherever the interface allows, and drops
to the underlying provider SDK (PyGithub, the Bitbucket client — both already
dependencies) for repository-level listing. This is a deliberate boundary: `GitProvider`
is pull-request-scoped by construction (`get_git_provider_with_context(pr_url)`) and has
no concept of "list the pull requests in this repository". Capability differences are
handled through `provider.is_supported(...)`, never through provider-type checks.

Provider responses are cached in SQLite with a short TTL so that refreshing the dashboard
does not consume the API rate limit.

## C. S1 user interface surfaces

| Route | Contents |
| --- | --- |
| `/` | All repositories, one card each: open pull requests, count reviewed, last run, seven-day tokens and cost |
| `/repos` | Registry create/read/update/delete, per-provider credential status |
| `/repos/{provider}/{slug}` | Pull request list: title, author, state, whether PR-Agent has reviewed it, findings count |
| `/pr/{provider}/{slug}/{number}` | Pull request detail: PR-Agent review comments rendered, findings extracted, inline suggestions, run history for that pull request |
| `/usage` | Consumption: tokens and cost over time, by repository, by model, by command; fallback rate; share of unpriced calls |

HTMX swaps server-rendered partials, Alpine handles local toggles, and Chart.js renders
the usage page.

### Identifying PR-Agent's comments

Matching is done on section markers — `## PR Reviewer Guide`, `## PR Code Suggestions`,
and the findings blocks introduced by commit `208aec46` — and **not** on comment author. A
self-hosted fork frequently posts under a human personal access token, so author matching
is unreliable. The marker set lives in one table in `comments.py` so that a prompt change
is a single edit.

## D. S2 usage store

SQLite at `~/.pr_dashboard/usage.db`, standard-library `sqlite3`, WAL mode, no ORM.

```
runs(
  id INTEGER PRIMARY KEY,
  started_at, finished_at, status,               -- status: running | ok | failed
  provider, repo_slug, pr_number, pr_url,        -- from handle_request arguments
  command,                                       -- parsed from the request string
  model_used, fallback_used,                     -- from RunDetails
  prompt_tokens, completion_tokens, total_tokens,
  num_ai_calls, known_cost_call_count, cost_status,
  total_cost_usd TEXT,                           -- Decimal as string
  duration_seconds, error_text
)

run_model_costs(
  run_id REFERENCES runs(id),
  model,
  cost_usd TEXT                                  -- from RunDetails.model_costs_usd
)

provider_cache(
  key TEXT PRIMARY KEY,                          -- provider:slug:resource[:number]
  fetched_at, expires_at,
  payload TEXT                                   -- JSON as returned by the provider
)
```

`provider_cache` backs the short-TTL caching described in section B. It holds only data
already public in the provider, never credentials, and a stale row is served with a
"stale" label rather than deleted when the provider is unreachable.

Costs are stored as `TEXT`. `RunDetails` uses `Decimal` deliberately to avoid float math
in pricing; a `REAL` column would reintroduce exactly that. Aggregation converts on read.

Column provenance matters and is not uniform: the identity columns (`provider`,
`repo_slug`, `pr_number`, `pr_url`, `command`) come from `handle_request`'s own arguments,
because `RunDetails` holds pure LLM accounting and knows nothing about repositories. Those
columns are what makes the join to the S1 views possible.

## E. S2 instrumentation

`pr_dashboard/recorder.py` exposes a context manager that wraps the dispatch inside
`_handle_request`:

1. On entry, insert a row with `status = running` and the identity columns.
2. After the await, read `get_run_details()` and update the row with the usage columns and
   `status = ok` or `failed`, plus `error_text` on failure.

**Attempts are recorded, not only completions.** A malformed URL, an authentication
failure, or a rate-limit error means `init_run_details()` never fires. A completion-only
table would silently under-report precisely the runs most worth seeing. Such rows land
with null token columns and an `error_text`.

For the same reason of honesty: only `pr_reviewer`, `pr_description`, and
`pr_code_suggestions` call `init_run_details()`. Commands such as `/ask` and `/help`
produce rows with no LLM accounting, and the interface renders "not reported" rather than
`$0.00`. This mirrors the existing `_as_decimal_cost` behaviour, which rejects zero
because litellm returns `0.0` both for unpriced models and for unbillable usage.

**Edit footprint inside `pr_agent/`: one.** A `with record_run(pr_url, request):` around
the dispatch in `pr_agent/agent/pr_agent.py`, with a lazy import so that `pr_dashboard`
remains an optional component and PR-Agent runs unchanged when it is absent. Everything
else in S1 and S2 is new files.

## F. Error handling

Failures surface rather than being swallowed:

- Missing credential: named per provider, with the repository marked unusable in the registry.
- Provider 401 or 403: banner naming the provider and the required scope.
- Provider 429: banner with the rate-limit reset time; cached data is still displayed and
  labelled stale.
- Database locked: bounded retry, then a visible error.

The recorder is the single exception. Its failures are logged and swallowed, because a
dashboard write must never break a review. This is a deliberate asymmetry and is stated in
the recorder's docstring.

## G. Testing

Tests live in `tests/unittest/test_pr_dashboard_*.py` and run with
`PYTHONPATH=. uv run pytest`:

- `registry`: TOML round-trip, duplicate slug rejection, unknown provider rejection.
- `comments`: marker parsing against captured fixtures from block_rush pull request 1,
  covering both the `details` and `expanded` findings layouts.
- `store`: schema creation, migration idempotence, `Decimal` cost round-trip, aggregation
  by repository/model/command.
- `recorder`: `ContextVar` propagation through a direct await — asserts the assumption
  documented above about `pr_agent/agent/pr_agent.py:291`, so a refactor to `gather` fails
  a test rather than silently zeroing usage. Also covers the attempt-row path where
  `init_run_details()` never fires.
- `routes`: smoke tests for every route against a faked provider, asserting that a
  provider error renders a banner instead of a stack trace.

## Risks

| Risk | Mitigation |
| --- | --- |
| Comment identification is heuristic and breaks when prompts change | Single marker table in `comments.py`; fixture-based tests over real captured comments |
| The `ContextVar` propagation assumption could be invalidated upstream | Dedicated test asserting it, so the break is loud |
| Provider API rate limits during dashboard browsing | Short-TTL SQLite cache of provider responses; stale data labelled, not hidden |
| Fork divergence from upstream | Single-line edit inside `pr_agent/`; all other code in `pr_dashboard/` |

## Out of scope

S3 run triggering, S4 configuration editing, S5 findings index. Each gets its own spec.
