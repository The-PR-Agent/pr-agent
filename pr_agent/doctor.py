"""Preflight checks that catch a broken deployment before a full run (#4320).

`pr-agent doctor` reports, without touching any git provider, whether the active
configuration can actually reach a model: the model is set, any required static
API key is present, and a completion succeeds. Ambient authentication and custom
endpoints defer to the completion. The exit code is 0 only when every check
passes, so CI setups can gate on it. Enabled output sinks receive an
additional basic settings check, without constructing sinks or probing their destinations.
"""

import os
import sys

from pr_agent.algo.ai_handlers.cloud_auth import (
    DUMMY_LITELLM_API_KEY,
    OPENAI_COMPATIBLE_REQUEST_PROVIDERS,
    PROVIDER_API_BASE_ENV_VARS,
    PROVIDER_API_KEY_ENV_VARS,
    PROVIDER_ROUTING_ENV_VARS,
    PROVIDER_SETTING_ALIASES,
    _is_openai_compatible_request_provider,
    _resolve_provider_registry,
    _uses_openai_responses_transport,
)
from pr_agent.algo.ai_handlers.litellm_ai_handler import (
    PROVIDER_SETTING_PATHS,
    LiteLLMAIHandler,
)
from pr_agent.algo.output_sinks import _check_output_sinks
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import redact_credentials
from pr_agent.log import get_logger

DOCTOR_COMMANDS = ("doctor",)

# Only these constant identifiers and messages may be rendered by the Action gate.
_WATSONX_HEADER_SOURCE = ("LITELLM.EXTRA_HEADERS.Authorization",)
_WATSONX_TOKEN_SOURCE = ("WATSONX_TOKEN",)
_WATSONX_ZEN_SOURCE = ("WATSONX_ZENAPIKEY",)
PROVIDER_AUTH_CORRECTIONS = {
    _WATSONX_HEADER_SOURCE: (
        "Correct the last case-insensitive Authorization entry in LITELLM.EXTRA_HEADERS, "
        "or remove all Authorization entries there; they override Watsonx tokens and API keys."
    ),
    _WATSONX_TOKEN_SOURCE: (
        "Correct or remove WATSONX_TOKEN; it overrides WATSONX_ZENAPIKEY and the Watsonx API key."
    ),
    _WATSONX_ZEN_SOURCE: (
        "Correct or remove WATSONX_ZENAPIKEY; it overrides the Watsonx API key."
    ),
}

_PING_SYSTEM = "You are a health check probe. Reply with exactly: ok"
_PING_USER = "ping"

# Providers with a usable default local endpoint and optional native API-key authentication.
_KEYLESS_DEFAULT_ENDPOINT_PROVIDERS = {"docker_model_runner", "lemonade", "llamafile", "ollama"}


def _active_provider(model: str, handler: LiteLLMAIHandler | None = None) -> str:
    """Resolve explicit provider selection before inferring the configured model provider."""
    if handler is not None:
        configured = handler._custom_llm_provider
        if configured:
            return PROVIDER_SETTING_ALIASES.get(configured, configured)
        model = handler._route_model(model, handler.deployment_id)
    if "/" in model:
        prefix = model.split("/", 1)[0].strip().lower()
        return PROVIDER_SETTING_ALIASES.get(prefix, prefix)
    # Unprefixed models are not always OpenAI's, and an OpenAI-compatible gateway keeps its
    # key under "openai" anyway: litellm's own resolver is the source of truth. Unknown ids
    # defer to the ping.
    try:
        from litellm import get_llm_provider

        provider = get_llm_provider(model=model)[1]
        return PROVIDER_SETTING_ALIASES.get(provider, provider)
    except Exception:
        return "unknown"


def _check_config(model: str) -> tuple[bool, str]:
    if model:
        return True, f"config.model = {model}"
    return False, "config.model is empty; set it in configuration.toml or CONFIG__MODEL"


def _uses_ambient_auth_or_custom_endpoint(provider: str, handler: LiteLLMAIHandler) -> bool:
    """Defer static key checks for ambient authentication and explicit endpoints."""
    if provider in ("watsonx", "watsonx_text"):
        # A Watsonx URL is routing, never authentication. Credentials are checked separately.
        return False
    settings = get_settings()
    anthropic_token = (handler._anthropic_auth_token or "").strip()
    ambient = (
        settings.get("AZURE_AD.CLIENT_ID", None)
        or provider.startswith(("bedrock", "azure"))
        or (
            provider == "anthropic"
            and anthropic_token not in ("", DUMMY_LITELLM_API_KEY)
            and not handler._captured_api_key("anthropic")
        )
    )
    api_base_setting = PROVIDER_SETTING_PATHS.get(provider, {}).get("api_base")
    custom_endpoint = settings.get(api_base_setting, None) if api_base_setting else None
    # Only registry-defined explicit endpoint aliases qualify. Do not trust Cloudflare's
    # account-ID URL or OpenRouter's native-key default URL as keyless gateways.
    registry = _resolve_provider_registry()
    registry_config = registry.get(provider) if registry is not None else None
    registry_endpoint = (
        handler._provider_request_params.get(provider, {}).get("api_base")
        if provider not in ("cloudflare", "openrouter") and getattr(registry_config, "api_base_env", None)
        else None
    )
    endpoint_env_vars = (
        PROVIDER_API_BASE_ENV_VARS.get(provider, ())
        + PROVIDER_ROUTING_ENV_VARS.get(provider, {}).get("api_base", ())
    )
    compatible_fallback = provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS or (
        registry is not None
        and _is_openai_compatible_request_provider(provider)
        and not handler._provider_request_params.get(provider)
        and not handler._provider_environment_api_keys.get(provider)
    )
    # Automatic Azure routing already resolves to "azure"; honor the effective provider.
    if provider == "openai" or (
        compatible_fallback and settings.get("OPENAI.API_TYPE", None) != "azure"
    ):
        custom_endpoint = custom_endpoint or settings.get("openai.api_base", None)
        endpoint_env_vars += ("OPENAI_BASE_URL", "OPENAI_API_BASE")
    return bool(ambient or custom_endpoint or registry_endpoint or any(
        os.environ.get(name) for name in endpoint_env_vars if name != "CLOUDFLARE_ACCOUNT_ID"
    ))


def _check_watsonx_auth(
    handler: LiteLLMAIHandler, provider: str,
) -> tuple[bool, str, tuple[str, ...]] | None:
    """Check captured Watsonx overrides and retain their constant source identifiers."""
    if provider not in ("watsonx", "watsonx_text"):
        return None
    # Match LiteLLM's authentication precedence: explicit header, bearer, Zen, API key.
    for name, value in reversed(tuple((handler._request_headers or {}).items())):
        if name.lower() != "authorization":
            continue
        value = value.strip() if isinstance(value, str) else ""
        parts = value.split(maxsplit=1)
        if parts and parts[0].lower() in ("bearer", "zenapikey"):
            credential = parts[1].strip() if len(parts) > 1 else ""
        else:
            credential = value
        if credential and credential != DUMMY_LITELLM_API_KEY:
            return True, f"Authorization header for '{provider}' captured (ping decides)", _WATSONX_HEADER_SOURCE
        return False, f"Authorization header for '{provider}' is missing or placeholder", _WATSONX_HEADER_SOURCE
    params = handler._provider_request_params.get(provider, {})
    for parameter, source in (("token", _WATSONX_TOKEN_SOURCE), ("zen_api_key", _WATSONX_ZEN_SOURCE)):
        selected = params.get(parameter)
        if selected:
            normalized = selected.strip() if isinstance(selected, str) else ""
            if not normalized or normalized == DUMMY_LITELLM_API_KEY:
                return False, f"{source[0]} for '{provider}' is blank or placeholder", source
            return True, f"bearer or Zen credentials for '{provider}' captured (ping decides)", source
    return None


def _volcengine_uses_responses_transport(handler: LiteLLMAIHandler) -> bool:
    """Resolve the same model and transport inputs the handler uses for its request."""
    model = str(get_settings().config.get("model", "") or "")
    custom_provider = handler._custom_llm_provider or ""
    deployment_id = handler.deployment_id
    routed_model = handler._route_model_for_request(model, custom_provider, deployment_id)
    request_provider = handler._resolve_configured_request_provider(routed_model, custom_provider)
    if request_provider != "volcengine":
        return False
    completion_model = handler._normalize_gpt5_model_for_request(routed_model, model, custom_provider)
    transport_provider = (
        custom_provider or handler._resolve_request_transport_provider(routed_model) or request_provider
    )
    transport_model = (
        handler._request_deployment_id(routed_model, request_provider, deployment_id) or completion_model
    )
    return _uses_openai_responses_transport(transport_model, transport_provider)


def _check_provider_key(handler: LiteLLMAIHandler, provider: str) -> tuple[bool, str]:
    """Check whether a required static key is present, without printing credentials."""
    env_vars = PROVIDER_API_KEY_ENV_VARS.get(provider, ())
    key = handler._captured_api_key(provider)
    watsonx_auth = _check_watsonx_auth(handler, provider)
    if watsonx_auth is not None:
        return watsonx_auth[0], watsonx_auth[1]
    if provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS and not handler._openai_api_base_is_azure:
        # Honor the compatible handler's inherited settings key and native-key precedence.
        params = handler._provider_request_params
        openai_params = params.get("openai", {})
        inherited_key = openai_params.get("api_key")
        native_base = params.get("openai_like", {}).get("api_base")
        if provider == "custom_openai":
            key = inherited_key or key
        elif not key and (not native_base or native_base == openai_params.get("api_base")):
            key = inherited_key
    if provider == "openrouter" and not key and not handler._openai_api_base_is_azure:
        # Match the handler's gateway fallback only when no native OpenRouter identity was captured.
        params = handler._provider_request_params
        environment_keys = handler._provider_environment_api_keys
        openai_params = params.get("openai", {})
        inherited_key = openai_params.get("api_key") or environment_keys.get("openai")
        if (
            not params.get("openrouter")
            and not environment_keys.get("openrouter")
            and openai_params.get("api_base")
            and inherited_key
        ):
            if inherited_key == DUMMY_LITELLM_API_KEY:
                return True, f"no static key for '{provider}' (keyless OpenAI gateway); ping decides"
            return True, "OpenRouter uses captured OpenAI gateway credentials"
    if provider == "volcengine" and _volcengine_uses_responses_transport(handler):
        # Responses uses native Volcengine credentials before the captured ARK key.
        # A provider-specific endpoint may be keyless; an unrelated OpenAI gateway may not.
        responses_key = key or handler._volcengine_ark_api_key
        if responses_key == DUMMY_LITELLM_API_KEY:
            native_endpoint = handler._provider_request_params.get(provider, {}).get("api_base")
            if native_endpoint:
                return True, "Volcengine Responses uses captured endpoint (ping decides)"
            return False, "Volcengine Responses API key is the placeholder; set a real key"
        if responses_key:
            return True, "Volcengine Responses API key found (captured)"
    if key and key != DUMMY_LITELLM_API_KEY:
        source = next((name for name in env_vars if os.environ.get(name)), None)
        origin = f"env {source}" if source else "settings"
        return True, f"API key for '{provider}' found ({origin})"
    if _uses_ambient_auth_or_custom_endpoint(provider, handler):
        return True, f"no static key for '{provider}' (ambient auth or custom endpoint); ping decides"
    if provider in _KEYLESS_DEFAULT_ENDPOINT_PROVIDERS:
        return True, f"no static key for '{provider}' (keyless local endpoint); ping decides"
    if key == DUMMY_LITELLM_API_KEY:
        return False, f"API key for '{provider}' is the placeholder; set a real key"
    if env_vars:
        names = " or ".join(env_vars)
        return False, f"no API key for '{provider}'; set {names} or the provider settings key"
    # Unknown to the env table: keyless auth (Azure AD, Bedrock IAM) or an exotic
    # provider. The ping below decides whether the deployment actually works.
    return True, f"no static key for '{provider}' (keyless auth or custom provider); ping decides"


def missing_provider_key_env_vars() -> tuple[str, tuple[str, ...]] | None:
    """Return the provider and constant identifiers of its failing credentials.

    Return None when preflight permits the run. Preserve Watsonx override sources
    instead of reporting native API-key names that cannot fix them. Identifiers
    come only from constant tuples or PROVIDER_API_KEY_ENV_VARS; never pass captured
    credential values, header values, or free-form diagnostic details to CI output.
    """
    model = str(get_settings().config.get("model", "") or "")
    handler = LiteLLMAIHandler()
    provider = _active_provider(model, handler)
    watsonx_auth = _check_watsonx_auth(handler, provider)
    if watsonx_auth is not None:
        ok, _detail, sources = watsonx_auth
    else:
        ok, _detail = _check_provider_key(handler, provider)
        sources = PROVIDER_API_KEY_ENV_VARS.get(provider, ())
    if ok:
        return None
    return provider, sources


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


def _print_report(report: str) -> None:
    try:
        print(report)
    except UnicodeEncodeError:
        # A redirected stdout uses the locale encoding (cp1252 on Windows), which cannot
        # encode the check marks; emit UTF-8 bytes instead of losing the report.
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            raise
        sys.stdout.flush()
        buffer.write((report + "\n").encode("utf-8"))
        buffer.flush()


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
    lines.append(f"{passed} of {len(results)} checks passed")
    _print_report("\n".join(lines))
    return 0 if passed == len(results) else 1
