"""Resolve which BaseAiHandler implementation a run should use.

Selection order:
  1. `config.ai_handler`, when set explicitly ("litellm", "openai", "cli_agent", "langchain").
  2. Otherwise inferred from `config.model`: a "cli/" prefix means a local coding-agent CLI.
  3. Otherwise LiteLLM, which is the historical default for every entry point.

Handlers are imported lazily inside the resolver so that optional dependencies
(langchain in particular, which is not in requirements.txt) only load when actually
selected, and so importing this module stays cheap for the common LiteLLM path.

The return value is a class, not an instance: the tools take a callable and construct
the handler themselves (`self.ai_handler = ai_handler()`), which is also why a
`functools.partial` is an equally valid thing for a caller to inject.
"""
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

CLI_MODEL_PREFIX = "cli/"


def _load(name: str):
    if name == "litellm":
        from pr_agent.algo.ai_handlers.litellm_ai_handler import \
            LiteLLMAIHandler
        return LiteLLMAIHandler
    if name == "openai":
        from pr_agent.algo.ai_handlers.openai_ai_handler import OpenAIHandler
        return OpenAIHandler
    if name == "cli_agent":
        from pr_agent.algo.ai_handlers.cli_agent_ai_handler import \
            CliAgentAIHandler
        return CliAgentAIHandler
    if name == "langchain":
        from pr_agent.algo.ai_handlers.langchain_ai_handler import \
            LangChainOpenAIHandler
        return LangChainOpenAIHandler
    raise ValueError(
        f"Unknown config.ai_handler '{name}'. "
        f"Supported: litellm, openai, cli_agent, langchain"
    )


def get_ai_handler_class() -> type[BaseAiHandler]:
    """Return the handler class for the current settings.

    Call this per request rather than at import time: `config.model` and
    `config.ai_handler` can both be overridden by a repo's .pr_agent.toml, which
    apply_repo_settings() merges in only once the target PR is known.
    """
    configured = str(get_settings().get("CONFIG.AI_HANDLER", "") or "").strip().lower()
    if configured:
        return _load(configured)

    model = str(get_settings().get("CONFIG.MODEL", "") or "")
    if model.startswith(CLI_MODEL_PREFIX):
        get_logger().debug(f"Model '{model}' selects the cli_agent handler")
        return _load("cli_agent")

    return _load("litellm")
