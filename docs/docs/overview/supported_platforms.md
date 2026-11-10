---
title: "Supported platforms"
sidebar_position: 3
---

Which commands, triggers and core abilities work on each git provider.

|       |    | <span class="pra-provider"><span class="pra-logo pra-logo--github" aria-hidden="true"></span>GitHub</span> | <span class="pra-provider"><span class="pra-logo pra-logo--gitlab" aria-hidden="true"></span>GitLab</span> | <span class="pra-provider"><span class="pra-logo pra-logo--bitbucket" aria-hidden="true"></span>Bitbucket Cloud</span> | <span class="pra-provider"><span class="pra-logo pra-logo--bitbucket" aria-hidden="true"></span>Bitbucket Server</span> | <span class="pra-provider"><span class="pra-logo pra-logo--azuredevops" aria-hidden="true"></span>Azure DevOps</span> | <span class="pra-provider"><span class="pra-logo pra-logo--gitea" aria-hidden="true"></span>Gitea</span> |
| ----- |---------------------------------------------------------------------------------------|:------:|:------:|:---------------:|:---------------:|:------------:|:-----:|
| [COMMANDS](../commands/index.md) | [Describe](../commands/describe.md)                                     |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Review](../commands/review.md)                                                           |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Improve](../commands/improve.mdx)                                                         |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Ask](../commands/ask.md)                                                                 |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Ask on code lines](../commands/ask.md#ask-lines)                                         |   ✅   |   ✅   |                 |                 |      ✅       |       |
|       | [Add Docs](../commands/add_docs.md)                                                       |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Generate Labels](../commands/generate_labels.md)                                         |   ✅   |   ✅   |       💬        |       💬        |      ✅       |  ✅   |
|       | [Similar Issues](../commands/similar_issues.md)                                           |   ✅   |        |                 |                 |              |       |
|       | [Help](../commands/help.md)                                                               |   ✅   |   ✅   |   ✅[^help]    |       ✅        |      ✅       |  ✅   |
|       | [Help Docs](../commands/help_docs.md) ⚠️                                                   |        |        |                 |                 |              |       |
|       | [Update CHANGELOG](../commands/update_changelog.md)                                       |   ✅   |   ✅   |       ✅        |       💬        |      💬       |  💬  |
|       |                                                                                       |        |        |                 |                 |              |       |
| [USAGE](../usage-guide/index.md) | [CLI](../usage-guide/automations_and_usage.md#local-repo-cli)      |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [App / webhook](../usage-guide/automations_and_usage.md#github-app)                    |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Tagging bot](../installation/index.md#github-polling-http-requests)                     |   ✅   |        |                 |                 |      ✅       |       |
|       | [Actions](../installation/github.md#run-as-a-github-action) <br /> [GitLab pipeline](../installation/gitlab.md#run-as-a-gitlab-pipeline) <br /> [Bitbucket pipeline](../installation/bitbucket.md#run-as-a-bitbucket-pipeline) <br /> [Azure DevOps pipeline](../installation/azure.md#azure-devops-pipeline) |   ✅   |   ✅   |       ✅        |                 |      ✅       |       |
|       |                                                                                       |        |        |                 |                 |              |       |
| [CORE](../core-abilities/index.md) | [Adaptive and token-aware file patch fitting](../core-abilities/compression_strategy.md) |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Agent skills (`SKILL.md`)](../core-abilities/agent_skills.md)                         |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Repo context files (`AGENTS.md`)](../usage-guide/additional_configurations.md#bringing-per-repo-context-files-to-pr-agent) |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Compression strategy](../core-abilities/compression_strategy.md)                      |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Dynamic context](../core-abilities/dynamic_context.md)                                |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Fetching ticket context](../core-abilities/fetching_ticket_context.md)                |   ✅   |  ✅   |    ✅[^tickets]  |    ✅[^tickets]  |      ✅       | ✅[^tickets] |
|       | [Local and global metadata](../core-abilities/metadata.md)                             |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |
|       | [Multiple models support](../usage-guide/changing_a_model.md)                          |   ✅   |   ✅   |       ✅        |       ✅        |      ✅       |  ✅   |

⚠️ `/help_docs` is temporarily disabled since v0.36.1 pending a fix for a credential-exposure issue ([#2445](https://github.com/The-PR-Agent/pr-agent/issues/2445)); see [Help Docs](../commands/help_docs.md).

Legend: ✅ = supported. 💬 = the command runs, but its output is posted as a PR comment instead of being applied (Update CHANGELOG where the provider cannot push files, Generate Labels where it cannot set labels). Blank = not supported.

[^tickets]: On Bitbucket Cloud, Bitbucket Server, and Gitea, fetching ticket context means the Jira, Asana, and Linear integrations. Native issue links are used on GitHub and GitLab, and work items on Azure DevOps.

[^help]: `/help <question>` is supported, but `/help` without a question displays an unsupported-format notice instead of the command list on Bitbucket Cloud, Gerrit and CodeCommit.

[^help-list]: Plain-diff mode supports the `/help` command list only. `/help <question>` is currently unavailable in both local and plain-diff modes; local mode displays an unsupported-format notice for `/help` without a question.

## Diff-only providers

The `local`, `plain-diff`, and MOSAICO modes do not use a hosting platform. The [local provider](../usage-guide/local_git_provider.md) compares branches in a local checkout, [plain-diff](../usage-guide/plain_diff_mode.md) reads a unified diff from stdin or a file, and the [MOSAICO A2A server](../installation/mosaico_server.md) accepts a PR URL or diff over A2A. They write results to files or stdout, or return them as a task artifact, instead of publishing comments or applying changes.

|       |    | <span class="pra-provider"><span class="pra-logo pra-logo--terminal" aria-hidden="true"></span>Local</span> | <span class="pra-provider"><span class="pra-logo pra-logo--terminal" aria-hidden="true"></span>Plain diff</span> | MOSAICO |
| ----- |---------------------------------------------------------------------------------------|:-----:|:-----:|:-----:|
| [COMMANDS](../commands/index.md) | [Describe](../commands/describe.md), [Review](../commands/review.md), [Improve](../commands/improve.mdx), [Ask](../commands/ask.md) |   ✅   |   ✅   |   ✅   |
|       | [Add Docs](../commands/add_docs.md)                                                       |   ✅   |   ✅   |       |
|       | [Generate Labels](../commands/generate_labels.md)                                         |   💬   |   💬   |       |
|       | [Similar Issues](../commands/similar_issues.md)                                           |        |        |       |
|       | [Help](../commands/help.md)                                                               |        | ✅[^help-list] |       |
|       | [Update CHANGELOG](../commands/update_changelog.md)                                       |   💬   |   💬   |       |
| [USAGE](../usage-guide/index.md) | [CLI](../usage-guide/automations_and_usage.md#local-repo-cli)                  |   ✅   |   ✅   |       |
|       | A2A server                                                                             |        |        |   ✅   |

Core abilities that work from the diff itself, such as compression, multiple models, and self reflection, apply to all three modes. Dynamic context cannot add lines absent from a supplied diff unless matching working-tree files are available in plain-diff mode; local mode uses the checkout's file contents. Repo context files apply only to local mode. Output marked 💬 is written to the local output instead of a PR comment.

## Gerrit and CodeCommit

Gerrit and CodeCommit are registered providers but are kept out of the main table to keep it readable. Support is:

|       |    | <span class="pra-provider"><span class="pra-logo pra-logo--gerrit" aria-hidden="true"></span>Gerrit</span> | <span class="pra-provider"><span class="pra-logo pra-logo--codecommit" aria-hidden="true"></span>CodeCommit</span> |
| ----- |---------------------------------------------------------------------------------------|:------:|:----------:|
| [COMMANDS](../commands/index.md) | [Describe](../commands/describe.md), [Review](../commands/review.md), [Improve](../commands/improve.mdx), [Ask](../commands/ask.md), [Add Docs](../commands/add_docs.md) |   ✅   |     ✅     |
|       | [Help](../commands/help.md)                                                               | ✅[^help] | ✅[^help] |
|       | [Ask on code lines](../commands/ask.md#ask-lines)                                         |        |            |
|       | [Generate Labels](../commands/generate_labels.md)                                         |   💬   |     💬     |
|       | [Update CHANGELOG](../commands/update_changelog.md)                                       |   💬   |     💬     |
|       | [Similar Issues](../commands/similar_issues.md)                                           |        |            |
| [USAGE](../usage-guide/index.md) | [CLI](../usage-guide/automations_and_usage.md#local-repo-cli)                  |   ✅   |     ✅     |
|       | [App / webhook](../usage-guide/automations_and_usage.md#github-app)                    |   ✅   |            |
|       | Tagging bot / Actions and pipelines                                                   |        |            |
| [CORE](../core-abilities/index.md) | [Agent skills (`SKILL.md`)](../core-abilities/agent_skills.md) and other core abilities |   ✅   |     ✅     |
|       | [Repo context files (`AGENTS.md`)](../usage-guide/additional_configurations.md#bringing-per-repo-context-files-to-pr-agent) |        |            |

Gerrit and CodeCommit do not support `gfm_markdown`, so `/describe` omits semantic file types and a few other sections. CodeCommit setup notes currently live in the [GitHub installation page](../installation/github.md); Gerrit has no dedicated installation page yet.

For Gerrit, the changed-file list and reviewed diff both respect `ignore.glob` and `ignore.regex`. Renamed files use their destination path, while deleted files retain their original path. Files with non-UTF-8 content remain in filename-based language detection but are omitted from the model diff.
