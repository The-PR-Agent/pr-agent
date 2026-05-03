## Overview

The `improve` tool scans the PR code changes, and automatically generates meaningful suggestions for improving the PR code.
The tool can be triggered automatically every time a new PR is [opened](../usage-guide/automations_and_usage.md#github-app-automatic-tools-when-a-new-pr-is-opened), or it can be invoked manually by commenting on any PR:

```toml
/improve
```

## How it looks

=== "Suggestions Overview"
    ![code_suggestions_as_comment_closed](https://codium.ai/images/pr_agent/code_suggestions_as_comment_closed.png){width=512}

=== "Selecting a specific suggestion"
    ![code_suggestions_as_comment_open](https://codium.ai/images/pr_agent/code_suggestions_as_comment_open.png){width=512}

___

## Example usage

### Manual triggering

Invoke the tool manually by commenting `/improve` on any PR. The code suggestions by default are presented as a single comment:

To edit [configurations](#configuration-options) related to the `improve` tool, use the following template:

```toml
/improve --pr_code_suggestions.some_config1=... --pr_code_suggestions.some_config2=...
```

For example, you can choose to present all the suggestions as committable code comments, by running the following command:

```toml
/improve --pr_code_suggestions.commitable_code_suggestions=true
```

![improve](https://codium.ai/images/pr_agent/improve.png){width=512}

### Automatic triggering

To run the `improve` automatically when a PR is opened, define in a [configuration file](../usage-guide/configuration_options.md#wiki-configuration-file):

```toml
[github_app]
pr_commands = [
    "/improve",
    ...
]

[pr_code_suggestions]
num_code_suggestions_per_chunk = ...
...
```

- The `pr_commands` lists commands that will be executed automatically when a PR is opened.
- The `[pr_code_suggestions]` section contains the configurations for the `improve` tool you want to edit (if any)

### Table vs Committable code comments

PR-Agent supports two modes for presenting code suggestions: 

1) [Table](https://codium.ai/images/pr_agent/code_suggestions_as_comment_closed.png) mode 

2) [Inline Committable](https://codium.ai/images/pr_agent/improve.png) code comments mode.

The table format offers several key advantages:

- **Reduced noise**: Creates a cleaner PR experience with less clutter
- **Quick overview and prioritization**: Enables quick review of one-liner summaries, impact levels, and easy prioritization
- **High-level suggestions**: High-level suggestions that aren't tied to specific code chunks are presented only in the table mode
- **Interactive features**: Provides 'more' and 'update' functionality via clickable buttons
- **Centralized tracking**: Shows suggestion implementation status in one place
- **IDE integration**: Allows applying suggestions directly in your IDE via the CLI tool

Table mode is the default of PR-Agent, and is recommended approach for most users due to these benefits. 

![code_suggestions_as_comment_closed.png](https://codium.ai/images/pr_agent/code_suggestions_as_comment_closed.png){width=512}

Teams with specific preferences can enable committable code comments mode in their local configuration, or use [dual publishing mode](#dual-publishing-mode).

> `Note - due to platform limitations, Bitbucket cloud and server supports only committable code comments mode.`


## `Extra instructions` and `best practices`

The `improve` tool can be further customized by providing additional instructions and best practices to the AI model.

### Extra instructions

You can use the `extra_instructions` configuration option to give the AI model additional instructions for the `improve` tool.
Be specific, clear, and concise in the instructions. With extra instructions, you are the prompter.

Examples for possible instructions:

```toml
[pr_code_suggestions]
extra_instructions="""\
(1) Answer in Japanese
(2) Don't suggest to add try-except block
(3) Ignore changes in toml files
...
"""
```

Use triple quotes to write multi-line instructions. Use bullet points or numbers to make the instructions more readable.

### Best practices

`Platforms supported: GitHub, GitLab, Bitbucket`

PR-Agent supports both simple and hierarchical best practices configurations to provide guidance to the AI model for generating relevant code suggestions.

???- tip "Writing effective best practices files"
    
    The following guidelines apply to all best practices files:
    
    - Write clearly and concisely
    - Include brief code examples when helpful with before/after patterns
    - Focus on project-specific guidelines that will result in relevant suggestions you actually want to get
    - Keep each file relatively short, under 800 lines, since:
        - AI models may not process effectively very long documents
        - Long files tend to contain generic guidelines already known to AI
        - Maximum multiple file accumulated content is limited to 2000 lines.
    - Use pattern-based structure rather than simple bullet points for better clarity

???- tip "Example of a best practices file"
 
    Pattern 1: Add proper error handling with try-except blocks around external function calls.
    
    Example code before:

    ```python
    # Some code that might raise an exception
    return process_pr_data(data)
    ```

    Example code after:

    ```python
    try:
        # Some code that might raise an exception
        return process_pr_data(data)
    except Exception as e:
        logger.exception("Failed to process request", extra={"error": e})
    ```

    Pattern 2: Add defensive null/empty checks before accessing object properties or performing operations on potentially null variables to prevent runtime errors.
    
    Example code before:

    ```python
    def get_pr_code(pr_data):
        if "changed_code" in pr_data:
            return pr_data.get("changed_code", "")
        return ""
    ```

    Example code after:

    ```python
    def get_pr_code(pr_data):
        if pr_data is None:
            return ""
        if "changed_code" in pr_data:
            return pr_data.get("changed_code", "")
        return ""
    ```

#### Local best practices

For basic usage, create a `best_practices.md` file in your repository's root directory containing a list of best practices, coding standards, and guidelines specific to your repository.

The AI model will use this `best_practices.md` file as a reference, and in case the PR code violates any of the guidelines, it will create additional suggestions, with a dedicated label: `Organization best practice`.

### Combining 'extra instructions' and 'best practices'

The `extra instructions` configuration is more related to the `improve` tool prompt. It can be used, for example, to avoid specific suggestions ("Don't suggest to add try-except block", "Ignore changes in toml files", ...) or to emphasize specific aspects or formats ("Answer in Japanese", "Give only short suggestions", ...)

In contrast, the `best_practices.md` file is a general guideline for the way code should be written in the repo.

Using a combination of both can help the AI model to provide relevant and tailored suggestions.

## Usage Tips

### Implementing the proposed code suggestions

Each generated suggestion consists of three key elements:

1. A single-line summary of the proposed change
2. An expandable section containing a comprehensive description of the suggestion
3. A diff snippet showing the recommended code modification (before and after)

We advise users to apply critical analysis and judgment when implementing the proposed suggestions.
In addition to mistakes (which may happen, but are rare), sometimes the presented code modification may serve more as an _illustrative example_ than a directly applicable solution.
In such cases, we recommend prioritizing the suggestion's detailed description, using the diff snippet primarily as a supporting reference.

### Dual publishing mode

Our recommended approach for presenting code suggestions is through a [table](./improve.md#overview) (`--pr_code_suggestions.commitable_code_suggestions=false`).
This method significantly reduces the PR footprint and allows for quick and easy digestion of multiple suggestions.

We also offer a complementary **dual publishing mode**. When enabled, suggestions exceeding a certain score threshold are not only displayed in the table, but also presented as committable PR comments.
This mode helps highlight suggestions deemed more critical.

To activate dual publishing mode, use the following setting:

```toml
[pr_code_suggestions]
dual_publishing_score_threshold = x
```

Where x represents the minimum score threshold (>=) for suggestions to be presented as committable PR comments in addition to the table. Default is -1 (disabled).

### Self-review

`Platforms supported: GitHub, GitLab`

If you set in a configuration file:

```toml
[pr_code_suggestions]
demand_code_suggestions_self_review = true
```

The `improve` tool will add a checkbox below the suggestions, prompting user to acknowledge that they have reviewed the suggestions.
You can set the content of the checkbox text via:

```toml
[pr_code_suggestions]
code_suggestions_self_review_text = "... (your text here) ..."
```

![self_review_1](https://codium.ai/images/pr_agent/self_review_1.png){width=512}

!!! tip "Tip - Reducing visual footprint after self-review"

    The configuration parameter `pr_code_suggestions.fold_suggestions_on_self_review` (default is True)
    can be used to automatically fold the suggestions after the user clicks the self-review checkbox.

    This reduces the visual footprint of the suggestions, and also indicates to the PR reviewer that the suggestions have been reviewed by the PR author, and don't require further attention.

!!! tip "Tip - Demanding self-review from the PR author"

    By setting:
    ```toml
    [pr_code_suggestions]
    approve_pr_on_self_review = true
    ```
    the tool can automatically add an approval when the PR author clicks the self-review checkbox.


    - If you set the number of required reviewers for a PR to 2, this effectively means that the PR author must click the self-review checkbox before the PR can be merged (in addition to a human reviewer).

    ![self_review_2](https://codium.ai/images/pr_agent/self_review_2.png){width=512}

    - If you keep the number of required reviewers for a PR to 1 and enable this configuration, this effectively means that the PR author can approve the PR by actively clicking the self-review checkbox.

        To prevent unauthorized approvals, this configuration defaults to false, and cannot be altered through online comments; enabling requires a direct update to the configuration file and a commit to the repository. This ensures that utilizing the feature demands a deliberate documented decision by the repository owner.


### How many code suggestions are generated?

PR-Agent uses a dynamic strategy to generate code suggestions based on the size of the pull request (PR). Here's how it works:

#### 1. Chunking large PRs

- PR-Agent divides large PRs into 'chunks'.
- Each chunk contains up to `config.max_model_tokens` tokens (default: 32,000).

#### 2. Generating suggestions

- For each chunk, PR-Agent generates up to `pr_code_suggestions.num_code_suggestions_per_chunk` suggestions (default: 3).

This approach has two main benefits:

- Scalability: The number of suggestions scales with the PR size, rather than being fixed.
- Quality: By processing smaller chunks, the AI can maintain higher quality suggestions, as larger contexts tend to decrease AI performance.

Note: Chunking is primarily relevant for large PRs. For most PRs (up to 600 lines of code), PR-Agent will be able to process the entire code in a single call.

## Configuration options

???+ example "General options"

    <table>
      <tr>
        <td><b>extra_instructions</b></td>
        <td>Optional extra instructions to the tool. For example: "focus on the changes in the file X. Ignore change in ...".</td>
      </tr>
      <tr>
        <td><b>commitable_code_suggestions</b></td>
        <td>If set to true, the tool will display the suggestions as committable code comments. Default is false.</td>
      </tr>
      <tr>
        <td><b>dual_publishing_score_threshold</b></td>
        <td>Minimum score threshold for suggestions to be presented as committable PR comments in addition to the table. Default is -1 (disabled).</td>
      </tr>
      <tr>
        <td><b>focus_only_on_problems</b></td>
        <td>If set to true, suggestions will focus primarily on identifying and fixing code problems, and less on style considerations like best practices, maintainability, or readability. Default is true.</td> 
      </tr>
      <tr>
        <td><b>persistent_comment</b></td>
        <td>If set to true, the improve comment will be persistent, meaning that every new improve request will edit the previous one. Default is true.</td>
      </tr>
      <tr>
        <td><b>persistent_inline_comments</b></td>
        <td>Controls how inline suggestions are deduplicated across re-runs on the same PR/MR. <code>"update"</code> (default) edits the matching existing inline comment in place; <code>"skip"</code> leaves the existing one untouched; <code>"off"</code> always posts a new inline comment (legacy behavior). The dedup key is the hash of the proposed edit (file + normalized <code>improved_code</code>), with a fallback to a prose-based hash only when no <code>improved_code</code> is available. Line numbers are intentionally excluded so dedup stays stable across upstream pushes that drift the target line. See <a href="#how-inline-comment-deduplication-works">How inline-comment deduplication works</a> below for details.</td>
      </tr>
      <tr>
        <td><b>resolve_outdated_inline_comments</b></td>
        <td>When dedup is enabled (<code>persistent_inline_comments != "off"</code>), automatically resolve inline-comment threads whose suggestion was not re-emitted on the latest run; the thread body gets a short auto-resolve note. Default is true. Has no effect when <code>persistent_inline_comments = "off"</code>. Reviewers can manually unresolve an auto-resolved thread to opt it out of future auto-resolution &mdash; the bot detects the prior resolution marker in the body and respects it.</td>
      </tr>
      <tr>
        <td><b>suggestions_score_threshold</b></td>
        <td> Any suggestion with importance score less than this threshold will be removed. Default is 0. Highly recommend not to set this value above 7-8, since above it may clip relevant suggestions that can be useful. </td>
      </tr>
      <tr>
        <td><b>enable_help_text</b></td>
        <td>If set to true, the tool will display a help text in the comment. Default is false.</td>
      </tr>
      <tr>
        <td><b>enable_chat_text</b></td>
        <td>If set to true, the tool will display a reference to the PR chat in the comment. Default is false.</td>
      </tr>
      <tr>
        <td><b>publish_output_no_suggestions</b></td>
        <td>If set to true, the tool will publish a comment even if no suggestions were found. Default is true.</td>
      </tr>
    </table>

???+ example "Params for number of suggestions and AI calls"

    <table>
      <tr>
        <td><b>num_code_suggestions_per_chunk</b></td>
        <td>Number of code suggestions provided by the 'improve' tool, per chunk. Default is 3.</td>
      </tr>
      <tr>
        <td><b>max_number_of_calls</b></td>
        <td>Maximum number of chunks. Default is 3.</td>
      </tr>
    </table>

## Understanding AI Code Suggestions

- **AI Limitations:** AI models for code are getting better and better, but they are not flawless. Not all the suggestions will be perfect, and a user should not accept all of them automatically. Critical reading and judgment are required. Mistakes of the AI are rare but can happen, and it is usually quite easy for a human to spot them.
- **Purpose of Suggestions:**
    - **Self-reflection:** The suggestions aim to enable developers to _self-reflect_ and improve their pull requests. This process can help to identify blind spots, uncover missed edge cases, and enhance code readability and coherency. Even when a specific code suggestion isn't suitable, the underlying issue it highlights often reveals something important that might deserve attention.
    - **Bug detection:** The suggestions also alert on any _critical bugs_ that may have been identified during the analysis. This provides an additional safety net to catch potential issues before they make it into production. It's perfectly acceptable to implement only the suggestions you find valuable for your specific context.
- **Hierarchy:** Presenting the suggestions in a structured hierarchical table enables the user to _quickly_ understand them, and to decide which ones are relevant and which are not.
- **Customization:** To guide the model to suggestions that are more relevant to the specific needs of your project, we recommend using the [`extra_instructions`](./improve.md#extra-instructions-and-best-practices) and [`best practices`](./improve.md#best-practices) fields.
- **Model Selection:** For specific programming languages or use cases, some models may perform better than others.

## How inline-comment deduplication works

When `persistent_inline_comments` is enabled (the default is `"update"`), re-running `/improve` on the same PR/MR will recognize and update — instead of duplicating — inline comments that were already posted for the same suggestion on a previous run. This uses a hidden marker (an HTML comment of the form `<!-- pr-agent-inline-id:XXXX -->`) embedded in each inline-comment body.

### Identity rule

Two inline comments are considered the same suggestion when the marker matches. The marker is a short hash computed as follows:

- **Structured (preferred):** When the suggestion has an `improved_code` field (i.e., the model proposed replacement code), the hash covers `(file, normalized improved_code)`. Wording changes in the suggestion's prose do **not** affect the key; `label` is **not** part of the key either — the edit itself is the identity.
- **Prose fallback:** When no `improved_code` is present, the hash falls back to `(file, label, normalized prose prefix)`.

Normalization of `improved_code` expands tabs, strips trailing whitespace, drops leading/trailing blank lines, removes the longest common leading indent, and collapses internal whitespace runs — so reindentation of the same proposed edit does not split comments.

### Strict behaviour

This is intentionally a strict rule: when a suggestion has `improved_code`, its prose is never consulted for dedup. Two suggestions at the same spot with identical prose but **different** proposed edits are treated as distinct and remain as two separate inline comments. We'd rather under-merge (and show two comments) than over-merge two genuinely different fixes into one.

### What's invariant across runs

- Prose paraphrase of the same finding (same proposed edit) — does **not** split.
- Reindentation or whitespace variation in the proposed edit — does **not** split.
- Upstream commits that push the target line up or down in the file — do **not** split. Line numbers are not part of the key.

### What still splits

- A genuinely different proposed edit at the same spot — by design.
- A different `label` when the prose fallback is in use (e.g., the same prose-only suggestion now tagged "best practice" vs "possible issue").

### Future work

A fuzzy near-miss signal (e.g., shingle/Jaccard similarity) may be added later if users report recurring duplicates that this deterministic scheme doesn't catch. For now the behaviour is strictly deterministic, with no similarity threshold to tune.
