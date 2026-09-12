import asyncio
import configparser
import contextlib
import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import stat
from contextvars import ContextVar
from functools import lru_cache, wraps
from types import FunctionType, SimpleNamespace
from urllib.parse import urlparse

import httpx
import litellm
import openai
import requests
from litellm import acompletion
from tenacity import retry, retry_if_exception, stop_after_attempt

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
    from litellm.llms.bedrock_mantle.common_utils import MANTLE_HOST_RE, BedrockMantleAuthMixin
except ImportError:
    BedrockMantleAuthMixin = None
    MANTLE_HOST_RE = None

from pr_agent.algo import (
    CLAUDE_EXTENDED_THINKING_MODELS,
    GROK_REASONING_EFFORT_LEVELS,
    NO_SUPPORT_TEMPERATURE_MODELS,
    STREAMING_REQUIRED_MODELS,
    SUPPORT_REASONING_EFFORT_MODELS,
    USER_MESSAGE_ONLY_MODELS,
    normalize_litellm_model,
)
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_helpers import (
    _get_azure_ad_credential,
    _get_azure_ad_token,
    _handle_streaming_response,
    _process_litellm_extra_body,
    _response_field,
    get_repetition_penalty,
)
from pr_agent.algo.run_details import _as_decimal_cost, record_ai_call
from pr_agent.algo.utils import ReasoningEffort, get_version
from pr_agent.config_loader import get_settings, get_verbosity_level
from pr_agent.log import get_logger

MODEL_RETRIES = 2
DUMMY_LITELLM_API_KEY = "dummy_key"  # request-local guard against LiteLLM's process-wide key fallbacks
_IMAGE_HEAD_TIMEOUT_SECONDS = 5
OPENAI_DEFAULT_API_BASE = "https://api.openai.com/v1"

PROVIDER_SETTING_PATHS = {
    "anthropic": {"api_key": "ANTHROPIC.KEY"},
    "codestral": {"api_key": "CODESTRAL.KEY"},
    "cohere": {"api_key": "COHERE.KEY"},
    "cohere_chat": {"api_key": "COHERE.KEY"},
    "dashscope": {"api_key": "DASHSCOPE.KEY"},
    "databricks": {"api_key": "DATABRICKS.API_KEY", "api_base": "DATABRICKS.API_BASE"},
    "deepinfra": {"api_key": "DEEPINFRA.KEY"},
    "deepseek": {"api_key": "DEEPSEEK.KEY"},
    "gemini": {"api_key": "GOOGLE_AI_STUDIO.GEMINI_API_KEY"},
    "groq": {"api_key": "GROQ.KEY"},
    "huggingface": {"api_key": "HUGGINGFACE.KEY", "api_base": "HUGGINGFACE.API_BASE"},
    "mistral": {"api_key": "MISTRAL.KEY"},
    "moonshot": {"api_key": "MOONSHOT.KEY", "api_base": "MOONSHOT.API_BASE"},
    "ollama": {"api_key": "OLLAMA.API_KEY", "api_base": "OLLAMA.API_BASE"},
    "openrouter": {"api_key": "OPENROUTER.KEY", "api_base": "OPENROUTER.API_BASE"},
    "replicate": {"api_key": "REPLICATE.KEY"},
    "sambanova": {"api_key": "SAMBANOVA.KEY"},
    "text-completion-codestral": {"api_key": "CODESTRAL.KEY"},
    "xai": {"api_key": "XAI.KEY"},
    "xiaomi_mimo": {"api_key": "XIAOMI_MIMO.KEY"},
    "zai": {"api_key": "ZAI.KEY"},
}

PROVIDER_SETTING_ALIASES = {
    "aiohttp_openai": "openai",
    "anthropic_text": "anthropic",
    "azure_text": "azure",
    "ollama_chat": "ollama",
    "text-completion-inception": "inception",
    "text-completion-openai": "openai",
    "vertex_ai_beta": "vertex_ai",
}

# Keep chat-completion endpoint aliases in the same precedence order as LiteLLM 1.100.0.
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
    "watsonx": {
        "api_base": ("WATSONX_API_BASE", "WATSONX_URL", "WX_URL", "WML_URL"),
        "project_id": ("WATSONX_PROJECT_ID", "WX_PROJECT_ID", "PROJECT_ID"),
        "space_id": ("WATSONX_DEPLOYMENT_SPACE_ID", "WATSONX_SPACE_ID", "WX_SPACE_ID", "SPACE_ID"),
        "region_name": ("WATSONX_REGION", "WX_REGION", "REGION"),
        "token": ("WATSONX_TOKEN",),
        "zen_api_key": ("WATSONX_ZENAPIKEY",),
    },
    "watsonx_text": {
        "api_base": ("WATSONX_API_BASE", "WATSONX_URL", "WX_URL", "WML_URL"),
        "project_id": ("WATSONX_PROJECT_ID", "WX_PROJECT_ID", "PROJECT_ID"),
        "space_id": ("WATSONX_DEPLOYMENT_SPACE_ID", "WATSONX_SPACE_ID", "WX_SPACE_ID", "SPACE_ID"),
        "region_name": ("WATSONX_REGION", "WX_REGION", "REGION"),
        "token": ("WATSONX_TOKEN",),
        "zen_api_key": ("WATSONX_ZENAPIKEY",),
    },
}

# Keep aliases in the same precedence order as LiteLLM 1.100.0.
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
AZURE_AD_TOKEN_ENV_VARS = ("AZURE_AD_TOKEN", "AZURE_OPENAI_AD_TOKEN")
AZURE_OIDC_ENV_VARS = ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST", "AZURE_SCOPE")
AZURE_OIDC_AUTH_ENV_VARS = ("AZURE_CLIENT_SECRET", "AZURE_USERNAME", "AZURE_PASSWORD")

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
    exchange = FunctionType(native.__code__, native_globals, native.__name__, native.__defaults__, native.__closure__)
    exchange.__kwdefaults__ = copy.copy(native.__kwdefaults__)
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
        factory = FunctionType(
            original_entra.__code__, globals_view, original_entra.__name__,
            original_entra.__defaults__, original_entra.__closure__,
        )
        factory.__kwdefaults__ = copy.copy(original_entra.__kwdefaults__)
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
        bound = FunctionType(
            original_resolver.__code__, native_globals, original_resolver.__name__,
            original_resolver.__defaults__, original_resolver.__closure__,
        )
        bound.__kwdefaults__ = copy.copy(original_resolver.__kwdefaults__)
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
        context = _azure_oidc_request.get()
        if (
            context is not None and litellm_params.get("azure_ad_token") == context["dispatch_token"]
            and env_var_key in context["auth_environment"]
        ):
            value = litellm_params.get(param_key)
            return value if value is not None else context["auth_environment"][env_var_key]
        return original_resolve_env(owner, litellm_params, param_key, env_var_key)

    @wraps(original_initialize)
    def initialize(*args, **kwargs):
        context = _azure_oidc_request.get()
        if context is None:
            return original_initialize(*args, **kwargs)
        bound = initialize_signature.bind(*args, **kwargs)
        params = bound.arguments.get("litellm_params") or {}
        if params.get("azure_ad_token") != context["dispatch_token"]:
            return original_initialize(*args, **kwargs)
        native_globals = _azure_oidc_companion_globals(original_initialize.__globals__, context)
        factory = FunctionType(
            original_initialize.__code__, native_globals, original_initialize.__name__,
            original_initialize.__defaults__, original_initialize.__closure__,
        )
        factory.__kwdefaults__ = copy.copy(original_initialize.__kwdefaults__)
        return factory(*args, **kwargs)

    @wraps(original_cache_key)
    def cache_key(client_initialization_params, client_type):
        key = original_cache_key(client_initialization_params, client_type)
        context = _azure_oidc_request.get()
        if (
            context is not None and client_type == "azure"
            and client_initialization_params.get("azure_ad_token") == context["selector_hash"]
        ):
            return f"{key}|pr_agent_oidc={context['identity_hash']}"
        return key

    @wraps(original_client)
    def client(*args, **kwargs):
        context = _azure_oidc_request.get()
        if context is None or not context["generated_guard"]:
            return original_client(*args, **kwargs)
        bound = client_signature.bind(*args, **kwargs)
        params = bound.arguments.get("litellm_params") or {}
        if (
            bound.arguments.get("api_key") == DUMMY_LITELLM_API_KEY
            and params.get("azure_ad_token") == context["dispatch_token"]
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
    return provider in getattr(litellm, "openai_compatible_providers", ()) or JSONProviderRegistry.exists(provider)


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
    _require_litellm_interface(_get_model_info_helper, "_get_model_info_helper")
    try:
        model_info = _get_model_info_helper(model=model, custom_llm_provider=provider)
    except Exception:
        return False
    return model_info.get("mode") == "responses"


def _uses_provider_api_key(provider: str) -> bool:
    """Return whether PR-Agent should forward or guard this provider's native API key."""
    return provider in PROVIDER_API_KEY_ENV_VARS or JSONProviderRegistry.exists(provider)


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

    # LiteLLM 1.100.0 consumes model_id before Invoke resolves its region.
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
        # Match the native chat model prefixes in LiteLLM 1.100.0 without
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
    provider_config = JSONProviderRegistry.get(provider)
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
            # LiteLLM 1.100.0's ChatGPT chat transformation ignores the request
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
        provider_config = JSONProviderRegistry.get(provider)
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
        loaded, resolved_project = _load_vertex_default_adc(snapshot, project_id)
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

    # LiteLLM 1.100.0 reads expired (sync) or token_state (async) on the
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
    """Restore ADC project discovery for request-owned WIF JSON in LiteLLM 1.100.0."""
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

        # Preserve LiteLLM 1.100.0's WIF factories, including its explicit AWS
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
    """Add LiteLLM 1.100.0's missing ADC type without replacing its refresh/cache path."""
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
    _require_litellm_interface(AnthropicModelInfo, "AnthropicModelInfo", ("get_api_key", "get_auth_token"))
    current_api_key_resolver = AnthropicModelInfo.get_api_key
    _anthropic_get_api_key = getattr(
        current_api_key_resolver,
        _ANTHROPIC_ORIGINAL_API_KEY_RESOLVER,
        current_api_key_resolver,
    )
    setattr(_resolve_anthropic_api_key, _ANTHROPIC_ORIGINAL_API_KEY_RESOLVER, _anthropic_get_api_key)
    AnthropicModelInfo.get_api_key = staticmethod(_resolve_anthropic_api_key)
    current_auth_token_resolver = AnthropicModelInfo.get_auth_token
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
    AnthropicModelInfo.get_auth_token = staticmethod(_resolve_anthropic_auth_token)


def _resolve_bedrock_mantle_bearer_token(api_key):
    """Prevent LiteLLM's generic key fallback while preserving request-local SigV4."""
    if _bedrock_mantle_block_bearer.get():
        return None if api_key == DUMMY_LITELLM_API_KEY else ""
    return _bedrock_mantle_resolve_bearer_token(api_key)


def _sign_bedrock_mantle_request(self, *args, **kwargs):
    """Bridge request-local AWS credentials to LiteLLM's Bedrock Mantle signer."""
    request_credentials = _bedrock_mantle_request_credentials.get()
    if request_credentials is not None:
        if "request_data" in kwargs:
            request_data = kwargs["request_data"]
        else:
            try:
                parameters = tuple(inspect.signature(_bedrock_mantle_sign_request).parameters.values())
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
                parameters = tuple(inspect.signature(_bedrock_mantle_sign_request).parameters.values())
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
                parameters = tuple(inspect.signature(_bedrock_mantle_sign_request).parameters.values())
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
    return _bedrock_mantle_sign_request(self, *args, **kwargs)


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


def _as_bool(value, default: bool) -> bool:
    """Parse a config value that may arrive as a bool (toml) or a string (env override)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return default


def _configured_client_retries():
    """config.num_retries as a non-negative int, or None (unset/invalid = client defaults).

    Invalid values are logged and ignored rather than raised: this is read on the request
    path, and a config typo should not fail the run — nor be wrapped and retried as an API
    error by the caller's exception handling.
    """
    value = get_settings().config.get("num_retries", None)
    if value is None:
        return None
    try:
        parsed = int(str(value).strip())
    except ValueError:
        get_logger().warning(f"Ignoring invalid config.num_retries: {value!r}")
        return None
    if parsed < 0:
        get_logger().warning(f"Ignoring negative config.num_retries: {parsed}")
        return None
    return parsed


def _should_retry_same_model(exc: BaseException) -> bool:
    """Whether chat_completion retries the SAME model, before falling back to fallback_models.

    With config.retry_same_model_on_timeout set to false, a timed-out call is handed to the
    fallback-models loop instead of being replayed on the model that just missed the deadline.
    """
    if isinstance(exc, openai.RateLimitError):
        return False
    if isinstance(exc, openai.APITimeoutError):
        return _as_bool(get_settings().config.get("retry_same_model_on_timeout", True), default=True)
    return isinstance(exc, openai.APIError)


class LiteLLMAIHandler(BaseAiHandler):
    """Handle chat completions across supported providers through LiteLLM.

    Request isolation covers PR-Agent settings and captured provider environment values,
    not deployment-owned LiteLLM secret managers. Embedding applications must keep
    process environment and LiteLLM globals stable while requests run; this handler
    is not a sandbox for external code mutating shared authentication or routing state.
    """

    def __init__(self):
        """Initialize provider credentials and request settings from configuration."""
        _require_litellm_interface(
            JSONProviderRegistry, "JSONProviderRegistry", ("list_providers", "get", "exists", "supports_responses_api"),
        )
        settings = get_settings()
        self._azure_ad = bool(settings.get("AZURE_AD.CLIENT_ID", None))
        self._azure_oidc_environment = {name: os.environ.get(name) for name in AZURE_OIDC_ENV_VARS}
        self._azure_oidc_auth_environment = {name: os.environ.get(name) for name in AZURE_OIDC_AUTH_ENV_VARS}
        self._azure_companion_auth = bool(
            self._azure_oidc_environment["AZURE_CLIENT_ID"] and (
                (self._azure_oidc_environment["AZURE_TENANT_ID"]
                 and self._azure_oidc_auth_environment["AZURE_CLIENT_SECRET"])
                or (self._azure_oidc_auth_environment["AZURE_USERNAME"]
                    and self._azure_oidc_auth_environment["AZURE_PASSWORD"])
            )
        )
        self._raw_guard_auth_snapshot = {
            "generic_key": bool(getattr(litellm, "api_key", None)),
            "xai_key": bool(getattr(litellm, "xai_key", None)),
            "azure_key": bool(
                getattr(litellm, "azure_key", None) or os.environ.get("AZURE_OPENAI_API_KEY")
                or os.environ.get("AZURE_API_KEY") or self._azure_ad
                or (settings.get("OPENAI.API_TYPE", None) == "azure" and settings.get("OPENAI.KEY", None))
            ),
            "azure_ad_token": bool(os.environ.get("AZURE_AD_TOKEN")),
            "azure_refresh": getattr(litellm, "enable_azure_ad_token_refresh", False) is True,
            "azure_environment": {**self._azure_oidc_environment, **self._azure_oidc_auth_environment},
        }
        try:
            self._azure_ad_credential = _get_azure_ad_credential(settings) if self._azure_ad else None
        except Exception as e:
            get_logger().error(f"Failed to create Azure AD credential: {type(e).__name__}")
            raise
        self.azure = settings.get("OPENAI.API_TYPE", None) == "azure" or self._azure_ad
        self._openai_api_base_is_azure = settings.get("OPENAI.API_TYPE", None) == "azure" or (
            self._azure_ad and not settings.get("AZURE_AD.API_BASE", None)
        )
        self.repetition_penalty = None
        self._aws_use_imds = False
        self._aws_imds_mode = False
        self._aws_static_creds = None
        self._aws_environment_creds = None
        self._aws_active_creds = {}
        self._aws_environment_credentials_incomplete = False
        self._aws_imds_fell_back = False
        self._aws_boto3_creds = None  # original boto3 credentials object for IMDS refresh
        self._aws_region_name = None
        self._aws_credential_chain_environment = {}
        self._aws_credential_chain_files = {}
        self._aws_bedrock_lock = asyncio.Lock()
        self._vertex_credentials, self._vertex_credentials_error = self._snapshot_vertex_credentials()
        self._vertex_aws_environment = {
            variable: os.environ.get(variable)
            for variable in (
                "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION",
            )
        }
        self._vertex_default_adc = None
        self._vertex_gac_adc = None
        if (
            self._vertex_credentials is not None
            and not os.environ.get("VERTEXAI_CREDENTIALS")
            and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            # Non-SDK GAC uses Google's ADC factories, not LiteLLM's explicit
            # Vertex JSON factories. Reuse the captured loader without SDK
            # project discovery or later file/environment fallback.
            snapshot = {
                "source": "gac", "info": None, "error": None,
                "sdk_project": None, "sdk_project_error": None,
                "quota_project": os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT"),
                "gae_runtime": None, "aws_environment": dict(self._vertex_aws_environment),
            }
            try:
                snapshot["info"] = json.loads(self._vertex_credentials)
                if not isinstance(snapshot["info"], dict):
                    raise ValueError("Invalid captured Vertex ADC")
            except (TypeError, ValueError) as error:
                snapshot["error"] = type(error).__name__
            identity = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()
            snapshot["cache_key"] = json.dumps({"_pr_agent_default_adc": identity})
            self._vertex_gac_adc = snapshot
        if self._vertex_credentials is None and self._vertex_credentials_error is None:
            from google.auth import _cloud_sdk

            snapshot = {
                "info": None, "error": None, "sdk_project": None, "sdk_project_error": None,
                "explicit_path": None,
                "quota_project": os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT"),
                "gae_runtime": os.environ.get("APPENGINE_RUNTIME"),
                "aws_environment": dict(self._vertex_aws_environment),
            }
            try:
                path = _cloud_sdk.get_application_default_credentials_path()
                if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") == path:
                    snapshot["explicit_path"] = path
                try:
                    with open(path, encoding="utf-8") as adc_file:
                        snapshot["info"] = json.load(adc_file)
                except FileNotFoundError:
                    pass
                else:
                    if not isinstance(snapshot["info"], dict):
                        raise ValueError("Invalid default Vertex credentials")
                    try:
                        snapshot["sdk_project"] = _snapshot_cloud_sdk_project(os.path.dirname(path))
                    except (OSError, ValueError, configparser.Error) as error:
                        snapshot["sdk_project_error"] = type(error).__name__
            except (OSError, ValueError, configparser.Error) as error:
                snapshot["error"] = type(error).__name__
            identity = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()
            snapshot["cache_key"] = json.dumps({"_pr_agent_default_adc": identity})
            self._vertex_default_adc = snapshot
        if self._vertex_credentials:
            try:
                vertex_info = json.loads(self._vertex_credentials)
            except (ValueError, TypeError):
                vertex_info = None
            if isinstance(vertex_info, dict) and vertex_info.get("type") == "external_account":
                source = vertex_info.get("credential_source", {})
                environment_id = source.get("environment_id", "") if isinstance(source, dict) else ""
                if isinstance(environment_id, str) and "aws" in environment_id:
                    # Separate different AWS identities without growing LiteLLM's
                    # cache for every new handler with the same captured source.
                    vertex_info["_pr_agent_aws_identity"] = hashlib.sha256(
                        json.dumps(self._vertex_aws_environment, sort_keys=True).encode("utf-8"),
                    ).hexdigest()
                    self._vertex_credentials = json.dumps(vertex_info)
        self._provider_request_params = self._snapshot_provider_request_params(settings)
        self._snowflake_account_id = os.environ.get("SNOWFLAKE_ACCOUNT_ID", "")
        self._provider_environment_api_keys = self._snapshot_provider_environment_api_keys()
        self._volcengine_ark_api_key = os.environ.get("ARK_API_KEY")
        self._request_headers = self._snapshot_request_headers(settings)
        sdk_headers = {}
        for line in os.environ.get("OPENAI_CUSTOM_HEADERS", "").split("\n"):
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            sdk_headers[name.strip()] = value.strip()
        _check_sdk_marker_collision(sdk_headers)
        _check_sdk_marker_collision(self._request_headers)
        self._sdk_header_defaults = {
            "organization": os.environ.get("OPENAI_ORG_ID"),
            "project": os.environ.get("OPENAI_PROJECT_ID"),
            "custom_headers": sdk_headers,
        }
        openrouter_settings = settings.get("openrouter", {}) or {}
        # Credentials and endpoints are isolated in _provider_request_params; keep this snapshot control-only.
        self._openrouter_controls = {
            key: copy.deepcopy(openrouter_settings.get(key))
            for key in (
                "provider_only",
                "provider_order",
                "allow_fallbacks",
                "reasoning_effort",
                "reasoning_max_tokens",
                "max_tokens",
            )
        }
        self._default_reasoning_effort = getattr(settings.config, "reasoning_effort", None)
        self._claude_thinking_controls = {
            key: copy.deepcopy(settings.config.get(key, default))
            for key, default in (
                ("enable_claude_adaptive_thinking", False),
                ("enable_claude_extended_thinking", False),
                ("extended_thinking_budget_tokens", 2048),
                ("extended_thinking_max_output_tokens", 4096),
            )
        }
        self._bedrock_model_id = settings.get("litellm.model_id", None)
        self._custom_llm_provider = str(
            getattr(settings.litellm, "custom_llm_provider", "") or ""
        ).strip().lower()
        self._anthropic_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        self._request_provider_cache = {}

        if settings.get("LITELLM.DISABLE_AIOHTTP", False):
            litellm.disable_aiohttp_transport = True
        self._initialize_aws_request_credentials(settings)
        if settings.get("LITELLM.DROP_PARAMS", None):
            litellm.drop_params = settings.litellm.drop_params
        if settings.get("LITELLM.SUCCESS_CALLBACK", None):
            litellm.success_callback = settings.litellm.success_callback
        if settings.get("LITELLM.FAILURE_CALLBACK", None):
            litellm.failure_callback = settings.litellm.failure_callback
        if settings.get("LITELLM.SERVICE_CALLBACK", None):
            litellm.service_callback = settings.litellm.service_callback
        # litellm callbacks attach full prompt and response content unless message logging is disabled.
        if settings.get("LITELLM.TURN_OFF_MESSAGE_LOGGING", False):
            litellm.turn_off_message_logging = True
        # Keep LiteLLM request spans separate from pr-agent command spans when both OTEL layers are enabled.
        if self._litellm_otel_callback_enabled() and settings.get("OTEL.IS_ENABLED", False):
            os.environ.setdefault("USE_OTEL_LITELLM_REQUEST_SPAN", "true")
        repetition_penalty = get_repetition_penalty()
        if repetition_penalty is not None:
            self.repetition_penalty = repetition_penalty

        # Models that only use user message
        self.user_message_only_models = USER_MESSAGE_ONLY_MODELS

        # Model that doesn't support temperature argument
        self.no_support_temperature_models = NO_SUPPORT_TEMPERATURE_MODELS

        # Models that support reasoning effort
        self.support_reasoning_models = SUPPORT_REASONING_EFFORT_MODELS

        # Models that support extended thinking (config override replaces the built-in list when non-empty)
        override = get_settings().config.get("claude_extended_thinking_models_override", []) or []
        if override and not isinstance(override, list):
            get_logger().warning(
                "Invalid claude_extended_thinking_models_override in config; expected a list of model names. "
                "Falling back to the built-in Claude extended-thinking model list."
            )
            override = []
        elif override and not all(isinstance(model, str) and model.strip() for model in override):
            get_logger().warning(
                "Invalid claude_extended_thinking_models_override in config; "
                "expected a list of model name strings. "
                "Falling back to the built-in Claude extended-thinking model list."
            )
            override = []
        # Store stripped names so exact-match checks against the model succeed even when the config
        # entries contain surrounding whitespace (validation above already used model.strip()).
        self.claude_extended_thinking_models = (
            [model.strip() for model in override] if override else CLAUDE_EXTENDED_THINKING_MODELS
        )

        # Models that require streaming
        self.streaming_required_models = STREAMING_REQUIRED_MODELS
        self.force_streaming_provider = str(
            getattr(get_settings().litellm, "force_streaming_custom_llm_provider", "") or ""
        ).strip().lower()
        raw_force_streaming_api_base_substrings = getattr(
            get_settings().litellm, "force_streaming_api_base_substrings", []
        )
        if isinstance(raw_force_streaming_api_base_substrings, (list, tuple, set)):
            self.force_streaming_api_base_substrings = [
                str(value).strip().lower()
                for value in raw_force_streaming_api_base_substrings
                if value is not None and str(value).strip()
            ]
        else:
            if raw_force_streaming_api_base_substrings:
                get_logger().warning(
                    "LITELLM.FORCE_STREAMING_API_BASE_SUBSTRINGS must be a list, tuple, or set. "
                    "Ignoring invalid value."
                )
            self.force_streaming_api_base_substrings = []

    @staticmethod
    def _litellm_otel_callback_enabled() -> bool:
        """True when litellm's built-in OpenTelemetry callback is registered."""
        return any(
            "otel" in (getattr(litellm, name, None) or [])
            for name in ("callbacks", "success_callback", "failure_callback", "service_callback")
        )

    def _snapshot_provider_request_params(self, settings) -> dict:
        """Capture provider credentials and endpoints for this handler instance."""
        provider_params = {}
        for provider, setting_paths in PROVIDER_SETTING_PATHS.items():
            params = {
                parameter: settings.get(setting_path, None)
                for parameter, setting_path in setting_paths.items()
                if settings.get(setting_path, None)
            }
            if params:
                provider_params[provider] = params

        for provider, environment_variables in PROVIDER_API_BASE_ENV_VARS.items():
            if provider_params.get(provider, {}).get("api_base"):
                continue
            for environment_variable in environment_variables:
                api_base = os.environ.get(environment_variable)
                if api_base:
                    provider_params.setdefault(provider, {})["api_base"] = api_base
                    break

        if "api_base" not in provider_params.get("cloudflare", {}):
            cloudflare_account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
            if cloudflare_account_id:
                provider_params.setdefault("cloudflare", {})["api_base"] = (
                    f"https://api.cloudflare.com/client/v4/accounts/{cloudflare_account_id}/ai/v1"
                )

        for provider in ("watsonx", "watsonx_text"):
            for parameter, environment_variables in PROVIDER_ROUTING_ENV_VARS[provider].items():
                if provider_params.get(provider, {}).get(parameter):
                    continue
                for environment_variable in environment_variables:
                    value = os.environ.get(environment_variable)
                    if value:
                        provider_params.setdefault(provider, {})[parameter] = value
                        break

        aws_region = (
            os.environ.get("AWS_REGION_NAME")
            or settings.get("aws.AWS_REGION_NAME", None)
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        if aws_region:
            provider_params.setdefault("bedrock", {})["aws_region_name"] = aws_region
        mantle_aws_region = aws_region
        use_imds = os.environ.get("AWS_USE_IMDS", "").strip().lower() in ("1", "true", "yes")
        has_static_credentials = all(settings.get(f"aws.{name}", None) for name in (
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION_NAME",
        ))
        if not use_imds and not has_static_credentials:
            # Native Mantle ignores AWS_DEFAULT_REGION. Pin its default too, so
            # the shared AWS credential snapshot cannot change the request region.
            mantle_aws_region = (
                os.environ.get("AWS_REGION_NAME")
                or settings.get("aws.AWS_REGION_NAME", None)
                or os.environ.get("AWS_REGION")
                or "us-east-1"
            )
        bedrock_mantle_region = os.environ.get("BEDROCK_MANTLE_REGION") or mantle_aws_region
        bedrock_mantle_api_base = provider_params.get("bedrock_mantle", {}).get("api_base")
        if bedrock_mantle_api_base and MANTLE_HOST_RE is not None:
            match = MANTLE_HOST_RE.match(bedrock_mantle_api_base.rstrip("/"))
            if match:
                bedrock_mantle_region = match.group(1)
        if bedrock_mantle_region:
            provider_params.setdefault("bedrock_mantle", {})["aws_region_name"] = bedrock_mantle_region

        for provider in JSONProviderRegistry.list_providers():
            provider_config = JSONProviderRegistry.get(provider)
            if provider_config is None or provider_params.get(provider, {}).get("api_base"):
                continue
            api_base_env = getattr(provider_config, "api_base_env", None)
            if not api_base_env:
                continue
            api_base = os.environ.get(api_base_env)
            if api_base:
                provider_params.setdefault(provider, {})["api_base"] = api_base

        openai_params = {
            parameter: value
            for parameter, value in {
                "api_key": settings.get("OPENAI.KEY", None),
                "api_base": (
                    settings.get("OPENAI.API_BASE", None)
                    or os.environ.get("OPENAI_BASE_URL")
                    or os.environ.get("OPENAI_API_BASE")
                ),
                "api_version": settings.get("OPENAI.API_VERSION", None),
                "organization": settings.get("OPENAI.ORG", None) or os.environ.get("OPENAI_ORGANIZATION"),
            }.items()
            if value
        }
        if openai_params:
            provider_params["openai"] = openai_params

        bedrock_runtime_endpoint = (
            settings.get("aws.AWS_BEDROCK_RUNTIME_ENDPOINT", None)
            or os.environ.get("AWS_BEDROCK_RUNTIME_ENDPOINT")
        )
        if bedrock_runtime_endpoint:
            for provider in ("bedrock", "bedrock_mantle"):
                provider_params.setdefault(provider, {})[
                    "aws_bedrock_runtime_endpoint"
                ] = bedrock_runtime_endpoint

        configured_api_base = settings.get("OPENAI.API_BASE", None)
        configured_api_version = settings.get("OPENAI.API_VERSION", None)
        azure_api_base = os.environ.get("AZURE_API_BASE")
        azure_api_version = os.environ.get("AZURE_API_VERSION")
        azure_ad_api_base = settings.get("AZURE_AD.API_BASE", None) if self._azure_ad else None
        if azure_ad_api_base:
            request_api_base = azure_ad_api_base
            request_api_version = configured_api_version or azure_api_version
        elif self._openai_api_base_is_azure:
            if configured_api_base:
                request_api_base = configured_api_base
                request_api_version = configured_api_version or azure_api_version
            else:
                request_api_base = azure_api_base
                request_api_version = azure_api_version or configured_api_version
        elif azure_api_base:
            request_api_base = azure_api_base
            request_api_version = azure_api_version or configured_api_version
        else:
            request_api_base = configured_api_base
            request_api_version = configured_api_version or azure_api_version

        # The SDK endpoint alias is a fallback, not an override of configured routing.
        request_api_base = request_api_base or os.environ.get("AZURE_OPENAI_ENDPOINT")

        # Cloudflare's key branch lets the SDK read this alias independently.
        self._azure_sdk_ad_token = os.environ.get("AZURE_OPENAI_AD_TOKEN")
        azure_params = {
            parameter: value
            for parameter, value in {
                "api_key": (
                    settings.get("OPENAI.KEY", None)
                    if settings.get("OPENAI.API_TYPE", None) == "azure"
                    else None
                ),
                "api_base": request_api_base,
                "api_version": request_api_version,
                "azure_ad_token": (
                    os.environ.get("AZURE_AD_TOKEN") or self._azure_sdk_ad_token
                ),
            }.items()
            if value
        }
        if azure_params:
            provider_params["azure"] = {key: value for key, value in azure_params.items() if value}

        vertex_params = {
            parameter: value
            for parameter, value in {
                "vertex_project": (
                    settings.get("VERTEXAI.VERTEX_PROJECT", None)
                    or _vertex_project_from_environment()
                ),
                "vertex_location": (
                    settings.get("VERTEXAI.VERTEX_LOCATION", None)
                    or os.environ.get("VERTEXAI_LOCATION")
                    or os.environ.get("VERTEX_LOCATION")
                ),
            }.items()
            if value
        }
        vertex_credentials = self._vertex_credentials
        if self._vertex_gac_adc is not None:
            vertex_params["vertex_credentials"] = self._vertex_gac_adc["cache_key"]
        elif vertex_credentials:
            vertex_params["vertex_credentials"] = vertex_credentials
        elif self._vertex_default_adc is not None:
            vertex_params["vertex_credentials"] = self._vertex_default_adc["cache_key"]
        if vertex_params:
            provider_params.setdefault("vertex_ai", {}).update(vertex_params)

        if settings.get("OPENROUTER.KEY", None) or any(
            os.environ.get(environment_variable)
            for environment_variable in PROVIDER_API_KEY_ENV_VARS["openrouter"]
        ):
            openrouter_api_base = (
                settings.get("OPENROUTER.API_BASE", None)
                or os.environ.get("OPENROUTER_API_BASE")
                or "https://openrouter.ai/api/v1"
            )
            provider_params.setdefault("openrouter", {}).setdefault("api_base", openrouter_api_base)
        return provider_params

    @staticmethod
    def _snapshot_vertex_credentials() -> tuple[str | None, str | None]:
        """Capture explicit Vertex credentials before another request can change their environment."""
        vertex_credentials = os.environ.get("VERTEXAI_CREDENTIALS")
        if vertex_credentials and not os.path.isfile(vertex_credentials):
            try:
                parsed_credentials = json.loads(vertex_credentials)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                if isinstance(parsed_credentials, dict):
                    return vertex_credentials, None
                return None, "ValueError"

        credentials_path = vertex_credentials or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not credentials_path:
            return None, None
        if not vertex_credentials:
            from google.auth import _cloud_sdk

            # Match Google Auth's exact-path exception so the captured SDK
            # resource project remains distinct from the billing quota project.
            if credentials_path == _cloud_sdk.get_application_default_credentials_path():
                return None, None
        try:
            with open(credentials_path, encoding="utf-8") as credentials_file:
                return credentials_file.read(), None
        except OSError as e:
            return None, type(e).__name__

    @staticmethod
    def _snapshot_provider_environment_api_keys() -> dict:
        """Capture native provider API keys without mixing them with configured credentials."""
        provider_api_keys = {}
        for provider, environment_variables in PROVIDER_API_KEY_ENV_VARS.items():
            for environment_variable in environment_variables:
                api_key = os.environ.get(environment_variable)
                if api_key:
                    provider_api_keys[provider] = api_key
                    break
        for provider in JSONProviderRegistry.list_providers():
            provider_config = JSONProviderRegistry.get(provider)
            if provider_config is None or provider in provider_api_keys:
                continue
            api_key_env = getattr(provider_config, "api_key_env", None)
            if not api_key_env:
                continue
            api_key = os.environ.get(api_key_env)
            if api_key:
                provider_api_keys[provider] = api_key
        return provider_api_keys

    @staticmethod
    def _snapshot_request_headers(settings) -> dict:
        """Capture explicitly configured headers for every request from this handler."""
        raw_headers = settings.get("LITELLM.EXTRA_HEADERS", None)
        if not raw_headers:
            return {}
        try:
            request_headers = json.loads(raw_headers)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"LITELLM.EXTRA_HEADERS contains invalid JSON: {str(e)}") from e
        if not isinstance(request_headers, dict):
            raise ValueError("LITELLM.EXTRA_HEADERS must be a JSON object")
        return request_headers

    def _initialize_aws_request_credentials(self, settings) -> None:
        """Capture request-local credentials and synchronously resolve the opted-in AWS provider chain."""
        use_imds = os.environ.get("AWS_USE_IMDS", "").strip().lower() in ("1", "true", "yes")
        self._aws_use_imds = use_imds
        self._aws_credential_chain_environment = {
            variable: os.environ.get(variable)
            for variable in AWS_CREDENTIAL_CHAIN_ENV_VARS
        }
        request_region = (
            os.environ.get("AWS_REGION_NAME")
            or settings.get("aws.AWS_REGION_NAME", None)
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        ambient_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
        ambient_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
        if bool(ambient_access_key) != bool(ambient_secret_key):
            self._aws_environment_credentials_incomplete = True
        elif ambient_access_key and ambient_secret_key:
            self._aws_environment_creds = {
                "aws_access_key_id": ambient_access_key,
                "aws_secret_access_key": ambient_secret_key,
                # Prevent LiteLLM from falling back to a later ambient STS token.
                # Only the opted-in boto3 chain recognizes the SECURITY alias;
                # native LiteLLM's explicit keypair path uses SESSION alone.
                "aws_session_token": (
                    (os.environ.get("AWS_SECURITY_TOKEN") or os.environ.get("AWS_SESSION_TOKEN"))
                    if use_imds else os.environ.get("AWS_SESSION_TOKEN")
                ) or "",
            }
            if request_region:
                self._aws_environment_creds["aws_region_name"] = request_region

        static_access_key = settings.get("aws.AWS_ACCESS_KEY_ID", None)
        if static_access_key:
            static_secret_key = settings.get("aws.AWS_SECRET_ACCESS_KEY", None)
            static_region = settings.get("aws.AWS_REGION_NAME", None)
            if not (static_secret_key and static_region):
                if not use_imds:
                    raise ValueError("AWS credentials are incomplete")
                get_logger().warning(
                    "AWS_USE_IMDS is set but configured static AWS credentials are incomplete; "
                    "no static fallback is available"
                )
            if static_secret_key and static_region:
                self._aws_static_creds = {
                    "aws_access_key_id": static_access_key,
                    "aws_secret_access_key": static_secret_key,
                    # LiteLLM falls back to AWS_SESSION_TOKEN only when this value is None.
                    # An empty token keeps long-lived static keys isolated from ambient STS credentials.
                    "aws_session_token": settings.get("aws.AWS_SESSION_TOKEN", None) or "",
                    "aws_region_name": static_region,
                }

        if not use_imds:
            self._aws_active_creds = dict(self._aws_static_creds or self._aws_environment_creds or {})
            return

        if not (ambient_access_key or ambient_secret_key):
            self._aws_credential_chain_files = self._snapshot_aws_credential_chain_files()
        self._aws_region_name = request_region
        self._initialize_aws_imds_credentials()

    def _initialize_aws_imds_credentials(self) -> bool:
        """Resolve ambient AWS credentials during handler initialization without changing process credentials."""
        import boto3
        import botocore.exceptions

        self._validate_aws_credential_chain_environment()
        region = self._aws_region_name
        if self._aws_environment_credentials_incomplete:
            if not self._aws_static_creds:
                raise ValueError("AWS environment credentials are incomplete")
            self._aws_active_creds = dict(self._aws_static_creds)
            self._aws_imds_fell_back = True
            get_logger().warning(
                "AWS_USE_IMDS: ambient credentials are incomplete; using static credentials"
            )
            return False
        try:
            session_kwargs = {}
            if self._aws_environment_creds:
                session_kwargs = {
                    "aws_access_key_id": self._aws_environment_creds["aws_access_key_id"],
                    "aws_secret_access_key": self._aws_environment_creds["aws_secret_access_key"],
                }
                if self._aws_environment_creds.get("aws_session_token"):
                    session_kwargs["aws_session_token"] = self._aws_environment_creds["aws_session_token"]
                if region:
                    session_kwargs["region_name"] = region
            elif os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_SECRET_ACCESS_KEY"):
                raise ValueError("Refusing live AWS credential environment fallback")
            session = boto3.Session(**session_kwargs)
            if not self._aws_environment_creds:
                self._bind_aws_workload_token_sources(session)
            if not self._aws_environment_creds and self._aws_profile_uses_credential_process(session):
                if not self._aws_static_creds:
                    raise ValueError("AWS credential_process is incompatible with request isolation")
                self._aws_active_creds = dict(self._aws_static_creds)
                self._aws_imds_fell_back = True
                get_logger().warning(
                    "AWS_USE_IMDS: credential_process is incompatible with request isolation; "
                    "using static credentials"
                )
                return False
            if not region:
                try:
                    region = session.region_name
                except Exception as e:
                    get_logger().warning(f"AWS_USE_IMDS: failed to resolve region via boto3: {e}")
            creds = session.get_credentials()
            if creds:
                frozen_credentials = creds.get_frozen_credentials()
                self._validate_aws_credential_chain_environment()
                self._aws_boto3_creds = creds
                self._aws_active_creds = self._aws_request_params_from_frozen(frozen_credentials, region)
                self._aws_imds_mode = True
                get_logger().info("Using ambient AWS credentials from IMDS/task-role/IRSA")
            else:
                get_logger().warning(
                    "AWS_USE_IMDS is set but boto3 found no credentials; falling through to static keys"
                )
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError, OSError):
            get_logger().exception(
                "AWS_USE_IMDS: failed to resolve credentials via boto3; falling through to static keys"
            )

        if not region:
            get_logger().warning("AWS_USE_IMDS: could not determine AWS region; set AWS_REGION_NAME explicitly")
        if not self._aws_imds_mode and self._aws_static_creds:
            self._aws_active_creds = dict(self._aws_static_creds)
            self._aws_imds_fell_back = True
            get_logger().info("AWS_USE_IMDS: IMDS resolution failed; using static credentials")
        return self._aws_imds_mode

    def _bind_aws_workload_token_sources(self, session) -> None:
        """Bind token selectors without freezing deployment-owned token rotation."""
        from botocore.credentials import AssumeRoleWithWebIdentityProvider, ContainerProvider

        environment = {
            key: value for key, value in self._aws_credential_chain_environment.items() if value is not None
        }
        resolver = session._session.get_component("credential_provider")
        # Adapt only this session's botocore providers. Its EcsContainer role
        # sourcer shares the container provider; nested web identity profiles
        # already disable environment selection in botocore 1.43.x.
        for provider in resolver.providers:
            if isinstance(provider, ContainerProvider):
                provider._environ = dict(environment)
            elif isinstance(provider, AssumeRoleWithWebIdentityProvider) and not provider._disable_env_vars:
                values = {
                    key: environment[variable]
                    for key, variable in provider._CONFIG_TO_ENV_VAR.items() if variable in environment
                }

                def get_env_config(key, values=values):
                    return values.get(key)

                provider._get_env_config = get_env_config

    @staticmethod
    def _aws_profile_uses_credential_process(session) -> bool:
        """Return whether the selected boto3 profile chain executes a credential process."""
        profile_name = getattr(session, "profile_name", None)
        botocore_session = getattr(session, "_session", None)
        full_config = getattr(botocore_session, "full_config", None)
        if not isinstance(profile_name, str) or not isinstance(full_config, dict):
            return False

        profiles = full_config.get("profiles", {})
        visited_profiles = set()
        while profile_name and profile_name not in visited_profiles:
            visited_profiles.add(profile_name)
            profile = profiles.get(profile_name, {})
            if not isinstance(profile, dict):
                return False
            if profile.get("credential_process"):
                return True
            profile_name = profile.get("source_profile")
        return False

    @staticmethod
    def _aws_request_params_from_frozen(frozen, region) -> dict:
        """Convert a botocore credential snapshot to LiteLLM request parameters."""
        params = {
            "aws_access_key_id": frozen.access_key,
            "aws_secret_access_key": frozen.secret_key,
            "aws_session_token": frozen.token or "",
        }
        if region:
            params["aws_region_name"] = region
        return params

    def _refresh_aws_imds_credentials(self) -> bool:
        """Refresh ambient AWS credentials from boto3 provider chain. Called before each Bedrock call
        to avoid serving stale credentials from long-lived processes (EC2 roles rotate every ~6h).

        Uses the credentials object stored during initial ambient resolution rather than creating a new boto3.Session.

        Returns True on success, False on failure (caller should trigger static fallback)."""
        import botocore.exceptions
        try:
            if self._aws_boto3_creds is None:
                get_logger().warning("IMDS credential refresh: no boto3 credentials object stored")
                return False
            self._validate_aws_credential_chain_environment()
            region = self._aws_active_creds.get("aws_region_name")
            frozen_credentials = self._aws_boto3_creds.get_frozen_credentials()
            self._validate_aws_credential_chain_environment()
            self._aws_active_creds = self._aws_request_params_from_frozen(frozen_credentials, region)
            return True
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError, OSError):
            # ClientError (STS/AssumeRole failures) is not a BotoCoreError subclass.
            get_logger().exception("IMDS credential refresh failed")
            return False

    def _activate_static_aws_fallback(self):
        """Select static request credentials for an AWS provider fallback after IMDS failure."""
        self._aws_active_creds = dict(self._aws_static_creds)
        self._aws_imds_fell_back = True
        get_logger().warning("AWS provider call failed with ambient credentials; retrying with static credentials")

    def _validate_aws_credential_chain_environment(self) -> None:
        """Reject credential-chain selectors changed after this handler was initialized."""
        if any(
            os.environ.get(variable) != value
            for variable, value in self._aws_credential_chain_environment.items()
        ):
            raise ValueError("Refusing changed AWS credential-chain environment")
        if (
            self._aws_credential_chain_files
            and self._snapshot_aws_credential_chain_files() != self._aws_credential_chain_files
        ):
            raise ValueError("Refusing changed AWS credential-chain file")

    def _validate_aws_request_endpoint_environment(self) -> None:
        """Reject AWS request endpoints changed after this handler was initialized."""
        if any(
            os.environ.get(variable) != self._aws_credential_chain_environment.get(variable)
            for variable in AWS_REQUEST_ENDPOINT_ENV_VARS
        ):
            raise ValueError("Refusing changed AWS request endpoint environment")

    @staticmethod
    def _original_ec2_credential_file_path() -> str | None:
        """Return the path that botocore's OriginalEC2Provider would read."""
        if original_ec2_credentials := os.environ.get("AWS_CREDENTIAL_FILE"):
            return os.path.abspath(os.path.expanduser(original_ec2_credentials))
        return None

    @classmethod
    def _aws_credential_chain_file_paths(cls) -> tuple[str, ...]:
        """Return credential files that boto3 may read for this handler."""
        home = os.path.expanduser("~")
        paths = []
        for variable, default_path in (
            ("AWS_SHARED_CREDENTIALS_FILE", os.path.join(home, ".aws", "credentials")),
            ("AWS_CONFIG_FILE", os.path.join(home, ".aws", "config")),
        ):
            configured_path = os.environ.get(variable)
            if configured_path is None:
                paths.append(default_path)
            elif configured_path:
                paths.append(configured_path)
        boto_config = os.environ.get("BOTO_CONFIG")
        if boto_config is None:
            paths.extend(("/etc/boto.cfg", os.path.join(home, ".boto")))
        elif boto_config:
            paths.append(boto_config)
        normalized_paths = [
            os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
            for path in paths
        ]
        if original_ec2_credentials := cls._original_ec2_credential_file_path():
            normalized_paths.append(original_ec2_credentials)
        return tuple(dict.fromkeys(normalized_paths))

    @staticmethod
    def _fingerprint_aws_credential_chain_file(path: str) -> tuple:
        """Return a stable fingerprint without retaining credential file contents."""
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                return "nonfile",
            with open(path, "rb") as credential_file:
                return "file", hashlib.file_digest(credential_file, "sha256").hexdigest()
        except FileNotFoundError:
            return "missing",
        except OSError as error:
            return "error", type(error).__name__, error.errno

    @classmethod
    def _snapshot_aws_credential_chain_files(cls) -> dict:
        """Capture fingerprints for boto3 credential-chain files."""
        fingerprints = {
            path: cls._fingerprint_aws_credential_chain_file(path)
            for path in cls._aws_credential_chain_file_paths()
        }
        original_ec2_credentials = cls._original_ec2_credential_file_path()
        if original_ec2_credentials and fingerprints.get(original_ec2_credentials) == ("nonfile",):
            raise ValueError("AWS_CREDENTIAL_FILE must reference a regular file")
        return fingerprints

    @contextlib.asynccontextmanager
    async def _snapshot_aws_request_credentials(self, enabled):
        """Refresh synchronously and serialize this handler's AWS call and static fallback."""
        if not enabled:
            yield dict(self._aws_active_creds), False
            return
        async with self._aws_bedrock_lock:
            if not self._aws_imds_fell_back:
                self._validate_aws_credential_chain_environment()
                if self._aws_imds_mode and not self._refresh_aws_imds_credentials() and self._aws_static_creds:
                    self._activate_static_aws_fallback()
            can_fallback = self._aws_imds_mode and not self._aws_imds_fell_back and bool(self._aws_static_creds)
            yield dict(self._aws_active_creds), can_fallback

    def _should_use_aws_imds(self, provider: str | None) -> bool:
        """Return whether this request needs SigV4 credentials from the ambient AWS chain."""
        if not getattr(self, "_aws_use_imds", False) or provider not in AWS_REQUEST_PROVIDERS:
            return False
        if provider not in ("bedrock", "bedrock_mantle"):
            return True
        provider_params = getattr(self, "_provider_request_params", {}).get(provider, {})
        provider_environment_api_keys = getattr(self, "_provider_environment_api_keys", {})
        return not (provider_params.get("api_key") or provider_environment_api_keys.get(provider))

    def _resolve_request_provider(self, model: str) -> str | None:
        """Resolve the LiteLLM provider after PR-Agent has normalized the model name."""
        if not isinstance(model, str) or not model:
            return None
        provider_cache = getattr(self, "_request_provider_cache", None)
        if provider_cache is None:
            provider_cache = self._request_provider_cache = {}
        if model in provider_cache:
            return provider_cache[model]
        transport_provider = None
        if "/" in model:
            transport_provider = model.split("/", 1)[0]
            provider = PROVIDER_SETTING_ALIASES.get(transport_provider, transport_provider)
            provider_params = getattr(self, "_provider_request_params", {})
            if (
                provider in provider_params
                or provider in PROVIDER_API_KEY_ENV_VARS
                or provider in AWS_REQUEST_PROVIDERS
                or provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS
                or _is_openai_compatible_request_provider(provider)
                or provider in getattr(litellm, "provider_list", ())
                or provider in ("azure", "databricks", "openai", "vertex_ai")
            ):
                resolved_provider = provider
            else:
                resolved_provider = None
        else:
            try:
                _, transport_provider, _, _ = litellm.get_llm_provider(model=model)
                resolved_provider = PROVIDER_SETTING_ALIASES.get(transport_provider, transport_provider)
            except litellm.BadRequestError:
                if model.startswith("claude"):
                    resolved_provider = "anthropic"
                elif model.startswith("command"):
                    resolved_provider = "cohere_chat"
                else:
                    resolved_provider = "openai"
                transport_provider = resolved_provider
        provider_cache[model] = resolved_provider
        transport_provider_cache = getattr(self, "_request_transport_provider_cache", None)
        if transport_provider_cache is None:
            transport_provider_cache = self._request_transport_provider_cache = {}
        transport_provider_cache[model] = transport_provider
        return resolved_provider

    def _resolve_request_transport_provider(self, model: str) -> str | None:
        """Resolve the unaliased provider LiteLLM uses to select a transport."""
        self._resolve_request_provider(model)
        return getattr(self, "_request_transport_provider_cache", {}).get(model)

    def _route_model(self, model: str, deployment_id: str | None) -> str:
        """Apply provider routing shared by regular calls and health probes."""
        if model.startswith("azure_text/") and deployment_id:
            return f"azure_text/{deployment_id}"
        if self.azure:
            if "/" not in model:
                return "azure/" + model
            provider, model_name = model.split("/", 1)
            if provider == "azure_text" or provider == "openai" or PROVIDER_SETTING_ALIASES.get(provider) == "openai":
                azure_provider = "azure_text" if provider in ("text-completion-openai", "azure_text") else "azure"
                if azure_provider == "azure_text" and deployment_id:
                    model_name = deployment_id
                return f"{azure_provider}/{model_name}"
        return model

    def _route_model_for_request(
        self,
        model: str,
        custom_llm_provider: str,
        deployment_id: str | None,
    ) -> str:
        """Apply automatic routing while preserving explicit custom-provider model IDs."""
        if not custom_llm_provider:
            model = self._route_model(model, deployment_id)
        elif deployment_id and (custom_llm_provider == "azure_text" or model.startswith("azure_text/")):
            model = f"azure_text/{deployment_id}"
        return normalize_litellm_model(model, custom_llm_provider)

    @staticmethod
    def _canonical_openrouter_model(model: str, provider: str | None) -> str | None:
        """Return an OpenRouter-prefixed model for request-control matching."""
        if provider != "openrouter" or not isinstance(model, str):
            return None
        return model if model.startswith("openrouter/") else f"openrouter/{model}"

    @staticmethod
    def _is_gpt6_astra_model(model: str) -> bool:
        """Recognize native Astra models without changing gateway model IDs."""
        while model.startswith(("openai/", "azure/")):
            model = model.removeprefix("openai/").removeprefix("azure/")
        return model.removesuffix("_thinking") == "gpt-6-astra"

    @staticmethod
    def _is_gpt5_model(model: str) -> bool:
        """Return whether a routed model belongs to the GPT-5 family."""
        model_base = model.removeprefix("openrouter/")
        while model_base.startswith(("openai/", "azure/")):
            model_base = model_base.removeprefix("openai/").removeprefix("azure/")
        return model_base.startswith("gpt-5")

    def _normalize_gpt5_model_for_request(self, model: str, user_model: str, custom_llm_provider: str) -> str:
        """Normalize GPT-5/Astra suffixes and prefixes before request parameters are selected."""
        model_base = model
        while model_base.startswith(("openai/", "azure/")):
            model_base = model_base.removeprefix("openai/").removeprefix("azure/")
        if not model_base.startswith("gpt-5") and model_base.removesuffix("_thinking") != "gpt-6-astra":
            return model
        if custom_llm_provider:
            return model.replace("_thinking", "")
        if self.azure or user_model.startswith("azure/"):
            provider_prefix = "azure/"
        else:
            provider_prefix = "openai/"
        return provider_prefix + model_base.replace("_thinking", "")

    def _uses_captured_azure_companion_auth(self, provider):
        if provider not in ("azure", "azure_ai") or not getattr(self, "_azure_companion_auth", False):
            return False
        if provider == "azure_ai":
            # This bridge covers initially tokenless raw companion selection,
            # not another Azure key or AD-token choice in the common validator.
            snapshot = self._raw_guard_auth_snapshot
            return not (snapshot["azure_key"] or snapshot["azure_ad_token"])
        return True

    def _get_provider_request_params(
        self,
        model: str,
        azure_ad_token=None,
        provider=None,
        transport_provider=None,
        transport_model=None,
        aws_request_credentials=None,
    ) -> dict:
        """Return only the credentials and routing parameters for this model's provider."""
        provider = provider or self._resolve_request_provider(model)
        if provider in AWS_REQUEST_PROVIDERS:
            self._validate_aws_request_endpoint_environment()
        if transport_provider is None:
            transport_provider = self._resolve_request_transport_provider(model) or provider
        provider_params = getattr(self, "_provider_request_params", {})
        provider_environment_api_keys = getattr(self, "_provider_environment_api_keys", {})
        openai_api_base_is_azure = getattr(self, "_openai_api_base_is_azure", getattr(self, "azure", False))
        if provider in OPENAI_COMPATIBLE_REQUEST_PROVIDERS:
            openai_params = {} if openai_api_base_is_azure else provider_params.get("openai", {})
            params = {
                key: openai_params[key]
                for key in ("api_key", "api_base")
                if openai_params.get(key)
            }
            native_api_key = provider_environment_api_keys.get(provider)
            if provider == "openai_like" and provider_params.get(provider, {}).get("api_base"):
                params["api_base"] = provider_params[provider]["api_base"]
                if params["api_base"] != openai_params.get("api_base"):
                    params.pop("api_key", None)
            if provider == "openai_like" and native_api_key:
                params["api_key"] = native_api_key
            elif "api_key" not in params and native_api_key:
                params["api_key"] = native_api_key
            if provider == "openai_like" and "api_key" not in params and (
                openai_params.get("api_key")
                or getattr(litellm, "api_key", None)
                or getattr(litellm, "openai_key", None)
                or _has_provider_api_key_global(provider)
                or provider_environment_api_keys.get("openai")
                or _has_live_provider_api_key_environment(provider)
                or os.environ.get("OPENAI_API_KEY")
            ):
                params["api_key"] = DUMMY_LITELLM_API_KEY
            if provider == "custom_openai" and "api_key" not in params:
                params["api_key"] = DUMMY_LITELLM_API_KEY
            if provider == "custom_openai" and "api_base" not in params:
                params["api_base"] = OPENAI_DEFAULT_API_BASE
            request_headers = _request_local_openai_headers(transport_provider, model=transport_model or model)
            if request_headers is not None:
                params["headers"] = request_headers
            return self._finalize_provider_request_params(provider, params)
        if provider is None:
            # OPENAI.API_BASE also configures gateways such as MOSAICO whose model
            # names can retain another provider prefix. Forward only the matching
            # request-local key to that explicitly configured endpoint.
            openai_params = (
                {}
                if openai_api_base_is_azure
                else getattr(self, "_provider_request_params", {}).get("openai", {})
            )
            api_base = openai_params.get("api_base")
            params = {"api_base": api_base} if api_base else {}
            request_api_key = openai_params.get("api_key") or provider_environment_api_keys.get("openai")
            if api_base:
                params["api_key"] = request_api_key or DUMMY_LITELLM_API_KEY
            elif (
                getattr(litellm, "api_key", None)
                or getattr(litellm, "openai_key", None)
                or os.environ.get("OPENAI_API_KEY")
                or getattr(openai, "api_key", None)
            ):
                params["api_key"] = DUMMY_LITELLM_API_KEY
            return self._finalize_provider_request_params(provider, params)
        if provider in MANAGED_AUTH_REQUEST_PROVIDERS:
            params = dict(provider_params.get(provider, {}))
            params["api_key"] = DUMMY_LITELLM_API_KEY
            request_headers = _request_local_openai_headers(transport_provider, model=transport_model or model)
            if request_headers is not None:
                params["headers"] = request_headers
            return self._finalize_provider_request_params(provider, params)
        params = dict(provider_params.get(provider, {}))
        if provider == "gdc":
            api_base = params.get("api_base")
            if not api_base:
                raise ValueError("GDC API base was not resolved for this request; set GDC_API_BASE")
            if "/v1/projects/" not in api_base:
                from litellm.llms.gdc.chat.transformation import GDCGeminiConfig

                routing = {
                    name: provider_params.get("vertex_ai", {}).get(name)
                    for name in ("vertex_project", "vertex_location")
                }
                if not all(routing.values()):
                    raise ValueError("GDC host routing requires captured Vertex project and location")
                # Native dispatch places routing kwargs below process globals.
                # Resolve the full URL first, using the native highest-priority
                # inputs, so authentication and quota headers share this route.
                params["api_base"] = GDCGeminiConfig().get_complete_url(
                    api_base=api_base, api_key=params.get("api_key"), model=transport_model or model,
                    optional_params={}, litellm_params=routing,
                )
        if provider == "vertex_ai":
            if self._vertex_credentials_error:
                raise ValueError(
                    f"Unable to snapshot explicit Vertex credentials: {self._vertex_credentials_error}"
                )
            if self._vertex_credentials is None:
                captured_path = (self._vertex_default_adc or {}).get("explicit_path")
                live_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                if os.environ.get("VERTEXAI_CREDENTIALS") or (live_path and live_path != captured_path):
                    raise ValueError("Refusing live Vertex credential environment fallback")
        if provider == "openrouter" and not params and not provider_environment_api_keys.get("openrouter"):
            openai_params = {} if openai_api_base_is_azure else provider_params.get("openai", {})
            gateway_api_base = openai_params.get("api_base")
            gateway_api_key = openai_params.get("api_key") or provider_environment_api_keys.get("openai")
            if gateway_api_base and gateway_api_key:
                params.update(api_base=gateway_api_base, api_key=gateway_api_key)
        if provider == "openrouter" and "api_base" not in params and (
            params.get("api_key")
            or provider_environment_api_keys.get("openrouter")
            or _has_live_provider_api_key_environment("openrouter")
        ):
            params["api_base"] = "https://openrouter.ai/api/v1"
        volcengine_responses = provider == "volcengine" and _uses_openai_responses_transport(
            transport_model or model, transport_provider,
        )
        if volcengine_responses:
            # Native provider resolution prefers VOLCENGINE_API_KEY; only the
            # Responses transport falls back to ARK_API_KEY afterward.
            if "api_key" not in params:
                api_key = provider_environment_api_keys.get(provider) or getattr(self, "_volcengine_ark_api_key", None)
                if api_key:
                    params["api_key"] = api_key
        if (
            _is_openai_compatible_request_provider(provider)
            and not params
            and not provider_environment_api_keys.get(provider)
        ):
            # Use OPENAI.API_BASE only when this provider has no native request
            # identity. This prevents sending provider credentials to a gateway.
            api_base = None if openai_api_base_is_azure else provider_params.get("openai", {}).get("api_base")
            if api_base:
                params["api_base"] = api_base
                params["api_key"] = (
                    provider_params.get("openai", {}).get("api_key")
                    or provider_environment_api_keys.get("openai")
                    or DUMMY_LITELLM_API_KEY
                )
        if volcengine_responses and "api_key" not in params and os.environ.get("ARK_API_KEY"):
            raise ValueError("Refusing Volcengine ARK API key added after handler initialization")
        if provider in ("watsonx", "watsonx_text") and "api_key" not in params and (
            params.get("token") or params.get("zen_api_key")
        ):
            # LiteLLM resolves a Watsonx API key before applying explicit token
            # or Zen authentication. Block that ambient fallback request-locally.
            params["api_key"] = DUMMY_LITELLM_API_KEY
        if "api_key" not in params:
            api_key = provider_environment_api_keys.get(provider)
            if api_key:
                params["api_key"] = api_key
        if provider == "snowflake":
            # Require captured identity before the generic placeholder guard;
            # native Snowflake cannot authenticate a keyless request.
            if not params.get("api_key"):
                raise ValueError("Snowflake JWT was not resolved for this request; set SNOWFLAKE_JWT")
            if not params.get("api_base"):
                account_id = getattr(self, "_snowflake_account_id", "")
                if not account_id:
                    raise ValueError("Snowflake account was not resolved for this request; set SNOWFLAKE_ACCOUNT_ID")
                params["account_id"] = account_id
        if provider in ("hosted_vllm", "lm_studio") and params.get("api_base") and "api_key" not in params:
            params["api_key"] = DUMMY_LITELLM_API_KEY
        # Unlike Mantle, classic Bedrock never consumes litellm.api_key.
        if provider == "bedrock" and "api_key" not in params and _has_live_provider_api_key_environment(provider):
            raise ValueError("Refusing process-wide Bedrock bearer token fallback")
        if provider in ("sagemaker_chat", "sagemaker_nova") and os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            # LiteLLM 1.100.0's SageMaker signer ignores its api_key argument and
            # otherwise reads this Bedrock-only token directly from the environment.
            raise ValueError("Refusing Bedrock bearer token fallback for SageMaker")
        if provider == "azure" and getattr(self, "_azure_ad", False):
            if azure_ad_token is None:
                raise ValueError("Azure AD token was not resolved for this request")
            params.pop("api_key", None)
            params["azure_ad_token"] = azure_ad_token
        if provider == "azure" and "azure_ad_token" not in params and any(
            os.environ.get(environment_variable) for environment_variable in AZURE_AD_TOKEN_ENV_VARS
        ):
            raise ValueError("Refusing Azure AD token added after handler initialization")
        if provider == "anthropic" and "api_key" not in params:
            # LiteLLM has no completion parameter for ANTHROPIC_AUTH_TOKEN. The
            # placeholder blocks ambient API keys until the request-local bridge
            # restores Anthropic's keyless state and supplies the bearer token.
            params["api_key"] = DUMMY_LITELLM_API_KEY
        requires_api_key_guard = provider == "bedrock_mantle" or (
            provider not in AWS_REQUEST_PROVIDERS
            and provider != "vertex_ai"
            and (_uses_provider_api_key(provider) or provider in getattr(litellm, "provider_list", ()))
        )
        companion_auth = (
            self._uses_captured_azure_companion_auth(provider)
            and not _is_cloudflare_gateway(params.get("api_base"))
        )
        # Token- or companion-based Azure requests must block even a key that appears only
        # after native dispatch starts, not just an already visible fallback.
        if "api_key" not in params and requires_api_key_guard and (
            (provider == "azure" and params.get("azure_ad_token"))
            or companion_auth
            or getattr(litellm, "api_key", None)
            or _has_provider_api_key_global(provider)
            or (
                _is_openai_compatible_request_provider(provider)
                and (getattr(litellm, "openai_key", None) or provider_environment_api_keys.get("openai"))
            )
            or _has_live_provider_api_key_environment(provider)
        ):
            params["api_key"] = DUMMY_LITELLM_API_KEY
            params["_raw_api_key_guard"] = True
            if provider == "azure" and str(params.get("azure_ad_token") or "").startswith("oidc/"):
                params["_azure_oidc_guard"] = True
            elif provider == "azure" and (params.get("azure_ad_token") or companion_auth):
                params["_azure_ad_guard"] = True
        if provider == "openai" and "api_key" not in params:
            # Do not trust process-wide LiteLLM globals here: another request or
            # embedding may have populated them with a different tenant's key.
            params["api_key"] = DUMMY_LITELLM_API_KEY
        if provider == "openai" and "api_base" not in params:
            params["api_base"] = OPENAI_DEFAULT_API_BASE
        if provider == "gemini":
            # Freeze absence before LiteLLM moves dispatch to an executor, where
            # a later key/base would otherwise replace the native default route.
            params.setdefault("api_key", DUMMY_LITELLM_API_KEY)
            if "api_base" not in params:
                from litellm.llms.vertex_ai.common_utils import _get_gemini_url

                native_model = (transport_model or model).removeprefix("gemini/")
                url, endpoint = _get_gemini_url(mode="chat", model=native_model, stream=False)
                suffix = f"/models/{native_model}:{endpoint}"
                if not url.endswith(suffix) or len(url) == len(suffix):
                    raise RuntimeError("LiteLLM's Gemini default URL is incompatible with request isolation")
                params["api_base"] = url[:-len(suffix)]
        if provider in AWS_REQUEST_PROVIDERS:
            model_region = (
                _get_bedrock_model_region(transport_model or model, getattr(self, "_bedrock_model_id", None))
                if provider == "bedrock" else None
            )
            if model_region:
                # Native model regions precede environment/settings regions;
                # capturing credentials must not promote their region above it.
                params["aws_region_name"] = model_region
            uses_bedrock_bearer = (
                provider in ("bedrock", "bedrock_mantle")
                and params.get("api_key") not in (None, DUMMY_LITELLM_API_KEY)
            )
            if not uses_bedrock_bearer:
                if any(os.environ.get(variable) for variable in LITELLM_AWS_CREDENTIAL_SELECTOR_ENV_VARS):
                    # LiteLLM 1.100.0 resolves these selectors ahead of explicit
                    # request credentials, which would replace the isolated keys.
                    raise ValueError(f"Refusing ambient LiteLLM AWS credential selector for provider {provider}")
                aws_request_credentials = dict(aws_request_credentials or {})
                if (provider == "bedrock_mantle" or model_region) and params.get("aws_region_name"):
                    aws_request_credentials["aws_region_name"] = params["aws_region_name"]
                if not (
                    aws_request_credentials.get("aws_access_key_id")
                    and aws_request_credentials.get("aws_secret_access_key")
                ):
                    if getattr(self, "_aws_environment_credentials_incomplete", False):
                        raise ValueError("AWS environment credentials are incomplete")
                    raise ValueError("AWS credentials were not resolved for this request")
                if not aws_request_credentials.get("aws_region_name"):
                    raise ValueError(
                        "AWS region was not resolved for this request; set AWS_REGION_NAME, "
                        "aws.AWS_REGION_NAME, AWS_REGION, or AWS_DEFAULT_REGION"
                    )
                params.update(aws_request_credentials)
        request_headers = _request_local_openai_headers(
            transport_provider,
            params.get("organization"),
            transport_model or model,
        )
        if request_headers is not None:
            params["headers"] = request_headers
        return self._finalize_provider_request_params(provider, params)

    def _finalize_provider_request_params(self, provider: str | None, params: dict) -> dict:
        """Merge request-local headers and reject LiteLLM's process-wide header fallback."""
        params = _guard_request_routing_globals(provider, params)
        watsonx_token = params.pop("token", None) if provider in ("watsonx", "watsonx_text") else None
        request_headers = dict(getattr(self, "_request_headers", {}))
        for header, value in (params.get("headers") or {}).items():
            matching_headers = [name for name in request_headers if name.lower() == header.lower()]
            if matching_headers and isinstance(value, openai.Omit):
                value = request_headers[matching_headers[-1]]
            for matching_header in matching_headers:
                del request_headers[matching_header]
            request_headers[header] = value
        authorization_headers = [header for header in request_headers if header.lower() == "authorization"]
        has_authorization = bool(authorization_headers)
        if provider in ("watsonx", "watsonx_text") and has_authorization:
            authorization = request_headers[authorization_headers[-1]]
            for header in authorization_headers:
                del request_headers[header]
            request_headers["Authorization"] = authorization
        if watsonx_token and not has_authorization:
            request_headers["Authorization"] = f"Bearer {watsonx_token}"
            has_authorization = True
        if provider in ("watsonx", "watsonx_text") and has_authorization:
            params.pop("zen_api_key", None)
            params["api_key"] = DUMMY_LITELLM_API_KEY
        if request_headers:
            params["headers"] = request_headers
        elif getattr(litellm, "headers", None):
            raise ValueError(f"Refusing process-wide LiteLLM headers fallback for provider {provider or 'unknown'}")
        return params

    def _requires_streaming(self, model: str) -> bool:
        """Return whether this model requires streaming after OpenAI/Azure routing."""
        def normalize(candidate: str) -> str:
            while candidate.startswith(("azure/", "openai/")):
                candidate = candidate.removeprefix("azure/").removeprefix("openai/")
            return candidate

        normalized_model = normalize(model)
        return any(normalize(candidate) == normalized_model for candidate in self.streaming_required_models)

    def _force_streaming_for_request(self, custom_llm_provider, api_base) -> bool:
        """Return whether an OpenAI-compatible endpoint requires streaming."""
        custom_llm_provider = str(custom_llm_provider or "").strip().lower()
        api_base = api_base.strip().lower() if isinstance(api_base, str) else ""
        return (
            bool(custom_llm_provider)
            and custom_llm_provider == self.force_streaming_provider
            and bool(self.force_streaming_api_base_substrings)
            and any(substring in api_base for substring in self.force_streaming_api_base_substrings)
        )

    async def _get_provider_request_params_async(
        self,
        model: str,
        provider=None,
        transport_provider=None,
        transport_model=None,
        aws_request_credentials=None,
    ) -> dict:
        """Resolve provider parameters without blocking the event loop on Azure AD refresh."""
        provider = provider or self._resolve_request_provider(model)
        azure_ad_token = None
        if getattr(self, "_azure_ad", False) and provider == "azure":
            azure_ad_token = await asyncio.to_thread(_get_azure_ad_token, self._azure_ad_credential)
        return self._get_provider_request_params(
            model,
            azure_ad_token=azure_ad_token,
            provider=provider,
            transport_provider=transport_provider,
            transport_model=transport_model,
            aws_request_credentials=aws_request_credentials,
        )

    def prepare_logs(self, response, system, user, resp, finish_reason):
        response_log = response.dict().copy()
        response_log['system'] = system
        response_log['user'] = user
        response_log['output'] = resp
        response_log['finish_reason'] = finish_reason
        if hasattr(self, 'main_pr_language'):
            response_log['main_pr_language'] = self.main_pr_language
        else:
            response_log['main_pr_language'] = 'unknown'
        return response_log

    @staticmethod
    def _record_completion_metadata(response, model=None, display_model=None) -> None:
        """Count a successful call and synchronously collect usage-based cost when possible."""
        usage = _response_field(response, "usage")

        cost_usd = None
        if get_settings().get("config.output_run_cost", False):
            # The guard covers the whole cost block, not just completion_cost:
            # reading inline costs and probing usage call model_dump() on
            # provider-specific objects, and a cost estimate must never fail a
            # call that already succeeded and was billed.
            try:
                cost_usd = LiteLLMAIHandler._read_positive_response_cost(response, usage)
                if cost_usd is None and model and LiteLLMAIHandler._has_priceable_usage(usage):
                    # Preserve LiteLLM's full usage object so completion_cost can price cache,
                    # reasoning, and provider-specific categories. Convert the small completed
                    # stream wrapper to a dictionary while retaining `response.usage`.
                    cost_response = response
                    if not isinstance(response, dict) and not hasattr(response, "model_dump"):
                        cost_response = response.dict()
                    cost_usd = litellm.completion_cost(completion_response=cost_response, model=model)
            except Exception as e:
                # Treat missing model pricing or insufficient usage as an unavailable call cost.
                # Retain the successful call so the collector marks the aggregate safely.
                get_logger().debug(f"Unable to estimate API cost for model {model}: {type(e).__name__}")

        recorded_model = display_model if display_model is not None else model
        record_ai_call(usage, model=recorded_model, cost_usd=cost_usd)

    @staticmethod
    def _read_positive_response_cost(response, usage):
        """Read a finalized inline cost, rejecting zero placeholders and invalid values."""
        candidates = [
            _response_field(usage, "response_cost"),
            _response_field(usage, "cost"),
        ]

        hidden_params = _response_field(response, "_hidden_params")
        if hasattr(hidden_params, "model_dump"):
            hidden_params = hidden_params.model_dump()
        if isinstance(hidden_params, dict):
            candidates.append(hidden_params.get("response_cost"))

        for candidate in candidates:
            decimal_cost = _as_decimal_cost(candidate)
            if decimal_cost is not None:
                return decimal_cost
        return None

    @staticmethod
    def _has_priceable_usage(usage) -> bool:
        """Return true when finalized usage reports a positive token count.

        Only token counters gate pricing: provider extras such as Groq's timing
        floats (queue_time, prompt_time) are not billable quantities, and letting
        them pass would send zero-token usage to completion_cost, which prices
        it as 0.0 instead of raising.
        """
        if usage is None:
            return False
        return any(
            isinstance(count, int) and not isinstance(count, bool) and count > 0
            for count in (
                _response_field(usage, "prompt_tokens"),
                _response_field(usage, "completion_tokens"),
                _response_field(usage, "total_tokens"),
            )
        )

    @staticmethod
    def _grok_reasoning_levels_for(model: str) -> set[str] | None:
        """Return the reasoning-effort levels accepted by a registered Grok model."""
        normalized_model = model.rsplit(":", 1)[0] if model.startswith("openrouter/") else model
        return next(
            (
                levels
                for grok_id, levels in GROK_REASONING_EFFORT_LEVELS.items()
                if normalized_model == grok_id or normalized_model.endswith("/" + grok_id)
            ),
            None,
        )

    @classmethod
    def _clamp_grok_reasoning_effort(cls, model: str, reasoning_effort: str) -> str:
        """Clamp a configured reasoning effort to the closest supported Grok level."""
        grok_levels = cls._grok_reasoning_levels_for(model)
        if not grok_levels or reasoning_effort in grok_levels:
            return reasoning_effort
        try:
            ReasoningEffort(reasoning_effort)
        except (ValueError, TypeError):
            return reasoning_effort
        if reasoning_effort in ("max", "xhigh"):
            return "xhigh" if "xhigh" in grok_levels else "high"
        return "low"

    def _resolve_reasoning_effort(self, model: str, configured_effort) -> str:
        """Validate and normalize a configured reasoning effort for this model."""
        try:
            ReasoningEffort(configured_effort)
            reasoning_effort = configured_effort
        except (ValueError, TypeError):
            reasoning_effort = ReasoningEffort.MEDIUM.value
            if configured_effort is not None:
                get_logger().warning(
                    f"Invalid reasoning_effort '{configured_effort}' in config. "
                    f"Using default '{reasoning_effort}'. Valid values: {[e.value for e in ReasoningEffort]}"
                )

        clamped_effort = self._clamp_grok_reasoning_effort(model, reasoning_effort)
        if clamped_effort != reasoning_effort:
            get_logger().info(
                f"Grok model {model} does not support reasoning_effort='{reasoning_effort}'; "
                f"using '{clamped_effort}' instead."
            )
        return clamped_effort

    def _apply_openrouter_request_controls(
        self,
        model: str,
        kwargs: dict,
        inherited_reasoning_effort: str | None = None,
    ) -> dict:
        """Apply handler-local OpenRouter routing, reasoning, and output controls."""
        openrouter_settings = self._openrouter_controls
        extra_body = kwargs.get("extra_body") or {}

        # Normalize operator-controlled config: Dynaconf/env overrides can
        # arrive as strings (AUTO_CAST_FOR_DYNACONF is disabled), so coerce
        # defensively instead of trusting the declared types.
        def _as_list(value):
            if isinstance(value, (list, tuple)):
                return [str(v).strip() for v in value if str(v).strip()]
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            return []

        def _as_bool(value, default=True):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return default

        provider_only = _as_list(openrouter_settings.get("provider_only", []))
        provider_order = _as_list(openrouter_settings.get("provider_order", []))
        if provider_only:
            extra_body.setdefault("provider", {})["only"] = provider_only
        elif provider_order:
            provider = extra_body.setdefault("provider", {})
            provider["order"] = provider_order
            provider["allow_fallbacks"] = _as_bool(openrouter_settings.get("allow_fallbacks", True))

        reasoning = {}
        effective_reasoning_effort = str(
            openrouter_settings.get("reasoning_effort", "") or ""
        ).strip().lower()
        reasoning_max_tokens = self._coerce_token_value(openrouter_settings.get("reasoning_max_tokens", 0))
        if effective_reasoning_effort:
            try:
                ReasoningEffort(effective_reasoning_effort)
            except (TypeError, ValueError):
                get_logger().warning(
                    f"Ignoring invalid openrouter.reasoning_effort '{effective_reasoning_effort}'. "
                    f"Valid values: {[effort.value for effort in ReasoningEffort]}."
                )
                effective_reasoning_effort = ""
        if not effective_reasoning_effort:
            if reasoning_max_tokens > 0 and inherited_reasoning_effort:
                if inherited_reasoning_effort == "none":
                    get_logger().warning(
                        f"Ignoring config.reasoning_effort='{inherited_reasoning_effort}' because "
                        "openrouter.reasoning_max_tokens takes precedence."
                    )
                else:
                    get_logger().info(
                        "Using openrouter.reasoning_max_tokens over"
                        f" config.reasoning_effort='{inherited_reasoning_effort}'."
                    )
            elif reasoning_max_tokens <= 0:
                effective_reasoning_effort = inherited_reasoning_effort or ""

        if effective_reasoning_effort:
            clamped_effort = self._clamp_grok_reasoning_effort(model, effective_reasoning_effort)
            if clamped_effort != effective_reasoning_effort:
                get_logger().info(
                    f"Grok model {model} does not support reasoning_effort="
                    f"'{effective_reasoning_effort}'; using '{clamped_effort}' instead."
                )
                effective_reasoning_effort = clamped_effort

        # Preserve explicit disablement; otherwise keep effort and max_tokens
        # mutually exclusive by preferring the token budget.
        if effective_reasoning_effort == "none":
            if reasoning_max_tokens > 0:
                get_logger().warning(
                    "Ignoring openrouter.reasoning_max_tokens because "
                    "openrouter.reasoning_effort='none' disables reasoning."
                )
            reasoning["enabled"] = False
        elif reasoning_max_tokens > 0:
            if effective_reasoning_effort:
                get_logger().warning(
                    f"Ignoring openrouter.reasoning_effort='{effective_reasoning_effort}' because "
                    "openrouter.reasoning_max_tokens takes precedence."
                )
            reasoning["max_tokens"] = reasoning_max_tokens
        elif effective_reasoning_effort:
            # OpenRouter uses xhigh for the max alias; extra_body bypasses
            # LiteLLM's OpenRouter parameter mapping.
            reasoning["effort"] = "xhigh" if effective_reasoning_effort == "max" else effective_reasoning_effort
        if reasoning:
            get_logger().info(f"Adding OpenRouter reasoning {reasoning} to model {model}.")
            extra_body["reasoning"] = reasoning

        if extra_body:
            kwargs["extra_body"] = extra_body

        max_tokens = self._coerce_token_value(openrouter_settings.get("max_tokens", 0))
        output_limit_param = (
            "max_completion_tokens" if "max_completion_tokens" in kwargs else "max_tokens"
        )
        if max_tokens > 0:
            existing = self._coerce_token_value(kwargs.get(output_limit_param, 0))
            kwargs[output_limit_param] = min(existing, max_tokens) if existing > 0 else max_tokens
        effective_max_tokens = self._coerce_token_value(kwargs.get(output_limit_param, 0))
        effective_reasoning_max_tokens = self._coerce_token_value(reasoning.get("max_tokens", 0))
        effective_reasoning_effort = reasoning.get("effort")
        if (
            model.startswith("openrouter/anthropic/")
            and 0 < effective_max_tokens
            and (
                0 < effective_reasoning_max_tokens >= effective_max_tokens
                or (effective_reasoning_effort and effective_max_tokens <= 1024)
            )
        ):
            minimum_reasoning_tokens = effective_reasoning_max_tokens or 1024
            get_logger().warning(
                f"OpenRouter Anthropic max_tokens ({effective_max_tokens}) must be greater than "
                f"the reasoning budget ({minimum_reasoning_tokens}) to leave output headroom."
            )
        return kwargs

    @staticmethod
    def _coerce_token_value(value) -> int:
        """Mirror the request's permissive integer coercion without conversion failures."""
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    def _claude_thinking_mode(self, model: str) -> str | None:
        """Return the thinking mode selected by request construction for this model."""
        adaptive_model = self._is_claude_adaptive_thinking_model(model)
        if adaptive_model and self._claude_thinking_controls["enable_claude_adaptive_thinking"]:
            return "adaptive"
        if (
            model in self.claude_extended_thinking_models
            and self._claude_thinking_controls["enable_claude_extended_thinking"]
        ):
            return "unsupported_extended" if adaptive_model else "extended"
        return None

    def _get_claude_extended_thinking_limits(self) -> tuple[int, int]:
        """Validate and return the snapshotted extended-thinking budget and output cap."""
        extended_thinking_budget_tokens = self._claude_thinking_controls["extended_thinking_budget_tokens"]
        extended_thinking_max_output_tokens = self._claude_thinking_controls["extended_thinking_max_output_tokens"]

        if not isinstance(extended_thinking_budget_tokens, int) or extended_thinking_budget_tokens <= 0:
            raise ValueError(
                f"extended_thinking_budget_tokens must be a positive integer, "
                f"got {extended_thinking_budget_tokens}"
            )
        if not isinstance(extended_thinking_max_output_tokens, int) or extended_thinking_max_output_tokens <= 0:
            raise ValueError(
                f"extended_thinking_max_output_tokens must be a positive integer, "
                f"got {extended_thinking_max_output_tokens}"
            )
        if extended_thinking_max_output_tokens < extended_thinking_budget_tokens:
            raise ValueError(
                f"extended_thinking_max_output_tokens ({extended_thinking_max_output_tokens}) must be greater than "
                f"or equal to extended_thinking_budget_tokens ({extended_thinking_budget_tokens})"
            )
        return extended_thinking_budget_tokens, extended_thinking_max_output_tokens

    def _resolve_output_token_limit(self, model: str, openrouter_model: str | None) -> int:
        """Return the final positive output cap selected by PR-Agent request controls."""
        output_tokens = self._coerce_token_value(get_settings().config.get("max_output_tokens", 0))
        if self._claude_thinking_mode(model) == "extended":
            _, output_tokens = self._get_claude_extended_thinking_limits()

        if openrouter_model:
            openrouter_output_tokens = self._coerce_token_value(self._openrouter_controls.get("max_tokens", 0))
            if openrouter_output_tokens > 0:
                output_tokens = (
                    min(output_tokens, openrouter_output_tokens)
                    if output_tokens > 0
                    else openrouter_output_tokens
                )
        return output_tokens if output_tokens > 0 else 0

    def get_output_token_limit(self, model: str) -> int:
        """Return the output cap that this handler will request for the supplied model."""
        custom_llm_provider = self._custom_llm_provider
        configured_deployment_id = self.deployment_id
        routed_model = self._route_model_for_request(model, custom_llm_provider, configured_deployment_id)
        completion_model = self._normalize_gpt5_model_for_request(routed_model, model, custom_llm_provider)
        request_provider = (
            PROVIDER_SETTING_ALIASES.get(custom_llm_provider, custom_llm_provider)
            if custom_llm_provider
            else self._resolve_request_provider(routed_model)
        )
        openrouter_model = self._canonical_openrouter_model(completion_model, request_provider)
        return self._resolve_output_token_limit(completion_model, openrouter_model)

    def get_output_token_reserve(self, model: str, default_output_tokens: int) -> int:
        """Return completion headroom to reserve while fitting a request prompt."""
        output_tokens = self.get_output_token_limit(model)
        if output_tokens > 0:
            return output_tokens

        default_output_tokens = self._coerce_token_value(default_output_tokens)
        custom_llm_provider = self._custom_llm_provider
        routed_model = self._route_model_for_request(model, custom_llm_provider, self.deployment_id)
        request_provider = (
            PROVIDER_SETTING_ALIASES.get(custom_llm_provider, custom_llm_provider)
            if custom_llm_provider
            else self._resolve_request_provider(routed_model)
        )
        openrouter_model = self._canonical_openrouter_model(
            routed_model, request_provider
        )
        if not openrouter_model:
            return default_output_tokens

        reasoning_effort = str(
            self._openrouter_controls.get("reasoning_effort", "") or ""
        ).strip().lower()
        reasoning_effort = self._clamp_grok_reasoning_effort(
            openrouter_model, reasoning_effort
        )
        reasoning_tokens = self._coerce_token_value(
            self._openrouter_controls.get("reasoning_max_tokens", 0)
        )
        if reasoning_effort == "none" or reasoning_tokens <= 0:
            return default_output_tokens
        return default_output_tokens + reasoning_tokens

    @staticmethod
    def normalize_request_prompts(model: str, system_prompt: str, user_prompt: str) -> tuple[str, str]:
        """Return the prompt strings that request construction will send."""
        if 'claude' in model and not system_prompt:
            system_prompt = "No system prompt provided"
        return system_prompt, user_prompt

    def _configure_claude_extended_thinking(self, model: str, kwargs: dict) -> dict:
        """
        Configure Claude extended thinking parameters if applicable.

        Args:
            model (str): The AI model being used
            kwargs (dict): The keyword arguments for the model call

        Returns:
            dict: Updated kwargs with extended thinking configuration
        """
        extended_thinking_budget_tokens, extended_thinking_max_output_tokens = (
            self._get_claude_extended_thinking_limits()
        )

        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": extended_thinking_budget_tokens
        }
        if get_verbosity_level() >= 2:
            get_logger().info(f"Adding max output tokens {extended_thinking_max_output_tokens} to model {model}, extended thinking budget tokens: {extended_thinking_budget_tokens}")
        kwargs["max_tokens"] = extended_thinking_max_output_tokens

        # temperature may only be set to 1 when thinking is enabled
        if get_verbosity_level() >= 2:
            get_logger().info("Temperature may only be set to 1 when thinking is enabled with claude models.")
        kwargs["temperature"] = 1

        return kwargs

    @staticmethod
    def _is_claude_adaptive_thinking_model(model: str) -> bool:
        """Return whether a Claude model requires the adaptive thinking API."""
        normalized_model = model.lower().replace("_", "-").replace(".", "-")
        return re.search(
            r"claude-(?:opus-4-(?:7|8)|(?:opus|sonnet|fable)-5)(?:[^0-9]|$)",
            normalized_model,
        ) is not None

    def _configure_claude_adaptive_thinking(self, model: str, kwargs: dict) -> dict:
        """Configure thinking for Claude models that reject token budgets."""
        kwargs["thinking"] = {"type": "adaptive"}
        effort = self._default_reasoning_effort
        if effort in ("low", "medium", "high", "xhigh", "max"):
            kwargs["output_config"] = {"effort": effort}
        get_logger().info(
            f"Using adaptive thinking for model {model}"
            + (f" with output_config effort '{effort}'" if "output_config" in kwargs else "")
        )
        # Adaptive-thinking Claude models have sampling parameters removed, so
        # never send temperature here. This pop is load-bearing rather than
        # defensive: NO_SUPPORT_TEMPERATURE_MODELS covers most of these ids
        # after #2400/#2449, but not all of them. It carries
        # bedrock/anthropic.claude-opus-4-7-v1:0 and
        # bedrock/us.anthropic.claude-opus-4-7 without the two combined, so for
        # bedrock/us.anthropic.claude-opus-4-7-v1:0 this line is the only thing
        # stopping a temperature reaching the model.
        kwargs.pop("temperature", None)
        return kwargs

    def add_litellm_callbacks(self, kwargs) -> dict:
        probe = object()
        captured_extra = []

        def capture_logs(message):
            # Parsing the log message and context
            record = message.record
            extra = record.get("extra") or {}
            if extra.get("litellm_callbacks_probe") is not probe:
                return
            log_entry = {}
            if extra.get("command") is not None:
                log_entry.update({"command": extra["command"]})
            if extra.get("pr_url") is not None:
                log_entry.update({"pr_url": extra["pr_url"]})

            # Append the captured request context.
            captured_extra.append(log_entry)

        # Adding the custom sink to Loguru
        handler_id = get_logger().add(capture_logs)
        try:
            get_logger().debug("Capturing logs for litellm callbacks",
                               litellm_callbacks_probe=probe)
        finally:
            get_logger().remove(handler_id)

        context = captured_extra[0] if len(captured_extra) > 0 else {}

        command = context.get("command", "unknown")
        pr_url = context.get("pr_url", "unknown")
        git_provider = get_settings().config.git_provider

        metadata = dict()
        callbacks = litellm.success_callback + litellm.failure_callback + litellm.service_callback
        if "langfuse" in callbacks:
            metadata.update({
                "trace_name": command,
                "tags": [git_provider, command, f'version:{get_version()}'],
                "trace_metadata": {
                    "command": command,
                    "pr_url": pr_url,
                },
            })
        if "langsmith" in callbacks:
            metadata.update({
                "run_name": command,
                "tags": [git_provider, command, f'version:{get_version()}'],
                "extra": {
                    "metadata": {
                        "command": command,
                        "pr_url": pr_url,
                    }
                },
            })

        # Adding the captured logs to the kwargs
        kwargs["metadata"] = metadata

        return kwargs

    def _get_request_user_field(self) -> str:
        """
        Build the value for the OpenAI-compatible "user" request field from the current
        logging context: a compact JSON string carrying the command and the PR URL,
        e.g. {"command":"improve","pr_url":"https://..."}. Returns an empty string when
        no context is available.
        """
        # The probe record is matched by identity, so a concurrent request adding
        # its own sink at the same time cannot capture this request's context nor
        # leak its own into it.
        probe = object()
        captured_extra = []

        def capture_logs(message):
            extra = message.record.get("extra") or {}
            if extra.get("user_field_probe") is not probe:
                return
            log_entry = {}
            if extra.get("command") is not None:
                log_entry.update({"command": extra["command"]})
            if extra.get("pr_url") is not None:
                log_entry.update({"pr_url": extra["pr_url"]})
            captured_extra.append(log_entry)

        handler_id = get_logger().add(capture_logs)
        try:
            get_logger().debug("Capturing the request context for the user field",
                               user_field_probe=probe)
        finally:
            get_logger().remove(handler_id)

        context = captured_extra[0] if len(captured_extra) > 0 else {}
        if not context:
            return ""
        # Cap the individual values before serialization, so the result stays
        # valid JSON: slicing the serialized string could cut through closing
        # quotes and braces. 30 chars cover every tool command; 200 chars of
        # pr_url keep the total under 256 with the JSON overhead.
        for key, max_len in (("command", 30), ("pr_url", 200)):
            value = context.get(key)
            if isinstance(value, str) and len(value) > max_len:
                context[key] = value[:max_len]
        return json.dumps(context, separators=(",", ":"))

    @property
    def deployment_id(self):
        """
        Returns the deployment ID for the OpenAI API.
        """
        return get_settings().get("OPENAI.DEPLOYMENT_ID", None)

    @staticmethod
    def _resolve_cache_control_injection_points():
        """Read and validate LITELLM.CACHE_CONTROL_INJECTION_POINTS for Anthropic prompt caching
        via LiteLLM (https://docs.litellm.ai/docs/tutorials/prompt_caching).

        Accepts a native TOML array in the [litellm] section of configuration.toml / .pr_agent.toml,
        e.g. ``cache_control_injection_points = [{location = "message", role = "system"}]``; a
        JSON-string form is also accepted so the value can be supplied via an environment-variable
        override. Returns the parsed list, or None when unset/disabled. Raises ValueError on a
        malformed value so the caller can surface it as a configuration error rather than retrying it.
        """
        cache_control_injection_points = get_settings().get("LITELLM.CACHE_CONTROL_INJECTION_POINTS", None)
        # Only genuinely unset/disabled values short-circuit. Other falsy-but-malformed values
        # (e.g. 0, False, {}) fall through to type validation below and raise ValueError.
        if cache_control_injection_points in (None, "", []):
            return None
        if isinstance(cache_control_injection_points, str):
            try:
                cache_control_injection_points = json.loads(cache_control_injection_points)
            except json.JSONDecodeError as e:
                raise ValueError(f"LITELLM.CACHE_CONTROL_INJECTION_POINTS contains invalid JSON: {str(e)}") from e
        if not isinstance(cache_control_injection_points, list):
            raise ValueError("LITELLM.CACHE_CONTROL_INJECTION_POINTS must be a JSON/TOML array")
        return cache_control_injection_points

    async def chat_completion(self, model: str, system: str, user: str, temperature: float = 0.2, img_path: str = None):
        configured_deployment_id = self.deployment_id
        return await self._chat_completion_with_retry(
            model,
            system,
            user,
            temperature,
            img_path,
            configured_deployment_id=configured_deployment_id,
        )

    @retry(
        retry=retry_if_exception(_should_retry_same_model),
        stop=stop_after_attempt(MODEL_RETRIES),
        reraise=True,  # surface the provider's error; RetryError hides the reason
    )
    async def _chat_completion_with_retry(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.2,
        img_path: str = None,
        *,
        configured_deployment_id: str | None,
    ):
        # Validate config-derived kwargs before the try/except below, so a malformed value raises a
        # ValueError config error instead of being wrapped as openai.APIError and retried.
        cache_control_injection_points = self._resolve_cache_control_injection_points()
        client_retries = _configured_client_retries()
        custom_llm_provider = self._custom_llm_provider
        user_model = model
        routed_model = self._route_model_for_request(user_model, custom_llm_provider, configured_deployment_id)
        completion_model = self._normalize_gpt5_model_for_request(routed_model, user_model, custom_llm_provider)
        request_provider = (
            PROVIDER_SETTING_ALIASES.get(custom_llm_provider, custom_llm_provider)
            if custom_llm_provider
            else self._resolve_request_provider(routed_model)
        )
        deployment_id = (
            configured_deployment_id
            if request_provider == "azure" and not routed_model.startswith("azure_text/")
            else None
        )
        if img_path:
            try:
                # Finish external image I/O before validating mutable credential fallbacks.
                r = await asyncio.to_thread(
                    requests.head,
                    img_path,
                    allow_redirects=True,
                    timeout=_IMAGE_HEAD_TIMEOUT_SECONDS,
                )
                if r.status_code == 404:
                    error_msg = "The image link is not [alive](img_path).\nPlease repost the original image as a comment, and send the question again with 'quote reply' (see [instructions](https://docs.pr-agent.ai/tools/ask/#ask-on-images))."
                    get_logger().error(error_msg)
                    return f"{error_msg}", "error"
            except Exception as e:
                get_logger().error(f"Error fetching image: {img_path}", e)
                return f"Error fetching image: {img_path}", "error"

        _aws_imds = self._should_use_aws_imds(request_provider)
        async with self._snapshot_aws_request_credentials(_aws_imds) as (
            aws_request_credentials,
            aws_can_fallback,
        ):
            # Resolve credentials before the retry-wrapped inference block. In
            # particular, Azure AD refresh failures are authentication errors and
            # should not be converted to retryable OpenAI API errors below.
            provider_request_params = await self._get_provider_request_params_async(
                routed_model,
                provider=request_provider,
                transport_provider=custom_llm_provider or None,
                transport_model=deployment_id or completion_model,
                aws_request_credentials=aws_request_credentials,
            )
            try:
                resp, finish_reason = None, None
                # Azure mode rewrites only OpenAI models. Explicit non-OpenAI provider
                # prefixes must remain intact in multi-provider configurations.
                model = completion_model
                openrouter_model = self._canonical_openrouter_model(model, request_provider)
                normalized_system, user = self.normalize_request_prompts(model, system, user)
                if normalized_system != system:
                    get_logger().warning(
                        "Empty system prompt for claude model. Adding a newline character to prevent OpenAI API error.")
                system = normalized_system
                messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

                if img_path:
                    messages[1]["content"] = [{"type": "text", "text": messages[1]["content"]},
                                              {"type": "image_url", "image_url": {"url": img_path}}]

                thinking_kwargs_gpt5 = None
                openrouter_reasoning_effort = None
                # Detect GPT-5 family regardless of provider prefix(es) on the model name.
                # Users sometimes put a provider prefix in config (e.g. "openai/gpt-5.1-codex-max"),
                # and Azure mode auto-prepends "azure/", which together can produce stacked prefixes
                # like "azure/openai/gpt-5...". Without normalization the GPT-5 path is skipped and
                # litellm rejects the request with UnsupportedParamsError for temperature=0.2.
                is_gpt6_astra = self._is_gpt6_astra_model(model)
                is_gpt5_model = self._is_gpt5_model(openrouter_model or model)
                if is_gpt5_model or is_gpt6_astra:
                    # Use configured reasoning_effort or default to MEDIUM
                    config_effort = self._default_reasoning_effort
                    try:
                        ReasoningEffort(config_effort)
                        effort = config_effort
                    except (ValueError, TypeError):
                        effort = ReasoningEffort.MEDIUM.value
                        if config_effort is not None:
                            get_logger().warning(
                                f"Invalid reasoning_effort '{config_effort}' in config. "
                                f"Using default '{effort}'. Valid values: {[e.value for e in ReasoningEffort]}"
                            )

                    if is_gpt6_astra and effort in (ReasoningEffort.NONE.value, ReasoningEffort.MINIMAL.value):
                        get_logger().info(f"GPT-6 Astra does not support reasoning_effort='{effort}'; using 'low'")
                        effort = ReasoningEffort.LOW.value
                    elif not is_gpt6_astra and effort == ReasoningEffort.MAX.value:
                        # 'max' is this project's own alias for "the most reasoning available",
                        # already translated on the Grok and OpenRouter paths. GPT-5.2 and later
                        # name that level 'xhigh'; litellm reports supports_xhigh_reasoning_effort
                        # false for gpt-5 and gpt-5.1, so those stay unsupported either way.
                        # GPT-6 Astra accepts 'max' natively and is left untouched.
                        get_logger().info(
                            "GPT-5 models name their top reasoning level 'xhigh'; "
                            "using 'xhigh' for reasoning_effort='max'"
                        )
                        effort = ReasoningEffort.XHIGH.value

                    if openrouter_model:
                        openrouter_reasoning_effort = effort
                    else:
                        thinking_kwargs_gpt5 = {
                            "reasoning_effort": effort,
                            "allowed_openai_params": ["reasoning_effort"],
                        }
                    model_family = "GPT-6 Astra" if is_gpt6_astra else "GPT-5"
                    get_logger().info(f"Using reasoning_effort='{effort}' for {model_family} model")
                # Currently, some models do not support a separate system and user prompts
                if model in self.user_message_only_models or get_settings().config.custom_reasoning_model:
                    user = f"{system}\n\n\n{user}"
                    system = ""
                    get_logger().info(f"Using model {model}, combining system and user prompts")
                    messages = [{"role": "user", "content": user}]

                # Build request kwargs after normalizing the model and messages so credentials and
                # endpoints can be selected for the provider that will actually receive this call.
                kwargs = {
                    "model": model,
                    "messages": messages,
                    "timeout": get_settings().config.ai_timeout,
                }
                if deployment_id:
                    kwargs["deployment_id"] = deployment_id
                kwargs.update(provider_request_params)

                # Caps the completion client's own per-call retries, which otherwise
                # multiply this handler's retry attempts. Parsed before the request
                # try/except (see _configured_client_retries).
                if client_retries is not None:
                    kwargs["num_retries"] = client_retries
                    kwargs["max_retries"] = client_retries

                # Add temperature only if model supports it
                if model not in self.no_support_temperature_models and not get_settings().config.custom_reasoning_model:
                    # get_logger().info(f"Adding temperature with value {temperature} to model {model}.")
                    kwargs["temperature"] = temperature

                if thinking_kwargs_gpt5:
                    kwargs.update(thinking_kwargs_gpt5)
                if is_gpt5_model or is_gpt6_astra:
                    kwargs.pop('temperature', None)

                reasoning_model = openrouter_model.rsplit(":", 1)[0] if openrouter_model else model
                # Add reasoning_effort if model supports it. Match the bare model
                # id as well as any provider-prefixed form (e.g.
                # "openrouter/google/gemini-2.5-pro", "gemini/gemini-2.5-pro"), so a
                # configured reasoning_effort is not silently dropped for models the
                # user references with a provider prefix. OpenRouter routing variants
                # such as :nitro and :floor are stripped only for this membership test.
                if any(
                    reasoning_model == m or reasoning_model.endswith("/" + m)
                    for m in self.support_reasoning_models
                ):
                    config_effort = self._default_reasoning_effort
                    reasoning_effort = self._resolve_reasoning_effort(openrouter_model or model, config_effort)

                    if openrouter_model:
                        # LiteLLM rejects top-level reasoning_effort for some OpenRouter
                        # model IDs it does not mark as reasoning-capable; defer to
                        # OpenRouter's unified reasoning object below.
                        openrouter_reasoning_effort = reasoning_effort
                    else:
                        get_logger().info(f"Adding reasoning_effort with value {reasoning_effort} to model {model}.")
                        kwargs["reasoning_effort"] = reasoning_effort
                        if self._grok_reasoning_levels_for(model):
                            try:
                                supported_params = litellm.get_supported_openai_params(
                                    model=model,
                                    custom_llm_provider=custom_llm_provider or None,
                                ) or []
                            except Exception:
                                supported_params = []
                            # LiteLLM may omit reasoning_effort for grok-build-latest
                            # and OpenAI-compatible gateway-prefixed Grok IDs.
                            if "reasoning_effort" not in supported_params:
                                kwargs["allowed_openai_params"] = ["reasoning_effort"]

                # https://docs.anthropic.com/en/docs/build-with-claude/extended-thinking
                adaptive_thinking_enabled = self._claude_thinking_controls["enable_claude_adaptive_thinking"]
                extended_thinking_enabled = self._claude_thinking_controls["enable_claude_extended_thinking"]
                claude_thinking_mode = self._claude_thinking_mode(model)
                if claude_thinking_mode == "adaptive":
                    kwargs = self._configure_claude_adaptive_thinking(model, kwargs)
                elif claude_thinking_mode == "extended":
                    kwargs = self._configure_claude_extended_thinking(model, kwargs)
                elif claude_thinking_mode == "unsupported_extended":
                    get_logger().warning(
                        f"Skipping extended thinking for {model}: adaptive-only models reject "
                        f"budget_tokens. Enable config.enable_claude_adaptive_thinking instead."
                    )
                elif adaptive_thinking_enabled or extended_thinking_enabled:
                    message = (
                        f"No thinking configuration applied for model {model}: adaptive thinking "
                        f"requires a recognized claude 5 model name in the id and extended "
                        f"thinking requires exact membership in claude_extended_thinking_models."
                    )
                    if "arn:aws:bedrock:" in model:
                        message += (
                            " For a Bedrock inference profile, address the model by name and pass "
                            "the ARN with litellm.model_id."
                        )
                    get_logger().warning(message)

                # Optional output token limit; 0 = unset. Without max_tokens some
                # providers apply a low service-side default (Bedrock Converse: 4096,
                # which reasoning can fully consume, returning empty content).
                # setdefault keeps the extended-thinking limit authoritative.
                max_output_tokens = self._resolve_output_token_limit(model, openrouter_model)
                if max_output_tokens > 0:
                    output_limit_param = "max_completion_tokens" if is_gpt6_astra else "max_tokens"
                    kwargs.setdefault(output_limit_param, max_output_tokens)

                if get_settings().litellm.get("enable_callbacks", False):
                    kwargs = self.add_litellm_callbacks(kwargs)

                seed = get_settings().config.get("seed", -1)
                if temperature > 0 and seed >= 0:
                    raise ValueError(f"Seed ({seed}) is not supported with temperature ({temperature}) > 0")
                elif seed >= 0:
                    get_logger().info(f"Using fixed seed of {seed}")
                    kwargs["seed"] = seed

                if self.repetition_penalty:
                    kwargs["repetition_penalty"] = self.repetition_penalty

                # Support for custom OpenAI body fields (e.g., Flex Processing)
                kwargs = _process_litellm_extra_body(kwargs)

                # Optional provider-side request attribution: when config.add_user_to_requests
                # is enabled, send the current command and PR URL in the OpenAI-compatible
                # "user" field, so provider logs and usage exports can be attributed to a
                # specific PR without timestamp correlation (OpenRouter shows it as
                # "external_user" and includes it in the activity export). Disabled by
                # default: it shares request-attribution data with the model provider.
                if get_settings().config.get("add_user_to_requests", False):
                    request_user = self._get_request_user_field()
                    if request_user:
                        try:
                            supported_params = litellm.get_supported_openai_params(model=model) or []
                        except Exception:
                            supported_params = []
                        if "user" in supported_params:
                            kwargs["user"] = request_user
                        elif openrouter_model:
                            # LiteLLM's OpenRouter transformation does not forward the
                            # standard "user" parameter; extra_body reaches the
                            # OpenAI-compatible request body verbatim.
                            user_extra_body = kwargs.get("extra_body") or {}
                            user_extra_body["user"] = request_user
                            kwargs["extra_body"] = user_extra_body
                        else:
                            # Providers whose parameter mapping does not accept "user"
                            # (e.g. gemini, deepseek) would reject the request when
                            # litellm.drop_params is off: skip the field instead of
                            # breaking the call.
                            get_logger().debug(
                                f"add_user_to_requests: user field unsupported for {model}, skipped")

                # Anthropic prompt caching via LiteLLM's cache_control_injection_points. The value
                # is validated before the try/except (see above) so a malformed config surfaces as
                # a ValueError instead of being retried. The kwarg is Anthropic-specific (Claude via
                # the Anthropic API, Bedrock or Vertex), so gate on the model to avoid passing an
                # unsupported param to other providers when litellm.drop_params is off. setdefault
                # guards against overwriting a value already merged into kwargs.
                if cache_control_injection_points:
                    if isinstance(model, str) and "claude" in model.lower():
                        kwargs.setdefault("cache_control_injection_points", cache_control_injection_points)
                    else:
                        get_logger().debug(
                            f"cache_control_injection_points configured but not applied: {model} is not an "
                            "Anthropic (Claude) model")

                # Classic `bedrock/` calls use model_id for Bedrock Runtime inference profiles.
                # Bedrock Mantle uses Projects, so `bedrock_mantle/` intentionally omits it.
                bedrock_model_id = getattr(self, "_bedrock_model_id", None)
                if bedrock_model_id and request_provider == "bedrock":
                    kwargs["model_id"] = bedrock_model_id
                    get_logger().info(f"Using Bedrock custom inference profile: {bedrock_model_id}")

                # OpenRouter provider routing, reasoning control and output cap.
                # Registered reasoning models inherit config.reasoning_effort when
                # no OpenRouter-specific effort or token budget is configured.
                if openrouter_model:
                    kwargs = self._apply_openrouter_request_controls(
                        openrouter_model,
                        kwargs,
                        openrouter_reasoning_effort,
                    )

                get_logger().debug("Prompts", artifact={"system": system, "user": user})

                if get_verbosity_level() >= 2:
                    get_logger().info(f"\nSystem prompt:\n{system}")
                    get_logger().info(f"\nUser prompt:\n{user}")

                # Optional fixed provider override, so a raw hosted model id reaches the
                # provider unchanged instead of being rewritten by LiteLLM's prefix inference.
                if custom_llm_provider:
                    kwargs["custom_llm_provider"] = custom_llm_provider

                # Get completion with automatic streaming detection
                resp, finish_reason, response_obj = await self._get_completion(**kwargs)

            except openai.RateLimitError as e:
                get_logger().error(f"Rate limit error during LLM inference: {e}")
                raise
            except openai.APIError as e:
                if aws_can_fallback:
                    if not self._aws_imds_fell_back:
                        self._activate_static_aws_fallback()
                    fallback_credentials = dict(self._aws_active_creds)
                    request_region = kwargs.get("aws_region_name")
                    for key in AWS_REQUEST_CREDENTIAL_KEYS:
                        kwargs.pop(key, None)
                    kwargs.update(fallback_credentials)
                    if request_region and (
                        request_provider == "bedrock_mantle"
                        or (request_provider == "bedrock" and _get_bedrock_model_region(
                            kwargs["model"], kwargs.get("model_id"),
                        ))
                    ):
                        kwargs["aws_region_name"] = request_region
                    resp, finish_reason, response_obj = await self._get_completion(**kwargs)
                else:
                    get_logger().warning(f"Error during LLM inference: {e}")
                    raise
            except Exception as e:
                get_logger().warning(f"Unknown error during LLM inference: {e}")
                raise openai.APIError(
                    str(e),
                    request=httpx.Request("POST", model),
                    body=None,
                ) from e

        # Post-response bookkeeping happens outside the Bedrock IMDS lock above: it
        # touches no os.environ credentials, and in IMDS mode the lock serializes
        # every concurrent call, so holding it through logging and cost pricing
        # would make each waiting coroutine pay for them serially.
        get_logger().debug(f"\nAI response:\n{resp}")

        # log the full response for debugging
        response_log = self.prepare_logs(response_obj, system, user, resp, finish_reason)
        get_logger().debug("Full_response", artifact=response_log)

        # for CLI debugging
        if get_verbosity_level() >= 2:
            get_logger().info(f"\nAI response:\n{resp}")

        self._record_completion_metadata(response_obj, model=model, display_model=user_model)

        return resp, finish_reason

    async def probe_completion(self, model: str, *, max_tokens: int = 10, timeout: int = 10, _completion=None) -> None:
        """Preserve the single-call health probe using request-local credentials."""
        custom_llm_provider = self._custom_llm_provider
        configured_deployment_id = self.deployment_id
        routed_model = self._route_model_for_request(model, custom_llm_provider, configured_deployment_id)
        request_provider = (
            PROVIDER_SETTING_ALIASES.get(custom_llm_provider, custom_llm_provider)
            if custom_llm_provider
            else self._resolve_request_provider(routed_model)
        )
        deployment_id = (
            configured_deployment_id
            if request_provider == "azure" and not routed_model.startswith("azure_text/")
            else None
        )
        async with self._snapshot_aws_request_credentials(self._should_use_aws_imds(request_provider)) as (
            aws_request_credentials,
            _,
        ):
            kwargs = {
                "model": self._normalize_gpt5_model_for_request(routed_model, model, custom_llm_provider),
                "messages": [{"role": "system", "content": "Say ping"}],
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
            if deployment_id:
                kwargs["deployment_id"] = deployment_id
            kwargs.update(await self._get_provider_request_params_async(
                routed_model,
                provider=request_provider,
                transport_provider=custom_llm_provider or None,
                transport_model=deployment_id or kwargs["model"],
                aws_request_credentials=aws_request_credentials,
            ))
            if custom_llm_provider:
                kwargs["custom_llm_provider"] = custom_llm_provider
            if self._bedrock_model_id and request_provider == "bedrock":
                kwargs["model_id"] = self._bedrock_model_id
            kwargs["model"] = normalize_litellm_model(kwargs["model"], custom_llm_provider)
            await self._acompletion(_completion=_completion, **kwargs)

    async def _get_completion(self, **kwargs):
        """
        Wrapper that automatically handles streaming for required models.
        """
        model = kwargs["model"]
        custom_llm_provider = str(kwargs.get("custom_llm_provider") or "").strip().lower()
        # Double the prefix so LiteLLM strips its provider prefix but preserves
        # OpenRouter's native router ID; leave other explicit providers unchanged.
        kwargs["model"] = normalize_litellm_model(model, custom_llm_provider)
        force_streaming = self._force_streaming_for_request(custom_llm_provider, kwargs.get("api_base"))

        # Some OpenAI-compatible endpoints can return an empty-string
        # finish_reason on non-streaming responses, which LiteLLM rejects during
        # response normalization. Streaming avoids that conversion path.
        if self._requires_streaming(model) or force_streaming:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            if force_streaming and not self._requires_streaming(model):
                get_logger().info(
                    f"Using streaming mode for model {model} "
                    "due to OpenAI-compatible endpoint compatibility"
                )
            else:
                get_logger().info(f"Using streaming mode for model {model}")
            response = await self._acompletion(**kwargs)
            return await _handle_streaming_response(response, model=model)
        else:
            response = await self._acompletion(**kwargs)
            if response is None or len(response["choices"]) == 0:
                raise openai.APIError(
                    f"No choices in model response from {model}",
                    request=httpx.Request("POST", model),
                    body=None,
                )
            content = response["choices"][0]['message']['content']
            finish_reason = response["choices"][0]["finish_reason"]
            if not content:
                get_logger().warning(
                    f"Empty content in model response, finish_reason: {finish_reason}")
                raise openai.APIError(
                    f"Empty content in model response (finish_reason: {finish_reason})",
                    request=httpx.Request("POST", model),
                    body=None,
                )
            return content, finish_reason, response

    async def _acompletion(
        self, _completion=None, _azure_oidc_guard=False, _azure_ad_guard=False, _raw_api_key_guard=False, **kwargs,
    ):
        """Call LiteLLM with any provider compatibility context scoped to this task."""
        _completion = _completion or acompletion
        custom_llm_provider = str(kwargs.get("custom_llm_provider") or "").strip().lower()
        provider = (
            PROVIDER_SETTING_ALIASES.get(custom_llm_provider, custom_llm_provider)
            if custom_llm_provider
            else self._resolve_request_provider(kwargs.get("model"))
        )
        transport = (
            custom_llm_provider or self._resolve_request_transport_provider(kwargs.get("model")) or provider
        )
        transport_model = kwargs.get("deployment_id") or kwargs.get("model")
        azure_ad_token = kwargs.get("azure_ad_token")
        oidc_selector = azure_ad_token if isinstance(azure_ad_token, str) and azure_ad_token.startswith("oidc/") else None
        companion_auth = (
            self._uses_captured_azure_companion_auth(provider)
            and not _is_cloudflare_gateway(kwargs.get("api_base"))
        )
        if provider == "azure_ai" and companion_auth:
            native_transport, native_model = _azure_ai_native_transport(transport, transport_model)
            if native_transport == "azure":
                # Keep Azure AI's captured credentials and public kwargs; only
                # select the bridge matching LiteLLM's downstream transport.
                provider = transport = native_transport
                transport_model = native_model
                _azure_ad_guard = _azure_ad_guard or _raw_api_key_guard
        ordinary_sdk_auth = (
            provider == "azure" and (azure_ad_token or companion_auth) and not oidc_selector
            and not _is_cloudflare_gateway(kwargs.get("api_base"))
            and _request_local_openai_headers(transport, model=transport_model) is not None
        )
        if provider == "azure" and azure_ad_token and kwargs.get("api_key") is not None:
            captured_key = (
                self._provider_request_params.get("azure", {}).get("api_key")
                or self._provider_environment_api_keys.get("azure")
            )
            model = kwargs.get("model", "")
            headers = dict(kwargs.get("headers") or {})
            guard_key = kwargs.get("api_key") == DUMMY_LITELLM_API_KEY and (self._azure_ad or not captured_key)
            cloudflare_key_branch = _is_cloudflare_gateway(kwargs.get("api_base"))
            if (
                (guard_key or cloudflare_key_branch)
                and (not oidc_selector or not _azure_oidc_guard or cloudflare_key_branch)
                and not (ordinary_sdk_auth and _azure_ad_guard)
                and (cloudflare_key_branch or (transport != "azure_text" and not model.startswith("azure_text/")))
                and not _uses_openai_responses_transport(transport_model, transport)
            ):
                for canonical in ("Authorization", "api-key"):
                    matches = [name for name in headers if name.lower() == canonical.lower()]
                    if matches:
                        value = headers[matches[-1]]
                        for name in matches:
                            del headers[name]
                        headers[canonical] = value
                if cloudflare_key_branch and (not guard_key or "api-key" in headers):
                    # Preserve the initial SDK alias/key choice, including its
                    # absent header; a later alias must not replace this identity.
                    sdk_token = self._azure_sdk_ad_token
                    headers.setdefault("Authorization", f"Bearer {sdk_token}" if sdk_token else openai.Omit())
                    headers.setdefault("api-key", openai.Omit() if sdk_token else kwargs["api_key"])
                elif guard_key:
                    # Azure v1 and Cloudflare can prefer the guard key over AD
                    # auth. Keep the guard, but choose auth through SDK headers.
                    if "Authorization" not in headers:
                        if azure_ad_token.startswith("oidc/"):
                            # Keep LiteLLM's pinned OIDC exchange/cache semantics;
                            # the selector must never become a Bearer credential.
                            azure_ad_token = await asyncio.to_thread(
                                _exchange_azure_oidc_token,
                                azure_ad_token,
                                dict(self._azure_oidc_environment),
                            )
                            kwargs["azure_ad_token"] = azure_ad_token
                        headers["Authorization"] = f"Bearer {azure_ad_token}"
                    headers.setdefault("api-key", openai.Omit())
                kwargs["headers"] = headers
        anthropic_token = None
        vertex_token = None
        vertex_active_token = None
        vertex_aws_token = None
        vertex_default_token = None
        databricks_token = None
        if provider == "vertex_ai":
            _install_vertex_executable_guard()
            adc_snapshot = self._vertex_gac_adc or self._vertex_default_adc
            if adc_snapshot is not None:
                _install_vertex_default_adc_bridge()
                kwargs.setdefault("vertex_credentials", adc_snapshot["cache_key"])
            vertex_credentials = kwargs.get("vertex_credentials")
            if adc_snapshot is not None and vertex_credentials == adc_snapshot["cache_key"]:
                vertex_credentials = adc_snapshot["info"]
            if isinstance(vertex_credentials, str):
                try:
                    vertex_credentials = json.loads(vertex_credentials)
                except (ValueError, TypeError):
                    vertex_credentials = None
            if isinstance(vertex_credentials, dict):
                source = vertex_credentials.get("credential_source")
                if (
                    vertex_credentials.get("type") == "external_account"
                    and isinstance(source, dict)
                    and "executable" in source
                ):
                    raise ValueError("Vertex executable credentials are incompatible with request isolation")
                _install_vertex_impersonated_credentials_bridge()
                if vertex_credentials.get("type") == "external_account":
                    _install_vertex_wif_project_bridge()
                vertex_token = _vertex_request_credentials.set(vertex_credentials)
            vertex_active_token = _vertex_request_active.set(True)
            vertex_aws_token = _vertex_request_aws_environment.set(self._vertex_aws_environment)
            vertex_default_token = _vertex_request_default_adc.set(adc_snapshot)
        if provider == "databricks":
            captured_key = (
                getattr(self, "_provider_request_params", {}).get("databricks", {}).get("api_key")
                or getattr(self, "_provider_environment_api_keys", {}).get("databricks")
            )
            keyless = kwargs.get("api_key") == DUMMY_LITELLM_API_KEY and not captured_key
            if keyless:
                _install_databricks_keyless_bridge()
            databricks_token = _databricks_request_keyless.set(keyless)
        if provider == "anthropic":
            _install_anthropic_auth_token_bridge()
            captured_key = (
                getattr(self, "_provider_request_params", {}).get("anthropic", {}).get("api_key")
                or getattr(self, "_provider_environment_api_keys", {}).get("anthropic")
            )
            anthropic_token = _anthropic_request_auth_token.set({
                "auth_token": getattr(self, "_anthropic_auth_token", None),
                "generated_guard": kwargs.get("api_key") == DUMMY_LITELLM_API_KEY and not captured_key,
            })
        sdk_token = None
        if _request_local_openai_headers(transport, model=transport_model) is not None:
            if kwargs.get("client") is not None:
                raise ValueError("Externally supplied SDK clients cannot guarantee request header isolation")
            _install_sdk_header_bridge()
            explicit_headers = _merge_sdk_headers(kwargs.get("headers") or {}, kwargs.get("extra_headers") or {})
            _check_sdk_marker_collision(explicit_headers)
            sdk_token = _sdk_request_headers.set({
                **self._sdk_header_defaults,
                "provider": provider,
                "explicit_headers": explicit_headers,
            })
        oidc_context = None
        ad_responses_context = None
        raw_key_context = None
        raw_auth_context = None
        try:
            # A tokenless context must not match an unrelated nested None token.
            oidc_context = _azure_oidc_request.set(None)
            ad_responses_context = _azure_ad_responses_request.set(None)
            raw_key_provider = None
            if provider == "azure_ai" and companion_auth and _raw_api_key_guard:
                _install_raw_api_key_guard_override_bridge(transport)
                raw_key_provider = transport
            if _raw_api_key_guard and any(
                name.lower() == "authorization" for name in (kwargs.get("headers") or {})
            ):
                _install_raw_api_key_guard_bridge()
                _install_raw_api_key_guard_override_bridge(transport)
                raw_key_provider = transport
                snapshot = getattr(self, "_raw_guard_auth_snapshot", {})
                if (
                    kwargs.get("api_key") == DUMMY_LITELLM_API_KEY
                    and not snapshot.get("generic_key", True)
                    and JSONProviderRegistry.supports_responses_api(transport)
                    and _uses_openai_responses_transport(transport_model, transport)
                ):
                    headers = dict(kwargs["headers"])
                    authorization = [name for name in headers if name.lower() == "authorization"]
                    if len(authorization) == 1:
                        # Native JSON Responses merges explicit headers after
                        # creating its guard header, using case-sensitive keys.
                        headers["Authorization"] = headers.pop(authorization[0])
                        kwargs["headers"] = headers
            # A nested non-guard call must not inherit an outer guard's provenance.
            raw_key_context = _raw_api_key_guard_provider.set(raw_key_provider)
            raw_auth_context = _raw_api_key_guard_auth.set(
                getattr(self, "_raw_guard_auth_snapshot", None) if raw_key_provider else None
            )
            if (
                provider == "azure" and (azure_ad_token or companion_auth) and not oidc_selector
                and _uses_openai_responses_transport(transport_model, transport)
            ) or (provider == "azure_ai" and companion_auth):
                _install_azure_oidc_bridge()
                environment = dict(self._azure_oidc_environment)
                _azure_ad_responses_request.set({
                    "dispatch_token": azure_ad_token,
                    "generated_guard": _azure_ad_guard or (provider == "azure_ai" and _raw_api_key_guard),
                    "companion": companion_auth,
                    "environment": environment,
                    "auth_environment": {**environment, **self._azure_oidc_auth_environment},
                })
            if provider == "azure" and (oidc_selector or ordinary_sdk_auth):
                _install_azure_oidc_bridge()
                environment = dict(self._azure_oidc_environment)
                if set(environment) != set(AZURE_OIDC_ENV_VARS):
                    raise RuntimeError("Azure OIDC exchange snapshot is incomplete")
                auth_environment = {**environment, **self._azure_oidc_auth_environment}
                # A missing selector scopes ordinary AD to SDK identity/cache
                # handling without entering the OIDC exchange or resolver.
                _azure_oidc_request.set({
                    "selector": oidc_selector,
                    "dispatch_token": azure_ad_token,
                    "generated_guard": _azure_oidc_guard or _azure_ad_guard,
                    "environment": environment,
                    "auth_environment": auth_environment,
                    "selector_hash": hashlib.sha256(azure_ad_token.encode()).hexdigest()
                    if isinstance(azure_ad_token, str) else None,
                    "identity_hash": hashlib.sha256(json.dumps(auth_environment, sort_keys=True).encode()).hexdigest(),
                })
            if provider != "bedrock_mantle":
                return await _completion(**kwargs)
            request_credentials = {
                key: kwargs[key]
                for key in BEDROCK_MANTLE_REQUEST_CONTEXT_KEYS
                if key in kwargs
            }
            guard_generic_api_key = kwargs.get("api_key") == DUMMY_LITELLM_API_KEY
            if not request_credentials and not guard_generic_api_key:
                return await _completion(**kwargs)
            if BedrockMantleAuthMixin is None:
                raise RuntimeError("The installed LiteLLM version does not provide the Bedrock Mantle signer bridge")
            # LiteLLM stores aws_* completion kwargs in litellm_params, while the
            # Bedrock Mantle signer only receives optional_params. Install the bridge
            # only when this provider is used, then scope credentials to this task.
            _install_bedrock_mantle_signer_bridge()
            credentials_token = _bedrock_mantle_request_credentials.set(request_credentials)
            block_bearer_token = _bedrock_mantle_block_bearer.set(guard_generic_api_key)
            try:
                return await _completion(**kwargs)
            finally:
                _bedrock_mantle_block_bearer.reset(block_bearer_token)
                _bedrock_mantle_request_credentials.reset(credentials_token)
        finally:
            if raw_auth_context is not None:
                _raw_api_key_guard_auth.reset(raw_auth_context)
            if raw_key_context is not None:
                _raw_api_key_guard_provider.reset(raw_key_context)
            if ad_responses_context is not None:
                _azure_ad_responses_request.reset(ad_responses_context)
            if oidc_context is not None:
                _azure_oidc_request.reset(oidc_context)
            if sdk_token is not None:
                _sdk_request_headers.reset(sdk_token)
            if databricks_token is not None:
                _databricks_request_keyless.reset(databricks_token)
            if vertex_default_token is not None:
                _vertex_request_default_adc.reset(vertex_default_token)
            if vertex_aws_token is not None:
                _vertex_request_aws_environment.reset(vertex_aws_token)
            if vertex_active_token is not None:
                _vertex_request_active.reset(vertex_active_token)
            if vertex_token is not None:
                _vertex_request_credentials.reset(vertex_token)
            if anthropic_token is not None:
                _anthropic_request_auth_token.reset(anthropic_token)
