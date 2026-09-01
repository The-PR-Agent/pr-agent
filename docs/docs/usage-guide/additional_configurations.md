## Show possible configurations

The possible configurations of PR-Agent are stored in [here](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml){:target="_blank"}.
In the [tools](../tools/index.md) page you can find explanations on how to use these configurations for each tool.

To print all the available configurations as a comment on your PR, you can use the following command:

```
/config
```

![possible_config1](https://codium.ai/images/pr_agent/possible_config1.png){width=512}

To view the **actual** configurations used for a specific tool, after all the user settings are applied, you can add for each tool a `--config.output_relevant_configurations=true` suffix.
For example:

```
/improve --config.output_relevant_configurations=true
```

Will output an additional field showing the actual configurations used for the `improve` tool.

![possible_config2](https://codium.ai/images/pr_agent/possible_config2.png){width=512}

### Showing the agent run details

To see which model actually answered, how many tokens the run consumed, and how long the AI processing phase took, enable `config.output_run_details`:

```
/review --config.output_run_details=true
```

API-cost collection is a separate, default-off option controlled by `config.output_run_cost`. Enable both flags to collect it and add it inside the run-details section:

```
/review --config.output_run_details=true --config.output_run_cost=true
```

`config.output_run_details` remains the public-output gate: setting only `config.output_run_cost=true` collects run-level cost data but never adds it to a PR comment.

On providers that support GitHub-Flavored Markdown this appends a collapsible section to the generated comment; elsewhere `/review` and `/describe` append the same information as plain text:

```
⚙️ Agent run details
- Model: gpt-5.6-terra (fallback)
- Tokens: 12,340 in / 1,205 out / 13,545 total
- Time cost: 8.2s
- AI calls: 1
- Estimated API cost: $0.08 USD
  - anthropic/claude-opus-5: $0.07 USD
  - anthropic/claude-sonnet-5: $0.01 USD
```

`Model` shows the model that produced the answer, marked `(fallback)` when the primary model failed and a fallback took over. The `Tokens` line appears only when the model provider reports usage. `AI calls` counts the successful LLM invocations made during the run. The flag is disabled by default.

`Estimated API cost` is derived synchronously from each completed LiteLLM response and its finalized usage. LiteLLM can account for cache reads, cache writes, reasoning tokens, and provider-specific usage categories when the response and its pricing data include them. Multi-model runs show a compact breakdown of the known costs. Exact `Decimal` values are retained for aggregation, while public currency output is rounded to two decimal places; a tiny positive value that would round to zero is shown as `<$0.01` instead of `$0.00`. If only some successful calls can be priced, the total is marked `partial` with the priced-call count; if none can be priced, the line reports `unavailable`. Missing pricing is never rendered as `$0`.

The amount is an estimate based on LiteLLM's pricing data, not provider-invoice-authoritative billing. Reconcile it with the provider's billing records before using it for accounting or chargeback. Streaming responses are priced only after finalized usage is available; asynchronous callbacks and transient `response_cost` callback metadata are not treated as the sole source of truth. The public section contains only aggregate costs and configured model names, never prompts, response bodies, API keys, or provider request IDs.

Notes:

- `/improve` appends the section only when it publishes a summary comment. If the provider lacks GFM support or `pr_code_suggestions.commitable_code_suggestions` is enabled, `/improve` posts inline comments instead, so no run details section appears.
- With `pr_description.use_description_markers=true`, repeated `/describe` runs accumulate one run details block per run because the existing PR description is preserved and only the markers are replaced.

## Ignoring files from analysis

In some cases, you may want to exclude specific files or directories from the analysis performed by PR-Agent. This can be useful, for example, when you have files that are generated automatically or files that shouldn't be reviewed, like vendor code.

You can ignore files or folders using the following methods:

- `IGNORE.GLOB`
- `IGNORE.REGEX`

which you can edit to ignore files or folders based on glob or regex patterns.

### Example usage

Let's look at an example where we want to ignore all files with `.py` extension from the analysis.

To ignore Python files in a PR with online usage, comment on a PR:
`/review --ignore.glob="['*.py']"`

To ignore Python files in all PRs using `glob` pattern, set in a configuration file:

```
[ignore]
glob = ['*.py']
```

And to ignore Python files in all PRs using `regex` pattern, set in a configuration file:

```
[ignore]
regex = ['.*\.py$']
```

## Extra instructions

All PR-Agent tools have a parameter called `extra_instructions`, that enables to add free-text extra instructions. Example usage:

```
/update_changelog --pr_update_changelog.extra_instructions="Make sure to update also the version ..."
```

## Language Settings

The default response language for PR-Agent is **U.S. English**. However, some development teams may prefer to display information in a different language. For example, your team's workflow might improve if PR descriptions and code suggestions are set to your country's native language.

To configure this, set the `response_language` parameter in the configuration file. This will prompt the model to respond in the specified language. Use a **standard locale code** based on [ISO 3166](https://en.wikipedia.org/wiki/ISO_3166) (country codes) and [ISO 639](https://en.wikipedia.org/wiki/ISO_639) (language codes) to define a language-country pair. See this [comprehensive list of locale codes](https://simplelocalize.io/data/locales/).

Example:

```toml
[config]
response_language = "it-IT"
```

This will set the response language globally for all the commands to Italian.

> **Important:** Note that only dynamic text generated by the AI model is translated to the configured language. Static text such as labels and table headers that are not part of the AI models response will remain in US English. In addition, the model you are using must have good support for the specified language.

[//]: # (## Working with large PRs)

[//]: # ()
[//]: # (The default mode of CodiumAI is to have a single call per tool, using GPT-4, which has a token limit of 8000 tokens.)

[//]: # (This mode provides a very good speed-quality-cost tradeoff, and can handle most PRs successfully.)

[//]: # (When the PR is above the token limit, it employs a [PR Compression strategy]&#40;../core-abilities/index.md&#41;.)

[//]: # ()
[//]: # (However, for very large PRs, or in case you want to emphasize quality over speed and cost, there are two possible solutions:)

[//]: # (1&#41; [Use a model]&#40;./changing_a_model.md&#41; with larger context, like GPT-32K, or claude-100K. This solution will be applicable for all the tools.)

[//]: # (2&#41; For the `/improve` tool, there is an ['extended' mode]&#40;../tools/improve.md&#41; &#40;`/improve --extended`&#41;,)

[//]: # (which divides the PR into chunks, and processes each chunk separately. With this mode, regardless of the model, no compression will be done &#40;but for large PRs, multiple model calls may occur&#41;)


## Expand GitLab submodule diffs

By default, GitLab merge requests show submodule updates as `Subproject commit` lines. To include the actual file-level changes from those submodules in PR-Agent analysis, enable:

```toml
[gitlab]
expand_submodule_diffs = true
```

When enabled, PR-Agent will fetch and attach diffs from the submodule repositories. The default is `false` to avoid extra GitLab API calls.

## Post the review as a GitLab thread

By default, PR-Agent posts the `/review` summary as a plain note. To post it as a resolvable thread (GitLab discussion) instead, enable (default: `false`):

```toml
[gitlab]
publish_review_as_thread = true
```
- With `pr_reviewer.persistent_comment=true` (the default), each run updates the existing review thread and reopens it if it was resolved, so the refreshed review gets another look.
- Enabling the flag does not convert a review that was already posted as a plain note: it keeps being updated in place, and GitLab cannot promote a note to a thread. Only MRs whose first review runs after the flag is set get a thread.
- Set `pr_reviewer.persistent_comment=false` to open a new review thread on each run instead.

## Post the /improve suggestions as a GitLab thread

By default, PR-Agent posts the `/improve` suggestions as a plain note. To post them as a resolvable thread (GitLab discussion) instead, enable (default: `false`):

```toml
[gitlab]
publish_improve_as_thread = true
```

- A run that finds no suggestions edits the thread in place and resolves it, so a status message does not leave an open thread behind.

## Resolve outdated GitLab inline threads

Each inline suggestion is anchored to the MR head commit it was posted against. When a later push moves the head, GitLab marks that thread as belonging to an outdated diff version: it renders empty, loses its `Resolve` control, and can then only be closed through the API, so superseded suggestions pile up on a long-lived MR. To have PR-Agent resolve those threads before it publishes a fresh batch of inline suggestions, enable (default: `false`):

```toml
[gitlab]
resolve_outdated_inline_threads = true
```

A thread is only resolved when all of the following hold, so a thread with any human reply is never closed:

- the thread was opened by PR-Agent itself, judged from the body: it carries a dedup marker (proof, and present on every inline review finding), or it opens with the `**Suggestion:**` lead that inline suggestions are published with (a strong hint rather than proof, since a person could type it). This is what keeps the cleanup off hand-written comments made from the same account, which matters because the GitLab token often belongs to a person rather than a dedicated bot user;
- every non-system note in it was written by that same user, so a single human reply leaves the thread alone. GitLab system notes are ignored, because the "changed this line in version N of the diff" note that marks a thread outdated is authored by whoever pushed;
- the thread is unresolved and anchored to a line of the diff;
- the head SHA recorded in that anchor differs from the MR's current diff head SHA.

Anything that cannot be confirmed (an unreadable position, an unknown current head SHA, an unresolvable bot identity) is skipped rather than resolved.

Note that the anchor's recorded head SHA never changes after the thread is created, so any PR-Agent thread older than the latest push qualifies, including ones GitLab still renders on unchanged lines. With `config.persistent_inline_comments` enabled the next run re-posts a suggestion that still applies, so the effect is resolve-and-repost rather than removal.

## Log Level

PR-Agent allows you to control the verbosity of logging by using the `log_level` configuration parameter. This is particularly useful for troubleshooting and debugging issues with your PR workflows.

```
[config]
log_level = "DEBUG"  # Options: "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"
```

The default log level is "DEBUG", which provides detailed output of all operations. If you prefer less verbose logs, you can set higher log levels like "INFO" or "WARNING".

## Attributing requests to a PR on the provider side

When `add_user_to_requests` is enabled, PR-Agent sends the current command and PR URL in the
OpenAI-compatible `user` request field, as a compact JSON string:

```
{"command":"improve","pr_url":"https://gitlab.example.com/group/project/-/merge_requests/171"}
```

Providers that record this field per request (for example OpenRouter, which shows it as
`external_user` in the generation details and includes it in the activity export) can then
attribute every request, its cost and its outcome to a specific PR and command, without
timestamp correlation.

```
[config]
add_user_to_requests = true
```

The setting is disabled by default, since it shares request-attribution data with the model
provider: enabling it is an explicit operator choice.

## Integrating with Logging Observability Platforms

Various logging observability tools can be used out-of-the box when using the default LiteLLM AI Handler. Simply configure the LiteLLM callback settings in `configuration.toml` and set environment variables according to the LiteLLM [documentation](https://docs.litellm.ai/docs/).

For example, to use [LangSmith](https://www.langchain.com/langsmith) you can add the following to your `configuration.toml` file:

```
[litellm]
enable_callbacks = true
success_callback = ["langsmith"]
failure_callback = ["langsmith"]
service_callback = []
```

Then set the following environment variables:

```
LANGSMITH_API_KEY=<api_key>
LANGSMITH_PROJECT=<project>
LANGSMITH_BASE_URL=<url>
```

To use [Langfuse](https://langfuse.com) for utilization and adoption tracking (LLM cost, token usage, latency, and unique repo adoption), add the following to your configuration:

```toml
[litellm]
enable_callbacks = true
success_callback = ["langfuse_otel"]
failure_callback = ["langfuse_otel"]
```

> Note: use `langfuse_otel` (the OpenTelemetry-based integration), not the legacy `langfuse` callback — the legacy callback is incompatible with the Langfuse 3.x SDK this project pins.

Then set the following environment variables:

```
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=<public_key>
LANGFUSE_SECRET_KEY=<secret_key>
```

Each LLM call is traced with the command name, git provider, PR URL, model, token counts, and version as tags — giving you full visibility into how pr-agent is being used across your repositories.

### LLM telemetry via LiteLLM's OpenTelemetry integration

To emit OpenTelemetry traces and metrics for the LLM calls themselves — cost in USD, call duration, time to first token, and the input/output token split, on standard `gen_ai.*` semantic-convention names — enable LiteLLM's built-in `otel` callback:

```toml
[litellm]
success_callback = ["otel"]
failure_callback = ["otel"]
turn_off_message_logging = true
```

> **Set `turn_off_message_logging = true`.** LiteLLM attaches full prompt and response content to what its callbacks emit, which here means the entire PR diff. The default is `false` to preserve existing behavior for Langfuse and LangSmith users.

The integration is configured through environment variables:

```
OTEL_EXPORTER=otlp_http            # or "console", "otlp_grpc"
OTEL_EXPORTER_OTLP_ENDPOINT=https://collector:4318
OTEL_EXPORTER_OTLP_HEADERS=...
OTEL_SERVICE_NAME=pr-agent
LITELLM_OTEL_INTEGRATION_ENABLE_METRICS=true   # metrics are off by default
```

Pending callbacks are flushed before the CLI and the GitHub Action runner exit, bounded by `callback_timeout_seconds` (see [Custom callbacks](#custom-callbacks)).

### Custom callbacks

If you embed PR-Agent in your own code, you can also register callbacks programmatically — for example a
`litellm.CustomLogger` that records per-call token usage and cost:

```python
import litellm
from pr_agent import cli

class UsageLogger(litellm.integrations.custom_logger.CustomLogger):
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        record_usage(kwargs.get("model"), response_obj)

litellm.callbacks = [UsageLogger()]
cli.run_command("<pr_url>", "/review")
```

LiteLLM dispatches these callbacks asynchronously, after the completion call has already returned. PR-Agent
flushes any pending callbacks before the CLI (and the GitHub Action runner) exits, so they are not lost when
the event loop is torn down — no configuration required. Use `callback_timeout_seconds` to bound how long
that flush may take:

```
[litellm]
callback_timeout_seconds = 30 # default
```

## Built-in OpenTelemetry command telemetry

PR-Agent can emit its own [OpenTelemetry](https://opentelemetry.io/) signals for utilization and adoption tracking. These cover the **command** layer — how often each tool runs, on which git provider, and whether it succeeded — which no LLM-level integration can report, because many failures happen before any model call:

- **Traces**: one span per request, named `pr_agent <command>` (for example `pr_agent review`), carrying `pr_agent.command`, `pr_agent.args_count`, `vcs.provider.name`, a span status, and a bounded `error.type` on failure. Prompt and response content is never attached.
- **Metrics**: `pr_agent.commands`, a counter of executed commands labeled by command and git provider.

### Two independent layers

Command telemetry (this section) and [LLM telemetry](#llm-telemetry-via-litellms-opentelemetry-integration) are separate toggles with separate configuration, so you can enable either alone and aggregate them independently. When both are on, the LLM spans are children of the command span in the same trace, so a single command's model calls stay attributable to it.

Telemetry is disabled by default. To enable it, set in `configuration.toml`:

```toml
[otel]
is_enabled = true
exporter_type = "console" # "console", "otlp", or "none"
service_name = "pr-agent"
environment = "development" # e.g. "development", "staging", "production"
```

To export to an OpenTelemetry collector instead of the console, set `exporter_type = "otlp"` and configure the endpoint and any authentication headers in `.secrets.toml` (they are secrets — keep them out of `configuration.toml`):

```toml
[otel]
otlp_endpoint = "http://my-collector:4318"
otlp_headers = "x-honeycomb-team=YOUR_API_KEY" # optional, "key1=value1,key2=value2"
```

Export uses OTLP over HTTP by default; `otlp_endpoint` is the base URL, and the `/v1/traces` and `/v1/metrics` paths are appended automatically. To use OTLP over gRPC instead, install the optional exporter and select the protocol — the endpoint is then used as-is (gRPC collectors typically listen on port 4317):

```bash
pip install pr-agent[otel-grpc]
```

```toml
[otel]
otlp_protocol = "grpc" # default: "http"
```

This is the recommended topology for fleets: point every PR-Agent instance at the same collector and aggregate there. Each process creates its own exporter connection; use the `service_name` and `environment` resource attributes to slice instances apart on the backend.

Privacy controls (both off by default):

- `include_pr_url = true` attaches PR URLs to spans. Off by default because URLs expose private repo names.
- `include_error_details = true` attaches exception messages and rejected-command text to error spans. Off by default because that content can embed PR URLs, repo names, or other request-specific text. Bounded values (the exception class name and error category) are always attached.

Notes:

- Telemetry configuration is **process-level**: it is read once at startup from the global configuration or environment, and cannot be enabled or reconfigured per-repo via `.pr_agent.toml`. In a multi-tenant server, telemetry is a shared process resource — configure it where the process is deployed.
- PR-Agent keeps its own OpenTelemetry providers and never registers the process-global one, so embedding PR-Agent in an application that already uses OpenTelemetry will not interfere with the host's telemetry. Pending spans and metrics are flushed automatically on process exit.
- Each OTLP export call is bounded by `otlp_timeout` (default 3 seconds, retries included), so an unreachable collector cannot hang CLI exit or request completion. Raise it for slow collectors at the cost of longer worst-case stalls.
- If `exporter_type = "otlp"` is set but no endpoint is configured, telemetry is disabled entirely (fail closed) — it never falls back to another exporter, so a missing secret cannot redirect telemetry into process logs.
- **Serverless deployments** (e.g. the AWS Lambda webhooks) are supported: buffered spans and metrics are force-flushed at the end of every handled request, because frozen execution environments stop background export threads and are reaped without running exit handlers. No extra configuration is needed.

## Bringing per-repo context files to PR-Agent

`Platforms supported: GitHub, GitLab, Gitea, Bitbucket, Azure DevOps`

To give PR-Agent's tools additional project context, you can have it include repository instruction files — such as [AGENTS.md](https://agents.md/) or [CLAUDE.md](https://www.anthropic.com/engineering/claude-code-best-practices) — in the prompts for the `/review`, `/describe` and `/improve` tools.

By default, PR-Agent looks for an `AGENTS.md` file at the repository root:

```toml
[config]
repo_context_files = ["AGENTS.md"]
```

You can list any repository-relative paths. By default the files are read from the repository's **default branch**, so only trusted, already-merged content is used and a PR cannot influence the guidance used to review it. A file that is missing is silently skipped. Set the option to an empty list to disable the feature entirely:

```toml
[config]
repo_context_files = ["AGENTS.md", "CLAUDE.md", "docs/conventions.md"]
```

!!! note "Which branch the files are read from"
    By default (`repo_context_from_default_branch = true`), instruction files are read from the repository's **default branch** — a single trusted source — so neither the PR nor its target branch can alter the guidance used to review it. This matches how Qodo Merge reads these files.

    Set `repo_context_from_default_branch = false` to instead read from the PR's **target (base) branch**. This respects branch-specific instructions (for example a release branch, or a stacked PR that carries its own `AGENTS.md`), at the cost of trusting whoever can write to that target branch. Even then, files are never read from the PR's own head.

    ```toml
    [config]
    repo_context_from_default_branch = false
    ```

To bound how much of this context is sent to the model, `repo_context_max_lines` (default `500`) caps the total number of rendered lines, including the wrapper tags. Content beyond the budget is truncated safely:

```toml
[config]
repo_context_max_lines = 500
```

## Ignoring automatic commands in PRs

PR-Agent allows you to automatically ignore certain PRs based on various criteria:

- PRs with specific titles (using regex matching)
- PRs between specific branches (using regex matching)
- PRs from specific repositories (using regex matching)
- PRs not from specific folders
- PRs containing specific labels
- PRs opened by specific users

### Ignoring PRs with specific titles

To ignore PRs with a specific title such as "[Bump]: ...", you can add the following to your `configuration.toml` file:

```toml
[config]
ignore_pr_title = ["\\[Bump\\]"]
```

Where the `ignore_pr_title` is a list of regex patterns to match the PR title you want to ignore. Default is `ignore_pr_title = ["^\\[Auto\\]", "^Auto"]`.

### Ignoring PRs between specific branches

To ignore PRs from specific source or target branches, you can add the following to your `configuration.toml` file:

```toml
[config]
ignore_pr_source_branches = ['develop', 'main', 'master', 'stage']
ignore_pr_target_branches = ["qa"]
```

Where the `ignore_pr_source_branches` and `ignore_pr_target_branches` are lists of regex patterns to match the source and target branches you want to ignore.
They are not mutually exclusive, you can use them together or separately.

### Ignoring PRs from specific repositories

To ignore PRs from specific repositories, you can add the following to your `configuration.toml` file:

```toml
[config]
ignore_repositories = ["my-org/my-repo1", "my-org/my-repo2"]
```

Where the `ignore_repositories` is a list of regex patterns to match the repositories you want to ignore. This is useful when you have multiple repositories and want to exclude certain ones from analysis.


### Ignoring PRs not from specific folders

To allow only specific folders (often needed in large monorepos), set:

```
[config]
allow_only_specific_folders=['folder1','folder2']
```

For the configuration above, automatic feedback will only be triggered when the PR changes include files where 'folder1' or 'folder2' is in the file path

### Ignoring PRs containing specific labels

To ignore PRs containing specific labels, you can add the following to your `configuration.toml` file:

```
[config]
ignore_pr_labels = ["do-not-merge"]
```

Where the `ignore_pr_labels` is a list of labels that when present in the PR, the PR will be ignored.

### Ignoring PRs from specific users

PR-Agent tries to automatically identify and ignore pull requests created by bots using:

- GitHub's native bot detection system
- Name-based pattern matching

While this detection is robust, it may not catch all cases, particularly when:

- Bots are registered as regular user accounts
- Bot names don't match common patterns

To supplement the automatic bot detection, you can manually specify users to ignore. Add the following to your `configuration.toml` file to ignore PRs from specific users:

```
[config]
ignore_pr_authors = ["my-special-bot-user", ...]
```

Where the `ignore_pr_authors` is a regex list of usernames that you want to ignore.

!!! note
    There is one specific case where bots will receive an automatic response - when they generated a PR with a _failed test_.

### Ignoring Generated Files by Language/Framework

To automatically exclude files generated by specific languages or frameworks, you can add the following to your `configuration.toml` file:

```
[config]
ignore_language_framework = ['protobuf', ...]
```

You can view the list of auto-generated file patterns in [`generated_code_ignore.toml`](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/generated_code_ignore.toml).
Files matching these glob patterns will be automatically excluded from PR Agent analysis.

### Ignoring Tickets with Specific Labels

When PR-Agent analyzes tickets (JIRA, GitHub Issues, GitLab Issues, etc.) referenced in your PR, you may want to exclude tickets that have certain labels from the analysis. This is useful for filtering out tickets marked as "ignore-compliance", "skip-review", or other labels that indicate the ticket should not be considered during PR review.

To ignore tickets with specific labels, add the following to your `configuration.toml` file:

```toml
[config]
ignore_ticket_labels = ["ignore-compliance", "skip-review", "wont-fix"]
```

Where `ignore_ticket_labels` is a list of label names that should be ignored during ticket analysis.

### Restricted Mode

When running PR-Agent with limited GitHub/GitLab permissions, set `restricted_mode` to `true` to gracefully skip operations that require elevated access (e.g., pushing changelog changes):

```toml
[config]
restricted_mode = true
```

With restricted mode, the minimum workflow permissions are:

```yaml
permissions:
  issues: write
  pull-requests: write
```

Within an explicit `permissions:` block, any scope you do not list (such as `contents`) is set to `none`, so you do not need to grant `contents` — restricted mode skips every operation that would require `contents: write`. All tools (`/review`, `/describe`, `/improve`, etc.) continue to work normally with just `pull-requests: write`.

> **Note:** this only holds when a `permissions:` block is present (as above). If you omit the `permissions:` block entirely, the effective defaults are governed by your repository/organization GitHub Actions settings and may grant broader access.
