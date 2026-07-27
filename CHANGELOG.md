# Changelog

**Release notes now live on GitHub.** The authoritative, per-release changelog is the
[Releases page](https://github.com/The-PR-Agent/pr-agent/releases), which is generated from the
merged pull requests of each release. This file is no longer updated per release.

To see what changed between two versions, use the compare view — for example
[`v0.40.0...v0.41.0`](https://github.com/The-PR-Agent/pr-agent/compare/v0.40.0...v0.41.0).

---

## Archive

The entries below are a partial, date-based log kept in this file during July–August 2023, before
the project switched to GitHub Releases. They stop at `2023-08-03` and are retained for history
only; they do **not** describe any release after that date.

### 2023-08-03

#### Optimized

- Optimized PR diff processing by introducing caching for diff files, reducing the number of API calls.
- Refactored `load_large_diff` function to generate a patch only when necessary.
- Fixed a bug in the GitLab provider where the new file was not retrieved correctly.

### 2023-08-02

#### Enhanced

- Updated several tools in the `pr_agent` package to use commit messages in their functionality.
- Commit messages are now retrieved and stored in the `vars` dictionary for each tool.
- Added a section to display the commit messages in the prompts of various tools.

### 2023-08-01

#### Enhanced

- Introduced the ability to retrieve commit messages from pull requests across different git providers.
- Implemented commit messages retrieval for GitHub and GitLab providers.
- Updated the PR description template to include a section for commit messages if they exist.
- Added support for repository-specific configuration files (.pr_agent.yaml) for the PR Agent.
- Implemented this feature for both GitHub and GitLab providers.
- Added a new configuration option 'use_repo_settings_file' to enable or disable the use of a repo-specific settings file.

### 2023-07-30

#### Enhanced

- Added the ability to modify any configuration parameter from 'configuration.toml' on-the-fly.
- Updated the command line interface and bot commands to accept configuration changes as arguments.
- Improved the PR agent to handle additional arguments for each action.

### 2023-07-28

#### Improved

- Enhanced error handling and logging in the GitLab provider.
- Improved handling of inline comments and code suggestions in GitLab.
- Fixed a bug where an additional unneeded line was added to code suggestions in GitLab.

### 2023-07-26

#### Added

- New feature for updating the CHANGELOG.md based on the contents of a PR.
- Added support for this feature for the Github provider.
- New configuration settings and prompts for the changelog update feature.
