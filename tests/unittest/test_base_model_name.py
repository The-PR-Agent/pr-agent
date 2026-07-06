from pr_agent.algo import (
    CLAUDE_EXTENDED_THINKING_MODELS,
    NO_SUPPORT_TEMPERATURE_MODELS,
    STREAMING_REQUIRED_MODELS,
    base_model_name,
)


class TestBaseModelName:
    def test_provider_prefix_stripped(self):
        """anthropic/, bedrock/ (with region+vendor+version), and vertex_ai/ collapse to one base"""
        for variant in [
            "claude-opus-4-8",
            "anthropic/claude-opus-4-8",
            "vertex_ai/claude-opus-4-8",
            "bedrock/anthropic.claude-opus-4-8",
            "bedrock/us.anthropic.claude-opus-4-8",
            "bedrock/global.anthropic.claude-opus-4-8",
        ]:
            assert base_model_name(variant) == "claude-opus-4-8"

    def test_date_and_cloud_version_suffixes(self):
        """vertex '@date' and bedrock '-v1:0' normalize to the anthropic spelling"""
        expected = "claude-sonnet-4-5-20250929"
        for variant in [
            "claude-sonnet-4-5-20250929",
            "anthropic/claude-sonnet-4-5-20250929",
            "vertex_ai/claude-sonnet-4-5@20250929",
            "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
            "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        ]:
            assert base_model_name(variant) == expected

    def test_dotted_version_preserved(self):
        """A model version containing a dot must not be truncated"""
        assert base_model_name("gpt-5.2-codex") == "gpt-5.2-codex"
        assert base_model_name("openai/qwq-plus") == "qwq-plus"

    def test_capability_set_membership(self):
        """Provider-prefixed models resolve into the base-name capability sets"""
        assert base_model_name("bedrock/eu.anthropic.claude-opus-4-8") in NO_SUPPORT_TEMPERATURE_MODELS
        assert base_model_name("vertex_ai/claude-opus-4-6") in CLAUDE_EXTENDED_THINKING_MODELS
        assert base_model_name("openai/qwq-plus") in STREAMING_REQUIRED_MODELS
        # a model with no special capability resolves out of every set
        assert base_model_name("gpt-4o") not in NO_SUPPORT_TEMPERATURE_MODELS
