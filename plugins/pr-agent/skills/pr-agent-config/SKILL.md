---
name: pr-agent-config
description: >-
  Configure PR-Agent with .pr_agent.toml, a global configuration, or an
  external URL: precedence, per-command sections like pr_reviewer and
  pr_description, extra_instructions, host-controlled settings that the repo
  file cannot set, changing the model, and testing config from a branch. Use
  when tuning PR-Agent output or debugging why a setting has no effect.
---

# Configuring PR-Agent

References: `docs/docs/usage-guide/configuration_options.md`,
`docs/docs/usage-guide/configuration_reference.md`,
`docs/docs/usage-guide/changing_a_model.md`,
`pr_agent/settings/configuration.toml`.

## Where settings come from

1. Local `.pr_agent.toml` at the repo root, read from the default branch.
2. Global configuration (org-level).
3. External configuration URL (CLI flag).

Local overrides global, global overrides the external URL. Environment
variables (`SECTION__KEY`) and command arguments (`--section.key=value`)
also work.

## Keep the file minimal

Only set what you change; do not copy the whole defaults file.

```toml
[pr_reviewer]
extra_instructions = """\
- Focus on error handling and input validation
- Flag any new dependency
"""
num_max_findings = 5

[pr_description]
generate_ai_title = true
```

Each command reads its own section (`review` reads `[pr_reviewer]`,
`describe` reads `[pr_description]`, and so on).

## Host-controlled settings

Ignored in `.pr_agent.toml` and rejected in command arguments; set them on
the host: provider endpoints (`openai.api_base`, `ollama.api_base`,
`openrouter.api_base`, ...), auth/TLS (`github.deployment_type`,
`gitlab.auth_type`, `gitlab.ssl_verify`, ...) and Jira credentials. For
Gitea, repository-local settings need `GITEA__REPO_SETTING=.pr_agent.toml`
on the host.

## Debugging

- `config.output_relevant_configurations = true` prints the settings each
  command used.
- Test changes before merging (GitHub, GitLab):

```bash
python -m pr_agent.cli --pr_url=<PR URL> --config-branch=<branch> review
```

## Changing the model

Set `config.model` (and fallbacks) plus the provider's key section; see
`docs/docs/usage-guide/changing_a_model.md` for provider-specific keys
(Azure OpenAI needs extra settings). Local models via Ollama work but may
need workload-specific evaluation.
