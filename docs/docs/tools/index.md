# Tools

Each PR-Agent tool has a dedicated page that explains its behavior and usage:

- [PR Description (`/describe`)](./describe.md)
- [PR Review (`/review`)](./review.md)
- [Code Suggestions (`/improve`)](./improve.md)
- [Question Answering (`/ask ...`)](./ask.md)
- [Add Documentation (`/add_docs`)](./add_docs.md)
- [Generate Labels (`/generate_labels`)](./generate_labels.md)
- [Similar Issues (`/similar_issue`)](./similar_issues.md)
- [Help (`/help`)](./help.md)
- [Help Docs (`/help_docs`)](./help_docs.md)
- [Update Changelog (`/update_changelog`)](./update_changelog.md)

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
