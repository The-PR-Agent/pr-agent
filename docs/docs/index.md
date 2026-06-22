# Overview

[PR-Agent](https://github.com/the-pr-agent/pr-agent) is an open-source, AI-powered code review agent and a community-maintained legacy project of Qodo. It is distinct from Qodo's primary AI code review offering, which provides a feature-rich, context-aware experience. Qodo now offers a free tier that integrates seamlessly with GitHub, GitLab, Bitbucket, and Azure DevOps for high-quality automated reviews.

- See the [Installation Guide](./installation/index.md) for instructions on installing and running the tool on different git platforms.

- See the [Usage Guide](./usage-guide/index.md) for instructions on running commands via different interfaces, including _CLI_, _online usage_, or by _automatically triggering_ them when a new PR is opened.

- See the [Tools Guide](./tools/index.md) for a detailed description of the different tools.

## Docs Smart Search

To search the documentation site using natural language:

1) Comment `/help "your question"` in a pull request where PR-Agent is installed

2) The bot will respond with an [answer](https://github.com/the-pr-agent/pr-agent/pull/1241#issuecomment-2365259334) that includes relevant documentation links.

## Features

PR-Agent offers comprehensive pull request functionalities integrated with various git providers:

|       |                                                                                       | GitHub | GitLab | Bitbucket | Azure DevOps | Gitea |
| ----- |---------------------------------------------------------------------------------------|:------:|:------:|:---------:|:------------:|:-----:|
| [TOOLS](./tools/index.md) | [Describe](./tools/describe.md)                                     |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Review](./tools/review.md)                                                           |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Improve](./tools/improve.md)                                                         |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Ask](./tools/ask.md)                                                                 |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | ⮑ [Ask on code lines](./tools/ask.md#ask-lines)                                       |   ✅   |   ✅   |           |              |       |
|       | [Add Docs](./tools/add_docs.md)                                                       |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Generate Labels](./tools/generate_labels.md)                                         |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Similar Issues](./tools/similar_issues.md)                                           |   ✅   |        |           |              |       |
|       | [Help](./tools/help.md)                                                               |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Help Docs](./tools/help_docs.md)                                                     |   ✅   |   ✅   |    ✅     |              |       |
|       | [Update CHANGELOG](./tools/update_changelog.md)                                       |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       |                                                                                       |        |        |           |              |       |
| [USAGE](./usage-guide/index.md) | [CLI](./usage-guide/automations_and_usage.md#local-repo-cli)      |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [App / webhook](./usage-guide/automations_and_usage.md#github-app)                    |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Tagging bot](https://github.com/the-pr-agent/pr-agent#try-it-now)                       |   ✅   |        |           |              |       |
|       | [Actions](./installation/github.md#run-as-a-github-action)                            |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       |                                                                                       |        |        |           |              |       |
| [CORE](./core-abilities/index.md) | [Adaptive and token-aware file patch fitting](./core-abilities/compression_strategy.md) |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Chat on code suggestions](./core-abilities/interactivity.md)                         |   ✅   |  ✅   |           |              |       |
|       | [Compression strategy](./core-abilities/compression_strategy.md)                      |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Dynamic context](./core-abilities/dynamic_context.md)                                |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Fetching ticket context](./core-abilities/fetching_ticket_context.md)                |   ✅   |  ✅   |    ✅     |              |       |
|       | [Interactivity](./core-abilities/interactivity.md)                                    |   ✅   |  ✅   |           |              |       |
|       | [Local and global metadata](./core-abilities/metadata.md)                             |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Multiple models support](./usage-guide/changing_a_model.md)                          |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       | [Self reflection](./core-abilities/self_reflection.md)                                |   ✅   |   ✅   |    ✅     |      ✅       |       |

## Example Results

<hr>

#### [/describe](https://github.com/the-pr-agent/pr-agent/pull/530)

<figure markdown="1">
![/describe](https://www.codium.ai/images/pr_agent/describe_new_short_main.png){width=512}
</figure>
<hr>

#### [/review](https://github.com/the-pr-agent/pr-agent/pull/732#issuecomment-1975099151)

<figure markdown="1">
![/review](https://www.codium.ai/images/pr_agent/review_new_short_main.png){width=512}
</figure>
<hr>

#### [/improve](https://github.com/the-pr-agent/pr-agent/pull/732#issuecomment-1975099159)

<figure markdown="1">
![/improve](https://www.codium.ai/images/pr_agent/improve_new_short_main.png){width=512}
</figure>
<hr>

## How it Works

The following diagram illustrates PR-Agent tools and their flow:

![PR-Agent Tools](https://codium.ai/images/pr_agent/diagram-v0.9.png)

Check out the [PR Compression strategy](core-abilities/index.md) page for more details on how we convert a code diff to a manageable LLM prompt
