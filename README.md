

<br />

<div align="center">


<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://codium.ai/images/pr_agent/logo-dark.png" width="330">
  <source media="(prefers-color-scheme: light)" srcset="https://codium.ai/images/pr_agent/logo-light.png" width="330">
  <img src="https://codium.ai/images/pr_agent/logo-light.png" alt="logo" width="330">

</picture>
<br>
The Original Open-Source PR Reviewer
<br><br>
<a href="https://github.com/the-pr-agent/pr-agent/commits/main">
<img alt="GitHub" src="https://img.shields.io/github/last-commit/the-pr-agent/pr-agent/main?style=for-the-badge" height="20">
</a>
</div>

---

 This repository contains the open-source PR Agent Project. 
 It is not the Qodo offering for open-source projects.
 
PR-Agent is an open-source, AI-powered code review agent and a community-maintained legacy project of Qodo. It is distinct from Qodo's primary AI code review offering, which provides a feature-rich, context-aware experience. Qodo offers a free version for open-source projects and integrates seamlessly with GitHub, GitLab, Bitbucket, and Azure DevOps for high-quality automated reviews.


## Sponsors

PR-Agent is a community-maintained open-source project, with its ongoing development supported by our sponsors. If you'd like to support the project, consider [becoming a sponsor](https://github.com/sponsors/naorpeled).

<p align="center">
  <h3 align="center">🥇 Gold Sponsor</h3>
</p>

<p align="center">
  <a target="_blank" href="https://www.qodo.ai/">
    <img alt="Qodo — Gold sponsor" src="https://www.qodo.ai/wp-content/uploads/2025/03/qodo-logo.svg" width="300">
  </a>
</p>

<p align="center">
  <a target="_blank" href="https://www.qodo.ai/solutions/open-source/">Free version of Qodo for open-source projects</a>
</p>


## Table of Contents

- [Getting Started](#getting-started)
- [Why Use PR-Agent?](#why-use-pr-agent)
- [Features](#features)
- [See It in Action](#see-it-in-action)
- [How It Works](#how-it-works)
- [Data Privacy](#data-privacy)
- [Contributing](#contributing)

## Getting Started

> [!NOTE]
> **Docker Hub namespace migration.** Releases `0.34.2` and later are published under [`pragent/pr-agent`](https://hub.docker.com/r/pragent/pr-agent). Older releases (up to and including `v0.31`) remain available at the legacy [`codiumai/pr-agent`](https://hub.docker.com/r/codiumai/pr-agent) namespace as a frozen archive — no new images are pushed there. Update any pinned `image:` / `docker pull` / `uses: docker://` references when upgrading to `0.34.2+`.

### 🚀 Quick Start for PR-Agent

#### 1. GitHub Action (Recommended)

Add automated PR reviews to your repository with a simple workflow file:

```yaml
# .github/workflows/pr-agent.yml
name: PR Agent
on:
  pull_request:
    types: [opened, synchronize]
jobs:
  pr_agent_job:
    runs-on: ubuntu-latest
    steps:
    - name: PR Agent action step
      uses: the-pr-agent/pr-agent@main
      env:
        OPENAI_KEY: ${{ secrets.OPENAI_KEY }}
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```
[Full GitHub Action setup guide](https://docs.pr-agent.ai/installation/github/#run-as-a-github-action)

#### 2. CLI Usage (Local Development)

Run PR-Agent locally on your repository:

```bash
pip install pr-agent
export OPENAI_KEY=your_key_here
pr-agent --pr_url https://github.com/owner/repo/pull/123 review
```
[Complete CLI setup guide](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#local-repo-cli)

#### 3. Other Platforms

- [GitLab webhook setup](https://docs.pr-agent.ai/installation/gitlab/)
- [BitBucket app installation](https://docs.pr-agent.ai/installation/bitbucket/)
- [Azure DevOps setup](https://docs.pr-agent.ai/installation/azure/)

## News and Updates

Full notes for every release are on the [Releases page](https://github.com/the-pr-agent/pr-agent/releases).

### Jul 26, 2026 — [v0.41.0](https://github.com/the-pr-agent/pr-agent/releases/tag/v0.41.0)

Claude Opus 5 support, and `docker/mosaico` became a self-contained deployment bundle.

### Jul 25, 2026 — [v0.40.0](https://github.com/the-pr-agent/pr-agent/releases/tag/v0.40.0)

Default model moved to GPT-5.6, Gemini 3.6 support, persistent inline comments (no more duplicate
suggestions across runs), an OpenRouter provider-routing/reasoning config, a tokenless
[plain-diff provider](https://docs.pr-agent.ai/usage-guide/plain_diff_mode/), and CI artifact
context injection.

### Jul 5, 2026 — [v0.39.0](https://github.com/the-pr-agent/pr-agent/releases/tag/v0.39.0)

`AGENTS.md` and friends are now fed to `/review`, `/describe` and `/improve` **by default**, so the
model picks up your project's conventions out of the box. Also:
[Agent Skills (`SKILL.md`)](https://docs.pr-agent.ai/core-abilities/agent_skills/),
[organization-level settings](https://docs.pr-agent.ai/usage-guide/configuration_options/),
[restricted mode](https://docs.pr-agent.ai/usage-guide/additional_configurations/#restricted-mode)
for reduced GitHub permissions, GitHub Checks as an output target, and Claude Sonnet 5 support.

## Why Use PR-Agent?

### 🎯 Built for Real Development Teams

**Fast & Affordable**: Each tool (`/review`, `/improve`, `/ask`) uses a single LLM call (~30 seconds, low cost)

**Handles Any PR Size**: Our [PR Compression strategy](https://docs.pr-agent.ai/core-abilities/#pr-compression-strategy) effectively processes both small and large PRs

**Highly Customizable**: JSON-based prompting allows easy customization of review categories and behavior via [configuration files](pr_agent/settings/configuration.toml)

**Platform Agnostic**: 
- **Git Providers**: GitHub, GitLab, BitBucket, Azure DevOps, Gitea
- **Deployment**: CLI, GitHub Actions, Docker, self-hosted, webhooks
- **AI Models**: OpenAI GPT, Anthropic Claude, Google Gemini, DeepSeek, Mistral, and any other model reachable through LiteLLM (Azure OpenAI, AWS Bedrock, Vertex AI, Databricks, OpenRouter, Ollama, and more) — see [Changing a model](https://docs.pr-agent.ai/usage-guide/changing_a_model/)

**Open Source Benefits**:
- Full control over your data and infrastructure
- Customize prompts and behavior for your team's needs
- No vendor lock-in
- Community-driven development

## Features

<div style="text-align:left;">

PR-Agent offers comprehensive pull request functionalities integrated with various git providers:

|                                                         |                                                                                        | GitHub | GitLab | Bitbucket | Azure DevOps | Gitea |
|---------------------------------------------------------|----------------------------------------------------------------------------------------|:------:|:------:|:---------:|:------------:|:-----:|
| [TOOLS](https://docs.pr-agent.ai/tools/)         | [Describe](https://docs.pr-agent.ai/tools/describe/)                            |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [Review](https://docs.pr-agent.ai/tools/review/)                                |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [Improve](https://docs.pr-agent.ai/tools/improve/)                              |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [Ask](https://docs.pr-agent.ai/tools/ask/)                                      |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         | ⮑ [Ask on code lines](https://docs.pr-agent.ai/tools/ask/#ask-lines)            |   ✅   |   ✅   |           |              |       |
|                                                         | [Help Docs](https://docs.pr-agent.ai/tools/help_docs/) ⚠️                       |   —    |   —    |    —      |              |       |
|                                                         | [Update CHANGELOG](https://docs.pr-agent.ai/tools/update_changelog/)            |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         |                                                                                                                     |        |        |           |              |       |
| [USAGE](https://docs.pr-agent.ai/usage-guide/)   | [CLI](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#local-repo-cli)                            |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [App / webhook](https://docs.pr-agent.ai/usage-guide/automations_and_usage/#github-app)                      |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [Tagging bot](https://github.com/the-pr-agent/pr-agent#try-it-now)                                                     |   ✅   |        |           |              |       |
|                                                         | [Actions](https://docs.pr-agent.ai/installation/github/#run-as-a-github-action)                              |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         |                                                                                                                     |        |        |           |              |       |
| [CORE](https://docs.pr-agent.ai/core-abilities/) | [Adaptive and token-aware file patch fitting](https://docs.pr-agent.ai/core-abilities/compression_strategy/) |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         | [Agent skills (`SKILL.md`)](https://docs.pr-agent.ai/core-abilities/agent_skills/)                           |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [Repo context files (`AGENTS.md`)](https://docs.pr-agent.ai/usage-guide/additional_configurations/#bringing-per-repo-context-files-to-pr-agent) |   ✅   |   ✅   |    ✅     |      ✅      |  ✅   |
|                                                         | [Dynamic context](https://docs.pr-agent.ai/core-abilities/dynamic_context/)                                  |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         | [Fetching ticket context](https://docs.pr-agent.ai/core-abilities/fetching_ticket_context/)                  |   ✅    |  ✅    |     ✅     |              |       |
|                                                         | [Local and global metadata](https://docs.pr-agent.ai/core-abilities/metadata/)                               |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         | [Multiple models support](https://docs.pr-agent.ai/usage-guide/changing_a_model/)                            |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         | [PR compression](https://docs.pr-agent.ai/core-abilities/compression_strategy/)                              |   ✅   |   ✅   |    ✅     |      ✅      |       |
|                                                         | [Self reflection](https://docs.pr-agent.ai/core-abilities/self_reflection/)                                  |   ✅   |   ✅   |    ✅     |      ✅      |       |

⚠️ `/help_docs` is temporarily disabled since `v0.36.1` pending a fix for a credential-exposure issue ([#2445](https://github.com/the-pr-agent/pr-agent/issues/2445)).

[//]: # (- Support for additional git providers is described in [here]&#40;./docs/Full_environments.md&#41;)
___

## See It in Action

</div>
<h4><a href="https://github.com/the-pr-agent/pr-agent/pull/530">/describe</a></h4>
<div align="center">
<p float="center">
<img src="https://www.codium.ai/images/pr_agent/describe_new_short_main.png" width="512">
</p>
</div>
<hr>

<h4><a href="https://github.com/the-pr-agent/pr-agent/pull/732#issuecomment-1975099151">/review</a></h4>
<div align="center">
<p float="center">
<kbd>
<img src="https://www.codium.ai/images/pr_agent/review_new_short_main.png" width="512">
</kbd>
</p>
</div>
<hr>

<h4><a href="https://github.com/the-pr-agent/pr-agent/pull/732#issuecomment-1975099159">/improve</a></h4>
<div align="center">
<p float="center">
<kbd>
<img src="https://www.codium.ai/images/pr_agent/improve_new_short_main.png" width="512">
</kbd>
</p>
</div>

<hr>

### Usage Examples

PR-Agent tools run as a comment on a PR or from the CLI. A few common ones:

```bash
# Comment on a PR (GitHub/GitLab/Bitbucket/…):
/describe                        # generate title, summary, walkthrough and labels
/review                          # findings, security, review effort and tests
/improve                         # actionable code-improvement suggestions
/ask "What does this PR change?" # free-text Q&A about the PR

# Or locally via the CLI:
pr-agent --pr_url <PR_URL> review
```

See the [Tools docs](https://docs.pr-agent.ai/tools/#usage-examples) for the full list of tools with example commands, and each tool's page for screenshots and options.

<hr>

## How It Works

The following diagram illustrates PR-Agent tools and their flow:

![PR-Agent Tools](https://www.qodo.ai/images/pr_agent/diagram-v0.9.png)

## Data Privacy

### Self-hosted PR-Agent

- If you host PR-Agent with your OpenAI API key, it is between you and OpenAI. You can read their API data privacy policy here:
https://openai.com/enterprise-privacy

## Contributing

To contribute to the project, get started by reading our [Contributing Guide](https://github.com/the-pr-agent/pr-agent/blob/main/CONTRIBUTING.md).


## Big News for PR-Agent

PR-Agent has a new home!

After years of building this tool alongside the community, Qodo has donated PR-Agent to the open-source community - and we couldn't be more excited about what comes next.

The project now lives in the PR-Agent org on GitHub, is fully community-owned, and is open for contributions and additional maintainers.

What else changed: 
- Docs moved to - [docs.pr-agent.ai](https://docs.pr-agent.ai/)
- Qodo Merge (Qodo 1.0), the hosted URL, which was the enterprise version of PR-Agent, has been rebranded and evolved into Qodo (Qodo 2.0), a full AI code review platform.

## ❤️ Community

This open-source release remains here as a community contribution from Qodo — the origin of modern AI-powered code collaboration. We’re proud to share it and inspire developers worldwide.

The project now has its first external maintainer, Naor ([@naorpeled](https://github.com/naorpeled)), and is currently in the process of being donated to an open-source foundation.
