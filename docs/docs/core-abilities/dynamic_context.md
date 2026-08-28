
`Supported Git Platforms: GitHub, GitLab, Bitbucket`

PR-Agent uses an **asymmetric and dynamic context strategy** to improve AI analysis of code changes in pull requests.
It provides more context before changes than after, and dynamically adjusts the context based on code structure (e.g., enclosing functions or classes).
This approach balances providing sufficient context for accurate analysis, while avoiding needle-in-the-haystack information overload that could degrade AI performance or exceed token limits.

## Introduction

Pull request code changes are retrieved in a unified diff format, showing three lines of context before and after each modified section, with additions marked by '+' and deletions by '-'.

```diff
@@ -12,5 +12,5 @@ def func1():
 code line that already existed in the file...
 code line that already existed in the file...
 code line that already existed in the file....
-code line that was removed in the PR
+new code line added in the PR
 code line that already existed in the file...
 code line that already existed in the file...
 code line that already existed in the file...

@@ -26,2 +26,4 @@ def func2():
...
```

This unified diff format can be challenging for AI models to interpret accurately, as it provides limited context for understanding the full scope of code changes.
The presentation of code using '+', '-', and ' ' symbols to indicate additions, deletions, and unchanged lines respectively also differs from the standard code formatting typically used to train AI models.

## Challenges of expanding the context window

While expanding the context window is technically feasible, it presents a more fundamental trade-off:

Pros:

- Enhanced context allows the model to better comprehend and localize the code changes, results (potentially) in more precise analysis and suggestions. Without enough context, the model may struggle to understand the code changes and provide relevant feedback.

Cons:

- Excessive context may overwhelm the model with extraneous information, creating a "needle in a haystack" scenario where focusing on the relevant details (the code that actually changed) becomes challenging.
LLM quality is known to degrade when the context gets larger.
Pull requests often encompass multiple changes across many files, potentially spanning hundreds of lines of modified code. This complexity presents a genuine risk of overwhelming the model with excessive context.

- Increased context expands the token count, increasing processing time and cost, and may prevent the model from processing the entire pull request in a single pass.

## Asymmetric and dynamic context

To address these challenges, PR-Agent employs an **asymmetric** and **dynamic** context strategy, providing the model with more focused and relevant context information for each code change.

**Asymmetric:**

We start by recognizing that the context preceding a code change is typically more crucial for understanding the modification than the context following it.
Consequently, PR-Agent implements an asymmetric context policy, decoupling the context window into two distinct segments: one for the code before the change and another for the code after.

By independently adjusting each context window, PR-Agent can supply the model with a more tailored and pertinent context for individual code changes.

**Dynamic:**

We also employ a "dynamic" context strategy.
We start by recognizing that the optimal context for a code change often corresponds to its enclosing code component (e.g., function, class), rather than a fixed number of lines.
Consequently, we dynamically adjust the context window based on the code's structure, ensuring the model receives the most pertinent information for each modification.

To prevent overwhelming the model with excessive context, we impose a limit on the number of lines searched when identifying the enclosing component.
This balance allows for comprehensive understanding while maintaining efficiency and limiting context token usage.

## Appendix - relevant configuration options

```toml
[config]
patch_extension_skip_types =[".md",".txt"]  # Skip files with these extensions when trying to extend the context
allow_dynamic_context=true                  # Allow dynamic context extension
max_extra_lines_before_dynamic_context = 8  # will try to include up to X extra lines before the hunk in the patch, until we reach an enclosing function or class
patch_extra_lines_before = 3                # Number of extra lines (+3 default ones) to include before each hunk in the patch
patch_extra_lines_after = 1                 # Number of extra lines (+3 default ones) to include after each hunk in the patch
```
