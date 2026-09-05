# ---------------------------------------------------------------------------
# Canonical Claude model-family definitions.
#
# Each modern Claude family that follows the standard provider-prefix pattern
# (bare, anthropic/, vertex_ai/, bedrock/ with optional cross-region prefixes)
# is declared once here. The _generate_claude_registries() helper expands
# these declarations into the token counts and capability lists that callers
# consume via the public MAX_TOKENS, NO_SUPPORT_TEMPERATURE_MODELS, and
# CLAUDE_EXTENDED_THINKING_MODELS registries.
#
# Historical / one-off Claude entries that do NOT follow the repeating
# provider-prefix pattern are left as static literals inside the registries
# themselves to avoid over-complicating the generator.
# ---------------------------------------------------------------------------

_DEFAULT_BEDROCK_REGIONS = ("global", "us", "eu", "au", "jp")

_CLAUDE_MODEL_FAMILIES = [
    # ── 1M-context models (no temperature, no extended thinking) ──────────
    {
        "model_id": "claude-opus-4-8",
        "max_tokens": 1000000,
        "bedrock_regions": _DEFAULT_BEDROCK_REGIONS,
        "no_temperature": True,
    },
    {
        "model_id": "claude-opus-5",
        "max_tokens": 1000000,
        "bedrock_regions": _DEFAULT_BEDROCK_REGIONS,
        "no_temperature": True,
    },
    {
        "model_id": "claude-sonnet-5",
        "max_tokens": 1000000,
        "bedrock_regions": _DEFAULT_BEDROCK_REGIONS,
        "no_temperature": True,
    },
    {
        "model_id": "claude-opus-4-7",
        "max_tokens": 1000000,
        "bedrock_regions": ("global", "us"),
        "extra_bedrock": ("anthropic.claude-opus-4-7-v1:0",),
        "no_temperature": True,
    },
    # ── 200K-context models with extended thinking ────────────────────────
    {
        "model_id": "claude-sonnet-4-6",
        "max_tokens": 200000,
        "bedrock_regions": ("us", "au", "eu", "jp", "apac", "global"),
        "extra_bedrock_regions": {
            "anthropic.claude-sonnet-4-6-v1:0": (
                "us", "au", "eu", "jp", "apac", "global",
            ),
        },
        "extended_thinking": True,
    },
    {
        "model_id": "claude-opus-4-6",
        "max_tokens": 200000,
        "bedrock_name": "claude-opus-4-6-v1:0",
        "bedrock_regions": ("global", "eu", "au", "jp", "apac", "us"),
        "extra_aliases": {
            "claude-opus-4-6-20260120": 200000,
            "anthropic/claude-opus-4-6-20260120": 200000,
            "vertex_ai/claude-opus-4-6@20260120": 200000,
            "bedrock/anthropic.claude-opus-4-6-20260120-v1:0": 200000,
            "bedrock/us.anthropic.claude-opus-4-6-20260120-v1:0": 200000,
        },
        "extended_thinking": True,
    },
    {
        "model_id": "claude-haiku-4-5-20251001",
        "max_tokens": 200000,
        "vertex": "claude-haiku-4-5@20251001",
        "bedrock_name": "claude-haiku-4-5-20251001-v1:0",
        "bedrock_regions": ("us", "eu", "au", "jp", "apac", "global"),
        "extended_thinking": True,
    },
    {
        "model_id": "claude-opus-4-5-20251101",
        "max_tokens": 200000,
        "vertex": "claude-opus-4-5@20251101",
        "bedrock_name": "claude-opus-4-5-20251101-v1:0",
        "bedrock_regions": ("global", "eu", "au", "jp", "apac", "us"),
        "extended_thinking": True,
    },
    {
        "model_id": "claude-sonnet-4-5-20250929",
        "max_tokens": 200000,
        "bare": False,  # bare not in MAX_TOKENS
        "vertex": "claude-sonnet-4-5@20250929",
        "bedrock_name": "claude-sonnet-4-5-20250929-v1:0",
        "bedrock_regions": ("us", "au", "eu", "jp", "global"),
        "extended_thinking": True,
    },
    {
        "model_id": "claude-3-7-sonnet-20250219",
        "max_tokens": 200000,
        "vertex": "claude-3-7-sonnet@20250219",
        "bedrock_name": "claude-3-7-sonnet-20250219-v1:0",
        "bedrock_regions": ("us", "apac"),
        # Only bare + anthropic/ get extended thinking for 3.7
        "extended_thinking": [
            "anthropic/claude-3-7-sonnet-20250219",
            "claude-3-7-sonnet-20250219",
        ],
    },
    # ── No-temperature only (not in MAX_TOKENS) ──────────────────────────
    {
        "model_id": "claude-fable-5",
        "max_tokens": None,
        "vertex": False,
        "bedrock": False,
        "no_temperature": True,
    },
]


_ALLOWED_FAMILY_KEYS = {
    "model_id",
    "max_tokens",
    "anthropic",
    "bare",
    "vertex",
    "bedrock",
    "bedrock_name",
    "bedrock_regions",
    "extra_aliases",
    "extra_bedrock",
    "extra_bedrock_regions",
    "no_temperature",
    "extended_thinking",
    "thinking_bedrock_regions",
}

_ALLOWED_EXTRA_ALIAS_KEYS = {
    "max_tokens",
    "no_temperature",
    "extended_thinking",
}


def _validate_claude_model_family(fam: dict) -> None:
    """Validate that a Claude model family dict contains only supported keys."""
    unknown = set(fam) - _ALLOWED_FAMILY_KEYS
    if unknown:
        raise ValueError(
            f"unknown Claude family key(s) for {fam.get('model_id')}: {sorted(unknown)}"
        )
    for extra_alias, extra_val in fam.get("extra_aliases", {}).items():
        if isinstance(extra_val, dict):
            nested_unknown = set(extra_val) - _ALLOWED_EXTRA_ALIAS_KEYS
            if nested_unknown:
                raise ValueError(
                    f"unknown Claude extra-alias key(s) for {fam.get('model_id')} / {extra_alias}: {sorted(nested_unknown)}"
                )


def _generate_claude_registries(families=None):
    """Expand _CLAUDE_MODEL_FAMILIES into token counts and capability lists.

    Returns (claude_tokens, claude_no_temp, claude_extended_thinking) where:
      - claude_tokens: dict[str, int] of generated model-id -> context-window
      - claude_no_temp: list[str] of models that do not support temperature
      - claude_extended_thinking: list[str] of models supporting extended thinking
    """
    if families is None:
        families = _CLAUDE_MODEL_FAMILIES

    claude_tokens = {}
    claude_no_temp = []
    claude_extended_thinking = []

    for fam in families:
        _validate_claude_model_family(fam)
        mid = fam["model_id"]
        tokens = fam.get("max_tokens")

        # -- Token counts -----------------------------------------------------
        if tokens is not None:
            if fam.get("bare", True):
                claude_tokens[mid] = tokens
            if fam.get("anthropic", True):
                claude_tokens[f"anthropic/{mid}"] = tokens
            vtx = fam.get("vertex", True)
            if vtx:
                vtx_name = vtx if isinstance(vtx, str) else mid
                claude_tokens[f"vertex_ai/{vtx_name}"] = tokens
            if fam.get("bedrock", True):
                b_name = fam.get("bedrock_name", mid)
                claude_tokens[f"bedrock/anthropic.{b_name}"] = tokens
                for reg in fam.get("bedrock_regions", ()):
                    claude_tokens[f"bedrock/{reg}.anthropic.{b_name}"] = tokens
                for extra in fam.get("extra_bedrock", ()):
                    claude_tokens[f"bedrock/{extra}"] = tokens
                for extra_name, regs in fam.get("extra_bedrock_regions", {}).items():
                    claude_tokens[f"bedrock/{extra_name}"] = tokens
                    for reg in regs:
                        claude_tokens[f"bedrock/{reg}.{extra_name}"] = tokens
            for extra_alias, extra_val in fam.get("extra_aliases", {}).items():
                if isinstance(extra_val, dict):
                    tok = extra_val.get("max_tokens", tokens)
                    if tok is not None:
                        claude_tokens[extra_alias] = tok
                    if extra_val.get("no_temperature"):
                        claude_no_temp.append(extra_alias)
                    if extra_val.get("extended_thinking"):
                        claude_extended_thinking.append(extra_alias)
                else:
                    claude_tokens[extra_alias] = extra_val

        # -- NO_SUPPORT_TEMPERATURE_MODELS ------------------------------------
        if fam.get("no_temperature"):
            if fam.get("bare", True):
                claude_no_temp.append(mid)
            if fam.get("anthropic", True):
                claude_no_temp.append(f"anthropic/{mid}")
            vtx = fam.get("vertex", True)
            if vtx:
                vtx_name = vtx if isinstance(vtx, str) else mid
                claude_no_temp.append(f"vertex_ai/{vtx_name}")
            if fam.get("bedrock", True):
                b_name = fam.get("bedrock_name", mid)
                claude_no_temp.append(f"bedrock/anthropic.{b_name}")
                for reg in fam.get("bedrock_regions", ()):
                    claude_no_temp.append(f"bedrock/{reg}.anthropic.{b_name}")
                for extra in fam.get("extra_bedrock", ()):
                    claude_no_temp.append(f"bedrock/{extra}")

        # -- CLAUDE_EXTENDED_THINKING_MODELS ----------------------------------
        thinking = fam.get("extended_thinking")
        if thinking:
            if isinstance(thinking, (list, tuple)):
                claude_extended_thinking.extend(thinking)
            else:
                if fam.get("anthropic", True):
                    claude_extended_thinking.append(f"anthropic/{mid}")
                claude_extended_thinking.append(mid)
                vtx = fam.get("vertex", True)
                if vtx:
                    vtx_name = vtx if isinstance(vtx, str) else mid
                    claude_extended_thinking.append(f"vertex_ai/{vtx_name}")
                if fam.get("bedrock", True):
                    b_name = fam.get("bedrock_name", mid)
                    claude_extended_thinking.append(f"bedrock/anthropic.{b_name}")
                    thinking_regions = fam.get(
                        "thinking_bedrock_regions", ("us", "au", "eu", "jp", "global")
                    )
                    for reg in thinking_regions:
                        claude_extended_thinking.append(f"bedrock/{reg}.anthropic.{b_name}")

    return claude_tokens, claude_no_temp, claude_extended_thinking



_claude_tokens, _claude_no_temp, _claude_extended_thinking = (
    _generate_claude_registries()
)


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
    'gpt-5.6': 1050000,  # 1.05M, but may be limited by config.max_model_tokens
    'gpt-5.6-sol': 1050000,  # 1.05M, but may be limited by config.max_model_tokens
    'gpt-5.6-terra': 1050000,  # 1.05M, but may be limited by config.max_model_tokens
    'gpt-5.6-luna': 1050000,  # 1.05M, but may be limited by config.max_model_tokens
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
    'deepseek/deepseek-v4-pro': 1000000,  # 1M, but may be limited by config.max_model_tokens
    'deepseek/deepseek-v4-flash': 1000000,  # 1M, but may be limited by config.max_model_tokens
    'zai/glm-5.2': 200000,  # 200K, matching the Z.AI GLM-5/5.1 lineage, but may be limited by config.max_model_tokens
    'moonshot/kimi-k3': 262144,  # 256K, matching the Moonshot Kimi-k2.5/k2.6 lineage, but may be limited by config.max_model_tokens
    'openai/qwq-plus': 131072,  # 131K context length, but may be limited by config.max_model_tokens
    "openrouter/auto": 2000000,  # 2M context length, but may be limited by config.max_model_tokens
    "openrouter/free": 200000,  # 200K context length, but may be limited by config.max_model_tokens
    "openrouter/fusion": 1000000,  # 1M context length, but may be limited by config.max_model_tokens
    "openrouter/pareto-code": 2000000,  # 2M context length, but may be limited by config.max_model_tokens
    'replicate/llama-2-70b-chat:2c1608e18606fad2812020dc541930f2d0495ce32eee50074220b87300bc16e1': 4096,
    'meta-llama/Llama-2-7b-chat-hf': 4096,
    'vertex_ai/codechat-bison': 6144,
    'vertex_ai/codechat-bison-32k': 32000,
    # -- Vertex AI Claude --------------------------------------------------
    'vertex_ai/claude-3-haiku@20240307': 100000,
    'vertex_ai/claude-3-5-haiku@20241022': 100000,
    'vertex_ai/claude-3-sonnet@20240229': 100000,
    'vertex_ai/claude-3-opus@20240229': 100000,
    'vertex_ai/claude-opus-4@20250514': 200000,
    'vertex_ai/claude-opus-4-1@20250805': 200000,
    'vertex_ai/claude-3-5-sonnet@20240620': 100000,
    'vertex_ai/claude-3-5-sonnet-v2@20241022': 100000,
    'vertex_ai/claude-sonnet-4@20250514': 200000,
    # -- Vertex AI non-Claude / Gemini -------------------------------------
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
    'vertex_ai/gemini-3.1-flash': 1048576,
    'vertex_ai/gemini-3.1-pro': 1048576,
    'vertex_ai/gemini-3.1-flash-lite-preview': 1048576,
    'vertex_ai/gemini-3.1-pro-preview': 1048576,
    'vertex_ai/gemini-3.5-flash': 1048576,
    'vertex_ai/gemini-3.5-flash-lite': 1048576,
    'vertex_ai/gemini-3.5-pro': 1048576,
    'vertex_ai/gemini-3.6-flash': 1048576,
    'vertex_ai/gemini-3.7-flash': 1048576,
    'vertex_ai/gemini-3.8-flash': 1048576,
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
    'gemini/gemini-3.1-flash': 1048576,
    'gemini/gemini-3.1-pro': 1048576,
    'gemini/gemini-3.1-flash-lite-preview': 1048576,
    'gemini/gemini-3.1-pro-preview': 1048576,
    'gemini/gemini-3.5-flash': 1048576,
    'gemini/gemini-3.5-flash-lite': 1048576,
    'gemini/gemini-3.5-pro': 1048576,
    'gemini/gemini-3.6-flash': 1048576,
    'gemini/gemini-3.7-flash': 1048576,
    'gemini/gemini-3.8-flash': 1048576,
    'codechat-bison': 6144,
    'codechat-bison-32k': 32000,
    # -- Anthropic Claude --------------------------------------------------
    'anthropic.claude-instant-v1': 100000,
    'anthropic.claude-v1': 100000,
    'anthropic.claude-v2': 100000,
    'anthropic/claude-3-opus-20240229': 100000,
    'anthropic/claude-opus-4-20250514': 200000,
    'anthropic/claude-opus-4-1-20250805': 200000,
    'anthropic/claude-3-5-sonnet-20240620': 100000,
    'anthropic/claude-3-5-sonnet-20241022': 100000,
    'anthropic/claude-sonnet-4-20250514': 200000,
    # -- Bare Claude -------------------------------------------------------
    'claude-opus-4-1-20250805': 200000,
    # -- Haiku -------------------------------------------------------------
    'anthropic/claude-3-5-haiku-20241022': 100000,
    # -- Bedrock Claude ----------------------------------------------------
    'bedrock/anthropic.claude-instant-v1': 100000,
    'bedrock/anthropic.claude-v2': 100000,
    'bedrock/anthropic.claude-v2:1': 100000,
    'bedrock/anthropic.claude-3-sonnet-20240229-v1:0': 100000,
    'bedrock/anthropic.claude-opus-4-20250514-v1:0': 200000,
    'bedrock/anthropic.claude-opus-4-1-20250805-v1:0': 200000,
    'bedrock/anthropic.claude-3-haiku-20240307-v1:0': 100000,
    'bedrock/anthropic.claude-3-5-haiku-20241022-v1:0': 100000,
    'bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0': 100000,
    'bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0': 100000,
    'bedrock/anthropic.claude-sonnet-4-20250514-v1:0': 200000,
    # -- Bedrock Claude (cross-region) -------------------------------------
    "bedrock/us.anthropic.claude-opus-4-20250514-v1:0": 200000,
    "bedrock/us.anthropic.claude-opus-4-1-20250805-v1:0": 200000,
    "bedrock/us.anthropic.claude-3-5-sonnet-20241022-v2:0": 100000,
    "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    "bedrock/global.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    "bedrock/apac.anthropic.claude-3-5-sonnet-20241022-v2:0": 100000,
    "bedrock/apac.anthropic.claude-sonnet-4-20250514-v1:0": 200000,
    'claude-3-5-sonnet': 100000,
    # -- Non-Claude models -------------------------------------------------
    'bedrock/us.meta.llama4-scout-17b-instruct-v1:0': 128000,
    'bedrock/us.meta.llama4-maverick-17b-instruct-v1:0': 128000,
    "bedrock_mantle/xai.grok-4.3": 1000000,  # 1M context, but may be limited by config.max_model_tokens
    'groq/openai/gpt-oss-120b': 131072,
    'groq/openai/gpt-oss-20b': 131072,
    'groq/qwen/qwen3-32b': 131000,
    'dashscope/qwen3.8-max': 1000000,  # 1M, qwen3.8-max is the actual DashScope model id (context_window 1M per QwenCode metadata), but may be limited by config.max_model_tokens
    'groq/moonshotai/kimi-k2-instruct': 131072,
    'groq/deepseek-r1-distill-llama-70b': 128000,
    'groq/meta-llama/llama-4-maverick-17b-128e-instruct': 131072,
    'groq/meta-llama/llama-4-scout-17b-16e-instruct': 131072,
    'groq/llama-3.3-70b-versatile': 128000,
    'groq/llama-3.1-8b-instant': 128000,
    'sambanova/MiniMax-M3': 192000,
    'sambanova/MiniMax-M2.7': 192000,
    'sambanova/MiniMax-M2.5': 160000,
    'sambanova/Meta-Llama-3.3-70B-Instruct': 128000,
    'sambanova/gpt-oss-120b': 128000,
    'sambanova/DeepSeek-V3.1': 128000,
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
    "xai/grok-4.5": 500000,  # 500K context, but may be limited by config.max_model_tokens
    "xai/grok-4.5-latest": 500000,
    "xai/grok-build-latest": 500000,
    "xai/grok-4.6": 500000,  # 500K context, but may be limited by config.max_model_tokens
    "openrouter/x-ai/grok-4.5": 500000,
    "openrouter/x-ai/grok-4.6": 500000,
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
    'xiaomi_mimo/mimo-v2.5': 1048576,  # 1M, matching the LiteLLM registry for mimo-v2.5, xiaomi_mimo/ is the native LiteLLM Xiaomi provider, but may be limited by config.max_model_tokens
    'xiaomi_mimo/mimo-v2.5-pro': 1048576,  # 1M, matching the LiteLLM registry for mimo-v2.5-pro, but may be limited by config.max_model_tokens
    # Provider-prefixed Claude model IDs generated from _CLAUDE_MODEL_FAMILIES
    **_claude_tokens,
}


OPENROUTER_ROUTER_MODEL_ALIASES = {
    "openrouter/auto": "openrouter/openrouter/auto",
    "openrouter/free": "openrouter/openrouter/free",
    "openrouter/fusion": "openrouter/openrouter/fusion",
    "openrouter/pareto-code": "openrouter/openrouter/pareto-code",
}


def normalize_litellm_model(model: str, custom_llm_provider: str = "") -> str:
    if custom_llm_provider.strip().lower() not in ("", "openrouter"):
        return model
    return OPENROUTER_ROUTER_MODEL_ALIASES.get(model, model)


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
    "gpt-5-mini",
    # Anthropic Claude -- temperature is deprecated (Issue #2400), (Issue #2449)
    # Generated from _CLAUDE_MODEL_FAMILIES:
    *_claude_no_temp,
]

SUPPORT_REASONING_EFFORT_MODELS = [
    "o3-mini",
    "o3-mini-2025-01-31",
    "o3",
    "o3-2025-04-16",
    "o4-mini",
    "o4-mini-2025-04-16",
    # Gemini 2.5 exposes a thinking budget controlled by reasoning_effort. Without
    # these entries a configured effort is silently dropped, so a runaway thinking
    # trace can consume the whole output budget and return an empty completion.
    # LiteLLM maps native provider paths to thinkingConfig.thinkingBudget, while
    # LiteLLMAIHandler routes OpenRouter-prefixed forms through extra_body.reasoning.
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    # Register each published Grok id separately so provider-prefixed forms match
    # and the allowlist below can clamp model-specific reasoning levels.
    "grok-4.5",
    "grok-4.5-latest",
    "grok-build-latest",
    "grok-4.6",
]

# Clamp OpenAI-only levels for always-on Grok reasoning; allow xhigh on 4.6+.
GROK_REASONING_EFFORT_LEVELS = {
    "grok-4.5": {"low", "medium", "high"},
    "grok-4.5-latest": {"low", "medium", "high"},
    "grok-build-latest": {"low", "medium", "high"},
    "grok-4.6": {"low", "medium", "high", "xhigh"},
}

# Claude models that support "extended thinking" through the manual
# thinking={"type": "enabled", "budget_tokens": ...} request built by
# LiteLLMAIHandler._configure_claude_extended_thinking(). Only models that
# accept budget_tokens belong here. Adaptive-only models (Claude Opus 4.7/4.8,
# Opus 5, Sonnet 5, Fable 5) reject budget_tokens with an HTTP 400 and must not be added
# without also adding an adaptive-thinking code path. This list is the built-in
# default; it can be replaced via the `claude_extended_thinking_models_override`
# configuration option.
#
# Generated from _CLAUDE_MODEL_FAMILIES:
CLAUDE_EXTENDED_THINKING_MODELS = list(_claude_extended_thinking)

# Models that require streaming mode
STREAMING_REQUIRED_MODELS = [
    "openai/qwq-plus"
]
