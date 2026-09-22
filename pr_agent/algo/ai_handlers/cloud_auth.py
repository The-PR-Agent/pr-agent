"""Cloud-credential plumbing for the LiteLLM model handler.

Extracted verbatim from ``litellm_ai_handler`` (see issue #3508): Azure OIDC
token exchange, Vertex WIF/ADC loading, Bedrock Mantle request signing, IMDS
snapshots, SDK header bridges and the request-local API-key guard overrides
they install. Behaviour is deliberately unchanged: ``litellm_ai_handler``
re-imports every name defined here, so module attributes and bare references
resolve exactly as before the move.
"""

import configparser
import copy
import inspect
import json
import os
import re
import shutil
import sys
from contextvars import ContextVar
from functools import lru_cache, wraps
from types import FunctionType, SimpleNamespace
from urllib.parse import urlparse

import litellm
import openai

try:
    from litellm.llms.anthropic.common_utils import AnthropicModelInfo
except ImportError:
    AnthropicModelInfo = None

try:
    from litellm.llms.openai_like.json_loader import JSONProviderRegistry
except ImportError:
    JSONProviderRegistry = None

try:
    from litellm.utils import _get_model_info_helper
except ImportError:
    _get_model_info_helper = None

try:
    from litellm.llms.bedrock_mantle.common_utils import BedrockMantleAuthMixin
except ImportError:
    BedrockMantleAuthMixin = None

_handler_module = None


def _handler_attr(name, default=None):
    """Resolve an interface slot from the live ``litellm_ai_handler`` module at call time.

    Read the module registered under its canonical import name so guarded interface
    checks observe the attrs that tests replace on the handler module, without importing
    the handler back and forming an import cycle. ``_handler_module`` re-routes the
    lookup at an isolated re-exec of the handler so fail-closed tests can prove its
    guards fire.
    """
    handler_module = _handler_module
    if handler_module is None:
        handler_module = sys.modules.get("pr_agent.algo.ai_handlers.litellm_ai_handler")
    if handler_module is None:
        return default
    return getattr(handler_module, name, default)


def _resolve_provider_registry():
    """Return ``JSONProviderRegistry``, preferring the handler module's replaceable binding.

    The handler keeps its own guarded registry import and tests replace that module
    attribute to simulate custom provider registries, so request-path lookups must read
    the live binding rather than this module's import-time copy.
    """
    return _handler_attr("JSONProviderRegistry", JSONProviderRegistry)


DUMMY_LITELLM_API_KEY = "dummy_key"  # request-local guard against LiteLLM's process-wide key fallbacks
PROVIDER_SETTING_ALIASES = {
    "aiohttp_openai": "openai",
    "anthropic_text": "anthropic",
    "azure_text": "azure",
    "ollama_chat": "ollama",
    "text-completion-inception": "inception",
    "text-completion-openai": "openai",
    "vertex_ai_beta": "vertex_ai",
}
# Keep chat-completion endpoint aliases in the same precedence order as LiteLLM 1.101.0.
# Native completion masks BASETEN_API_BASE, MISTRAL_API_BASE and ARK_API_BASE;
# VERTEX_API_BASE belongs to embedding, not chat. Do not promote them to explicit routing.
PROVIDER_API_BASE_ENV_VARS = {
    "a2a": ("A2A_API_BASE",),
    "ai21": ("AI21_API_BASE",),
    "ai21_chat": ("AI21_API_BASE",),
    "aiml": ("AIML_API_BASE",),
    "aleph_alpha": ("ALEPH_ALPHA_API_BASE",),
    "amazon_nova": ("AMAZON_NOVA_API_BASE",),
    "anthropic": ("ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL"),
    "anyscale": ("ANYSCALE_API_BASE",),
    "azure_ai": ("AZURE_AI_API_BASE",),
    "bedrock_mantle": ("BEDROCK_MANTLE_API_BASE",),
    "cerebras": ("CEREBRAS_API_BASE",),
    "chatgpt": ("CHATGPT_API_BASE", "OPENAI_CHATGPT_API_BASE"),
    "cloudflare": ("CLOUDFLARE_API_BASE",),
    "codestral": ("CODESTRAL_API_BASE",),
    "text-completion-codestral": ("CODESTRAL_API_BASE",),
    "cohere": ("COHERE_API_BASE",),
    "cohere_chat": ("COHERE_API_BASE",),
    "cometapi": ("COMETAPI_API_BASE",),
    "dashscope": ("DASHSCOPE_API_BASE",),
    "databricks": ("DATABRICKS_API_BASE",),
    "datarobot": ("DATAROBOT_ENDPOINT",),
    "deepinfra": ("DEEPINFRA_API_BASE",),
    "deepseek": ("DEEPSEEK_API_BASE",),
    "docker_model_runner": ("DOCKER_MODEL_RUNNER_API_BASE",),
    "empower": ("EMPOWER_API_BASE",),
    "featherless_ai": ("FEATHERLESS_AI_API_BASE", "FEATHERLESS_API_BASE"),
    "fireworks_ai": ("FIREWORKS_API_BASE",),
    "friendliai": ("FRIENDLI_API_BASE",),
    "galadriel": ("GALADRIEL_API_BASE",),
    "gdc": ("GDC_API_BASE",),
    "gemini": ("GEMINI_API_BASE",),
    "github": ("GITHUB_API_BASE",),
    "github_copilot": ("GITHUB_COPILOT_API_BASE",),
    "gigachat": ("GIGACHAT_API_BASE",),
    "gradient_ai": ("GRADIENT_AI_AGENT_ENDPOINT",),
    "groq": ("GROQ_API_BASE",),
    "heroku": ("HEROKU_API_BASE",),
    "hosted_vllm": ("HOSTED_VLLM_API_BASE",),
    "huggingface": ("HF_API_BASE", "HUGGINGFACE_API_BASE"),
    "hyperbolic": ("HYPERBOLIC_API_BASE",),
    "inception": ("INCEPTION_API_BASE",),
    "lambda_ai": ("LAMBDA_API_BASE",),
    "langflow": ("LANGFLOW_API_BASE",),
    "langgraph": ("LANGGRAPH_API_BASE",),
    "lemonade": ("LEMONADE_API_BASE",),
    "litellm_proxy": ("LITELLM_PROXY_API_BASE",),
    "llamafile": ("LLAMAFILE_API_BASE",),
    "lm_studio": ("LM_STUDIO_API_BASE",),
    "manus": ("MANUS_API_BASE",),
    "maritalk": ("MARITALK_API_BASE",),
    "meta_llama": ("LLAMA_API_BASE",),
    "minimax": ("MINIMAX_API_BASE",),
    "mistral": ("MISTRAL_AZURE_API_BASE",),
    "modelscope": ("MODELSCOPE_API_BASE",),
    "moonshot": ("MOONSHOT_API_BASE",),
    "morph": ("MORPH_API_BASE",),
    "nebius": ("NEBIUS_API_BASE",),
    "novita": ("NOVITA_API_BASE",),
    "nscale": ("NSCALE_API_BASE",),
    "nvidia_nim": ("NVIDIA_NIM_API_BASE",),
    "nvidia_riva": ("NVIDIA_RIVA_API_BASE",),
    "nlp_cloud": ("NLP_CLOUD_API_BASE",),
    "ollama": ("OLLAMA_API_BASE",),
    "openai_like": ("OPENAI_LIKE_API_BASE",),
    "openrouter": ("OPENROUTER_API_BASE",),
    "ovhcloud": ("OVHCLOUD_API_BASE",),
    "perplexity": ("PERPLEXITY_API_BASE",),
    "predibase": ("PREDIBASE_API_BASE",),
    "ragflow": ("RAGFLOW_API_BASE",),
    "replicate": ("REPLICATE_API_BASE",),
    "sambanova": ("SAMBANOVA_API_BASE",),
    "tencent": ("TENCENT_API_BASE",),
    "together_ai": ("TOGETHER_AI_API_BASE",),
    "v0": ("V0_API_BASE",),
    "vercel_ai_gateway": ("VERCEL_AI_GATEWAY_API_BASE",),
    "vertex_ai": ("VERTEXAI_API_BASE",),
    "volcengine": ("VOLCENGINE_API_BASE",),
    "wandb": ("WANDB_API_BASE",),
    "xai": ("XAI_API_BASE",),
    "xinference": ("XINFERENCE_API_BASE",),
    "zai": ("ZAI_API_BASE",),
}

_WATSONX_ROUTING_ENV_VARS = {
    "api_base": ("WATSONX_API_BASE", "WATSONX_URL", "WX_URL", "WML_URL"),
    "project_id": ("WATSONX_PROJECT_ID", "WX_PROJECT_ID", "PROJECT_ID"),
    "space_id": ("WATSONX_DEPLOYMENT_SPACE_ID", "WATSONX_SPACE_ID", "WX_SPACE_ID", "SPACE_ID"),
    "region_name": ("WATSONX_REGION", "WX_REGION", "REGION"),
    "token": ("WATSONX_TOKEN",),
    "zen_api_key": ("WATSONX_ZENAPIKEY",),
}

PROVIDER_ROUTING_ENV_VARS = {
    "azure": {
        "api_base": ("AZURE_API_BASE", "AZURE_OPENAI_ENDPOINT"),
        "api_version": ("AZURE_API_VERSION",),
    },
    "bedrock": {
        "aws_bedrock_runtime_endpoint": ("AWS_BEDROCK_RUNTIME_ENDPOINT",),
        "aws_region_name": ("AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION"),
    },
    "bedrock_mantle": {
        "aws_bedrock_runtime_endpoint": ("AWS_BEDROCK_RUNTIME_ENDPOINT",),
        "aws_region_name": ("BEDROCK_MANTLE_REGION", "AWS_REGION_NAME", "AWS_REGION", "AWS_DEFAULT_REGION"),
    },
    "cloudflare": {
        "api_base": ("CLOUDFLARE_API_BASE", "CLOUDFLARE_ACCOUNT_ID"),
    },
    "openai": {
        "organization": ("OPENAI_ORGANIZATION",),
    },
    "openrouter": {"api_base": ("OPENROUTER_API_BASE",)},
    "vertex_ai": {
        "vertex_project": ("VERTEXAI_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"),
        "vertex_location": ("VERTEXAI_LOCATION", "VERTEX_LOCATION"),
    },
    "watsonx": dict(_WATSONX_ROUTING_ENV_VARS),
    "watsonx_text": dict(_WATSONX_ROUTING_ENV_VARS),
}

# Keep aliases in the same precedence order as LiteLLM 1.101.0.
PROVIDER_API_KEY_ENV_VARS = {
    "ai21": ("AI21_API_KEY",),
    "ai21_chat": ("AI21_API_KEY",),
    "aiml": ("AIML_API_KEY",),
    "aleph_alpha": ("ALEPH_ALPHA_API_KEY", "ALEPHALPHA_API_KEY"),
    "amazon_nova": ("AMAZON_NOVA_API_KEY",),
    "anyscale": ("ANYSCALE_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY", "AZURE_API_KEY"),
    "azure_ai": ("AZURE_AI_API_KEY",),
    "baseten": ("BASETEN_API_KEY",),
    "bedrock": ("AWS_BEARER_TOKEN_BEDROCK",),
    "bedrock_mantle": ("BEDROCK_MANTLE_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"),
    "bytez": ("BYTEZ_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "clarifai": ("CLARIFAI_API_KEY",),
    "cloudflare": ("CLOUDFLARE_API_KEY",),
    "codestral": ("CODESTRAL_API_KEY",),
    "cohere": ("COHERE_API_KEY", "CO_API_KEY"),
    "cohere_chat": ("COHERE_API_KEY", "CO_API_KEY"),
    "cometapi": ("COMETAPI_KEY",),
    "compactifai": ("COMPACTIFAI_API_KEY",),
    "custom_openai": ("OPENAI_API_KEY",),  # Preserve the existing OpenAI-compatible fallback.
    "dashscope": ("DASHSCOPE_API_KEY",),
    "databricks": ("DATABRICKS_API_KEY",),
    "datarobot": ("DATAROBOT_API_TOKEN",),
    "deepinfra": ("DEEPINFRA_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "docker_model_runner": ("DOCKER_MODEL_RUNNER_API_KEY",),
    "empower": ("EMPOWER_API_KEY",),
    "featherless_ai": ("FEATHERLESS_AI_API_KEY", "FEATHERLESS_API_KEY"),
    "fireworks_ai": (
        "FIREWORKS_API_KEY",
        "FIREWORKS_AI_API_KEY",
        "FIREWORKSAI_API_KEY",
        "FIREWORKS_AI_TOKEN",
    ),
    "friendliai": ("FRIENDLIAI_API_KEY", "FRIENDLI_TOKEN"),
    "galadriel": ("GALADRIEL_API_KEY",),
    "gdc": ("GDC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "PALM_API_KEY"),
    "github": ("GITHUB_API_KEY",),
    "gigachat": ("GIGACHAT_API_KEY", "GIGACHAT_CREDENTIALS"),
    "gradient_ai": ("GRADIENT_AI_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "heroku": ("HEROKU_API_KEY",),
    "hosted_vllm": ("HOSTED_VLLM_API_KEY",),
    "huggingface": ("HF_TOKEN", "HUGGINGFACE_API_KEY"),
    "hyperbolic": ("HYPERBOLIC_API_KEY",),
    "inception": ("INCEPTION_API_KEY",),
    "lambda_ai": ("LAMBDA_API_KEY",),
    "langflow": ("LANGFLOW_API_KEY",),
    "langgraph": ("LANGGRAPH_API_KEY",),
    "lemonade": ("LEMONADE_API_KEY",),
    "litellm_proxy": ("LITELLM_PROXY_API_KEY",),
    "llamafile": ("LLAMAFILE_API_KEY",),
    "lm_studio": ("LM_STUDIO_API_KEY",),
    "manus": ("MANUS_API_KEY",),
    "maritalk": ("MARITALK_API_KEY",),
    "meta_llama": ("LLAMA_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "mistral": ("MISTRAL_AZURE_API_KEY", "MISTRAL_API_KEY"),
    "modelscope": ("MODELSCOPE_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "morph": ("MORPH_API_KEY",),
    "nebius": ("NEBIUS_API_KEY",),
    "novita": ("NOVITA_API_KEY",),
    "nscale": ("NSCALE_API_KEY",),
    "nvidia_nim": ("NVIDIA_NIM_API_KEY",),
    "nlp_cloud": ("NLP_CLOUD_API_KEY",),
    "ollama": ("OLLAMA_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai_like": ("OPENAI_LIKE_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY", "OR_API_KEY"),
    "ovhcloud": ("OVHCLOUD_API_KEY",),
    "perplexity": ("PERPLEXITYAI_API_KEY", "PERPLEXITY_API_KEY"),
    "predibase": ("PREDIBASE_API_KEY",),
    "ragflow": ("RAGFLOW_API_KEY",),
    "replicate": ("REPLICATE_API_KEY", "REPLICATE_API_TOKEN"),
    "sambanova": ("SAMBANOVA_API_KEY",),
    "sap": ("AICORE_SERVICE_KEY",),
    "snowflake": ("SNOWFLAKE_JWT",),
    "tencent": ("TENCENT_API_KEY",),
    "text-completion-codestral": ("CODESTRAL_API_KEY",),
    "together_ai": (
        "TOGETHER_API_KEY",
        "TOGETHER_AI_API_KEY",
        "TOGETHERAI_API_KEY",
        "TOGETHER_AI_TOKEN",
    ),
    "v0": ("V0_API_KEY",),
    "vercel_ai_gateway": ("VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"),
    "volcengine": ("VOLCENGINE_API_KEY",),
    "wandb": ("WANDB_API_KEY",),
    "watsonx": ("WATSONX_APIKEY", "WATSONX_API_KEY", "WX_API_KEY", "WATSONX_ZENAPIKEY"),
    "watsonx_text": ("WATSONX_APIKEY", "WATSONX_API_KEY", "WX_API_KEY"),
    "xai": ("XAI_API_KEY",),
    "xiaomi_mimo": ("XIAOMI_MIMO_API_KEY",),
    "xinference": ("XINFERENCE_API_KEY",),
    "zai": ("ZAI_API_KEY",),
}

OPENAI_COMPATIBLE_REQUEST_PROVIDERS = {"custom_openai", "openai_like"}
MANAGED_AUTH_REQUEST_PROVIDERS = {"chatgpt", "github_copilot"}
OPENAI_RAW_HTTP_REQUEST_PROVIDERS = {
    "aiohttp_openai",
    "azure_ai",
    "cometapi",
    "deepseek",
    "fireworks_ai",
    "groq",
    "heroku",
    "hosted_vllm",
    "minimax",
    "openai_like",
    "openrouter",
    "ragflow",
    "together_ai",
    "xai",
}
AZURE_OIDC_ENV_VARS = ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST", "AZURE_SCOPE")
PROVIDER_API_KEY_GLOBALS = {
    "ai21": ("ai21_key",),
    "ai21_chat": ("ai21_key",),
    "aleph_alpha": ("aleph_alpha_key",),
    "amazon_nova": ("amazon_nova_api_key",),
    "anthropic": ("anthropic_key",),
    "azure": ("azure_key",),
    "baseten": ("baseten_key",),
    "bytez": ("bytez_key",),
    "cloudflare": ("cloudflare_api_key",),
    "cohere": ("cohere_key",),
    "cohere_chat": ("cohere_key",),
    "cometapi": ("cometapi_key",),
    "custom_openai": ("openai_key",),
    "databricks": ("databricks_key",),
    "gdc": ("gdc_key",),
    "gigachat": ("gigachat_key",),
    "groq": ("groq_key",),
    "huggingface": ("huggingface_key",),
    "inception": ("inception_key",),
    "lemonade": ("lemonade_key",),
    "maritalk": ("maritalk_key",),
    "nebius": ("nebius_key",),
    "nlp_cloud": ("nlp_cloud_key",),
    "ollama": ("ollama_key", "openai_key"),
    "openai": ("openai_key",),
    "openai_like": ("openai_like_key",),
    "openrouter": ("openrouter_key",),
    "ovhcloud": ("ovhcloud_key",),
    "predibase": ("predibase_key",),
    "replicate": ("replicate_key",),
    "sap": ("sap_service_key",),
    "together_ai": ("togetherai_api_key",),
    "wandb": ("wandb_key",),
    "xai": ("xai_key",),
}
def _require_litellm_interface(interface, name: str, methods=()):
    """Reject unavailable isolation interfaces without disabling their protection."""
    available = (
        all(callable(getattr(interface, method, None)) for method in methods)
        if methods else callable(interface)
    )
    if not available:
        raise RuntimeError(
            f"LiteLLM interface {name} is unavailable; request isolation cannot be guaranteed. "
            "Restore the project's locked LiteLLM version or update and validate this integration."
        )


def _rebind_to_globals(native, native_globals):
    """Rebind a native function onto captured globals, keeping its own defaults."""
    bound = FunctionType(
        native.__code__, native_globals, native.__name__, native.__defaults__, native.__closure__,
    )
    bound.__kwdefaults__ = copy.copy(native.__kwdefaults__)
    return bound


_azure_oidc_request = ContextVar("pr_agent_azure_oidc_request", default=None)
_azure_ad_responses_request = ContextVar("pr_agent_azure_ad_responses_request", default=None)
_azure_oidc_bridge = None


def _exchange_azure_oidc_token(selector, environment, native=None):
    """Bind the native outer exchange to captured inputs without mutating its globals."""
    from litellm.llms.azure.common_utils import get_azure_ad_token_from_oidc

    if native is None:
        native = get_azure_ad_token_from_oidc
        if _azure_oidc_bridge is not None and native is _azure_oidc_bridge["exchange"]:
            native = _azure_oidc_bridge["original_exchange"]
    if set(environment) != set(AZURE_OIDC_ENV_VARS):
        raise RuntimeError("Azure OIDC exchange snapshot is incomplete")
    if not isinstance(native, FunctionType):
        raise RuntimeError("LiteLLM's Azure OIDC exchange interface is incompatible")
    parameters = tuple(inspect.signature(native).parameters.values())
    names = ("azure_ad_token", "azure_client_id", "azure_tenant_id", "scope")
    if len(parameters) != len(names) or any(
        parameter.name != name
        or parameter.kind != inspect.Parameter.POSITIONAL_OR_KEYWORD
        or parameter.default is not (inspect.Parameter.empty if index == 0 else None)
        for index, (parameter, name) in enumerate(zip(parameters, names, strict=True))
    ):
        raise RuntimeError("LiteLLM's Azure OIDC exchange interface is incompatible")
    native_globals = dict(native.__globals__)
    cache = native_globals.get("azure_ad_cache")
    _require_litellm_interface(cache, "Azure OIDC cache", ("get_cache", "set_cache"))
    _require_litellm_interface(native_globals.get("get_secret_str"), "Azure OIDC assertion resolver")

    def captured_getenv(name, default=None):
        if name not in environment:
            raise RuntimeError("LiteLLM's Azure OIDC exchange uses an uncaptured input")
        value = environment[name]
        return default if value is None else value

    # Match the pinned helper's default without normalizing a deliberate empty scope.
    scope = environment["AZURE_SCOPE"]
    effective_scope = "https://cognitiveservices.azure.com/.default" if scope is None else scope

    def scoped_key(key):
        return json.dumps(["pr-agent.azure-oidc.v1", effective_scope, key])

    def get_cache(key, *args, **kwargs):
        return cache.get_cache(scoped_key(key), *args, **kwargs)

    def set_cache(key, *args, **kwargs):
        return cache.set_cache(scoped_key(key), *args, **kwargs)

    # Keep native assertion resolution (including deployment-owned source selection)
    # and refresh/TTL behavior. Only the outer exchange's inputs are request-owned.
    native_globals["os"] = SimpleNamespace(getenv=captured_getenv)
    native_globals["azure_ad_cache"] = SimpleNamespace(get_cache=get_cache, set_cache=set_cache)
    exchange = _rebind_to_globals(native, native_globals)
    return exchange(
        selector,
        azure_client_id=environment["AZURE_CLIENT_ID"],
        azure_tenant_id=environment["AZURE_TENANT_ID"],
        scope=scope,
    )


@lru_cache(maxsize=128)
def _azure_oidc_entra_provider(tenant_id, client_id, client_secret, scope, authority):
    from azure.identity import ClientSecretCredential, get_bearer_token_provider

    credential = ClientSecretCredential(tenant_id, client_id, client_secret, authority=authority)
    return get_bearer_token_provider(credential, scope)


def _azure_oidc_companion_globals(native_globals, context):
    """Bind companion factories only inside the selected native auth path."""
    native_globals = dict(native_globals)
    original_entra = native_globals["get_azure_ad_token_from_entra_id"]
    original_password = native_globals["get_azure_ad_token_from_username_password"]

    def authority():
        from azure.identity import AzureAuthorityHosts

        value = context["environment"]["AZURE_AUTHORITY_HOST"]
        if value == "":
            raise ValueError("Azure companion credentials require a nonempty captured authority")
        return AzureAuthorityHosts.AZURE_PUBLIC_CLOUD if value is None else value

    def entra(tenant_id, client_id, client_secret, scope="https://cognitiveservices.azure.com/.default"):
        if not isinstance(original_entra, FunctionType):
            raise RuntimeError("LiteLLM's Azure Entra factory is incompatible")
        captured_authority = authority()

        def cached(tenant_id, client_id, client_secret, scope):
            return _azure_oidc_entra_provider(tenant_id, client_id, client_secret, scope, captured_authority)

        # Retain native secret-selector resolution, but never use the native
        # authority-free provider cache. Credentials retain authority on refresh.
        globals_view = {**original_entra.__globals__, "_cached_entra_id_token_provider": cached}
        factory = _rebind_to_globals(original_entra, globals_view)
        return factory(tenant_id, client_id, client_secret, scope)

    def password(client_id, azure_username, azure_password, scope="https://cognitiveservices.azure.com/.default"):
        from azure.identity import UsernamePasswordCredential, get_bearer_token_provider

        if not isinstance(original_password, FunctionType):
            raise RuntimeError("LiteLLM's Azure password factory is incompatible")
        # The native password path leaves tenant selection at the SDK default.
        credential = UsernamePasswordCredential(
            client_id=client_id, username=azure_username, password=azure_password, authority=authority(),
        )
        return get_bearer_token_provider(credential, scope)

    native_globals["get_azure_ad_token_from_entra_id"] = entra
    native_globals["get_azure_ad_token_from_username_password"] = password

    def reject_implicit_refresh(*args, **kwargs):
        # Native discovery re-reads ambient identity after companion selection.
        # ValueError is swallowed by LiteLLM and would silently change auth.
        raise RuntimeError("Request isolation forbids implicit Azure AD credential discovery; "
                           "configure complete companion credentials instead")

    native_globals["get_azure_ad_token_provider"] = reject_implicit_refresh
    return native_globals


def _install_azure_oidc_bridge():
    """Bind native OIDC and SDK AD selection without changing key precedence."""
    global _azure_oidc_bridge
    from litellm.llms.azure import azure as azure_module
    from litellm.llms.azure import common_utils as azure_common
    from litellm.llms.openai.common_utils import BaseOpenAILLM

    if _azure_oidc_bridge is not None:
        if (
            azure_common.get_azure_ad_token_from_oidc is not _azure_oidc_bridge["exchange"]
            or azure_module.get_azure_ad_token_from_oidc is not _azure_oidc_bridge["exchange"]
            or azure_common.get_azure_ad_token is not _azure_oidc_bridge["resolver"]
            or BaseOpenAILLM.get_openai_client_cache_key is not _azure_oidc_bridge["cache_key"]
            or azure_common.BaseAzureLLM._resolve_env_var is not _azure_oidc_bridge["resolve_env"]
            or azure_common.BaseAzureLLM.get_azure_openai_client is not _azure_oidc_bridge["client"]
            or azure_common.BaseAzureLLM._base_validate_azure_environment is not _azure_oidc_bridge["validate"]
            or azure_common.BaseAzureLLM.initialize_azure_sdk_client is not _azure_oidc_bridge["initialize"]
        ):
            raise RuntimeError("LiteLLM's Azure OIDC bridge interfaces were replaced")
        return
    original_exchange = azure_common.get_azure_ad_token_from_oidc
    original_resolver = azure_common.get_azure_ad_token
    original_cache_key = BaseOpenAILLM.get_openai_client_cache_key
    original_resolve_env = azure_common.BaseAzureLLM._resolve_env_var
    original_client = azure_common.BaseAzureLLM.get_azure_openai_client
    original_validate = azure_common.BaseAzureLLM._base_validate_azure_environment
    original_initialize = azure_common.BaseAzureLLM.initialize_azure_sdk_client
    client_signature = inspect.signature(original_client)
    initialize_signature = inspect.signature(original_initialize)
    if (
        not all(isinstance(function, FunctionType) for function in (
            original_exchange, original_resolver, original_cache_key, original_resolve_env,
            original_client, original_validate, original_initialize,
        ))
        or azure_module.get_azure_ad_token_from_oidc is not original_exchange
    ):
        raise RuntimeError("LiteLLM's Azure OIDC interfaces are incompatible")

    @wraps(original_exchange)
    def exchange(azure_ad_token, azure_client_id=None, azure_tenant_id=None, scope=None):
        context = _azure_oidc_request.get()
        if context is not None and context["selector"] and azure_ad_token == context["selector"]:
            return _exchange_azure_oidc_token(azure_ad_token, context["environment"], original_exchange)
        return original_exchange(azure_ad_token, azure_client_id, azure_tenant_id, scope)

    def resolve_captured(litellm_params, context):
        native_globals = _azure_oidc_companion_globals(original_resolver.__globals__, context)
        native_os = native_globals["os"]
        native_get_secret = native_globals["get_secret_str"]

        def get_secret_str(name, *args, **kwargs):
            if name == "AZURE_AD_TOKEN":
                return context["dispatch_token"]
            return native_get_secret(name, *args, **kwargs)

        def getenv(name, default=None):
            if name in context["auth_environment"]:
                value = context["auth_environment"][name]
                return default if value is None else value
            return native_os.getenv(name, default)

        native_globals["os"] = SimpleNamespace(getenv=getenv)
        native_globals["get_secret_str"] = get_secret_str
        native_globals["get_azure_ad_token_from_oidc"] = exchange
        bound = _rebind_to_globals(original_resolver, native_globals)
        return bound(litellm_params)

    @wraps(original_resolver)
    def resolver(litellm_params):
        ad_context = _azure_ad_responses_request.get()
        if ad_context is not None and litellm_params.get("azure_ad_token") == ad_context["dispatch_token"]:
            return resolve_captured(litellm_params, ad_context)
        context = _azure_oidc_request.get()
        if context is None or not context["selector"] or litellm_params.get("azure_ad_token") != context["selector"]:
            return original_resolver(litellm_params)
        environment = context["environment"]
        if not environment["AZURE_CLIENT_ID"] or not environment["AZURE_TENANT_ID"]:
            raise ValueError("Azure OIDC requires captured client and tenant identifiers")
        return resolve_captured(litellm_params, context)

    @wraps(original_resolve_env)
    def resolve_env(owner, litellm_params, param_key, env_var_key):
        context = _azure_oidc_request.get() or _azure_ad_responses_request.get()
        if (
            context is not None
            and (
                litellm_params.get("azure_ad_token") == context["dispatch_token"]
                or (context.get("companion") and litellm_params.get("azure_ad_token") is None)
            )
            and env_var_key in context["auth_environment"]
        ):
            value = litellm_params.get(param_key)
            return value if value is not None else context["auth_environment"][env_var_key]
        return original_resolve_env(owner, litellm_params, param_key, env_var_key)

    @wraps(original_initialize)
    def initialize(*args, **kwargs):
        context = _azure_oidc_request.get() or _azure_ad_responses_request.get()
        if context is None:
            return original_initialize(*args, **kwargs)
        bound = initialize_signature.bind(*args, **kwargs)
        params = bound.arguments.get("litellm_params") or {}
        if not (
            params.get("azure_ad_token") == context["dispatch_token"]
            or (context.get("companion") and params.get("azure_ad_token") is None)
        ):
            return original_initialize(*args, **kwargs)
        native_globals = _azure_oidc_companion_globals(original_initialize.__globals__, context)
        factory = _rebind_to_globals(original_initialize, native_globals)
        return factory(*args, **kwargs)

    @wraps(original_cache_key)
    def cache_key(client_initialization_params, client_type):
        key = original_cache_key(client_initialization_params, client_type)
        context = _azure_oidc_request.get() or _azure_ad_responses_request.get()
        if (
            context is not None and client_type == "azure"
            and (
                client_initialization_params.get("azure_ad_token") == context.get("selector_hash")
                or (
                    context.get("companion")
                    and client_initialization_params.get("azure_ad_token") is None
                )
            )
        ):
            return f"{key}|pr_agent_oidc={context['identity_hash']}"
        return key

    @wraps(original_client)
    def client(*args, **kwargs):
        context = _azure_oidc_request.get()
        if context is None:
            context = _azure_ad_responses_request.get()
        if context is None or not context["generated_guard"]:
            return original_client(*args, **kwargs)
        bound = client_signature.bind(*args, **kwargs)
        params = bound.arguments.get("litellm_params") or {}
        if (
            bound.arguments.get("api_key") == DUMMY_LITELLM_API_KEY
            and (
                params.get("azure_ad_token") == context["dispatch_token"]
                or (context.get("companion") and params.get("azure_ad_token") is None)
            )
        ):
            # Dispatch has already resolved fallbacks. Restore native keyless
            # selection before the SDK initializer and its client-cache lookup.
            bound.arguments["api_key"] = None
        return original_client(*bound.args, **bound.kwargs)

    @wraps(original_validate)
    def validate(headers, litellm_params):
        ad_context = _azure_ad_responses_request.get()
        if (
            ad_context is not None and ad_context["generated_guard"] and "api-key" not in headers
            and litellm_params.get("api_key") == DUMMY_LITELLM_API_KEY
            and litellm_params.get("azure_ad_token") == ad_context["dispatch_token"]
        ):
            # Only a positively identified generated guard may bypass key
            # selection. Retain native AD provider precedence, not global keys.
            headers = dict(headers)
            token = resolve_captured(litellm_params, ad_context)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            return headers
        context = _azure_oidc_request.get()
        if (
            context is None or not context["selector"] or not context["generated_guard"] or "api-key" in headers
            or litellm_params.get("api_key") != DUMMY_LITELLM_API_KEY
            or litellm_params.get("azure_ad_token") != context["dispatch_token"]
        ):
            return original_validate(headers, litellm_params)
        # Do not retry process-wide API-key fallback with a cleared guard.
        headers = dict(headers)
        token = resolver(litellm_params)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    azure_common.get_azure_ad_token_from_oidc = exchange
    azure_module.get_azure_ad_token_from_oidc = exchange
    azure_common.get_azure_ad_token = resolver
    azure_common.BaseAzureLLM._resolve_env_var = resolve_env
    azure_common.BaseAzureLLM.get_azure_openai_client = client
    azure_common.BaseAzureLLM._base_validate_azure_environment = staticmethod(validate)
    azure_common.BaseAzureLLM.initialize_azure_sdk_client = initialize
    BaseOpenAILLM.get_openai_client_cache_key = staticmethod(cache_key)
    _azure_oidc_bridge = {
        "exchange": exchange, "resolver": resolver, "cache_key": cache_key,
        "original_exchange": original_exchange,
        "resolve_env": resolve_env,
        "client": client, "validate": validate, "initialize": initialize,
    }


def _is_openai_compatible_request_provider(provider: str) -> bool:
    return (
        provider in getattr(litellm, "openai_compatible_providers", ())
        or _resolve_provider_registry().exists(provider)
    )


def _uses_openai_text_completion_transport(model: str | None, provider: str | None) -> bool:
    """Return whether LiteLLM will dispatch this request through the OpenAI text-completion SDK."""
    if provider == "text-completion-openai":
        return True
    if not isinstance(model, str):
        return False
    return any(prefix in model for prefix in ("ft:babbage-002", "ft:davinci-002"))


def _uses_openai_responses_transport(model: str | None, provider: str | None) -> bool:
    """Return whether LiteLLM will send this model through its raw Responses transport."""
    if not isinstance(model, str) or _uses_openai_text_completion_transport(model, provider):
        return False
    provider = provider or ""
    canonical_provider = PROVIDER_SETTING_ALIASES.get(provider, provider)
    if "/" in model and model.split("/", 1)[0] in (provider, canonical_provider):
        model = model.split("/", 1)[1]
    if model.startswith("responses/"):
        return True
    if provider == "openai" and getattr(litellm, "route_all_chat_openai_to_responses", False):
        return True
    model_info_helper = _handler_attr("_get_model_info_helper", _get_model_info_helper)
    _require_litellm_interface(model_info_helper, "_get_model_info_helper")
    try:
        model_info = model_info_helper(model=model, custom_llm_provider=provider)
    except Exception:
        return False
    return model_info.get("mode") == "responses"


def _uses_provider_api_key(provider: str) -> bool:
    """Return whether PR-Agent should forward or guard this provider's native API key."""
    return provider in PROVIDER_API_KEY_ENV_VARS or _resolve_provider_registry().exists(provider)


_SDK_HEADER_MARKER = "x-pr-agent-sdk-header-snapshot"
_sdk_request_headers = ContextVar("pr_agent_sdk_request_headers", default=None)


class _CapturedSDKHeader(openai.Omit):
    """Distinguish generated account guards from explicit header omissions."""


class _SDKHeaderSnapshot:
    # Keep credential-bearing snapshots out of SDK DEBUG and fallback serialization.
    __slots__ = ("snapshot",)

    def __init__(self, snapshot):
        self.snapshot = snapshot

    def __repr__(self):
        return "<request-local SDK headers>"


def _merge_sdk_headers(*layers):
    """Merge before HTTPX so differently cased names cannot duplicate credentials."""
    result = {}
    names = {}
    for layer in layers:
        for name, value in layer.items():
            lower = name.lower()
            canonical = names.setdefault(lower, name)
            result[canonical] = value
    return result


def _check_sdk_marker_collision(headers):
    if any(name.lower() == _SDK_HEADER_MARKER for name in headers):
        raise ValueError("Request headers contain a reserved SDK isolation header")


class _SDKHeaderLoggingProxy:
    def __init__(self, logger):
        object.__setattr__(self, "_logger", logger)

    def __getattr__(self, name):
        return getattr(self._logger, name)

    def __setattr__(self, name, value):
        setattr(self._logger, name, value)

    def pre_call(self, *args, **kwargs):
        additional = kwargs.get("additional_args")
        if isinstance(additional, dict) and isinstance(additional.get("complete_input_dict"), dict):
            data = dict(additional["complete_input_dict"])
            if isinstance(data.get("extra_headers"), dict):
                data["extra_headers"] = {
                    key: value for key, value in data["extra_headers"].items()
                    if key.lower() != _SDK_HEADER_MARKER
                }
            kwargs["additional_args"] = {**additional, "complete_input_dict": data}
        return self._logger.pre_call(*args, **kwargs)


def _sdk_data_adapter(original, *, text=False, copilot=False):
    signature = inspect.signature(original)

    @wraps(original)
    def scoped(*args, **kwargs):
        snapshot = _sdk_request_headers.get()
        if snapshot is None:
            return original(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        if copilot:
            if snapshot["provider"] != "github_copilot":
                return original(*args, **kwargs)
            params = bound.arguments.get("optional_params") or {}
            bound.arguments["headers"] = _merge_sdk_headers(
                params.get("extra_headers") or {}, bound.arguments.get("headers") or {},
            )
        else:
            data = dict(bound.arguments["data"])
            if text and snapshot["provider"] == "azure":
                # LiteLLM also copies this auth argument into text request data.
                # Keep it on the native client parameters, never in the SDK payload.
                data.pop("azure_ad_token", None)
            headers = dict(data.get("extra_headers") or {})
            _check_sdk_marker_collision(headers)
            headers[_SDK_HEADER_MARKER] = _SDKHeaderSnapshot(snapshot)
            data["extra_headers"] = headers
            bound.arguments["data"] = data
            if text and bound.arguments.get("logging_obj") is not None:
                bound.arguments["logging_obj"] = _SDKHeaderLoggingProxy(bound.arguments["logging_obj"])
        # Capture before returning a lazy text streaming generator, not at first iteration.
        return original(*bound.args, **bound.kwargs)

    scoped._pr_agent_sdk_headers_original = original
    scoped._pr_agent_sdk_headers_context = _sdk_request_headers
    return scoped


def _install_sdk_header_bridge():
    """Adapt the locked SDK's request boundary without mutating shared clients."""
    try:
        from litellm.llms.azure.azure import AzureChatCompletion
        from litellm.llms.azure.completion.handler import AzureTextCompletion
        from litellm.llms.openai.completion.handler import OpenAITextCompletion
        from litellm.llms.openai.openai import OpenAIChatCompletion
        from openai._base_client import BaseClient
    except ImportError as error:
        raise RuntimeError("The installed LiteLLM/OpenAI SDK cannot isolate request headers") from error

    bindings = (
        (OpenAIChatCompletion, "make_openai_chat_completion_request", False, False),
        (AzureChatCompletion, "make_azure_openai_chat_completion_request", False, False),
        (OpenAITextCompletion, "acompletion", True, False),
        (OpenAITextCompletion, "async_streaming", True, False),
        (AzureTextCompletion, "acompletion", True, False),
        (AzureTextCompletion, "async_streaming", True, False),
        (OpenAIChatCompletion, "completion", False, True),
    )
    _require_litellm_interface(BaseClient, "OpenAI.BaseClient", ("_build_headers",))
    for owner, name, _, copilot in bindings:
        _require_litellm_interface(owner, owner.__name__, (name,))
        required = {"optional_params", "headers"} if copilot else {"data", "logging_obj"}
        if not required <= inspect.signature(getattr(owner, name)).parameters.keys():
            raise RuntimeError("The installed LiteLLM SDK request interface cannot isolate headers")

    current = BaseClient._build_headers
    if getattr(current, "_pr_agent_sdk_headers_context", None) is not _sdk_request_headers:
        sdk_build_headers = getattr(current, "_pr_agent_sdk_headers_original", current)

        @wraps(sdk_build_headers)
        def build_headers(client, options, *, retries_taken=0):
            headers = dict(options.headers or {})
            marker = headers.get(_SDK_HEADER_MARKER)
            if not isinstance(marker, _SDKHeaderSnapshot):
                return sdk_build_headers(client, options, retries_taken=retries_taken)
            if type(client) not in (openai.OpenAI, openai.AsyncOpenAI, openai.AzureOpenAI, openai.AsyncAzureOpenAI):
                raise ValueError("Custom SDK clients are incompatible with request header isolation")
            del headers[_SDK_HEADER_MARKER]
            snapshot = marker.snapshot
            view = copy.copy(client)
            view._custom_headers = {}
            view.organization = snapshot["organization"]
            view.project = snapshot["project"]
            explicit = dict(snapshot["explicit_headers"])
            for layer in (headers, explicit):
                for name, value in tuple(layer.items()):
                    if isinstance(value, _CapturedSDKHeader):
                        # Captured SDK defaults already provide the account identity;
                        # internal guards must not override native custom headers.
                        del layer[name]
            native = {**view._auth_headers(options.security), **view.default_headers}
            merged = _merge_sdk_headers(native, snapshot["custom_headers"], headers, explicit)
            # Native defaults retain their canonical spelling; options override them without duplicates.
            request_options = options.model_copy(update={"headers": merged})
            result = sdk_build_headers(view, request_options, retries_taken=retries_taken)
            if _SDK_HEADER_MARKER in result:
                raise RuntimeError("SDK isolation marker reached HTTP headers")
            return result

        build_headers._pr_agent_sdk_headers_original = sdk_build_headers
        build_headers._pr_agent_sdk_headers_context = _sdk_request_headers
        BaseClient._build_headers = build_headers
    for owner, name, text, copilot in bindings:
        current = getattr(owner, name)
        if getattr(current, "_pr_agent_sdk_headers_context", None) is not _sdk_request_headers:
            original = getattr(current, "_pr_agent_sdk_headers_original", current)
            setattr(owner, name, _sdk_data_adapter(original, text=text, copilot=copilot))


def _azure_ai_native_transport(provider: str, model: str | None) -> tuple[str, str | None]:
    """Follow Azure AI's native model classification without resolving credentials."""
    if provider == "azure_ai" and isinstance(model, str):
        from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig

        native_model = model.removeprefix("azure_ai/")
        if AzureAIStudioConfig()._is_azure_openai_model(native_model, None):
            return "azure", native_model
    return provider, model


def _is_cloudflare_gateway(api_base: str | None) -> bool:
    """Match the AI Gateway host itself; a substring test also matches it inside a path or query."""
    hostname = urlparse(api_base or "").hostname or ""
    return hostname == "gateway.ai.cloudflare.com" or hostname.endswith(".gateway.ai.cloudflare.com")


def _request_local_openai_headers(provider: str, organization=None, model: str | None = None) -> dict | None:
    """Block LiteLLM's process-wide OpenAI headers for one request."""
    transport_provider, model = _azure_ai_native_transport(provider, model)
    provider = PROVIDER_SETTING_ALIASES.get(transport_provider, transport_provider)
    if not (
        provider in ("azure", "openai", "openrouter")
        or provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS
        or provider in MANAGED_AUTH_REQUEST_PROVIDERS
        or _is_openai_compatible_request_provider(provider)
    ):
        return None
    if _uses_openai_responses_transport(model, transport_provider):
        if provider == "openai" and organization:
            return {"OpenAI-Organization": organization}
        return None
    experimental_raw_http_handler = os.environ.get(
        "EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER", ""
    ).strip().lower() == "true"
    uses_raw_http_handler = transport_provider in OPENAI_RAW_HTTP_REQUEST_PROVIDERS or (
        experimental_raw_http_handler
        and not _uses_openai_text_completion_transport(model, transport_provider)
        and (
            transport_provider == "openai"
            or transport_provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS
            or transport_provider in MANAGED_AUTH_REQUEST_PROVIDERS
            or _is_openai_compatible_request_provider(transport_provider)
        )
    )
    if uses_raw_http_handler:
        return None
    request_organization = organization if provider == "openai" and organization else _CapturedSDKHeader()
    return {
        "OpenAI-Organization": request_organization,
        "OpenAI-Project": _CapturedSDKHeader(),
    }


def _vertex_project_from_environment():
    # Google Auth treats an empty GOOGLE_CLOUD_PROJECT as masking GCLOUD_PROJECT.
    return os.environ.get("VERTEXAI_PROJECT") or os.environ.get(
        "GOOGLE_CLOUD_PROJECT", os.environ.get("GCLOUD_PROJECT"),
    )


def _get_bedrock_model_region(model: str, model_id=None) -> str | None:
    """Resolve only the region carried by the request, without ambient AWS discovery."""
    from litellm.llms.bedrock.common_utils import BedrockModelInfo

    # LiteLLM 1.101.0 consumes model_id before Invoke resolves its region.
    # Only Converse uses that separate ID or region/model path for routing.
    is_converse = BedrockModelInfo.get_bedrock_route(model) == "converse"
    if not is_converse:
        model_id = None
    candidate = model_id or model
    if not isinstance(candidate, str):
        return None
    if not model_id:
        for prefix in ("bedrock/converse/", "bedrock/", "converse/"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
    arn_candidate = candidate
    if not model_id:
        arn_candidate = arn_candidate.removeprefix("invoke/")
        # Match the native chat model prefixes in LiteLLM 1.101.0 without
        # decoding an ARN or changing Converse's region/model path grammar.
        for prefix in ("llama/", "deepseek_r1/", "openai/", "qwen2/", "qwen3/", "moonshot/", "nova-2/", "nova/"):
            if arn_candidate.startswith(prefix):
                arn_candidate = arn_candidate[len(prefix):]
                break
    arn = re.match(r"\Aarn:aws(?:-[a-z0-9-]+)?:bedrock:([a-z0-9-]+):", arn_candidate)
    if arn:
        return arn.group(1)
    # Native Converse only recognizes region/model paths when model_id is absent.
    if is_converse and not model_id:
        region, separator, _ = candidate.partition("/")
        if separator and region in litellm.AmazonBedrockGlobalConfig().get_all_regions():
            return region
    return None


def _guard_request_routing_globals(provider: str | None, params: dict) -> dict:
    """Reject process-wide LiteLLM routing fallbacks for one request."""
    if getattr(litellm, "api_base", None) and (
        "api_base" not in params or provider in LITELLM_GLOBAL_FIRST_API_BASE_PROVIDERS
    ):
        raise ValueError(f"Refusing process-wide LiteLLM API base fallback for provider {provider or 'unknown'}")
    if provider == "gdc" and not params.get("api_base") and getattr(litellm, "gdc_api_base", None):
        raise ValueError("Refusing process-wide LiteLLM API base fallback for provider gdc")
    if provider == "azure" and "api_version" not in params and getattr(litellm, "api_version", None):
        raise ValueError("Refusing process-wide LiteLLM API version fallback for provider azure")
    organization_is_guarded = any(
        header.lower() == "openai-organization"
        for header in (params.get("headers") or {})
    )
    if (
        provider == "openai"
        and "organization" not in params
        and not organization_is_guarded
        and getattr(litellm, "organization", None)
    ):
        raise ValueError("Refusing process-wide LiteLLM organization fallback for provider openai")
    # GDC's complete project URL owns its routing; host-only URLs otherwise
    # inherit the same process-wide project/location fallbacks as Vertex.
    if provider == "vertex_ai" or (provider == "gdc" and "/v1/projects/" not in (params.get("api_base") or "")):
        for parameter, global_name in (
            ("vertex_project", "vertex_project"),
            ("vertex_location", "vertex_location"),
        ):
            if parameter not in params and getattr(litellm, global_name, None):
                raise ValueError(f"Refusing process-wide LiteLLM {parameter} fallback for provider {provider}")
    routing_environment_variables = dict(PROVIDER_ROUTING_ENV_VARS.get(provider, {}))
    api_base_environment_variables = list(PROVIDER_API_BASE_ENV_VARS.get(provider, ()))
    provider_config = _resolve_provider_registry().get(provider)
    api_base_env = getattr(provider_config, "api_base_env", None)
    if api_base_env:
        api_base_environment_variables.append(api_base_env)
    if api_base_environment_variables:
        routing_environment_variables.setdefault("api_base", tuple(api_base_environment_variables))
    for parameter, environment_variables in routing_environment_variables.items():
        live_value = next(
            (os.environ.get(variable) for variable in environment_variables if os.environ.get(variable)),
            None,
        )
        if provider == "vertex_ai" and parameter == "vertex_project":
            live_value = _vertex_project_from_environment()
        if parameter not in params and live_value:
            raise ValueError(f"Refusing live {parameter} environment fallback for provider {provider}")
        if (
            provider == "chatgpt"
            and parameter == "api_base"
            and parameter in params
            and live_value != params[parameter]
        ):
            # LiteLLM 1.101.0's ChatGPT chat transformation ignores the request
            # api_base and re-reads these variables when resolving provider info.
            raise ValueError("Refusing changed live api_base environment for provider chatgpt")
    return params


def _has_provider_api_key_global(provider: str) -> bool:
    """Return whether LiteLLM retains a process-wide key for this provider."""
    return any(getattr(litellm, name, None) for name in PROVIDER_API_KEY_GLOBALS.get(provider, ()))


def _has_live_provider_api_key_environment(provider: str) -> bool:
    """Return whether a provider key is currently available in the environment."""
    environment_variables = PROVIDER_API_KEY_ENV_VARS.get(provider, ())
    if not environment_variables:
        provider_config = _resolve_provider_registry().get(provider)
        api_key_env = getattr(provider_config, "api_key_env", None)
        environment_variables = (api_key_env,) if api_key_env else ()
    return any(os.environ.get(environment_variable) for environment_variable in environment_variables)


AWS_REQUEST_CREDENTIAL_KEYS = (
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_region_name",
)

LITELLM_AWS_CREDENTIAL_SELECTOR_ENV_VARS = (
    "AWS_PROFILE_NAME",
    "AWS_ROLE_NAME",
)

AWS_REQUEST_ENDPOINT_ENV_VARS = (
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
    "AWS_ENDPOINT_URL_SAGEMAKER_RUNTIME",
)

AWS_CREDENTIAL_CHAIN_ENV_VARS = (
    *LITELLM_AWS_CREDENTIAL_SELECTOR_ENV_VARS,
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "BOTO_CONFIG",
    "AWS_CREDENTIAL_FILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
    "AWS_EC2_METADATA_V1_DISABLED",
    "AWS_IMDS_USE_IPV6",
    *AWS_REQUEST_ENDPOINT_ENV_VARS,
    "AWS_ENDPOINT_URL_STS",
    "AWS_STS_REGIONAL_ENDPOINTS",
    "AWS_SECURITY_TOKEN",
    "AWS_REGION_NAME",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)

BEDROCK_MANTLE_REQUEST_CONTEXT_KEYS = (
    *AWS_REQUEST_CREDENTIAL_KEYS,
    "aws_bedrock_runtime_endpoint",
)

BEDROCK_MANTLE_REQUEST_BODY_EXCLUDED_KEYS = BEDROCK_MANTLE_REQUEST_CONTEXT_KEYS

AWS_REQUEST_PROVIDERS = {
    "bedrock",
    "bedrock_mantle",
    "sagemaker",
    "sagemaker_chat",
    "sagemaker_nova",
}

LITELLM_GLOBAL_FIRST_API_BASE_PROVIDERS = {
    "custom",
    "gradient_ai",
    "ollama",
    "ollama_chat",
    "triton",
}

_bedrock_mantle_request_credentials = ContextVar("bedrock_mantle_request_credentials", default=None)
_bedrock_mantle_block_bearer = ContextVar("bedrock_mantle_block_bearer", default=False)
_BEDROCK_MANTLE_ORIGINAL_SIGNER = "_pr_agent_original_sign_request"
_BEDROCK_MANTLE_ORIGINAL_TOKEN_RESOLVER = "_pr_agent_original_resolve_bearer_token"
_bedrock_mantle_sign_request = None
_bedrock_mantle_resolve_bearer_token = None
if BedrockMantleAuthMixin is not None:
    _bedrock_mantle_sign_request = getattr(
        BedrockMantleAuthMixin.sign_request,
        _BEDROCK_MANTLE_ORIGINAL_SIGNER,
        BedrockMantleAuthMixin.sign_request,
    )
    _bedrock_mantle_resolve_bearer_token = getattr(
        BedrockMantleAuthMixin._resolve_bearer_token,
        _BEDROCK_MANTLE_ORIGINAL_TOKEN_RESOLVER,
        BedrockMantleAuthMixin._resolve_bearer_token,
    )

_vertex_request_credentials = ContextVar("vertex_request_credentials", default=None)
_vertex_request_active = ContextVar("vertex_request_active", default=False)
_vertex_request_aws_environment = ContextVar("vertex_request_aws_environment", default=None)
_vertex_request_default_adc = ContextVar("vertex_request_default_adc", default=None)
_databricks_request_keyless = ContextVar("databricks_request_keyless", default=False)
_raw_api_key_guard_provider = ContextVar("raw_api_key_guard_provider", default=None)
_raw_api_key_guard_auth = ContextVar("raw_api_key_guard_auth", default=None)


def _raw_guard_has_header_only_auth(provider, params, headers):
    """Recognize only initial native header-only authentication choices."""
    snapshot = _raw_api_key_guard_auth.get()
    if not snapshot or snapshot["generic_key"]:
        return False
    if provider == "ragflow":
        return True
    if provider == "xai":
        return not snapshot["xai_key"] and not params.get("use_xai_oauth")
    if provider != "azure_ai" or any(name.lower() == "api-key" for name in headers):
        return False
    if (
        snapshot["azure_key"] or snapshot["azure_ad_token"] or snapshot["azure_refresh"]
        or params.get("azure_ad_token") or params.get("azure_ad_token_provider") is not None
    ):
        return False
    environment = snapshot["azure_environment"]
    tenant = params.get("tenant_id") or environment["AZURE_TENANT_ID"]
    client = params.get("client_id") or environment["AZURE_CLIENT_ID"]
    secret = params.get("client_secret") or environment["AZURE_CLIENT_SECRET"]
    username = params.get("azure_username") or environment["AZURE_USERNAME"]
    password = params.get("azure_password") or environment["AZURE_PASSWORD"]
    return not ((tenant and client and secret) or (username and password and client))


def _install_raw_api_key_guard_override_bridge(provider):
    """Undo generated auth headers without reopening a native key resolver."""
    if provider not in ("azure_ai", "ragflow", "xai"):
        return
    from litellm.llms.azure_ai.chat.transformation import AzureAIStudioConfig
    from litellm.llms.ragflow.chat.transformation import RAGFlowConfig
    from litellm.llms.xai.chat.transformation import XAIChatConfig

    config = {"azure_ai": AzureAIStudioConfig, "ragflow": RAGFlowConfig, "xai": XAIChatConfig}[provider]
    original = config.validate_environment
    marker = "_pr_agent_original_raw_override_validate_environment"
    if getattr(original, marker, None) is not None:
        return
    signature = inspect.signature(original)
    if not {"api_key", "headers", "litellm_params"}.issubset(signature.parameters):
        raise RuntimeError("LiteLLM's native HTTP authentication interface is incompatible")

    def validate_environment(self, *args, **kwargs):
        if _raw_api_key_guard_provider.get() != provider or type(self) is not config:
            return original(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        headers = bound.arguments.get("headers") or {}
        native_params = bound.arguments.get("litellm_params") or {}
        companion = _azure_ad_responses_request.get()
        if (
            provider == "azure_ai" and companion is not None and companion.get("companion")
            and companion["generated_guard"]
            and bound.arguments.get("api_key") == DUMMY_LITELLM_API_KEY
            and native_params.get("custom_llm_provider") == provider
            and native_params.get("azure_ad_token") == companion["dispatch_token"]
        ):
            # The outer validator selects AD only without a key. Keep the guard
            # in the inner validator, whose captured resolver bypasses fallback.
            bound.arguments["headers"] = dict(headers)
            bound.arguments["api_key"] = None
            bound.arguments["litellm_params"] = {**native_params, "api_key": DUMMY_LITELLM_API_KEY}
            return original(*bound.args, **bound.kwargs)
        authorization = [name for name in headers if name.lower() == "authorization"]
        if (
            bound.arguments.get("api_key") != DUMMY_LITELLM_API_KEY
            or native_params.get("custom_llm_provider") != provider
            or len(authorization) != 1
            or not _raw_guard_has_header_only_auth(provider, native_params, headers)
        ):
            return original(self, *args, **kwargs)
        name = authorization[0]
        value = headers[name]
        bound.arguments["headers"] = dict(headers)
        # Keep the guard during validation, including nested native resolvers.
        # Preserve non-auth effects such as Ragflow's model extraction.
        result = original(*bound.args, **bound.kwargs)
        for header in list(result):
            if header.lower() == "authorization" or (provider == "azure_ai" and header.lower() == "api-key"):
                del result[header]
        result[name] = value
        return result

    setattr(validate_environment, marker, original)
    config.validate_environment = validate_environment


def _install_raw_api_key_guard_bridge():
    """Keep generated fallback guards from replacing explicit native HTTP auth."""
    from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig

    original = OpenAIGPTConfig.validate_environment
    marker = "_pr_agent_original_raw_validate_environment"
    if getattr(original, marker, None) is not None:
        return
    signature = inspect.signature(original)
    if not {"api_key", "headers", "litellm_params"}.issubset(signature.parameters):
        raise RuntimeError("LiteLLM's native HTTP authentication interface is incompatible")

    def validate_environment(self, *args, **kwargs):
        provider = _raw_api_key_guard_provider.get()
        # Overrides that call super() may have their own authentication policy.
        if provider is None or type(self).validate_environment is not validate_environment:
            return original(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        headers = bound.arguments.get("headers") or {}
        native_params = bound.arguments.get("litellm_params") or {}
        if (
            native_params.get("custom_llm_provider") == provider
            and bound.arguments.get("api_key") == DUMMY_LITELLM_API_KEY
            and any(name.lower() == "authorization" for name in headers)
        ):
            # Only this pure validator sees None. Keep the guard in dispatch
            # kwargs so other native consumers cannot discover a foreign key.
            bound.arguments["api_key"] = None
        return original(*bound.args, **bound.kwargs)

    setattr(validate_environment, marker, original)
    OpenAIGPTConfig.validate_environment = validate_environment


def _install_databricks_keyless_bridge():
    """Restore native auth only for a placeholder generated by the active handler."""
    try:
        from litellm.llms.databricks.common_utils import DatabricksBase
    except ImportError as error:
        raise RuntimeError("LiteLLM's Databricks authentication interface is unavailable") from error
    original = getattr(DatabricksBase, "databricks_validate_environment", None)
    marker = "_pr_agent_original_databricks_validate_environment"
    if getattr(original, marker, None) is not None:
        return
    try:
        signature = inspect.signature(original)
        parameter = signature.parameters["api_key"]
        if parameter.kind not in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY):
            raise ValueError("Unsupported api_key parameter")
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("LiteLLM's Databricks authentication interface is incompatible") from error

    def validate_environment(self, *args, **kwargs):
        if not _databricks_request_keyless.get():
            return original(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        if bound.arguments.get("api_key") == DUMMY_LITELLM_API_KEY:
            bound.arguments["api_key"] = None
        # Keep native OAuth/SDK authentication; do not let the global-key guard
        # overwrite its Authorization header. Their deployment configuration
        # is not a request-level PR-Agent credential setting.
        return original(*bound.args, **bound.kwargs)

    setattr(validate_environment, marker, original)
    DatabricksBase.databricks_validate_environment = validate_environment


def _snapshot_cloud_sdk_project(directory):
    """Capture Cloud SDK 583's project property without running a subprocess."""
    if "CLOUDSDK_CORE_PROJECT" in os.environ:
        return os.environ["CLOUDSDK_CORE_PROJECT"].strip() or None
    paths = []
    executable = shutil.which("gcloud.cmd" if os.name == "nt" else "gcloud")
    if executable:
        sdk_root = os.path.dirname(os.path.dirname(os.path.realpath(executable)))
        if os.path.isdir(os.path.join(sdk_root, ".install")):
            paths.append(os.path.join(sdk_root, "properties"))
    configuration = os.environ.get("CLOUDSDK_ACTIVE_CONFIG_NAME")
    migrate_legacy = False
    if not configuration:
        try:
            with open(os.path.join(directory, "active_config"), encoding="utf-8") as active:
                configuration = active.read()
        except FileNotFoundError:
            configuration = None
            migrate_legacy = True
        if configuration and configuration != "NONE" and not re.match(r"^[a-z][-a-z0-9]*$", configuration):
            # The SDK removes an invalid activator before checking legacy files.
            migrate_legacy = True
            configuration = None
    if not configuration:
        legacy_path = os.path.join(directory, "properties")
        legacy_contents = ""
        if migrate_legacy:
            try:
                with open(legacy_path, encoding="utf-8") as legacy:
                    legacy_contents = legacy.read()
            except FileNotFoundError:
                # No legacy properties file: fall back to the default named config below.
                pass
        deprecated = (
            "# This properties file has been superseded by named configurations.\n"
            "# Editing it will have no effect.\n\n"
        )
        if legacy_contents and not legacy_contents.startswith(deprecated):
            paths.append(legacy_path)
        else:
            paths.append(os.path.join(directory, "configurations", "config_default"))
    elif configuration != "NONE":
        paths.append(os.path.join(directory, "configurations", f"config_{configuration}"))
    # Match the SDK's terminal-newline semantics without trimming the filename.
    if configuration and configuration != "NONE" and not re.match(r"^[a-z][-a-z0-9]*$", configuration):
        raise ValueError("Invalid Cloud SDK configuration name")
    project = None
    for path in paths:
        parser = configparser.ConfigParser()
        try:
            with open(path, encoding="utf-8") as config:
                parser.read_file(config)
        except FileNotFoundError:
            continue
        project = parser.get("core", "project", fallback=project)
    return (project.strip() or None) if project is not None else None


def _load_vertex_default_adc(snapshot, project_id):
    """Load only the captured ADC source, retaining native credential refresh."""
    from google.auth import _default, exceptions
    from google.auth import credentials as google_credentials
    from google.auth.transport.requests import Request

    if snapshot["error"]:
        raise ValueError(f"Unable to snapshot default Vertex credentials: {snapshot['error']}")
    info = snapshot["info"]
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    credential_project = None
    if info is None:
        # Skip file-based discovery permanently when no file was captured.
        # Retain google-auth 2.55.1's managed-runtime discovery and refresh.
        for name in ("_get_gae_credentials", "_get_gce_credentials"):
            if not callable(getattr(_default, name, None)):
                raise RuntimeError("Google Auth managed ADC interface is unavailable")
        loaded, credential_project = (None, None)
        if snapshot["gae_runtime"] == "python27":
            loaded, credential_project = _default._get_gae_credentials()
        if loaded is None:
            loaded, credential_project = _default._get_gce_credentials(request=Request())
        if loaded is None:
            raise exceptions.DefaultCredentialsError("No captured file or managed-runtime Vertex credentials available")
    else:
        credential_type = info.get("type")
        if credential_type == "authorized_user":
            from google.oauth2.credentials import Credentials

            loaded = Credentials.from_authorized_user_info(info, scopes=scopes)
        elif credential_type == "service_account":
            from google.oauth2.service_account import Credentials

            loaded = Credentials.from_service_account_info(info, scopes=scopes)
            credential_project = loaded.project_id
        elif credential_type == "impersonated_service_account":
            from google.auth.impersonated_credentials import Credentials

            loaded = Credentials.from_impersonated_service_account_info(info, scopes=scopes)
        elif credential_type == "external_account_authorized_user":
            from google.auth.external_account_authorized_user import Credentials

            loaded = Credentials.from_info(info)
        elif credential_type == "gdch_service_account":
            from google.oauth2.gdch_credentials import ServiceAccountCredentials

            loaded = ServiceAccountCredentials.from_service_account_info(info)
            credential_project = info.get("project")
        elif credential_type == "external_account":
            source = info.get("credential_source", {})
            if not isinstance(source, dict):
                raise ValueError("Invalid Vertex credential source")
            if "executable" in source:
                raise ValueError("Vertex executable credentials are incompatible with request isolation")
            if info.get("subject_token_type") == "urn:ietf:params:aws:token-type:aws4_request":
                loaded = _vertex_aws_credentials_from_snapshot(info, snapshot["aws_environment"], scopes)
            else:
                from google.auth.identity_pool import Credentials

                loaded = Credentials.from_info(info, scopes=scopes)
        else:
            raise ValueError("Unsupported default Vertex credential type")
    loaded = google_credentials.with_scopes_if_required(loaded, scopes)
    quota_project = snapshot["quota_project"]
    if isinstance(loaded, google_credentials.CredentialsWithQuotaProject):
        # Apply only the captured environment override; never consult a later one.
        if quota_project or info is None:
            loaded = loaded.with_quota_project(quota_project)
    if (
        info is not None and info.get("type") == "external_account"
        and not project_id and not snapshot["sdk_project"]
    ):
        credential_project = loaded.get_project_id(request=Request())
    resolved_project = project_id or credential_project or snapshot["sdk_project"]
    if not resolved_project:
        if snapshot["sdk_project_error"]:
            raise ValueError(f"Unable to snapshot Cloud SDK project: {snapshot['sdk_project_error']}")
        raise ValueError("Could not resolve project_id")
    if not isinstance(resolved_project, str):
        raise TypeError("Expected project_id to be a str")
    return loaded, resolved_project


def _install_vertex_default_adc_bridge():
    """Recognize handler-owned ADC cache keys without changing unrelated LiteLLM callers."""
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    original = VertexBase.load_auth
    marker = "_pr_agent_original_default_adc_load_auth"
    if getattr(original, marker, None) is not None:
        return

    def load_auth(self, credentials, project_id):
        snapshot = _vertex_request_default_adc.get()
        if snapshot is None or credentials != snapshot["cache_key"]:
            return original(self, credentials, project_id)
        load_default_adc = _handler_attr("_load_vertex_default_adc", _load_vertex_default_adc)
        loaded, resolved_project = load_default_adc(snapshot, project_id)
        self.refresh_auth(loaded)
        return loaded, resolved_project

    load_auth.__dict__.update(vars(original))
    setattr(load_auth, marker, original)
    VertexBase.load_auth = load_auth


def _vertex_aws_credentials_from_snapshot(info, environment, scopes):
    """Bind the native AWS WIF source without replacing Google's signer or refresh flow."""
    from google.auth import aws, exceptions

    environment = dict(environment)
    source = dict(info["credential_source"])

    class SnapshotSupplier(aws.AwsSecurityCredentialsSupplier):
        def _metadata(self, request, url, *, method="GET", headers=None):
            if not url:
                raise exceptions.RefreshError("Missing AWS WIF metadata endpoint")
            response = request(url=url, method=method, headers=headers)
            if response.status != 200:
                raise exceptions.RefreshError("Unable to retrieve AWS WIF metadata")
            return response.data.decode("utf-8") if isinstance(response.data, bytes) else response.data

        def _metadata_headers(self, request):
            token_url = source.get("imdsv2_session_token_url")
            if token_url is None:
                return None
            token = self._metadata(
                request, token_url, method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"},
            )
            return {"X-aws-ec2-metadata-token": token}

        def get_aws_security_credentials(self, context, request):
            access_key = environment.get("AWS_ACCESS_KEY_ID")
            secret_key = environment.get("AWS_SECRET_ACCESS_KEY")
            if access_key and secret_key:
                return aws.AwsSecurityCredentials(access_key, secret_key, environment.get("AWS_SESSION_TOKEN"))
            headers = self._metadata_headers(request)
            url = source.get("url")
            role = self._metadata(request, url, headers=headers)
            credentials = json.loads(self._metadata(request, f"{url}/{role}", headers=headers))
            return aws.AwsSecurityCredentials(
                credentials.get("AccessKeyId"), credentials.get("SecretAccessKey"), credentials.get("Token"),
            )

        def get_aws_region(self, context, request):
            for variable in ("AWS_REGION", "AWS_DEFAULT_REGION"):
                region = environment.get(variable)
                if region is not None:
                    return region
            return self._metadata(request, source.get("region_url"), headers=self._metadata_headers(request))[:-1]

    supplier = SnapshotSupplier()

    class SnapshotCredentials(aws.Credentials):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # google-auth 2.55.1 cannot combine a supplier with credential_source
            # through its public constructor. Preserve native validation and the
            # verification URL; reapply the supplier on copies and impersonation.
            if not isinstance(
                getattr(self, "_aws_security_credentials_supplier", None), aws.AwsSecurityCredentialsSupplier,
            ):
                raise RuntimeError("Google Auth AWS WIF supplier interface is unavailable")
            self._aws_security_credentials_supplier = supplier

    return SnapshotCredentials.from_info(info, scopes=scopes)


def _install_vertex_executable_guard():
    """Reject executable ADC without replacing LiteLLM's native token cache."""
    from google.auth import pluggable

    credentials = pluggable.Credentials
    marker = "_pr_agent_original_vertex_executable"
    replacements = {}

    def guard(original):
        def guarded(*args, **kwargs):
            if _vertex_request_active.get():
                raise ValueError("Vertex executable credentials are incompatible with request isolation")
            return original(*args, **kwargs)

        setattr(guarded, marker, original)
        return guarded

    # LiteLLM 1.101.0 reads expired (sync) or token_state (async) on the
    # actual cached credential before returning its token. Guarding the
    # descriptors also covers cache replacement during an async lock wait.
    for name in ("from_info", "refresh", "expired", "token_state"):
        descriptor = inspect.getattr_static(credentials, name, None)
        if isinstance(descriptor, classmethod):
            original = descriptor.__func__
        elif isinstance(descriptor, property):
            original = descriptor.fget
        else:
            original = descriptor
        _require_litellm_interface(original, f"Google executable credentials {name}")
        if getattr(original, marker, None) is not None:
            continue
        wrapped = guard(original)
        if isinstance(descriptor, classmethod):
            wrapped = classmethod(wrapped)
        elif isinstance(descriptor, property):
            wrapped = property(wrapped, descriptor.fset, descriptor.fdel, descriptor.__doc__)
        replacements[name] = wrapped

    # Validate every entry point before installing any of the guards.
    for name, replacement in replacements.items():
        setattr(credentials, name, replacement)


def _install_vertex_wif_project_bridge():
    """Restore ADC project discovery for request-owned WIF JSON in LiteLLM 1.101.0."""
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    original = VertexBase.load_auth
    marker = "_pr_agent_original_vertex_wif_load_auth"
    if getattr(original, marker, None) is not None:
        return

    def load_auth(self, credentials, project_id):
        snapshot = _vertex_request_credentials.get()
        if not isinstance(snapshot, dict) or snapshot.get("type") != "external_account":
            return original(self, credentials, project_id)
        try:
            json_obj = json.loads(credentials) if isinstance(credentials, str) else credentials
        except (ValueError, TypeError):
            return original(self, credentials, project_id)
        if json_obj != snapshot:
            return original(self, credentials, project_id)

        # Preserve LiteLLM 1.101.0's WIF factories, including its explicit AWS
        # supplier. Only project discovery is missing from its JSON load path.
        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        source = json_obj.get("credential_source", {})
        environment_id = source.get("environment_id", "") if isinstance(source, dict) else ""
        aws_environment = _vertex_request_aws_environment.get()
        bind_aws = isinstance(environment_id, str) and "aws" in environment_id and aws_environment is not None
        if project_id is not None and not bind_aws:
            return original(self, credentials, project_id)
        if isinstance(environment_id, str) and "aws" in environment_id:
            from litellm.llms.vertex_ai.vertex_ai_aws_wif import VertexAIAwsWifAuth

            aws_params = VertexAIAwsWifAuth.extract_aws_params(json_obj)
            if aws_params:
                loaded = VertexAIAwsWifAuth.credentials_from_explicit_aws(json_obj, aws_params, scopes)
            elif bind_aws:
                loaded = _vertex_aws_credentials_from_snapshot(json_obj, aws_environment, scopes)
            else:
                loaded = self._credentials_from_identity_pool_with_aws(json_obj, scopes)
        elif isinstance(source, dict) and "executable" in source:
            raise ValueError("Vertex executable credentials are incompatible with request isolation")
        else:
            loaded = self._credentials_from_identity_pool(json_obj, scopes)

        from google.auth.transport.requests import Request

        # Use the same credential for discovery, refresh, and the upstream cache;
        # never reload ADC or apply a process-wide quota project override here.
        resolved_project = project_id if project_id is not None else loaded.get_project_id(request=Request())
        self.refresh_auth(loaded)
        if not resolved_project:
            raise ValueError("Could not resolve project_id")
        if not isinstance(resolved_project, str):
            raise TypeError(f"Expected project_id to be a str but got {type(resolved_project)}")
        return loaded, resolved_project

    load_auth.__dict__.update(vars(original))
    setattr(load_auth, marker, original)
    VertexBase.load_auth = load_auth


def _install_vertex_impersonated_credentials_bridge():
    """Add LiteLLM 1.101.0's missing ADC type without replacing its refresh/cache path."""
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    original = VertexBase._credentials_from_service_account
    marker = "_pr_agent_original_vertex_service_account_loader"
    if getattr(original, marker, None) is not None:
        return

    def load_credentials(self, json_obj, scopes):
        request_credentials = _vertex_request_credentials.get()
        if (
            isinstance(json_obj, dict)
            and json_obj.get("type") == "impersonated_service_account"
            and request_credentials is not None
            and json_obj == request_credentials
        ):
            from google.auth import impersonated_credentials

            # The generic ADC loader may apply a live GOOGLE_CLOUD_QUOTA_PROJECT.
            # This constructor uses only the captured configuration, including quota.
            return impersonated_credentials.Credentials.from_impersonated_service_account_info(
                json_obj, scopes=scopes,
            )
        return original(self, json_obj, scopes)

    setattr(load_credentials, marker, original)
    VertexBase._credentials_from_service_account = load_credentials


_anthropic_request_auth_token = ContextVar("anthropic_request_auth_token", default=None)
_ANTHROPIC_ORIGINAL_API_KEY_RESOLVER = "_pr_agent_original_get_api_key"
_ANTHROPIC_ORIGINAL_AUTH_TOKEN_RESOLVER = "_pr_agent_original_get_auth_token"
_anthropic_get_api_key = getattr(AnthropicModelInfo, "get_api_key", None)
_anthropic_get_api_key = getattr(
    _anthropic_get_api_key,
    _ANTHROPIC_ORIGINAL_API_KEY_RESOLVER,
    _anthropic_get_api_key,
)
_anthropic_get_auth_token = getattr(AnthropicModelInfo, "get_auth_token", None)
_anthropic_get_auth_token = getattr(
    _anthropic_get_auth_token,
    _ANTHROPIC_ORIGINAL_AUTH_TOKEN_RESOLVER,
    _anthropic_get_auth_token,
)


def _resolve_anthropic_api_key(api_key=None):
    """Turn PR-Agent's request-local guard back into Anthropic's keyless state."""
    request_auth = _anthropic_request_auth_token.get()
    if request_auth is not None and request_auth["generated_guard"] and api_key == DUMMY_LITELLM_API_KEY:
        return None
    return _anthropic_get_api_key(api_key)


def _resolve_anthropic_auth_token(auth_token=None):
    """Resolve only the bearer token captured for the active Anthropic request."""
    request_auth = _anthropic_request_auth_token.get()
    if request_auth is not None:
        return request_auth["auth_token"]
    return _anthropic_get_auth_token(auth_token)


def _install_anthropic_auth_token_bridge():
    """Install request-local Anthropic credential resolvers without stacking wrappers."""
    global _anthropic_get_api_key, _anthropic_get_auth_token
    anthropic_model_info = _handler_attr("AnthropicModelInfo", AnthropicModelInfo)
    _require_litellm_interface(
        anthropic_model_info, "AnthropicModelInfo", ("get_api_key", "get_auth_token")
    )
    current_api_key_resolver = anthropic_model_info.get_api_key
    _anthropic_get_api_key = getattr(
        current_api_key_resolver,
        _ANTHROPIC_ORIGINAL_API_KEY_RESOLVER,
        current_api_key_resolver,
    )
    setattr(_resolve_anthropic_api_key, _ANTHROPIC_ORIGINAL_API_KEY_RESOLVER, _anthropic_get_api_key)
    anthropic_model_info.get_api_key = staticmethod(_resolve_anthropic_api_key)
    current_auth_token_resolver = anthropic_model_info.get_auth_token
    _anthropic_get_auth_token = getattr(
        current_auth_token_resolver,
        _ANTHROPIC_ORIGINAL_AUTH_TOKEN_RESOLVER,
        current_auth_token_resolver,
    )
    setattr(
        _resolve_anthropic_auth_token,
        _ANTHROPIC_ORIGINAL_AUTH_TOKEN_RESOLVER,
        _anthropic_get_auth_token,
    )
    anthropic_model_info.get_auth_token = staticmethod(_resolve_anthropic_auth_token)


def _resolve_bedrock_mantle_bearer_token(api_key):
    """Prevent LiteLLM's generic key fallback while preserving request-local SigV4."""
    if _bedrock_mantle_block_bearer.get():
        return None if api_key == DUMMY_LITELLM_API_KEY else ""
    return _bedrock_mantle_resolve_bearer_token(api_key)


def _sign_bedrock_mantle_request(self, *args, **kwargs):
    """Bridge request-local AWS credentials to LiteLLM's Bedrock Mantle signer."""
    bedrock_mantle_sign_request = _bedrock_mantle_sign_request
    request_credentials = _bedrock_mantle_request_credentials.get()
    if request_credentials is not None:
        if "request_data" in kwargs:
            request_data = kwargs["request_data"]
        else:
            try:
                parameters = tuple(inspect.signature(bedrock_mantle_sign_request).parameters.values())
                request_data_position = next(
                    index - 1
                    for index, parameter in enumerate(parameters)
                    if parameter.name == "request_data"
                    and parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                )
            except (StopIteration, TypeError, ValueError):
                request_data_position = None
            if request_data_position is None or len(args) <= request_data_position:
                raise RuntimeError(
                    "LiteLLM's Bedrock Mantle signer did not receive request_data; "
                    "request-local AWS credentials cannot be removed from the request body"
                )
            request_data = args[request_data_position]
        if not isinstance(request_data, dict):
            raise RuntimeError(
                "LiteLLM's Bedrock Mantle signer received invalid request_data; "
                "request-local AWS credentials cannot be removed from the request body"
            )
        for key in BEDROCK_MANTLE_REQUEST_BODY_EXCLUDED_KEYS:
            request_data.pop(key, None)
    if _bedrock_mantle_block_bearer.get():
        if "api_key" in kwargs:
            kwargs["api_key"] = ""
        else:
            try:
                parameters = tuple(inspect.signature(bedrock_mantle_sign_request).parameters.values())
                api_key_parameter = next(
                    (index, parameter)
                    for index, parameter in enumerate(parameters)
                    if parameter.name == "api_key"
                )
            except (StopIteration, TypeError, ValueError):
                raise RuntimeError(
                    "LiteLLM's Bedrock Mantle signer did not expose api_key; "
                    "request-local bearer isolation cannot be applied"
                )
            api_key_index, api_key_parameter = api_key_parameter
            if api_key_parameter.kind == api_key_parameter.KEYWORD_ONLY:
                kwargs["api_key"] = ""
            elif api_key_parameter.kind in (
                api_key_parameter.POSITIONAL_ONLY,
                api_key_parameter.POSITIONAL_OR_KEYWORD,
            ):
                api_key_position = api_key_index - 1
                if len(args) > api_key_position:
                    args = (*args[:api_key_position], "", *args[api_key_position + 1:])
                else:
                    kwargs["api_key"] = ""
            else:
                raise RuntimeError(
                    "LiteLLM's Bedrock Mantle signer has an unsupported api_key parameter; "
                    "request-local bearer isolation cannot be applied"
                )
    if request_credentials:
        if "optional_params" in kwargs:
            kwargs["optional_params"] = {**(kwargs["optional_params"] or {}), **request_credentials}
        else:
            try:
                parameters = tuple(inspect.signature(bedrock_mantle_sign_request).parameters.values())
                optional_params_position = next(
                    index - 1
                    for index, parameter in enumerate(parameters)
                    if parameter.name == "optional_params"
                    and parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                )
            except (StopIteration, TypeError, ValueError):
                optional_params_position = None
            if optional_params_position is None or len(args) <= optional_params_position:
                raise RuntimeError(
                    "LiteLLM's Bedrock Mantle signer did not receive optional_params; "
                    "request-local AWS credentials cannot be applied"
                )
            args = (
                *args[:optional_params_position],
                {**(args[optional_params_position] or {}), **request_credentials},
                *args[optional_params_position + 1:],
            )
    return bedrock_mantle_sign_request(self, *args, **kwargs)


def _install_bedrock_mantle_signer_bridge():
    """Install the signer bridge without wrapping it again after a module reload."""
    global _bedrock_mantle_resolve_bearer_token, _bedrock_mantle_sign_request
    if BedrockMantleAuthMixin is None:
        return
    current_signer = BedrockMantleAuthMixin.sign_request
    _bedrock_mantle_sign_request = getattr(
        current_signer,
        _BEDROCK_MANTLE_ORIGINAL_SIGNER,
        current_signer,
    )
    setattr(_sign_bedrock_mantle_request, _BEDROCK_MANTLE_ORIGINAL_SIGNER, _bedrock_mantle_sign_request)
    BedrockMantleAuthMixin.sign_request = _sign_bedrock_mantle_request
    current_resolver = BedrockMantleAuthMixin._resolve_bearer_token
    _bedrock_mantle_resolve_bearer_token = getattr(
        current_resolver,
        _BEDROCK_MANTLE_ORIGINAL_TOKEN_RESOLVER,
        current_resolver,
    )
    setattr(
        _resolve_bedrock_mantle_bearer_token,
        _BEDROCK_MANTLE_ORIGINAL_TOKEN_RESOLVER,
        _bedrock_mantle_resolve_bearer_token,
    )
    BedrockMantleAuthMixin._resolve_bearer_token = staticmethod(_resolve_bedrock_mantle_bearer_token)
