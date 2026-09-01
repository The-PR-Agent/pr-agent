# Extending PR-Agent

Contributors extending a model, git provider, or tool start here. To only change
the model, use [Changing a Model](./changing_a_model.md).

## Adding a model

Tool calls go through LiteLLM (`pr_agent/algo/ai_handlers/litellm_ai_handler.py`), the default handler. Most models need only configuration:

```toml
[config]
model="<model-name>"
fallback_models=["<fallback-model-name>"]
```

Set these under `[config]` in `pr_agent/settings/configuration.toml`.
Keep model names in configuration, not in tool code.

Models that behave differently are registered in `pr_agent/algo/__init__.py`:
`NO_SUPPORT_TEMPERATURE_MODELS` for models that reject a temperature
parameter; `CLAUDE_EXTENDED_THINKING_MODELS` for Claude models that
take extended thinking. Add the exact model name to the matching list.

Context windows are registered in `MAX_TOKENS` in `pr_agent/algo/__init__.py`:
add the model name and its context-window token count, or set
`config.custom_model_max_tokens` in `configuration.toml`. Without
either, `get_max_tokens()` raises.

Verify with `PYTHONPATH=. uv run pytest tests/unittest`.

## Adding a git provider

Implement a `GitProvider` subclass and register it:

1. Create `pr_agent/git_providers/<name>_provider.py`, extending the interface in `pr_agent/git_providers/git_provider.py` (`gitlab_provider.py` is the reference).
2. Register the class in `_GIT_PROVIDERS` in `pr_agent/git_providers/__init__.py`. Keys already used: `github`, `gitlab`, `bitbucket`, `bitbucket_server`, `azure`, `codecommit`, `local`, `gerrit`, `gitea`, `plain-diff`.
3. Select it via `[config]` → `git_provider="<name>"` in `pr_agent/settings/configuration.toml`.
4. Add `docs/docs/installation/<name>.md` (see [`gitlab.md`](../installation/gitlab.md)) and register it under `Installation` in `docs/mkdocs.yml`.
5. Select provider-dependent behavior with capability checks like `provider.is_supported("feature")` rather than provider-type checks.
6. Add unit tests under `tests/unittest/test_<name>_provider.py` (see `test_bitbucket_provider.py`) and list the required env vars in `pr_agent/settings/.secrets_template.toml`.

## Adding a tool

1. Implement the tool class in `pr_agent/tools/pr_<name>.py` with an `async def run(self)` entry point (see `pr_reviewer.py`).
2. Add a `[pr_<tool>]` section in `pr_agent/settings/configuration.toml` for the option keys the tool reads (`[pr_reviewer]` is the pattern to follow).
3. Add a prompt TOML under `pr_agent/settings/` and register it in the `settings_files=[...]` list in `pr_agent/config_loader.py` — it is not loaded otherwise.
4. Match the TOML section name to the settings key the tool reads: `[pr_review_prompt]` in `pr_reviewer_prompts.toml` ↔ `get_settings().pr_review_prompt` in `pr_reviewer.py`.
5. Register the tool in `command2class` in `pr_agent/agent/pr_agent.py` under a command name, e.g. `"my_tool": PRMyTool`. Then add it to the hardcoded help surfaces, or it will not show up in `/help`: `pr_agent/tools/pr_help_message.py`, `pr_agent/servers/help.py`, and the command list in `pr_agent/cli.py`.
6. Add a row to the tool list in `docs/docs/tools/index.md`, a page `docs/docs/tools/<name>.md` (see [`review.md`](../tools/review.md)), and register the page under `Tools` in `docs/mkdocs.yml`.
7. Add tests under `tests/unittest/` and verify with `PYTHONPATH=. uv run pytest tests/unittest`.
