---
title: "Supported platforms"
sidebar_position: 3
---

Which tools, triggers and core abilities work on each git provider.

|       |    | <span class="pra-provider"><span class="pra-logo pra-logo--github" aria-hidden="true"></span>GitHub</span> | <span class="pra-provider"><span class="pra-logo pra-logo--gitlab" aria-hidden="true"></span>GitLab</span> | <span class="pra-provider"><span class="pra-logo pra-logo--bitbucket" aria-hidden="true"></span>Bitbucket</span> | <span class="pra-provider"><span class="pra-logo pra-logo--azuredevops" aria-hidden="true"></span>Azure DevOps</span> | <span class="pra-provider"><span class="pra-logo pra-logo--gitea" aria-hidden="true"></span>Gitea</span> |
| ----- |---------------------------------------------------------------------------------------|:------:|:------:|:---------:|:------------:|:-----:|
| [TOOLS](../tools/index.md) | [Describe](../tools/describe.md)                                     |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Review](../tools/review.md)                                                           |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Improve](../tools/improve.mdx)                                                         |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Ask](../tools/ask.md)                                                                 |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Ask on code lines](../tools/ask.md#ask-lines)                                         |   ✅   |   ✅   |           |      ✅       |       |
|       | [Add Docs](../tools/add_docs.md)                                                       |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Generate Labels](../tools/generate_labels.md)                                         |   ✅   |   ✅   |   💬    |      ✅       |       |
|       | [Similar Issues](../tools/similar_issues.md)                                           |   ✅   |        |           |              |       |
|       | [Help](../tools/help.md)                                                               |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Help Docs](../tools/help_docs.md) ⚠️                                                   |        |        |           |              |       |
|       | [Update CHANGELOG](../tools/update_changelog.md)                                       |   ✅   |   ✅   |    ✅     |      💬       |  💬  |
|       |                                                                                       |        |        |           |              |       |
| [USAGE](../usage-guide/index.md) | [CLI](../usage-guide/automations_and_usage.md#local-repo-cli)      |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [App / webhook](../usage-guide/automations_and_usage.md#github-app)                    |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Tagging bot](https://github.com/the-pr-agent/pr-agent#try-it-now)                       |   ✅   |        |           |              |       |
|       | [Actions](../installation/github.md#run-as-a-github-action) <br /> [GitLab pipeline](../installation/gitlab.md#run-as-a-gitlab-pipeline) <br /> [Bitbucket pipeline](../installation/bitbucket.md#run-as-a-bitbucket-pipeline) <br /> [Azure DevOps pipeline](../installation/azure.md#azure-devops-pipeline) |   ✅   |   ✅   |    ✅     |      ✅       |       |
|       |                                                                                       |        |        |           |              |       |
| [CORE](../core-abilities/index.md) | [Adaptive and token-aware file patch fitting](../core-abilities/compression_strategy.md) |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Agent skills (`SKILL.md`)](../core-abilities/agent_skills.md)                         |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Repo context files (`AGENTS.md`)](../usage-guide/additional_configurations.md#bringing-per-repo-context-files-to-pr-agent) |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Compression strategy](../core-abilities/compression_strategy.md)                      |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Dynamic context](../core-abilities/dynamic_context.md)                                |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Fetching ticket context](../core-abilities/fetching_ticket_context.md)                |   ✅   |  ✅   |    ✅     |      ✅       |  ✅   |
|       | [Local and global metadata](../core-abilities/metadata.md)                             |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Multiple models support](../usage-guide/changing_a_model.md)                          |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |
|       | [Self reflection](../core-abilities/self_reflection.md)                                |   ✅   |   ✅   |    ✅     |      ✅       |  ✅   |

⚠️ `/help_docs` is temporarily disabled since v0.36.1 pending a fix for a credential-exposure issue ([#2445](https://github.com/The-PR-Agent/pr-agent/issues/2445)); see [Help Docs](../tools/help_docs.md).

Legend: ✅ = supported. 💬 = the tool runs, but its output is posted as a PR comment instead of being applied (Update CHANGELOG where the provider cannot push files, Generate Labels where it cannot set labels). Blank = not supported, or not yet verified (Generate Labels on Gitea).

## Gerrit and CodeCommit

Gerrit and CodeCommit are registered providers but are kept out of the main table to keep it readable. Support is:

|       |    | <span class="pra-provider"><span class="pra-logo pra-logo--gerrit" aria-hidden="true"></span>Gerrit</span> | <span class="pra-provider"><span class="pra-logo pra-logo--codecommit" aria-hidden="true"></span>CodeCommit</span> |
| ----- |---------------------------------------------------------------------------------------|:------:|:----------:|
| [TOOLS](../tools/index.md) | [Describe](../tools/describe.md), [Review](../tools/review.md), [Improve](../tools/improve.mdx), [Ask](../tools/ask.md), [Add Docs](../tools/add_docs.md), [Help](../tools/help.md) |   ✅   |     ✅     |
|       | [Ask on code lines](../tools/ask.md#ask-lines)                                         |        |            |
|       | [Generate Labels](../tools/generate_labels.md)                                         |   💬   |     💬     |
|       | [Update CHANGELOG](../tools/update_changelog.md)                                       |   💬   |     💬     |
|       | [Similar Issues](../tools/similar_issues.md)                                           |        |            |
| [USAGE](../usage-guide/index.md) | [CLI](../usage-guide/automations_and_usage.md#local-repo-cli)                  |   ✅   |     ✅     |
|       | [App / webhook](../usage-guide/automations_and_usage.md#github-app)                    |   ✅   |            |
|       | Tagging bot / Actions and pipelines                                                   |        |            |
| [CORE](../core-abilities/index.md) | [Agent skills (`SKILL.md`)](../core-abilities/agent_skills.md) and other core abilities |   ✅   |     ✅     |
|       | [Repo context files (`AGENTS.md`)](../usage-guide/additional_configurations.md#bringing-per-repo-context-files-to-pr-agent) |        |            |

Gerrit and CodeCommit do not support `gfm_markdown`, so `/describe` omits semantic file types and a few other sections. CodeCommit setup notes currently live in the [GitHub installation page](../installation/github.md); Gerrit has no dedicated installation page yet.

For Gerrit, the changed-file list and reviewed diff both respect `ignore.glob` and `ignore.regex`. Renamed files use their destination path, while deleted files retain their original path. Files with non-UTF-8 content remain in filename-based language detection but are omitted from the model diff.
