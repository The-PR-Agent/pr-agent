The different tools and sub-tools used by PR-Agent are adjustable via a Git configuration file.
There are four main ways to set persistent configurations:

1. [Wiki](./configuration_options.md#wiki-configuration-file) configuration page
2. [Local](./configuration_options.md#local-configuration-file) configuration file
3. [Global](./configuration_options.md#global-configuration-file) configuration file
4. [External configuration URL](./configuration_options.md#external-configuration-url) (CLI flag)

In terms of precedence, wiki configurations will override local configurations, local configurations will override global configurations, and global configurations will override an external configuration URL.


For a list of all possible configurations, see the [configuration options](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml) page.
In addition to general configuration options, each tool has its own configurations. For example, the `review` tool will use parameters from the [pr_reviewer](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml#L76) section in the configuration file.

!!! tip "Tip1: Edit only what you need"
    Your configuration file should be minimal, and edit only the relevant values. Don't copy the entire configuration options, since it can lead to legacy problems when something changes.
!!! tip "Tip2: Show relevant configurations"
    If you set `config.output_relevant_configurations` to True, each tool will also output in a collapsible section its relevant configurations. This can be useful for debugging, or getting to know the configurations better.



## Wiki configuration file

`Platforms supported: GitHub, GitLab, Bitbucket`

With PR-Agent, you can set configurations by creating a page called `.pr_agent.toml` in the [wiki](https://github.com/the-pr-agent/pr-agent/wiki/pr_agent.toml) of the repo.
The advantage of this method is that it allows to set configurations without needing to commit new content to the repo - just edit the wiki page and **save**.

![wiki_configuration](https://codium.ai/images/pr_agent/wiki_configuration.png){width=512}

Click [here](https://codium.ai/images/pr_agent/wiki_configuration_pr_agent.mp4) to see a short instructional video. We recommend surrounding the configuration content with triple-quotes (or \`\`\`toml), to allow better presentation when displayed in the wiki as markdown.
An example content:

```toml
[pr_description]
generate_ai_title=true
```

PR-Agent will know to remove the surrounding quotes when reading the configuration content.

## Local configuration file

`Platforms supported: GitHub, GitLab, Bitbucket, Azure DevOps`

By uploading a local `.pr_agent.toml` file to the root of the repo's default branch, you can edit and customize any configuration parameter. Note that you need to upload or update `.pr_agent.toml` before using the PR Agent tools (either at PR creation or via manual trigger) for the configuration to take effect.

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

`Platforms supported: GitHub`

By default, the local `.pr_agent.toml` is read from the repo's **default branch**. When running PR-Agent from the CLI (or any wrapper that exposes its arguments), you can point it at a different branch — for example to test configuration changes from a feature branch before merging them:

```bash
python -m pr_agent.cli \
  --pr_url=<PR URL> \
  --config-branch=<branch name> \
  review
```

Equivalently, set the `PR_AGENT_CONFIG_BRANCH` environment variable. The CLI flag takes precedence over the environment variable, and whitespace-only values are ignored.

If `.pr_agent.toml` cannot be loaded from the requested branch (e.g. the branch or file does not exist), PR-Agent logs a warning and falls back to the default branch.

!!! danger "Security: treat the config branch as privileged"
    By default, configuration is read from the **default branch**, so only users who can merge to it can change how PR-Agent behaves. `--config-branch` / `PR_AGENT_CONFIG_BRANCH` move that trust boundary to whatever branch you name.

    **Never set the config branch from untrusted or PR-derived input** (e.g. `--config-branch=$GITHUB_HEAD_REF` / `${{ github.head_ref }}` in CI). Doing so lets anyone who can push a branch to the repository supply their own `.pr_agent.toml` and control the review — for example pointing `model`/the API base at an attacker endpoint to exfiltrate the diff, injecting `extra_instructions`, or enabling auto-approval of their own PR. Always pin the config branch to a fixed, maintainer-controlled branch.

!!! note "GitHub only"
    Branch selection is currently implemented for GitHub. On all other platforms the `--config-branch` flag and `PR_AGENT_CONFIG_BRANCH` variable are ignored, and the local `.pr_agent.toml` is always read from the default branch.

## Global configuration file

`Platforms supported: GitHub, GitLab (cloud), Bitbucket (cloud)`

If you create a repo called `pr-agent-settings` in your **organization**, its configuration file `.pr_agent.toml` will be used as a global configuration file for any other repo that belongs to the same organization.
Parameters from a local `.pr_agent.toml` file, in a specific repo, will override the global configuration parameters.

For example, in the GitHub organization `qodo-ai`:

- The file [`https://github.com/the-pr-agent/pr-agent-settings/.pr_agent.toml`](https://github.com/the-pr-agent/pr-agent-settings/blob/main/.pr_agent.toml)  serves as a global configuration file for all the repos in the GitHub organization `qodo-ai`.

- The repo [`https://github.com/the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent/blob/main/.pr_agent.toml) inherits the global configuration file from `pr-agent-settings`.

## Project/Group level configuration file

`Platforms supported: GitLab, Bitbucket Data Center`

Create a repository named `pr-agent-settings` within a specific project (Bitbucket) or a group/subgroup (Gitlab). 
The configuration file in this repository will apply to all repositories directly under the same project/group/subgroup.

!!! note "Note"
    For Gitlab, in case of a repository nested in several sub groups, the lookup for a pr-agent-settings repo will be only on one level above such repository.


## Organization level configuration file

`Relevant platforms: Bitbucket Data Center`

Create a dedicated project to hold a global configuration file that affects all repositories across all projects in your organization.

**Setting up organization-level global configuration:**

1. Create a new project with both the name and key: PR_AGENT_SETTINGS.
2. Inside the PR_AGENT_SETTINGS project, create a repository named pr-agent-settings.
3. In this repository, add a `.pr_agent.toml` configuration file—structured similarly to the global configuration file described above.
4. Optionally, you can add organizational-level [global best practices](../tools/improve.md#global-hierarchical-best-practices).

Repositories across your entire Bitbucket organization will inherit the configuration from this file.

!!! note "Note"
    If both organization-level and project-level global settings are defined, the project-level settings will take precedence over the organization-level configuration. Additionally, parameters from a repository’s local .pr_agent.toml file will always override both global settings.

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
        < wiki .pr_agent.toml
          < environment variables (PR_AGENT__SECTION__KEY)
```

This means an external URL acts as an organization-wide *default* that any team can still override with their own `pr-agent-settings` or repo-local `.pr_agent.toml`.

### Security and limits

The external file is loaded through the same secure loader as the repo-local `.pr_agent.toml`: includes, preloads, custom loaders, and other directives that could execute code or read arbitrary files are rejected. The fetcher additionally:

- Limits the response size to **1 MB**
- Uses a **10-second** request timeout
- Only accepts `http`, `https`, `file` schemes (or a bare local path)

If the fetch fails, the request is logged and PR-Agent continues with the remaining configuration layers.
