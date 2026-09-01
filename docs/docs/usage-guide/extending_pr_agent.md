# Extending PR-Agent

PR-Agent is made of replaceable pieces: the AI model behind the tools, the git provider it talks to, and the individual tools (such as `review` or `describe`) that form its command surface. This page explains where each piece lives and how to add a new one. It is aimed at contributors, not users — see the [Changing a Model](./changing_a_model.md) page for the operator-side configuration guide.

## Adding a model

All AI calls go through [LiteLLM](https://docs.litellm.ai/) (see `pr_agent/algo/ai_handlers/litellm_ai_handler.py`), so adding a model usually needs no code:

1. Set `model` (and optionally `fallback_models`) in `pr_agent/settings/configuration.toml` to the LiteLLM model name.
2. If the model needs special handling — no temperature support, extended thinking, streaming quirks — the relevant lists live in `pr_agent/algo/__init__.py` (for example `NO_SUPPORT_TEMPERATURE_MODELS` or `CLAUDE_EXTENDED_THINKING_MODELS`). Add the model name there and cover it with a unit test.

Never hard-code model names in tool code; keep them in configuration.

## Adding a git provider

A git provider adapts PR-Agent to a specific git hosting platform:

1. Implement a provider class in `pr_agent/git_providers/` that extends the `GitProvider` interface in `pr_agent/git_providers/git_provider.py` (see `gitlab_provider.py` for a reference).
2. Register it in the `_GIT_PROVIDERS` map in `pr_agent/git_providers/__init__.py` under a short name.
3. Set `git_provider` in `pr_agent/settings/configuration.toml` to that name.
4. Add an `installation/<name>.md` page (see `installation/gitlab.md` for a reference) and register it in the Installation section of `docs/mkdocs.yml`.
5. Gate provider-dependent behavior with capability checks like `provider.is_supported("feature")` instead of concrete provider-type checks, since providers can stub or override capabilities.

## Adding a tool

A tool is a class that consumes PR data from the git provider and produces a response (the `PRReviewer` from `review` and `PRDescription` from `describe` are the usual references):

1. Implement the tool under `pr_agent/tools/`, following the existing conventions. Tool and prompt names normally correspond: `pr_reviewer.py` ↔ `pr_reviewer_prompts.toml` under `pr_agent/settings/`.
2. Register any new prompt TOML in the `settings_files=[...]` list in `pr_agent/config_loader.py`, or it will not be loaded into settings.
3. Register the tool in `command2class` in `pr_agent/agent/pr_agent.py` under a command name. That map is what makes the tool reachable from comment commands and the CLI.
4. Add a `tools/<name>.md` page (see `tools/review.md`) with usage and command examples, and register it under Tools in `docs/mkdocs.yml`.
5. Add unit tests under `tests/unittest/`.
