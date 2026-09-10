"""Warn when a model's token counts are only approximated.

Diff budgeting counts tokens through this encoder, and TokenHandler.count_tokens defaults to
force_accurate=False, so config.model_token_count_estimate_factor never applies on that path.
For a model whose vocabulary differs from o200k_base the budget can therefore run low, and an
oversized prompt is silently truncated by some self-hosted servers rather than rejected - losing
findings with no error anywhere. TokenHandler adds
config.approximate_token_count_safety_factor of headroom on that path, and the warning says so.
"""

from unittest.mock import patch

import pytest

from pr_agent.algo.token_handler import TokenEncoder, TokenHandler
from pr_agent.config_loader import get_settings


@pytest.fixture(autouse=True)
def forget_previous_warnings():
    original = set(TokenEncoder._warned_models)
    approximate = set(TokenEncoder._approximate_models)
    TokenEncoder._warned_models.clear()
    yield
    TokenEncoder._warned_models.clear()
    TokenEncoder._warned_models.update(original)
    TokenEncoder._approximate_models.clear()
    TokenEncoder._approximate_models.update(approximate)


@pytest.fixture
def safety_factor():
    """Set config.approximate_token_count_safety_factor for the duration of a test."""
    settings = get_settings()
    saved = settings.get("config.approximate_token_count_safety_factor", None)

    def apply(value):
        settings.set("config.approximate_token_count_safety_factor", value)
    yield apply
    settings.set("config.approximate_token_count_safety_factor", saved)


def _handler(model):
    handler = TokenHandler(model=model)
    return handler


def test_a_model_without_its_own_tokenizer_warns_that_counts_are_approximate():
    with patch("pr_agent.algo.token_handler.get_logger") as get_logger:
        TokenEncoder._create_encoder("ollama/qwen2.5-coder:7b")

    warnings = " ".join(str(call) for call in get_logger.return_value.warning.call_args_list)
    assert "approximated with the o200k_base tokenizer" in warnings
    assert "max_model_tokens" in warnings


def test_the_warning_is_emitted_once_per_model():
    with patch("pr_agent.algo.token_handler.get_logger") as get_logger:
        for _ in range(3):
            TokenEncoder._create_encoder("ollama/qwen2.5-coder:7b")

    approximation_warnings = [
        call for call in get_logger.return_value.warning.call_args_list
        if "approximated" in str(call)
    ]
    assert len(approximation_warnings) == 1


def test_each_model_in_a_fallback_chain_is_warned_about_separately():
    with patch("pr_agent.algo.token_handler.get_logger") as get_logger:
        TokenEncoder._create_encoder("ollama/qwen2.5-coder:7b")
        TokenEncoder._create_encoder("ollama/devstral")

    approximation_warnings = [
        call for call in get_logger.return_value.warning.call_args_list
        if "approximated" in str(call)
    ]
    assert len(approximation_warnings) == 2


def test_a_gpt_model_with_a_real_tokenizer_is_not_warned_about():
    with patch("pr_agent.algo.token_handler.get_logger") as get_logger:
        encoder = TokenEncoder._create_encoder("gpt-4o")

    assert encoder is not None
    assert get_logger.return_value.warning.call_args_list == []


def test_the_encoder_still_falls_back_when_a_gpt_name_has_no_encoding():
    with patch("pr_agent.algo.token_handler.get_logger"):
        encoder = TokenEncoder._create_encoder("gpt-does-not-exist")

    # the point is that it degrades to o200k_base rather than raising
    assert encoder.encode("def f(): pass")


# --- the headroom the warning refers to ----------------------------------------------------------

def test_an_approximated_count_gets_the_configured_headroom(safety_factor):
    """The under-count the warning describes is corrected, not merely reported."""
    safety_factor(0.25)
    with patch("pr_agent.algo.token_handler.get_logger"):
        handler = _handler("ollama/qwen2.5-coder:7b")

    exact = len(handler.encoder.encode("def f():\n    return 1\n"))
    assert handler.approximate_encoder is True
    assert handler.count_tokens("def f():\n    return 1\n") == -(-exact * 5 // 4)  # ceil(exact * 1.25)


def test_a_model_counted_by_its_own_tokenizer_is_left_exact(safety_factor):
    safety_factor(0.25)
    handler = _handler("gpt-4o")

    patch_text = "def f():\n    return 1\n"
    assert handler.approximate_encoder is False
    assert handler.count_tokens(patch_text) == len(handler.encoder.encode(patch_text))


def test_a_zero_factor_restores_the_raw_encoder_count(safety_factor):
    safety_factor(0)
    with patch("pr_agent.algo.token_handler.get_logger"):
        handler = _handler("ollama/devstral")

    patch_text = "x = 1\n"
    assert handler.count_tokens(patch_text) == len(handler.encoder.encode(patch_text))


def test_a_malformed_factor_is_ignored_rather_than_failing_the_run(safety_factor):
    safety_factor("a lot")
    with patch("pr_agent.algo.token_handler.get_logger"):
        handler = _handler("ollama/devstral")

    patch_text = "x = 1\n"
    assert handler.count_tokens(patch_text) == len(handler.encoder.encode(patch_text))


def test_the_headroom_is_not_applied_to_an_accurate_count(safety_factor):
    """force_accurate has its own factor; applying both would compound them."""
    safety_factor(0.25)
    with patch("pr_agent.algo.token_handler.get_logger"):
        handler = _handler("ollama/devstral")

    with patch.object(TokenHandler, "_get_token_count_by_model_type", return_value=100) as accurate:
        assert handler.count_tokens("x = 1\n", force_accurate=True) == 100
    accurate.assert_called_once()


def test_the_shipped_default_leaves_headroom():
    assert float(get_settings().config.approximate_token_count_safety_factor) > 0
