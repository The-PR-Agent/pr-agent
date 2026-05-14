MAX_TOKENS = {
    'text-embedding-ada-002': 8000,
    'gpt-3.5-turbo': 16000,
    'gpt-3.5-turbo-0125': 16000,
    'gpt-3.5-turbo-0613': 4000,
    'gpt-3.5-turbo-1106': 16000,
    'gpt-3.5-turbo-16k': 16000,
    'gpt-3.5-turbo-16k-0613': 16000,
    'gpt-4': 8000,
    'gpt-4-0613': 8000,
    'gpt-4-32k': 32000,
    'gpt-4-1106-preview': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4-0125-preview': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4o': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4o-2024-05-13': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4-turbo-preview': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4-turbo-2024-04-09': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4-turbo': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4o-mini': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4o-mini-2024-07-18': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4o-2024-08-06': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4o-2024-11-20': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4.5-preview': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4.5-preview-2025-02-27': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-4.1': 1047576,
    'gpt-4.1-2025-04-14': 1047576,
    'gpt-4.1-mini': 1047576,
    'gpt-4.1-mini-2025-04-14': 1047576,
    'gpt-4.1-nano': 1047576,
    'gpt-4.1-nano-2025-04-14': 1047576,
    'gpt-5-nano': 200000,  # 200K, but may be limited by config.max_model_tokens
    'gpt-5-mini': 200000,  # 200K, but may be limited by config.max_model_tokens
    'gpt-5': 200000,
    'gpt-5-2025-08-07': 200000,
    'gpt-5.1': 200000,
    'gpt-5.1-2025-11-13': 200000,
    'gpt-5.1-chat-latest': 200000,
    'gpt-5.1-codex': 200000,
    'gpt-5.1-codex-mini': 200000,
    'gpt-5.2': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.2-2025-12-11': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.2-chat-latest': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-5.2-codex': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.3-codex': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.3-chat': 128000,  # 128K, but may be limited by config.max_model_tokens
    'gpt-5.4': 272000,  # 272K safe default without opt-in 1M context parameters
    'gpt-5.4-2026-03-05': 272000,  # 272K safe default without opt-in 1M context parameters
    'gpt-5.4-mini': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.4-mini-2026-03-17': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.4-nano': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.4-nano-2026-03-17': 400000,  # 400K, but may be limited by config.max_model_tokens
    'gpt-5.5': 1050000,  # 1.05M, but may be limited by config.max_model_tokens
    'gpt-5.5-2026-04-23': 1050000,  # 1.05M, but may be limited by config.max_model_tokens
    'o1-mini': 128000,  # 128K, but may be limited by config.max_model_tokens
    'o1-mini-2024-09-12': 128000,  # 128K, but may be limited by config.max_model_tokens
    'o1-preview': 128000,  # 128K, but may be limited by config.max_model_tokens
    'o1-preview-2024-09-12': 128000,  # 128K, but may be limited by config.max_model_tokens
    'o1-2024-12-17': 204800,  # 200K, but may be limited by config.max_model_tokens
    'o1': 204800,  # 200K, but may be limited by config.max_model_tokens
    'o3-mini': 204800,  # 200K, but may be limited by config.max_model_tokens
    'o3-mini-2025-01-31': 204800,  # 200K, but may be limited by config.max_model_tokens
    'o3': 200000,  # 200K, but may be limited by config.max_model_tokens
    'o3-2025-04-16': 200000,  # 200K, but may be limited by config.max_model_tokens
    'o4-mini': 200000, # 200K, but may be limited by config.max_model_tokens
    'o4-mini-2025-04-16': 200000, # 200K, but may be limited by config.max_model_tokens
    'claude-instant-1': 100000,
    'claude-2': 100000,
    'command-nightly': 4096,
    'deepseek/deepseek-chat': 128000,  # 128K, but may be limited by config.max_model_tokens
    'deepseek/deepseek-reasoner': 64000,  # 64K, but may be limited by config.max_model_tokens
    'openai/qwq-plus': 131072,  # 131K context length, but may be limited by config.max_model_tokens
    'replicate/llama-2-70b-chat:2c1608e18606fad2812020dc541930f2d0495ce32eee50074220b87300bc16e1': 4096,
    'meta-llama/Llama-2-7b-chat-hf': 4096,
    'vertex_ai/codechat-bison': 6144,
    'vertex_ai/codechat-bison-32k': 32000,
    'vertex_ai/claude-3-haiku@20240307': 100000,
    'vertex_ai/claude-3-5-haiku@20241022': 100000,
    'vertex_ai/claude-haiku-4-5@20251001': 200000,
    'vertex_ai/claude-3-sonnet@20240229': 100000,
    'vertex_ai/claude-3-opus@20240229': 100000,
    'vertex_ai/claude-opus-4@20250514': 200000,
    'vertex_ai/claude-opus-4-1@20250805': 200000,
    'vertex_ai/claude-opus-4-5@20251101': 200000,
    'vertex_ai/claude-opus-4-6@20260120': 200000,
    'vertex_ai/claude-opus-4-6': 200000,
    'vertex_ai/claude-opus-4-7': 1000000,
    'vertex_ai/claude-3-5-sonnet@20240620': 100000,
    'vertex_ai/claude-3-5-sonnet-v2@20241022': 100000,
    'vertex_ai/claude-3-7-sonnet@20250219': 200000,
    'vertex_ai/claude-sonnet-4@20250514': 200000,
    'vertex_ai/claude-sonnet-4-5@20250929': 200000,
    'vertex_ai/claude-sonnet-4-6': 200000,
    'vertex_ai/gemini-1.5-pro': 1048576,
    'vertex_ai/gemini-2.5-pro-preview-03-25': 1048576,
    'vertex_ai/gemini-2.5-pro-preview-05-06': 1048576,
    'vertex_ai/gemini-2.5-pro-preview-06-05': 1048576,
    'vertex_ai/gemini-2.5-pro': 1048576,
    'vertex_ai/gemini-1.5-flash': 1048576,
    'vertex_ai/gemini-2.0-flash': 1048576,
    'vertex_ai/gemini-2.5-flash-preview-04-17': 1048576,
    'vertex_ai/gemini-2.5-flash-preview-05-20': 1048576,
    'vertex_ai/gemini-2.5-flash': 1048576,
    'vertex_ai/gemini-3-flash-preview': 1048576,
    'vertex_ai/gemini-3-pro-preview': 1048576,
    'vertex_ai/gemini-3.1-flash-lite-preview': 1048576,
    'vertex_ai/gemini-3.1-pro-preview': 1048576,
    'vertex_ai/gemma2': 8200,
    'gemini/gemini-1.5-pro': 1048576,
    'gemini/gemini-1.5-flash': 1048576,
    'gemini/gemini-2.0-flash': 1048576,
    'gemini/gemini-2.5-flash-preview-04-17': 1048576,
    'gemini/gemini-2.5-flash-preview-05-20': 1048576,
    'gemini/gemini-2.5-flash': 1048576,
    'gemini/gemini-2.5-pro-preview-03-25': 1048576,
    'gemini/gemini-2.5-pro-preview-05-06': 1048576,
    'gemini/gemini-2.5-pro-preview-06-05': 1048576,
    'gemini/gemini-2.5-pro': 1048576,
    'gemini/gemini-3-flash-preview': 1048576,
    'gemini/gemini-3-pro-preview': 1048576,
    'gemini/gemini-3.1-flash-lite-preview': 1048576,
    'gemini/gemini-3.1-pro-preview': 1048576,
    'codechat-bison': 6144,
    'codechat-bison-32k': 32000,
    'anthropic.claude-instant-v1': 100000,
    'anthropic.claude-v1': 100000,
    'anthropic.claude-v2': 100000,
    'anthropic/claude-3-opus-20240229': 100000,
    'anthropic/claude-opus-4-20250514': 200000,
    'anthropic/claude-opus-4-1-20250805': 200000,
    'anthropic/claude-opus-4-5-20251101': 200000,
    'anthropic/claude-opus-4-6': 200000,
    'anthropic/claude-opus-4-6-20260120': 200000,
    'anthropic/claude-opus-4-7': 1000000,
    'anthropic/claude-3-5-sonnet-20240620': 100000,
    'anthropic/claude-3-5-sonnet-20241022': 100000,
    'anthropic/claude-3-7-sonnet-20250219': 200000,
    'anthropic/claude-sonnet-4-20250514': 200000,
    'anthropic/claude-sonnet-4-5-20250929': 200000,
    'anthropic/claude-sonnet-4-6': 200000,
    'claude-opus-4-1-20250805': 200000,
    'claude-opus-4-5-20251101': 200000,
    'claude-opus-4-6': 200000,
    'claude-opus-4-6-20260120': 200000,
    'claude-opus-4-7': 1000000,
    'claude-3-7-sonnet-20250219': 200000,
    'claude-sonnet-4-6': 200000,
    'anthropic/claude-3-5-haiku-20241022': 100000,
    'anthropic/claude-haiku-4-5-20251001': 200000,
    'claude-haiku-4-5-20251001': 200000,
    'bedrock/anthropic.claude-instant-v1': 100000,
    'bedrock/anthropic.claude-v2': 100000,
    'bedrock/anthropic.claude-v2:1': 100000,
    'bedrock/anthropic.claude-3-sonnet-20240229-v1:0': 100000,
    'bedrock/anthropic.claude-opus-4-20250514-v1:0': 200000,
    'bedrock/anthropic.claude-opus-4-1-20250805-v1:0': 200000,
    'bedrock/anthropic.claude-opus-4-6-20260120-v1:0': 200000,
    'bedrock/anthropic.claude-opus-4-6-v1:0': 200000,
    'bedrock/anthropic.claude-opus-4-7': 1000000,
    'bedrock/anthropic.claude-3-haiku-20240307-v1:0': 100000,
    'bedrock/anthropic.claude-3-5-haiku-20241022-v1:0': 100000,
    'bedrock/anthropic.claude-haiku-4-5-20251001-v1:0': 200000,
    'bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0': 100000,
    'bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0': 100000,
    'bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0': 200000,
    'bedrock/anthropic.claude-sonnet-4-20250514-v1:0': 200000,
    'bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0': 200000,
    'bedrock/anthropic.claude-sonnet-4-6': 200000,
    'bedrock/anthropic.claude-sonnet-4-6-v1:0': 200000,
    'bedrock/anthropic.claude-opus-4-5-20251101-v1:0': 200000,
    "bedrock/us.anthropic.claude-opus-4-20250514-v1:0": 200000,
    "bedrock/us.anthropic.claude-opus-4-1-20250805-v1:0": 200000,
    "bedrock/us.anthropic.claude-opus-4-6-20260120-v1:0": 200000,
    "bedrock/global.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
    "bedrock/eu.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
    "bedrock/au.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
    "bedrock/jp.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
    "bedrock/apac.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
    "bedrock/us.anthropic.claude-opus-4-5-20251101-v1:0": 200000,
    "bedrock/global.anthropic.claude-opus-4-6-v1:0": 200000,
    "bedrock/eu.anthropic.claude-opus-4-6-v1:0": 200000,
    "bedrock/au.anthropic.claude-opus-4-6-v1:0": 200000,
    "bedrock/jp.anthropic.claude-opus-4-6-v1:0": 200000,
    "bedrock/apac.anthropic.claude-opus-4-6-v1:0": 200000,
    "bedrock/us.anthropic.claude-opus-4-6-v1:0": 200000,
    "bedrock/global.anthropic.claude-opus-4-7": 1000000,
    "bedrock/us.anthropic.claude-opus-4-7": 1000000,
    "bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0": 100000,
    "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
    "bedrock/eu.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
    "bedrock/au.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
    "bedrock/jp.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
    "bedrock/apac.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
    "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0": 200000,
    "bedrock/us.anthropic.claude-3-7-sonnet-20250219-v1:0": 200000,
    "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    "bedrock/global.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0": 200000,
    "bedrock/au.anthropic.claude-sonnet-4-5-20250929-v1:0": 200000,
    "bedrock/us.anthropic.claude-sonnet-4-6": 200000,
    "bedrock/us.anthropic.claude-sonnet-4-6-v1:0": 200000,
    "bedrock/au.anthropic.claude-sonnet-4-6": 200000,
    "bedrock/au.anthropic.claude-sonnet-4-6-v1:0": 200000,
    "bedrock/apac.anthropic.claude-3-5-sonnet-20241022-v2:0": 100000,
    "bedrock/apac.anthropic.claude-3-7-sonnet-20250219-v1:0": 200000,
    "bedrock/apac.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    "bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0": 200000,
    "bedrock/eu.anthropic.claude-sonnet-4-6": 200000,
    "bedrock/eu.anthropic.claude-sonnet-4-6-v1:0": 200000,
    "bedrock/jp.anthropic.claude-sonnet-4-5-20250929-v1:0": 200000,
    "bedrock/jp.anthropic.claude-sonnet-4-6": 200000,
    "bedrock/jp.anthropic.claude-sonnet-4-6-v1:0": 200000,
    "bedrock/apac.anthropic.claude-sonnet-4-6": 200000,
    "bedrock/apac.anthropic.claude-sonnet-4-6-v1:0": 200000,
    "bedrock/global.anthropic.claude-sonnet-4-5-20250929-v1:0": 200000,
    "bedrock/global.anthropic.claude-sonnet-4-6": 200000,
    "bedrock/global.anthropic.claude-sonnet-4-6-v1:0": 200000,
    'claude-3-5-sonnet': 100000,
    'bedrock/us.meta.llama4-scout-17b-instruct-v1:0': 128000,
    'bedrock/us.meta.llama4-maverick-17b-instruct-v1:0': 128000,
    'groq/openai/gpt-oss-120b': 131072,
    'groq/openai/gpt-oss-20b': 131072,
    'groq/qwen/qwen3-32b': 131000,
    'groq/moonshotai/kimi-k2-instruct': 131072,
    'groq/deepseek-r1-distill-llama-70b': 128000,
    'groq/meta-llama/llama-4-maverick-17b-128e-instruct': 131072,
    'groq/meta-llama/llama-4-scout-17b-16e-instruct': 131072,
    'groq/llama-3.3-70b-versatile': 128000,
    'groq/llama-3.1-8b-instant': 128000,
    'xai/grok-2': 131072,
    'xai/grok-2-1212': 131072,
    'xai/grok-2-latest': 131072,
    'xai/grok-3': 131072,
    'xai/grok-3-beta': 131072,
    'xai/grok-3-fast': 131072,
    'xai/grok-3-fast-beta': 131072,
    'xai/grok-3-mini': 131072,
    'xai/grok-3-mini-beta': 131072,
    'xai/grok-3-mini-fast': 131072,
    'xai/grok-3-mini-fast-beta': 131072,
    'ollama/llama3': 4096,
    'watsonx/meta-llama/llama-3-8b-instruct': 4096,
    "watsonx/meta-llama/llama-3-70b-instruct": 4096,
    "watsonx/meta-llama/llama-3-405b-instruct": 16384,
    "watsonx/ibm/granite-13b-chat-v2": 8191,
    "watsonx/ibm/granite-34b-code-instruct": 8191,
    "watsonx/mistralai/mistral-large": 32768,
    "deepinfra/deepseek-ai/DeepSeek-R1-Distill-Qwen-32B": 128000,
    "deepinfra/deepseek-ai/DeepSeek-R1-Distill-Llama-70B": 128000,
    "deepinfra/deepseek-ai/DeepSeek-R1": 128000,
    "mistral/mistral-small-latest": 8191,
    "mistral/mistral-medium-latest": 8191,
    "mistral/mistral-large-2407": 128000,
    "mistral/mistral-large-latest": 128000,
    "mistral/open-mistral-7b": 8191,
    "mistral/open-mixtral-8x7b": 8191,
    "mistral/open-mixtral-8x22b": 8191,
    "mistral/codestral-latest": 8191,
    "mistral/open-mistral-nemo": 128000,
    "mistral/open-mistral-nemo-2407": 128000,
    "mistral/open-codestral-mamba": 256000,
    "mistral/codestral-mamba-latest": 256000,
    "codestral/codestral-latest": 8191,
    "codestral/codestral-2405": 8191,
}

USER_MESSAGE_ONLY_MODELS = [
    "deepseek/deepseek-reasoner",
    "o1-mini",
    "o1-mini-2024-09-12",
    "o1-preview"
]

NO_SUPPORT_TEMPERATURE_MODELS = [
    "deepseek/deepseek-reasoner",
    "o1-mini",
    "o1-mini-2024-09-12",
    "o1",
    "o1-2024-12-17",
    "o3-mini",
    "o3-mini-2025-01-31",
    "o1-preview",
    "o3",
    "o3-2025-04-16",
    "o4-mini",
    "o4-mini-2025-04-16",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5.2-codex",
    "gpt-5.3-codex",
    "gpt-5-mini"
]

SUPPORT_REASONING_EFFORT_MODELS = [
    "o3-mini",
    "o3-mini-2025-01-31",
    "o3",
    "o3-2025-04-16",
    "o4-mini",
    "o4-mini-2025-04-16",
]

CLAUDE_EXTENDED_THINKING_MODELS = [
    "anthropic/claude-3-7-sonnet-20250219",
    "claude-3-7-sonnet-20250219"
]

# Models that require streaming mode
STREAMING_REQUIRED_MODELS = [
    "openai/qwq-plus"
]
