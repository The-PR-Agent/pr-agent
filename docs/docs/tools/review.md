---
title: "Review"
sidebar_position: 3
---

## Overview

Generate a PR review with feedback on possible issues, security concerns, tests and review effort.
<br>
The tool can be triggered automatically every time a new PR is [opened](../usage-guide/automations_and_usage.md#github-app-automatic-tools-when-a-new-pr-is-opened), or can be invoked manually by commenting on any PR:

```
/review
```

Note that the main purpose of the `review` tool is to provide the **PR reviewer** with useful feedback and insights. The PR author, in contrast, may prefer to save time and focus on the output of the [improve](./improve.mdx) tool, which provides actionable code suggestions.

(Read more about the different personas in the PR process and how PR-Agent aims to assist them in our [blog](https://www.qodo.ai/blog/understanding-the-challenges-and-pain-points-of-the-pull-request-cycle/))

## Example usage

### Manual triggering

Invoke the tool manually by commenting `/review` on any PR:

<img src="/img/review_comment.png" alt="review comment" width="512" />

After ~30 seconds, the tool will generate a review for the PR:

<img src="/img/review3.png" alt="review" width="512" />

If you want to edit [configurations](#configuration-options), add the relevant ones to the command:

```
/review --pr_reviewer.some_config1=... --pr_reviewer.some_config2=...
```

From the CLI, save the parsed review and token usage as JSON for a later CI step:

```bash
pr-agent --pr_url https://github.com/org/repo/pull/123 --json-output review.json review
```

The JSON file is written after the review completes. Place `--json-output` before `review` or `review_pr`.
Its `coverage` object lists the files the token budget kept out of the review (`files_not_reviewed`) and those whose
patches were clipped to fit it (`files_partially_reviewed`), the same files the review coverage footer names.

### Automatic triggering

To run the `review` automatically when a PR is opened, define in a [configuration file](../usage-guide/configuration_options.md#local-configuration-file):

```
[github_app]
pr_commands = [
    "/review",
    ...
]

[pr_reviewer]
extra_instructions = "..."
...
```

- The `pr_commands` lists commands that will be executed automatically when a PR is opened.
- The `[pr_reviewer]` section contains the configurations for the `review` tool you want to edit (if any).

## Configuration options

The descriptions below explain each option's behavior. See the relevant sections in
[`configuration.toml`](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml)
for the authoritative default values.

<details open>
<summary>General options</summary>

<table>
  <tr>
    <td><b>persistent_comment</b></td>
    <td>If set to true, the review comment will be persistent, meaning that every new review request will edit the previous one.</td>
  </tr>
  <tr>
    <td><b>publish_review_failure_comment</b></td>
    <td>
      Set to false to suppress the "Failed to review PR" comment, including when a persistent review comment
      cannot be updated. Successful review output and the command's failure status are unchanged. Default is true.
    </td>
  </tr>
  <tr>
    <td><b>publish_error_details</b></td>
    <td>
      If set to true, a failed manual review comment includes a deterministic, sanitized failure reason for known
      provider and runtime errors. The failure formatter does not call an AI model, never publishes raw exception
      text, and falls back to a generic internal-error message. Default is false.
    </td>
  </tr>
  <tr>
    <td><b>review_heading</b></td>
    <td>
      Visible base heading for review comments, without the Markdown prefix or incremental label.
      For example, <code>review_heading = "Guideline Compliance Check"</code> renders
      <code>## Guideline Compliance Check 🔍</code> for a full review and
      <code>## Incremental Guideline Compliance Check 🔍</code> for an incremental review.
      On GitHub, GitLab, Azure DevOps, and Bitbucket Cloud, changing this value updates the same
      persistent review comment; it does not create a separate review channel.
    </td>
  </tr>
  <tr>
    <td><b>persistent_finding_state</b></td>
    <td>If set to true, PR-Agent persists structured review finding state across complete review runs, so findings can be resolved and reopened. Resolved findings retain the original Markdown formatting of their description. Incremental and partial reviews do not resolve absent findings. Default is true.</td>

  </tr>
  <tr>
    <td><b>max_previous_findings_chars</b></td>
        <td>Character budget for the findings stored by earlier reviews (requires <code>persistent_finding_state</code>). They are given to the model so it repeats a still-valid finding with its earlier wording instead of re-raising it reworded, and does not re-raise a resolved one unless the code reintroduces it. On GitHub and GitLab, an inline key-issue thread that someone other than PR-Agent resolved is given as dismissed, with its last reply, so the model does not re-raise it unless the code makes it worse. Set to 0 to disable. Default is 8000.
      On Azure DevOps, verified PR-Agent inline key issues with <code>wontFix</code> or <code>byDesign</code>
      are also passed as dismissed when the status differs from <code>azure_devops.default_comment_status</code>.
      A matching default stays an ordinary prior finding. This infers a human decision; Azure does not identify
      the status-changing actor here, and the current default may differ from the default used at creation.
      Stable <code>azure_devops_server.agent_identity</code> configuration is required.
    </td>
  </tr>
  <tr>
  <td><b>final_update_message</b></td>
  <td>When set to true, updating a persistent review comment during online commenting will automatically add a short comment with a link to the updated review in the pull request.</td>
  </tr>
  <tr>
    <td><b>extra_instructions</b></td>
    <td>Optional extra instructions to the tool. For example: "focus on the changes in the file X. Ignore change in ...".</td>
  </tr>
  <tr>
    <td><b>enable_help_text</b></td>
    <td>If set to true, the tool will display a help text in the comment.</td>
  </tr>
  <tr>
    <td><b>enable_review_coverage_footer</b></td>
    <td>If set to true, the tool will display a review coverage footer when the token budget leaves files out of the review.</td>
  </tr>
  <tr>
    <td><b>enable_large_pr_chunking</b></td>
    <td>If set to true, and the token budget leaves files out of the review, the diff is split into chunks, each chunk is reviewed separately, and the per-chunk results are merged into one review. See <a href="#reviewing-a-pr-that-does-not-fit-in-one-call">Reviewing a PR that does not fit in one call</a>. Default is false.</td>
  </tr>
  <tr>
    <td><b>max_number_of_calls</b></td>
    <td>Maximum number of chunk review calls, used only when <code>enable_large_pr_chunking</code> is true. Default is 3.</td>
  </tr>
  <tr>
    <td><b>num_max_findings</b></td>
    <td>Maximum number of returned findings.</td>
  </tr>
  <tr>
    <td><b>inline_key_issues</b></td>
    <td>If set to true, each key issue is published as an inline comment where the provider supports verified inline-comment publication (GitHub, Bitbucket Cloud, Azure DevOps, GitLab, Gitea). A finding leaves the review summary when a matching comment exists or the provider accepts the new comment. Findings that cannot be anchored or published stay in the summary.</td>
  </tr>
</table>

</details>

<details open>
<summary>Enable\disable specific sub-sections</summary>

<table>
  <tr>
    <td><b>require_score_review</b></td>
    <td>If set to true, the tool will add a section that scores the PR.</td>
  </tr>
  <tr>
    <td><b>require_tests_review</b></td>
    <td>If set to true, the tool will add a section that checks if the PR contains tests.</td>
  </tr>
  <tr>
    <td><b>require_estimate_effort_to_review</b></td>
    <td>If set to true, the tool will add a section that estimates the effort needed to review the PR.</td>
  </tr>
  <tr>
    <td><b>require_estimate_contribution_time_cost</b></td>
    <td>If set to true, the tool will add a section that estimates the time required for a senior developer to create and submit such changes.</td>
  </tr>
  <tr>
    <td><b>require_can_be_split_review</b></td>
    <td>If set to true, the tool will add a section that checks if the PR contains several themes, and can be split into smaller PRs.</td>
  </tr>
  <tr>
    <td><b>require_security_review</b></td>
    <td>If set to true, the tool will add a section that checks if the PR contains a possible security or vulnerability issue.</td>
  </tr>
    <tr>
    <td><b>require_todo_scan</b></td>
    <td>If set to true, the tool will add a section that lists TODO comments found in the PR code changes.
    </td>
  </tr>
  <tr>
    <td><b>require_ticket_analysis_review</b></td>
    <td>
      If set to true and ticket context is available, the tool adds a ticket-compliance section to the review comment
      that checks whether the PR fulfills the ticket requirements. Supported sources include GitHub and GitLab issues,
      linked Azure DevOps work items, Jira Cloud tickets, and Asana tasks.
    </td>
  </tr>
  <tr>
    <td><b>require_risk_assessment</b></td>
    <td>If set to true, the tool will add a section that rates the overall risk of the PR as low, medium or high.</td>
  </tr>
  <tr>
    <td><b>require_merge_recommendation</b></td>
    <td>If set to true, the tool will add a section describing what the review found: no_concerns_found (no important blockers or risks identified), needs_review (seems acceptable but deserves focused reviewer attention) or changes_required (clear issues to fix before merge). The value reports the model's findings, not a guarantee about the code.</td>
  </tr>
  <tr>
    <td><b>require_failure_modes</b></td>
    <td>Off by default. If enabled, adds up to three concrete failure scenarios, each with what could break,
    where, its trigger, how it could be detected, and whether a test or check in this PR covers it.
    These scenarios guide human review; they do not gate merging or prove that the PR is safe.</td>
  </tr>
  <tr>
    <td><b>require_priority_files</b></td>
    <td>If set to true, the tool will add a section listing the files a human reviewer should inspect first.</td>
  </tr>
  <tr>
    <td><b>structural_reach_input_file</b></td>
    <td>Path to a JSON file with structural reach facts, written by a build-graph tool earlier in CI. When the file
    exists, adds a deterministic change-risk row (Low, Moderate, High or Critical) with no model call. Relative to
    <code>GITHUB_WORKSPACE</code> when that is set, otherwise to the working directory. Host configuration only: a
    repository settings file or a comment cannot set it, because the file is read from the host. See
    <a href="#structural-reach">Structural reach</a>.</td>
  </tr>
</table>

</details>

<details open>
<summary>Adding PR labels</summary>

You can enable\disable the `review` tool to add specific labels to the PR:

<table>
  <tr>
    <td><b>enable_review_labels_security</b></td>
    <td>If set to true, the tool will publish a 'possible security issue' label if it detects a security issue.</td>
  </tr>
  <tr>
    <td><b>enable_review_labels_effort</b></td>
    <td>If set to true, the tool will publish a 'Review effort x/5' label (1–5 scale). Existing effort labels are kept when this option or effort estimation is disabled, or when the review has no valid replacement estimate. Security labels follow their own settings.</td>
  </tr>
</table>

</details>

## Usage Tips

### General guidelines

:::tip

The `review` tool provides a collection of configurable feedbacks about a PR.
It is recommended to review the [Configuration options](#configuration-options) section, and choose the relevant options for your use case.

Some of the features that are disabled by default are quite useful, and should be considered for enabling. For example:
`require_score_review`, and more.

On the other hand, if you find one of the enabled features to be irrelevant for your use case, disable it. No default configuration can fit all use cases.
:::

### Automation

:::tip
When you first install PR-Agent app, the [default mode](../usage-guide/automations_and_usage.md#github-app-automatic-tools-when-a-new-pr-is-opened) for the `review` tool is:
```
pr_commands = ["/review", ...]
```
Meaning the `review` tool will run automatically on every PR, without any additional configurations.
Edit this field to enable/disable the tool, or to change the configurations used.
:::

### Auto-generated PR labels by the Review Tool

:::tip

The `review` tool can automatically add labels to your Pull Requests:

- **`possible security issue`**: This label is applied if the tool detects a potential [security vulnerability](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/pr_reviewer_prompts.toml#L147) in the PR's code. This feedback is controlled by the 'enable_review_labels_security' flag (default is true).
- **`review effort [x/5]`**: This label estimates the [effort](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/pr_reviewer_prompts.toml#L119) required to review the PR on a relative scale of 1 to 5, where 'x' represents the assessed effort. This feedback is controlled by the 'enable_review_labels_effort' flag (default is true).

Ticket compliance is reported in the review comment, not as a PR label. It is controlled by
`pr_reviewer.require_ticket_analysis_review` and requires available ticket context. The tool does not add
ticket-compliance labels or a "No ticket found" label. See
[Fetching ticket context](../core-abilities/fetching_ticket_context.md) for linking and authentication details.


:::

### Auto-blocking PRs from being merged based on the generated labels

:::tip

You can configure a CI/CD Action to prevent merging PRs with specific labels. For example, implement a dedicated [GitHub Action](https://medium.com/sequra-tech/quick-tip-block-pull-request-merge-using-labels-6cc326936221).

This approach helps ensure PRs with potential security issues will not be merged without further review.

Since AI may make mistakes or lack complete context, use this feature judiciously. For flexibility, users with appropriate permissions can remove generated labels when necessary. When a label is removed, this action will be automatically documented in the PR discussion, clearly indicating it was a deliberate override by an authorized user to allow the merge.
:::

### Extra instructions

:::tip

Extra instructions are important.
The `review` tool can be configured with extra instructions, which can be used to guide the model to a feedback tailored to the needs of your project.

Be specific, clear, and concise in the instructions. With extra instructions, you are the prompter. Specify the relevant sub-tool, and the relevant aspects of the PR that you want to emphasize.

Examples of extra instructions:
```
[pr_reviewer]
extra_instructions="""\
In the code feedback section, emphasize the following:
- Does the code logic cover relevant edge cases?
- Is the code logic clear and easy to understand?
- Is the code logic efficient?
...
"""
```
Use triple quotes to write multi-line instructions. Use bullet points to make the instructions more readable.
:::

### Reviewing a PR that does not fit in one call

:::tip

When a PR diff is larger than the model's token budget, the `review` tool drops whole files
until the diff fits, and lists the dropped files in the review coverage footer.

Setting `enable_large_pr_chunking = true` changes what happens next: the diff is split into up
to `max_number_of_calls` chunks, each chunk is reviewed on its own, and the answers are merged
into a single review that says how many chunks it was built from. Files that do not fit even
after chunking are still listed in the coverage footer. Every chunk is a separate model call,
so a chunked review costs roughly `max_number_of_calls` times a normal one.

While the chunks run, the temporary `Preparing review...` comment is rewritten in place with the
number of chunks already analyzed, for example `Preparing review... analyzed 2 of 3 chunks`, plus
`... 1 chunk failed` when a chunk gives up. The updates require
`config.publish_output_progress` and a provider that supports both editing and removing a
comment, so plain-diff runs keep the frozen placeholder. Automatic commands publish no progress
comment, but while `github.publish_as_check_run` is enabled their in-progress check run shows the
same chunk count. The comment stays
temporary and is still removed before the merged review is published. A fallback model restores
the placeholder before it starts, so the visible count never moves backward.

If a chunk fails or returns malformed output, successful chunks are retained and fallback
models retry only the pending work. Pending chunks can be split for a smaller model, within
the call limit; a larger model can also include previously omitted files. Chunks that still
exceed the model's budget are not sent to it.

If every fallback is exhausted, successful chunks are published as a partial review with a
failed-chunk coverage warning, even when `publish_output_no_suggestions = false`. Incomplete
reviews cannot resolve absent persistent findings. If no chunk succeeds, the review fails.
With `config.propagate_tool_errors = true`, an exhausted fallback chain still signals failure
to the caller after publishing the partial review and removing the progress comment.
Optional run details list all models that contributed to the merged review.

Each chunk answers the same questions about a different part of the PR, so the answers are
merged field by field:

| Field | Merge rule |
| --- | --- |
| `key_issues_to_review` | Union over the chunks, dropping findings that repeat the same file, header and text |
| `security_concerns` | Every chunk that reported a concern is kept; "No" only when every chunk said no |
| `todo_sections`, `review_priority_files`, `can_be_split` | Union, de-duplicated, in chunk order (`can_be_split` keeps at most 3 sub-PRs) |
| `relevant_tests` | Yes if any chunk found tests |
| `score` | The lowest score any chunk gave |
| `risk_level`, `merge_recommendation` | The most conservative value any chunk gave |
| `failure_modes` | Concatenate valid cases in chunk order, keeping at most three |
| `estimated_effort_to_review_[1-5]` | The highest value any chunk gave |
| `contribution_time_cost_estimate` | The sum over the chunks, per case |
| `ticket_compliance_check` | One entry per ticket, with its bullet lists unioned across chunks |

When `require_todo_scan` is enabled, the TODO union includes both located findings and prose
summaries. Prose without a file location is shown as text without a source link.

The merged verdict is deliberately never less alarming than the worst chunk: a clean chunk
cannot raise a score, clear a security concern, or soften a risk level set by another chunk.
:::

### Structural reach

:::tip

When `structural_reach_input_file` points at a file that exists, the review gets one row at the top that rates how
far a defect in the change could spread. The row is computed from that file, not by the model, so the same input
always gets the same rating.

```
🔗 Structural reach: Critical
Reach: 40 projects, cross-domain
Dependents: billing-api, payments-worker, web-app, admin-app, reports (+35 more)
Sensitivity: payments
Rating driven by sensitive domain (escalates regardless of size)
```

**Rating.** Two facts from the input file set the band:

- **Reach** is the number of downstream projects a defect in the change can break. Only a build-graph tool knows
  it.
- **Sensitivity** flags changes to areas the repository marks as high-criticality, such as authentication,
  payments or secrets. A one-line auth change outranks a wide styling sweep.

Reach is narrow up to 3 projects, moderate up to 25 and wide above that. The band then follows these rules:

| Condition | Band |
| --- | --- |
| Wide reach | At least Moderate |
| Cross-domain change | At least Moderate |
| Any sensitive domain | At least High |
| Sensitive domain with wide reach | Critical |
| Anything else | Low |

The row lists each factor and names the one that set the band. It also lists the first five names in
`dependents`, in the order the producer wrote them.

The reach bands are defined for projects only. When `reach_unit` is `modules` or `files`, the row shows the count
and the dependents without a band, because no threshold separates a change to a shared utility from a real spread.
A cross-domain or sensitive change still gets a band from the rules above. The Reach line still shows the
number, but it does not affect the band.

Change size is not part of the rating, because
`estimated_effort_to_review_[1-5]` already reflects it. That row and `require_risk_assessment` are the model's
judgement; this row adds the structural and sensitivity facts the model cannot see in the diff.

**Input file.** Any build-graph tool (Nx, Bazel, Gradle, Turborepo, and so on) can write the reach facts in an
earlier CI step, so pr-agent needs no adapter per tool. Point `structural_reach_input_file` at the file in host
configuration, for example the `PR_REVIEWER__STRUCTURAL_REACH_INPUT_FILE` environment variable. Every field is
optional:

```json
{
  "version": 1,
  "reach": 12,
  "reach_unit": "projects",
  "dependents": ["billing-api", "web-app"],
  "domains": ["billing", "web"],
  "cross_domain": true,
  "sensitive_domains": ["billing"]
}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `version` | integer | Schema version. Only `1` is accepted. |
| `reach` | integer, 0 or more | Number of units affected by the change, including transitive dependents. Defaults to the length of `dependents`. |
| `reach_unit` | string | What `reach` counts: `projects` (the default), `modules` or `files`. Only `projects` gets a reach band. |
| `dependents` | list of strings | Names of the affected units, most relevant first. |
| `domains` | list of strings | Distinct domains or areas the affected projects belong to. |
| `cross_domain` | boolean | Whether the change spans more than one domain. |
| `sensitive_domains` | list of strings | Affected domains the repository treats as sensitive. |

Unknown fields are ignored. Strings are limited to 200 characters and the file to 1 MB.

**Trust.** The file is read from inside `GITHUB_WORKSPACE` (or the working directory), so in the GitHub Action a
checkout of the pull request could already contain a file at that path, written by the pull request's author.
Generate the file in the same job, right before the review step, and delete any existing copy first. Take the
project tags and graph configuration the producer reads from the base commit, not from the pull request. Treat the
band as advisory context for reviewers, never as a merge gate.

**Example producer: Nx.** `nx show projects --affected` lists every project the change affects, including
transitive dependents. This step writes them as `dependents`, and lists affected projects tagged `sensitive` as
`sensitive_domains`:

```bash
affected() { npx nx show projects --affected --base="$BASE_SHA" --head="$HEAD_SHA" --json "$@"; }
affected | jq --argjson sensitive "$(affected -p 'tag:sensitive')" \
  '{version: 1, reach_unit: "projects", dependents: ., sensitive_domains: $sensitive}' > structural-reach.json
```

Then set `PR_REVIEWER__STRUCTURAL_REACH_INPUT_FILE=structural-reach.json` for the review step. Another tool
needs only a query that lists the affected projects or packages.

**Example producer: tree-sitter.** For a repository without a build-graph tool,
`scripts/structural_reach_producer.py` builds a reverse-import graph of the Python, TypeScript and JavaScript
files at the base commit and follows importers from the changed files. Test files are left out of reach and
domains, because counting them makes most changes to a shared module read wide. It writes `reach_unit` as
`modules` (directories) by default, so the row shows the count and the first dependents without a band. Run it as
a CI step before the review; it is not a pr-agent dependency:

```bash
pip install "tree-sitter-language-pack==1.22.1"
python scripts/structural_reach_producer.py --repo . --base "$BASE_SHA" --head "$HEAD_SHA" \
  --unit module --out structural-reach.json
```

On this repository, the change merged in pull request 4420 gives:

```
🔗 Structural reach
Reach: 9 modules
Dependents: pr_agent/git_providers, pr_agent/tools, pr_agent/servers, pr_agent/algo, pr_agent/mosaico (+4 more)
```

`dependents` is ordered by the number of affected files in each unit. The graph does not see imports built at
run time, files loaded by name such as settings, or dependencies declared only in a manifest, such as a shared
`tsconfig` package used through `extends`. In a monorepo with Nx, Turborepo or Bazel, use that tool instead.

**Failure handling.** The row never blocks a review. If the setting is empty, the input file is missing or does
not match the schema, or anything fails while computing the row, the review is published without it.

**Sources.**

- Beyer, B. et al. *Site Reliability Engineering* (Google, 2016), and the AWS Well-Architected Framework: blast
  radius as the scope of impact of a change or failure.
- OWASP guidance on sensitive functionality: authentication, session management, cryptography and payments.

:::
