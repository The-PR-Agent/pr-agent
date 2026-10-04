import litellm
from litellm.llms.bedrock.chat.converse_transformation import AmazonConverseConfig

_FUTURE_CLAUDE_MODEL = "claude-opus-9-1"
_FUTURE_BEDROCK_MODEL_ID = "eu.anthropic.claude-opus-9-1"
_FUTURE_BEDROCK_MODEL = f"bedrock/{_FUTURE_BEDROCK_MODEL_ID}"


def test_litellm_routes_unmapped_future_claude_to_anthropic():
    # Keep the synthetic id absent from the bundled map to exercise fallback routing.
    assert _FUTURE_CLAUDE_MODEL not in litellm.model_cost

    resolved_model, provider, _, _ = litellm.get_llm_provider(model=_FUTURE_CLAUDE_MODEL)

    assert resolved_model == _FUTURE_CLAUDE_MODEL
    assert provider == "anthropic"


def test_litellm_preserves_adaptive_thinking_for_unmapped_future_bedrock_claude():
    # Keep both lookup forms absent so capability resolution must use fallback rules.
    assert _FUTURE_BEDROCK_MODEL_ID not in litellm.model_cost
    assert _FUTURE_BEDROCK_MODEL not in litellm.model_cost

    info = litellm.get_model_info(
        _FUTURE_BEDROCK_MODEL_ID,
        custom_llm_provider="bedrock",
    )
    mapped = AmazonConverseConfig().map_openai_params(
        {"thinking": {"type": "adaptive"}},
        {},
        _FUTURE_BEDROCK_MODEL,
        False,
    )

    assert info["supports_adaptive_thinking"] is True
    assert mapped["thinking"] == {"type": "adaptive"}
