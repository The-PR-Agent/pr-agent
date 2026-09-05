# Tools

Here is a list of PR-Agent tools, each with a dedicated page that explains how to use it:

| Tool                                                                                     | Description                                                                                                                                 |
|------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| **[PR Description (`/describe`)](./describe.md)**                                        | Automatically generating PR description - title, type, summary, code walkthrough and labels                                                 |
| **[PR Review (`/review`)](./review.md)**                                                 | Adjustable feedback about the PR, possible issues, security concerns, review effort and more                                                |
| **[Code Suggestions (`/improve`)](./improve.md)**                                        | Code suggestions for improving the PR                                                                                                       |
| **[Question Answering (`/ask ...`)](./ask.md)**                                          | Answering free-text questions about the PR, or on specific code lines                                                                       |
| **[Add Documentation (`/add_docs`)](./add_docs.md)**                                     | Generate documentation for code components that are missing it                                                                              |
| **[Generate Labels (`/generate_labels`)](./generate_labels.md)**                         | Generate custom labels for the PR based on the code changes                                                                                 |
| **[Similar Issues (`/similar_issue`)](./similar_issues.md)**                             | Find similar issues in the repository based on the current issue                                                                            |
| **[Help (`/help`)](./help.md)**                                                          | Provides a list of all the available tools                                                                                                  |
| **[Help Docs (`/help_docs`)](./help_docs.md)**                                           | Answer a free-text question based on a git documentation folder                                                                             |
| **[Update Changelog (`/update_changelog`)](./update_changelog.md)**                      | Automatically updating the CHANGELOG.md file with the PR changes                                                                            |

## Usage examples

Each tool can be triggered in two ways:

- **As a comment** — write the command (e.g. `/review`) as a comment, and PR-Agent replies. Most tools are commented on a PR; issue-scoped tools such as `similar_issue` are commented on an issue.
- **From the [CLI](../usage-guide/automations_and_usage.md#local-repo-cli)** — run `python -m pr_agent.cli --pr_url=<PR_URL> <tool>`. Issue-scoped tools take `--issue_url=<ISSUE_URL>` instead of `--pr_url`. The module form works only from an environment where the `pr_agent` package is importable (for example, the venv created by `uv sync`). If `pr-agent` is on your `PATH`, run it directly.

Both accept the same tool arguments and [configuration overrides](../usage-guide/configuration_options.md).

| Tool                                     | Comment                          | CLI                                                             |
|------------------------------------------|----------------------------------|----------------------------------------------------------------|
| [Describe](./describe.md)                | `/describe`                      | `python -m pr_agent.cli --pr_url=<PR_URL> describe`             |
| [Review](./review.md)                    | `/review`                        | `python -m pr_agent.cli --pr_url=<PR_URL> review`              |
| [Improve](./improve.md)                  | `/improve`                       | `python -m pr_agent.cli --pr_url=<PR_URL> improve`             |
| [Ask](./ask.md)                          | `/ask "How does X work?"`        | `python -m pr_agent.cli --pr_url=<PR_URL> ask "How does X work?"` |
| [Add Docs](./add_docs.md)                | `/add_docs`                      | `python -m pr_agent.cli --pr_url=<PR_URL> add_docs`           |
| [Generate Labels](./generate_labels.md)  | `/generate_labels`               | `python -m pr_agent.cli --pr_url=<PR_URL> generate_labels`     |
| [Similar Issues](./similar_issues.md)    | `/similar_issue`                 | `python -m pr_agent.cli --issue_url=<ISSUE_URL> similar_issue` |
| [Help](./help.md)                        | `/help`                          | `python -m pr_agent.cli --pr_url=<PR_URL> help`                |
| [Update Changelog](./update_changelog.md)| `/update_changelog`              | `python -m pr_agent.cli --pr_url=<PR_URL> update_changelog`    |

`/help_docs` is temporarily disabled (see [#2445](https://github.com/The-PR-Agent/pr-agent/issues/2445)) and is therefore omitted from the table above.

For screenshots, arguments, and a walkthrough of a typical use case, see the **Example usage** section on each tool's page linked above.
