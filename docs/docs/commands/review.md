---
title: "Review"
sidebar_position: 3
---

## Overview

Generate a PR review with feedback on possible issues, security concerns, tests and review effort.
<br>
The command can be triggered automatically every time a new PR is [opened](../usage-guide/automations_and_usage.md#github-app-automatic-commands-when-a-new-pr-is-opened), or can be invoked manually by commenting on any PR:

```
/review
```

Note that the main purpose of the `review` command is to provide the **PR reviewer** with useful feedback and insights. Findings can also carry a fix, which GitHub and the other supported providers offer as a [committable suggestion](#fixes-and-committable-suggestions).

(Read more about the different personas in the PR process and how PR-Agent aims to assist them in our [blog](https://www.qodo.ai/blog/understanding-the-challenges-and-pain-points-of-the-pull-request-cycle/))

:::note
`/improve` runs `/review` as of 0.51. See [Improve](./improve.mdx).
:::

## Example usage

### Manual triggering

Invoke the command manually by commenting `/review` on any PR:

<img src="/img/review_comment.png" alt="review comment" width="512" />

After ~30 seconds, the command will generate a review for the PR:

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
Its `coverage` object lists the files the token budget or chunk-call limit kept out of the review (`files_not_reviewed`)
and those whose patches were clipped to fit the token budget (`files_partially_reviewed`).
The JSON includes the full lists even when the Markdown footer summarizes them.
It also includes `total_chunks` and `failed_chunks` for the final analysis plan, after fallback recovery.
These count analysis chunks, not model requests or retry attempts. A nonzero `failed_chunks` means that the
review is partial even if both file lists are empty; findings from successful chunks are retained.

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
- The `[pr_reviewer]` section contains the configurations for the `review` command you want to edit (if any).

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
    <td><b>per_commit_review</b></td>
    <td>If set to true, each commit of the PR is reviewed on its own diff and commit message, and one review comment is published per commit. See <a href="#reviewing-each-commit-separately">Reviewing each commit separately</a>. It can't be turned on from a command argument, only from a configuration file or environment variable. Default is false.</td>
  </tr>
  <tr>
    <td><b>max_commits_per_review</b></td>
    <td>Maximum number of commits <code>per_commit_review</code> reviews one by one. When more commits than that are not reviewed yet, the whole PR is reviewed in one review instead, with a notice. It can't be set from a command argument or a per directory configuration file. Default is 0, no limit.</td>
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
      PR-Agent resolves its posting identity through Azure DevOps. Set
      <code>azure_devops_server.agent_identity</code> to override it or when automatic lookup is unavailable.
    </td>
  </tr>
  <tr>
  <td><b>final_update_message</b></td>
  <td>When set to true, updating a persistent review comment during online commenting will automatically add a short comment with a link to the updated review in the pull request.</td>
  </tr>
  <tr>
    <td><b>extra_instructions</b></td>
    <td>Optional extra instructions to the command. For example: "focus on the changes in the file X. Ignore change in ...".</td>
  </tr>
  <tr>
    <td><b>enable_help_text</b></td>
    <td>If set to true, the command will display a help text in the comment.</td>
  </tr>
  <tr>
    <td><b>enable_review_coverage_footer</b></td>
    <td>If set to true, the command will display a review coverage footer when the token budget leaves files out of the review.</td>
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
    <td>Maximum number of returned findings. Default is 6.</td>
  </tr>
  <tr>
    <td><b>inline_key_issues</b></td>
    <td>If set to true (the default), each key issue is published as an inline comment where the provider supports verified inline-comment publication (GitHub, Bitbucket Cloud, Azure DevOps, GitLab, Gitea). A finding leaves the review summary when a matching comment exists or the provider accepts the new comment. Findings that cannot be anchored or published stay in the summary. A finding that carries a fix is published as a committable suggestion when `pr_code_suggestions.committable_code_suggestions` is true, the fix is at most ten lines and its `existing_code` matches the file at the finding's lines; otherwise the fix appears as a code block in the comment.</td>
  </tr>
  <tr>
    <td><b>min_finding_confidence</b></td>
    <td>When above 0, the model reports an integer confidence from 0 to 10 for each finding, shown in its header in the review summary (inline comments do not show it). Findings below this value are dropped before publishing, and in a chunked review they are dropped before the <code>num_max_findings</code> cut, so they cannot displace a more confident finding. A run that dropped findings does not mark earlier findings as resolved. A missing or out-of-range confidence is treated as absent: the finding is kept. A threshold outside 0-10 is treated as 0 (disabled). Default is 0: keep all findings, and the model is not asked for a confidence.</td>
  </tr>
</table>

</details>

<details open>
<summary>Enable\disable specific sub-sections</summary>

<table>
  <tr>
    <td><b>require_score_review</b></td>
    <td>If set to true, the command will add a section that scores the PR.</td>
  </tr>
  <tr>
    <td><b>require_tests_review</b></td>
    <td>If set to true, the command will add a section that checks if the PR contains tests.</td>
  </tr>
  <tr>
    <td><b>require_estimate_effort_to_review</b></td>
    <td>If set to true, the command will add a section that estimates the effort needed to review the PR.</td>
  </tr>
  <tr>
    <td><b>require_estimate_contribution_time_cost</b></td>
    <td>If set to true, the command will add a section that estimates the time required for a senior developer to create and submit such changes.</td>
  </tr>
  <tr>
    <td><b>require_can_be_split_review</b></td>
    <td>If set to true, the command will add a section that checks if the PR contains several themes, and can be split into smaller PRs.</td>
  </tr>
  <tr>
    <td><b>require_security_review</b></td>
    <td>If set to true, the command will add a section that checks if the PR contains a possible security or vulnerability issue.</td>
  </tr>
    <tr>
    <td><b>require_todo_scan</b></td>
    <td>If set to true, the command will add a section that lists TODO comments found in the PR code changes.
    </td>
  </tr>
  <tr>
    <td><b>require_ticket_analysis_review</b></td>
    <td>
      If set to true and ticket context is available, the command adds a ticket-compliance section to the review comment
      that checks whether the PR fulfills the ticket requirements. Supported sources include GitHub and GitLab issues,
      linked Azure DevOps work items, Jira Cloud tickets, and Asana tasks.
    </td>
  </tr>
  <tr>
    <td><b>require_risk_assessment</b></td>
    <td>If set to true, the command will add a section that rates the overall risk of the PR as low, medium or high.</td>
  </tr>
  <tr>
    <td><b>require_merge_recommendation</b></td>
    <td>If set to true, the command publishes a recommendation computed from deterministic checks. Only passing checks
    allow no_concerns_found; needs_review means a check did not pass or could not be evaluated, and changes_required
    reflects a model veto. The value reports the review's findings, not a guarantee about the code. See
    <a href="#merge-recommendation">Merge recommendation</a>.</td>
  </tr>
  <tr>
    <td><b>publish_merge_recommendation_status</b></td>
    <td>If set to true with <code>require_merge_recommendation</code>, publishes the verdict on the reviewed commit.
    GitHub uses the <code>PR Agent - Review</code> check; GitLab uses a commit status. <code>needs_review</code>
    publishes as neutral on GitHub and skipped on GitLab, so it does not block a merge; <code>changes_required</code>
    publishes as a failure.</td>
  </tr>
  <tr>
    <td><b>max_changed_lines</b></td>
    <td>Host-only changed-line cap for <code>no_concerns_found</code>. A larger diff makes the recommendation
    <code>needs_review</code> at best. The cap counts added and removed lines.</td>
  </tr>
  <tr>
    <td><b>require_failure_modes</b></td>
    <td>Off by default. If enabled, adds up to three concrete failure scenarios, each with what could break,
    where, its trigger, how it could be detected, and whether a test or check in this PR covers it.
    These scenarios guide human review; they do not gate merging or prove that the PR is safe.</td>
  </tr>
  <tr>
    <td><b>require_artifact_findings</b></td>
    <td>Off by default, and only active when a <a href="../../installation/github/#ci-artifact-context">CI artifact</a> reaches /review.
    If enabled, adds a "CI artifact findings" section with up to five neutral facts from the artifact: destroyed or replaced
    resources, failed jobs (including allowed-to-fail ones), failed tests, vulnerabilities and notable warnings. Each fact
    carries a short quote from the artifact; a quote that cannot be found in the artifact is marked, and credentials in
    facts and quotes are redacted. The section reports what the artifact shows and leaves the decision to the reviewer.
    Host configuration only: a repository settings file or comment cannot set it.</td>
  </tr>
  <tr>
    <td><b>require_priority_files</b></td>
    <td>If set to true, the command will add a section listing the files a human reviewer should inspect first.</td>
  </tr>
  <tr>
    <td><b>structural_reach_input_file</b></td>
    <td>Path to a JSON file with structural reach facts, written by a build-graph tool earlier in CI. When the file
    exists, adds a deterministic change-risk row (Low, Moderate, High or Critical) with no model call. Relative to
    <code>GITHUB_WORKSPACE</code> when that is set, otherwise to the working directory. Host configuration only: a
    repository settings file or a comment cannot set it, because the file is read from the host. See
    <a href="#structural-reach">Structural reach</a>.</td>
  </tr>
  <tr>
    <td><b>enable_risk_checks</b></td>
    <td>
      If set to true, deterministically scans diff files and unfiltered PR files for sensitive paths, destructive SQL,
      cache flushes, and authentication/secrets patterns without making model calls. Findings are rendered as a risk-checks
      row in the review summary table. When no findings are detected, nothing is added. Default is true. See
      <a href="#deterministic-risk-checks">Deterministic risk checks</a>.
    </td>
  </tr>
  <tr>
    <td><b>sensitive_paths</b></td>
    <td>
      List of glob patterns identifying files considered sensitive (such as CI workflows, package manifests, lockfiles,
      migrations, and infrastructure configurations). Changes to matching files flag a deterministic review risk finding.
      Default includes common CI/CD, package manager, container, infrastructure, and migration paths.
    </td>
  </tr>
  <tr>
    <td><b>extra_risky_shapes</b></td>
    <td>
      List of custom regular expression patterns to scan within added or modified lines in diff hunks. Patterns matching a
      line flag a deterministic risk finding. Host configuration only: a repository settings file or comment cannot set it.
      Default is empty.
    </td>
  </tr>
</table>

</details>

<details open>
<summary>Adding PR labels</summary>

You can enable\disable the `review` command to add specific labels to the PR:

<table>
  <tr>
    <td><b>enable_review_labels_security</b></td>
    <td>If set to true, the command will publish a 'Possible security concern' label if it detects a security issue.</td>
  </tr>
  <tr>
    <td><b>enable_review_labels_effort</b></td>
    <td>If set to true, the command will publish a 'Review effort x/5' label (1–5 scale). Existing effort labels are kept when this option or effort estimation is disabled, or when the review has no valid replacement estimate. Security labels follow their own settings.</td>
  </tr>
</table>

</details>

## Usage Tips

### General guidelines

:::tip

The `review` command provides a collection of configurable feedbacks about a PR.
It is recommended to review the [Configuration options](#configuration-options) section, and choose the relevant options for your use case.

Some of the features that are disabled by default are quite useful, and should be considered for enabling. For example:
`require_score_review`, and more.

On the other hand, if you find one of the enabled features to be irrelevant for your use case, disable it. No default configuration can fit all use cases.
:::

### Automation

:::tip
When you first install PR-Agent app, the [default mode](../usage-guide/automations_and_usage.md#github-app-automatic-commands-when-a-new-pr-is-opened) for the `review` command is:
```
pr_commands = ["/review", ...]
```
Meaning the `review` command will run automatically on every PR, without any additional configurations.
Edit this field to enable/disable the command, or to change the configurations used.
:::

### Auto-generated PR labels by the Review Command

:::tip

The `review` command can automatically add labels to your Pull Requests:

- **`Possible security concern`**: This label is applied if the command detects a potential [security vulnerability](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/pr_reviewer_prompts.toml#L147) in the PR's code. This feedback is controlled by the 'enable_review_labels_security' flag (default is true).
- **`review effort [x/5]`**: This label estimates the [effort](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/pr_reviewer_prompts.toml#L119) required to review the PR on a relative scale of 1 to 5, where 'x' represents the assessed effort. This feedback is controlled by the 'enable_review_labels_effort' flag (default is true).

Ticket compliance is reported in the review comment, not as a PR label. It is controlled by
`pr_reviewer.require_ticket_analysis_review` and requires available ticket context. The command does not add
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
The `review` command can be configured with extra instructions, which can be used to guide the model to a feedback tailored to the needs of your project.

Be specific, clear, and concise in the instructions. With extra instructions, you are the prompter. Specify the relevant sub-command, and the relevant aspects of the PR that you want to emphasize.

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

### Fixes and committable suggestions

A finding can carry a fix: the model adds `existing_code` (the lines it wants to change, copied from the file) and
`improved_code` (their replacement) when a concrete change resolves the issue. A finding without a clear fix has neither field.

When `inline_key_issues` is on (the default) and the provider can verify inline comments (GitHub, Bitbucket Cloud, Azure DevOps,
GitLab, Gitea), each finding is posted as an inline comment. If `pr_code_suggestions.committable_code_suggestions` is true and
`existing_code` matches the file at the finding's lines, the fix is posted as a committable suggestion that you can apply from the
PR page. Otherwise the fix is shown as a text block in the comment. A finding that is not posted inline (the provider cannot verify
inline comments, `inline_key_issues` is off, or publishing failed) stays in the review summary, and its fix appears there as a text
block. Findings are capped by `pr_reviewer.num_max_findings` (6 by default), and chunked reviews keep the same cap.

Two `pr_code_suggestions` settings are honoured: `committable_code_suggestions` and `extra_instructions`
(added to the review's extra instructions when it differs from `pr_reviewer.extra_instructions`).
Every other `pr_code_suggestions` setting is ignored since 0.51.

```
/review --pr_code_suggestions.committable_code_suggestions=true
```

### Reviewing each commit separately

:::tip

Projects that keep one logical change per commit can ask for a review of every commit instead of the combined PR diff,
in a configuration file:

```
[pr_reviewer]
per_commit_review = true
```

Each commit is reviewed against its own parent, with its commit message in the prompt, and gets its own
incremental review comment that names and links the commit. These comments are never the persistent
review comment, and they do not change the review labels.

Each run reviews only the commits of the PR that were not reviewed yet, so `-i` is not needed in `pr_commands` or
`push_commands`, and the first run reviews every commit. A commit is identified by a fingerprint of its own changes
(file names and patch lines, without line numbers), and counts as reviewed once its review succeeds: the fingerprint
is kept in a hidden marker in the commit's review comment or, when no comment is published for it, in one record
comment that is edited in place. A commit whose review failed is reviewed again on the next run; automatic runs stop
retrying it after 3 failed attempts, while a manual `/review` always retries.
`minimal_commits_for_incremental_review` and `minimal_minutes_for_incremental_review` do not apply to per commit
review, since each run picks the commits to review from these markers instead of the `-i` thresholds.
The commit list is read from the git provider on every run, so after a force push the review follows the rewritten
history: a commit that was rebased without changing its own patch is not reviewed again, a commit that was amended or
had fixups folded into it (for example with `git rebase --autosquash`) is reviewed again, and fixup commits that no
longer exist are not reviewed. Markers count only in comments PR-Agent wrote; when the git provider cannot confirm
that, the whole PR is reviewed in one review with a notice instead.
On GitLab, a commit with a collapsed, too large or rename only file is reviewed again after a rebase, since its
fingerprint includes its SHA.

Set `publish_output_no_suggestions = false` in the `[pr_reviewer]` section
(`--pr_reviewer.publish_output_no_suggestions=false` on the command line) to skip the comment of a commit where
nothing was found.
With `inline_key_issues = true`, each finding is published as an inline comment on the commit it was found in,
anchored to that commit's own diff, so GitHub and GitLab show it as outdated once a later commit changes those lines.
A finding that cannot be anchored there, for example because its line is outside that commit's diff or the provider
rejects it, stays in that commit's review comment instead of being moved to a nearby line.

Merge commits, empty commits and the root commit of a history have no diff of their own and are skipped. A commit that
fails to review is reported like a failed review and does not stop the remaining commits. Every reviewed commit is a
separate model call, so the first review of a PR with many commits costs accordingly; later runs review only the new
commits.
Hosts that want to bound that cost can set `max_commits_per_review` (default 0, no limit): when more commits than that
are not reviewed yet, the whole PR is reviewed in one review with a notice, and its commits count as reviewed.
The markers live only in PR-Agent's own comments, so if those comments are removed, the next run reviews every commit
of the PR again, unless `max_commits_per_review` is set and the PR has more commits than that, in which case the whole
PR gets one review.
Both settings belong to the operator, so a command argument such as `--pr_reviewer.per_commit_review=true` is
rejected.
Each commit's comment shows its own run details, while the `--json-output` file and the GitHub Action `review` output
describe the last reviewed commit only.

Per commit review is available on GitHub and GitLab. Other git providers fall back to a regular review of the whole PR.
GitHub lists at most 250 commits of a PR, so a PR with more commits than that is also reviewed as a whole, with a
notice explaining why.
:::

### Reviewing a PR that does not fit in one call

:::tip

When a PR diff is larger than the model's token budget, the `review` command drops whole files
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
With `config.propagate_command_errors = true`, an exhausted fallback chain still signals failure
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
Dependents: pr_agent/git_providers, pr_agent/commands, pr_agent/servers, pr_agent/algo, pr_agent/mosaico (+4 more)
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

### Deterministic risk checks

:::tip

When `enable_risk_checks` is enabled (the default), PR-Agent scans changed files and diff hunks for known risk patterns
without making model calls. Findings are rendered as a dedicated row in the review table:

```
⚠️ Risk checks: 3 finding(s) detected
Sensitive paths (1): .github/workflows/deploy.yml
Risky shapes (1): `Privilege revocation` in db/migrations/002_revoke.sql
Deleted files (1): old_auth.py
```

When no findings are detected, the row is omitted completely so clean reviews stay uncluttered.

**Checks performed.**

- **Sensitive paths**: Flags changes to files matching glob patterns in `sensitive_paths`. By default, this covers
  CI/CD pipelines, package manager manifests and lockfiles, Dockerfiles, Terraform and infrastructure configs, and
  database migrations.
- **Risky change shapes**: Scans added or modified lines in diff hunks for destructive SQL (`DROP TABLE`, `ALTER ... RENAME`,
  `REVOKE ... ON/FROM`, `TRUNCATE`), authentication and secrets keywords, cache invalidations, and any custom patterns
  configured in `extra_risky_shapes`.
- **Deleted files**: Flags removed files.

**Custom patterns.** Additional regular expressions can be configured via `extra_risky_shapes`. Because regex execution
has security implications, `extra_risky_shapes` must be configured through host configuration (such as environment
variables or host settings) and cannot be overridden by repository `.pr_agent.toml` files. Each search with a custom
pattern has a 50 ms limit, and each custom pattern has 1 s in total per review. If a pattern exceeds either one,
PR-Agent logs a warning and skips that pattern for the rest of the review.

:::

### Merge recommendation

When `require_merge_recommendation` is enabled, PR-Agent computes the recommendation from deterministic checks;
the model can only make it more cautious. `no_concerns_found` requires passing CI on the reviewed head, a current
head, human authors, no sensitive paths or risky changes, and a diff within `max_changed_lines`. A check that
cannot be evaluated fails closed. The review includes each check and its result.

When `publish_merge_recommendation_status` is enabled, PR-Agent publishes the verdict against the exact head SHA
that was reviewed. GitHub uses the `PR Agent - Review` check run and GitLab uses a commit status. `needs_review`
publishes as neutral on GitHub and skipped on GitLab, so it does not block a merge. `changes_required` publishes
as failure/failed and blocks merging when that status is required by branch protection or the project's merge rules.
A new commit has no PR-Agent verdict status until it is reviewed.

The line cap, sensitive-path patterns, and custom risky-shape patterns are host-only settings; repository,
per-directory, and comment configuration cannot weaken or replace them. The verdict scans the complete PR diff,
including ignored paths and files already reviewed by an incremental review. If the full diff or a trustworthy CI
timestamp is unavailable, the verdict cannot be `no_concerns_found`.

### Gated auto-approval

:::warning

Off by default. An approval counts toward a protected branch's required reviews, so enable it only where an
automated approval of small, clean changes is acceptable. PR-Agent never merges.

:::

When the host sets `config.enable_auto_approval` (for example `CONFIG__ENABLE_AUTO_APPROVAL=true`), `/review` on
GitHub approves the exact commit it reviewed when all of these hold:

- the computed merge recommendation is `no_concerns_found`;
- the model returned both a merge recommendation and a risk level, so `require_merge_recommendation` and
  `require_risk_assessment` must be on;
- the review covered the whole pull request: not an incremental review, no file left out or cut short to fit the
  model's context, and no part of the diff failed;
- the review completed and its finding state could be updated.

Each `/review` run withdraws the approval PR-Agent gave when it starts, and approves again only when it ends with all
of these holding, so a run that fails or finds a concern leaves no approval behind. On a push, a change of base branch or a reopen, the GitHub App and the GitHub Action dismiss it
before any other event filter runs. PR-Agent only dismisses approvals it gave automatically; manual approvals from
the same account are left alone. With `publish_output` off, nothing is approved or dismissed.

`enable_auto_approval` is host-only: a repository `.pr_agent.toml` or a comment argument cannot turn it on.

**Required repository settings.**

- Turn on "Dismiss stale pull request approvals when new commits are pushed" in the branch protection rule or
  ruleset. PR-Agent can miss a push (a failed webhook delivery, a polling deployment, or a workflow that does not
  listen to `synchronize`), and GitHub only removes a stale approval itself when this is on.
- For the GitHub Action, allow "GitHub Actions to create and approve pull requests" in the repository's Actions
  settings. Pull requests from forks get a read-only token, so they cannot be approved.
- If the rule restricts who can dismiss reviews, include PR-Agent's account; otherwise it cannot withdraw its own
  approval, and only logs an error.

GitHub does not let an account approve a pull request it opened. Other git providers are not supported yet.
