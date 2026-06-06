"""Maps MOSAICO's env-var contract onto pr-agent's Dynaconf settings and registers
the Langfuse litellm callback. Every function is a no-op unless the corresponding
MOSAICO env var is set: importing or calling apply_mosaico_env() with no MOSAICO env
present changes nothing."""
import os

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

ENV_API_BASE = "API_BASE"
ENV_API_KEY = "API_KEY"
ENV_MODEL_NAME = "MODEL_NAME"
ENV_MODEL_MAX_TOKENS = "MODEL_MAX_TOKENS"
ENV_LANGFUSE_HOST = "LANGFUSE_HOST"
# Token budget for a MOSAICO model not in pr-agent's built-in MAX_TOKENS table.
DEFAULT_CUSTOM_MODEL_MAX_TOKENS = 32000
ENV_LANGFUSE_PUBLIC_KEY = "LANGFUSE_PUBLIC_KEY"
ENV_LANGFUSE_SECRET_KEY = "LANGFUSE_SECRET_KEY"


def langfuse_env_present() -> bool:
    return bool(os.getenv(ENV_LANGFUSE_HOST) and os.getenv(ENV_LANGFUSE_PUBLIC_KEY) and os.getenv(ENV_LANGFUSE_SECRET_KEY))


def apply_mosaico_env() -> None:
    """Idempotent. Call once at MOSAICO-server startup, BEFORE LiteLLMAIHandler() is
    constructed. Does nothing when MOSAICO env is absent."""
    settings = get_settings()
    api_base = os.getenv(ENV_API_BASE)
    api_key = os.getenv(ENV_API_KEY)
    model_name = os.getenv(ENV_MODEL_NAME)

    if api_base:
        settings.set("OPENAI.API_BASE", api_base)
    if api_key:
        settings.set("OPENAI.KEY", api_key)
    if model_name:
        model = model_name if "/" in model_name else f"openai/{model_name}"
        settings.set("CONFIG.MODEL", model)
        settings.set("CONFIG.FALLBACK_MODELS", [])
        # MOSAICO models are not in pr-agent's built-in MAX_TOKENS table; declare a budget
        # so reviews don't fail with "not defined in MAX_TOKENS". Overridable via env.
        max_tokens_env = os.getenv(ENV_MODEL_MAX_TOKENS)
        try:
            custom_max_tokens = int(max_tokens_env) if max_tokens_env else DEFAULT_CUSTOM_MODEL_MAX_TOKENS
        except ValueError:
            custom_max_tokens = DEFAULT_CUSTOM_MODEL_MAX_TOKENS
        if custom_max_tokens <= 0:
            get_logger().warning(
                f"MOSAICO: MODEL_MAX_TOKENS={custom_max_tokens!r} is non-positive; "
                f"falling back to DEFAULT_CUSTOM_MODEL_MAX_TOKENS={DEFAULT_CUSTOM_MODEL_MAX_TOKENS}."
            )
            custom_max_tokens = DEFAULT_CUSTOM_MODEL_MAX_TOKENS
        settings.set("CONFIG.CUSTOM_MODEL_MAX_TOKENS", custom_max_tokens)

    if langfuse_env_present():
        _register_langfuse_callback(settings)
    else:
        get_logger().info("MOSAICO: Langfuse env not fully set; LLM-call tracing disabled.")


def _register_langfuse_callback(settings) -> None:
    for key in ("LITELLM.SUCCESS_CALLBACK", "LITELLM.FAILURE_CALLBACK"):
        current = list(settings.get(key, []) or [])
        if "langfuse" not in current:
            current.append("langfuse")
            settings.set(key, current)
    settings.set("LITELLM.ENABLE_CALLBACKS", True)
    get_logger().info("MOSAICO: registered Langfuse litellm callback.")
