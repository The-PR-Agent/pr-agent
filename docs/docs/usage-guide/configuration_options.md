---
title: "Configuration File"
sidebar_position: 3
---

The different tools and sub-tools used by PR-Agent are adjustable via a Git configuration file.
There are three main ways to set persistent configurations:

1. [Local](./configuration_options.md#local-configuration-file) configuration file
2. [Global](./configuration_options.md#global-configuration-file) configuration file
3. [External configuration URL](./configuration_options.md#external-configuration-url) (CLI flag)

In terms of precedence, local configurations will override global configurations, and global configurations will override an external configuration URL.


For a list of all possible configurations, see the [configuration options](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) page, or the rendered [Configuration Reference](./configuration_reference.md) which lists every option grouped by section.
In addition to general configuration options, each tool has its own configurations. For example, the `review` tool will use parameters from the [pr_reviewer](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) section in the configuration file.

:::tip[Tip1: Edit only what you need]
Your configuration file should be minimal, and edit only the relevant values. Don't copy the entire configuration options, since it can lead to legacy problems when something changes.
:::

:::tip[Tip2: Show relevant configurations]
If you set `config.output_relevant_configurations` to True, each tool will also output in a collapsible section its relevant configurations. This can be useful for debugging, or getting to know the configurations better.
:::



## Local configuration file

`Platforms supported: GitHub, GitLab, Bitbucket, Azure DevOps`

By uploading a local `.pr_agent.toml` file to the root of the repo's default branch, you can customize parameters that support repository-level overrides. Note that you need to upload or update `.pr_agent.toml` before using the PR Agent tools (either at PR creation or via manual trigger) for the configuration to take effect.

Provider endpoint settings are host-controlled: `openai.api_base`, `openai.api_type`, `openai.api_version`, `azure_ad.api_base`, `databricks.api_base`, `huggingface.api_base`, `moonshot.api_base`, `ollama.api_base`, and `openrouter.api_base` are ignored when set in repository-local `.pr_agent.toml` and must be configured on the host.

For example, if you set in `.pr_agent.toml`:

```
[pr_reviewer]
extra_instructions="""\
- instruction a
- instruction b
...
"""
```

Then you can give a list of extra instructions to the `review` tool.

### Loading the local configuration from a non-default branch

`Platforms supported: GitHub, GitLab`

By default, the local `.pr_agent.toml` is read from the repo's **default branch**. When running PR-Agent from the CLI (or any wrapper that exposes its arguments), you can point it at a different branch — for example to test configuration changes from a feature branch before merging them:

```bash
python -m pr_agent.cli \
  --pr_url=<PR URL> \
  --config-branch=<branch name> \
  review
```

Equivalently, set the `PR_AGENT_CONFIG_BRANCH` environment variable. The CLI flag takes precedence over the environment variable, and whitespace-only values are ignored.

If `.pr_agent.toml` cannot be loaded from the requested branch (e.g. the branch or file does not exist), PR-Agent logs a warning and falls back to the default branch.

:::danger[Security: treat the config branch as privileged]
By default, configuration is read from the **default branch**, so only users who can merge to it can change how PR-Agent behaves. `--config-branch` / `PR_AGENT_CONFIG_BRANCH` move that trust boundary to whatever branch you name.

**Never set the config branch from untrusted or PR-derived input** (e.g. `--config-branch=$GITHUB_HEAD_REF` / `${{ github.head_ref }}` in CI). Doing so lets anyone who can push a branch to the repository supply their own `.pr_agent.toml` and control the review — for example pointing `model`/the API base at an attacker endpoint to exfiltrate the diff, injecting `extra_instructions`, or enabling auto-approval of their own PR. Always pin the config branch to a fixed, maintainer-controlled branch.
:::

:::note[GitHub and GitLab only]
Branch selection is currently implemented for GitHub and GitLab. On all other platforms the `--config-branch` flag and `PR_AGENT_CONFIG_BRANCH` variable are ignored, and the local `.pr_agent.toml` is always read from the default branch.
:::

## Global configuration file

`Platforms supported: GitHub, GitLab, Bitbucket (cloud), Bitbucket Server, Azure DevOps, Gitea`

Namespace-wide settings come from a single settings repository that **you name explicitly**. Set `global_settings_repo` to the repository name (resolved inside the pull request's own organization/group/workspace):

```toml
[config]
global_settings_repo = "pr-agent-settings"
```

Its `.pr_agent.toml` (read from that repository's default branch) is applied to every repository in the same namespace:

- **GitHub:** `<organization>/<global_settings_repo>`
- **GitLab:** `<top-level-group>/<global_settings_repo>` (both GitLab.com and self-hosted GitLab)
- **Bitbucket (cloud):** `<workspace>/<global_settings_repo>`
- **Bitbucket Server:** `<project>/<global_settings_repo>`
- **Azure DevOps:** the repository named by `global_settings_repo`, looked up in the same project as the current repository
- **Gitea:** `<owner>/<global_settings_repo>`

A full `<namespace>/<name>` value is also accepted, and it must match the namespace of the pull request being handled.

`global_settings_repo` is **empty by default, which disables this feature**. Nothing is resolved by convention: a repository is never adopted at namespace scope just because it is named `pr-agent-settings`. Otherwise anyone able to create a repository inside a namespace could set the configuration used by every other repository in it. The value is host-only, so a repository's `.pr_agent.toml` and comment commands cannot set it either.

:::note[Caching]
In long-running deployments (the GitHub App / webhook server), the fetched global settings are cached **in-process** for up to 15 minutes to avoid re-fetching on every webhook event, so a change to the settings repository may take up to that long to take effect there. CLI and CI (GitHub Action) runs are short-lived processes, so they fetch the global settings once per invocation and always see the latest version.
:::

Two settings control this feature, both host-only and both required:

- `global_settings_repo` names the settings repository (empty disables it).
- `use_global_settings_file` is **enabled by default**; set it to `false` to rely only on each repo's local `.pr_agent.toml`.

```toml
[config]
global_settings_repo = "pr-agent-settings"
use_global_settings_file = false
```

Parameters from a local `.pr_agent.toml` file, in a specific repo, will override the global configuration parameters (the global file is merged *beneath* the repo-local one).
For GitHub Enterprise Server, use the same organization-level repository on your GHES host.
The app installation or token used by PR-Agent must have read access to both the pull request repository and the settings repository; otherwise, PR-Agent will skip the global configuration and continue with repository-local settings.

For example, with `global_settings_repo = "pr-agent-settings"` in a GitHub organization named `my-org`:

- The file `my-org/pr-agent-settings/.pr_agent.toml` (read from that repository's default branch) serves as a global configuration file for all the repos in the organization.

- A repository such as `my-org/my-repo` inherits that global configuration file, and may override any of its values in its own `.pr_agent.toml`.

## Project/Group level configuration file

`Platforms supported: GitLab, Bitbucket Data Center`

Create a repository named `pr-agent-settings` within a specific project (Bitbucket) or a group/subgroup (GitLab). This project/group-level lookup is a separate convention from the namespace-level [global configuration file](#global-configuration-file) above.
The configuration file in this repository will apply to all repositories directly under the same project/group/subgroup.

:::note[Note]
For GitLab, in case of a repository nested in several sub groups, the lookup for a pr-agent-settings repo will be only on one level above such repository.
:::

## External configuration URL

`Platforms supported: GitHub, GitLab, Bitbucket, Azure DevOps`

When running PR-Agent from the CLI (or any wrapper that exposes its arguments), you can merge an additional `.pr_agent.toml` from any URL or local path before the repo-local and global configurations are applied. This is useful when:

- You want a single shared configuration that applies to repositories nested deep inside subgroups, where the [project/group-level lookup](./configuration_options.md#projectgroup-level-configuration-file) only walks one level up.
- The shared configuration is published outside of a Git host (a static site, an internal artifact server, an S3 bucket, etc.).
- You want CI-time control over which defaults are layered in, without committing a file to the target repository.

### Usage

Pass `--extra_config_url` to the CLI, or set the `PR_AGENT_EXTRA_CONFIG_URL` environment variable:

```bash
python -m pr_agent.cli \
  --pr_url=<MR/PR URL> \
  --extra_config_url=https://config.example.com/pr-agent/shared.toml \
  review
```

Accepted values:

- `https://…` or `http://…` — fetched at runtime
- `file:///path/to/shared.toml` — read from the local filesystem
- A bare filesystem path — same as `file://`

### Authentication for private endpoints

For private endpoints (e.g. a GitLab API URL pointing at a private `pr-agent-settings` file), provide a single header via the `PR_AGENT_EXTRA_CONFIG_AUTH_HEADER` environment variable, formatted as `<HeaderName>: <value>`:

```bash
# GitLab Personal Access Token
export PR_AGENT_EXTRA_CONFIG_AUTH_HEADER="PRIVATE-TOKEN: <your-personal-access-token>"

# GitLab CI job token
export PR_AGENT_EXTRA_CONFIG_AUTH_HEADER="JOB-TOKEN: $CI_JOB_TOKEN"

# Generic bearer token
export PR_AGENT_EXTRA_CONFIG_AUTH_HEADER="Authorization: Bearer <your-token>"
```

### Precedence

External-URL settings are applied **first**, so every other layer overrides them:

```
built-in defaults
  < --extra_config_url
    < global pr-agent-settings
      < local .pr_agent.toml (repo default branch)
        < environment variables (PR_AGENT__SECTION__KEY)
```

This means an external URL acts as an organization-wide *default* that any team can still override with their own `pr-agent-settings` or repo-local `.pr_agent.toml`.

### Security and limits

The external file is loaded through the same secure loader as the repo-local `.pr_agent.toml`: includes, preloads, custom loaders, and other directives that could execute code or read arbitrary files are rejected. The fetcher additionally:

- Limits the response size to **1 MB**
- Uses a **10-second** request timeout
- Only accepts `http`, `https`, `file` schemes (or a bare local path)

If the fetch fails, the request is logged and PR-Agent continues with the remaining configuration layers.
