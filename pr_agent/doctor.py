"""Preflight checks that catch a broken deployment before a full run (#4320).

`pr-agent doctor` reports, without touching any git provider, whether the active
configuration can actually reach a model: the model is set, any required static
API key is present, and a completion succeeds. Ambient authentication and custom
endpoints defer to the completion. The exit code is 0 only when every check
passes, so CI setups can gate on it. Enabled output sinks receive an
additional basic settings check, without constructing sinks or probing their destinations.
"""

import os

from pr_agent.algo.ai_handlers.cloud_auth import (
    DUMMY_LITELLM_API_KEY,
    OPENAI_COMPATIBLE_REQUEST_PROVIDERS,
    PROVIDER_API_BASE_ENV_VARS,
    PROVIDER_API_KEY_ENV_VARS,
    PROVIDER_SETTING_ALIASES,
    PROVIDER_ROUTING_ENV_VARS,
    _is_openai_compatible_request_provider,
)
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.output_sinks import _check_output_sinks
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import redact_credentials
from pr_agent.log import get_logger

DOCTOR_COMMANDS = ("doctor",)

_PING_SYSTEM = "You are a health check probe. Reply with exactly: ok"
_PING_USER = "ping"


def _active_provider(model: str, handler: LiteLLMAIHandler | None = None) -> str:
    """Resolve explicit provider selection before inferring the configured model provider."""
    configured = getattr(handler, "_custom_llm_provider", "")
    if configured:
        return PROVIDER_SETTING_ALIASES.get(configured, configured)
    if "/" in model:
        return model.split("/", 1)[0].strip().lower()
    # Unprefixed models are not always OpenAI's, and an OpenAI-compatible gateway keeps its
    # key under "openai" anyway: litellm's own resolver is the source of truth. Unknown ids
    # defer to the ping.
    try:
        from litellm import get_llm_provider

        return get_llm_provider(model=model)[1]
    except Exception:
        return "unknown"


def _check_config(model: str) -> tuple[bool, str]:
    if model:
        return True, f"config.model = {model}"
    return False, "config.model is empty; set it in configuration.toml or CONFIG__MODEL"


def _uses_ambient_auth_or_custom_endpoint(provider: str, handler: LiteLLMAIHandler) -> bool:
    """Defer static key checks for ambient authentication and explicit endpoints."""
    settings = get_settings()
    ambient = settings.get("AZURE_AD.CLIENT_ID", None) or provider.startswith(("bedrock", "azure"))
    custom_endpoint = settings.get(f"{provider}.api_base", None)
    endpoint_env_vars = (
        PROVIDER_API_BASE_ENV_VARS.get(provider, ())
        + PROVIDER_ROUTING_ENV_VARS.get(provider, {}).get("api_base", ())
    )
    compatible_fallback = provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS or (
        _is_openai_compatible_request_provider(provider)
        and not getattr(handler, "_provider_request_params", {}).get(provider)
        and not getattr(handler, "_provider_environment_api_keys", {}).get(provider)
    )
    # Use Azure aliases for OpenAI-prefixed models routed to Azure.
    if (
        provider == "openai"
        and settings.get("OPENAI.API_TYPE", None) == "azure"
        and not getattr(handler, "_custom_llm_provider", "")
    ):
        endpoint_env_vars += PROVIDER_ROUTING_ENV_VARS["azure"]["api_base"]
    elif provider == "openai" or (
        compatible_fallback and settings.get("OPENAI.API_TYPE", None) != "azure"
    ):
        custom_endpoint = custom_endpoint or settings.get("openai.api_base", None)
        endpoint_env_vars += ("OPENAI_BASE_URL", "OPENAI_API_BASE")
    return bool(ambient or custom_endpoint or any(
        os.environ.get(name) for name in endpoint_env_vars if name != "CLOUDFLARE_ACCOUNT_ID"
    ))


def _check_provider_key(handler: LiteLLMAIHandler, provider: str) -> tuple[bool, str]:
    """Check whether a required static key is present, without printing credentials."""
    env_vars = PROVIDER_API_KEY_ENV_VARS.get(provider, ())
    key = handler._captured_api_key(provider)
    if provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS and not getattr(handler, "_openai_api_base_is_azure", False):
        # Honor the compatible handler's inherited settings key and native-key precedence.
        params = getattr(handler, "_provider_request_params", {})
        openai_params = params.get("openai", {})
        inherited_key = openai_params.get("api_key")
        native_base = params.get("openai_like", {}).get("api_base")
        if provider == "custom_openai":
            key = inherited_key or key
        elif not key and (not native_base or native_base == openai_params.get("api_base")):
            key = inherited_key
    if key and key != DUMMY_LITELLM_API_KEY:
        source = next((name for name in env_vars if os.environ.get(name)), None)
        origin = f"env {source}" if source else "settings"
        return True, f"API key for '{provider}' found ({origin})"
    if _uses_ambient_auth_or_custom_endpoint(provider, handler):
        return True, f"no static key for '{provider}' (ambient auth or custom endpoint); ping decides"
    if key == DUMMY_LITELLM_API_KEY:
        return False, f"API key for '{provider}' is the placeholder; set a real key"
    if env_vars:
        names = " or ".join(env_vars)
        return False, f"no API key for '{provider}'; set {names} or the provider settings key"
    # Unknown to the env table: keyless auth (Azure AD, Bedrock IAM) or an exotic
    # provider. The ping below decides whether the deployment actually works.
    return True, f"no static key for '{provider}' (keyless auth or custom provider); ping decides"


def missing_provider_key_env_vars() -> tuple[str, tuple[str, ...]] | None:
    """(provider, env var names) to name when the active provider's key is missing.

    Returns None when the run can proceed: a usable key, ambient auth, or a custom
    endpoint. Shared by `doctor` and the GitHub Action runner, so a deployment is
    judged the same way by hand and in CI. Callers that surface CI output render only
    the env var names — they come from the constant PROVIDER_API_KEY_ENV_VARS table,
    so nothing settings- or key-derived can flow into a message; the provider is for
    lookup keys only.
    """
    model = str(get_settings().config.get("model", "") or "")
    handler = LiteLLMAIHandler()
    provider = _active_provider(model, handler)
    ok, _detail = _check_provider_key(handler, provider)
    if ok:
        return None
    return provider, PROVIDER_API_KEY_ENV_VARS.get(provider, ())


async def _check_model_ping(handler: LiteLLMAIHandler, model: str) -> tuple[bool, str]:
    try:
        await handler.chat_completion(model, _PING_SYSTEM, _PING_USER)
    except Exception as error:
        # Provider errors can quote request URLs or response bodies; redact the way the
        # rest of the codebase does before the text reaches stdout or a shareable log.
        reason = redact_credentials(str(error).split("\n")[0])[:200]
        get_logger().debug(f"Doctor model ping failed: {redact_credentials(repr(error))}")
        return False, f"model completion failed: {reason}"
    return True, "model completion succeeded"


async def run_doctor_checks() -> int:
    """Run every check, print one line per check, and return the process exit code."""
    model = str(get_settings().config.get("model", "") or "")
    handler = LiteLLMAIHandler()
    provider = _active_provider(model, handler)

    results = [
        _check_config(model),
        _check_provider_key(handler, provider),
        await _check_model_ping(handler, model),
    ]
    try:
        sink_result = _check_output_sinks(get_settings().get("push_outputs", {}) or {})
    except Exception:
        # Settings operations can include secret-bearing values in their exceptions.
        sink_result = False, "push_outputs configuration could not be checked"
    if sink_result is not None:
        results.append(sink_result)

    lines = []
    for ok, detail in results:
        lines.append(f"{'✓' if ok else '✗'} {detail}")
    passed = sum(1 for ok, _ in results if ok)
    print("\n".join(lines))
    print(f"{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1
