import re
from decimal import Decimal

import pytest

from pr_agent.algo import run_details
from pr_agent.algo.run_details import (get_run_details, init_run_details,
                                       record_ai_call, record_model_used)
from pr_agent.algo.utils import show_run_details
from pr_agent.config_loader import get_settings


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


@pytest.fixture(autouse=True)
def disable_cost_output_by_default():
    settings = get_settings()
    previous = settings.config.get("output_run_cost", False)
    settings.set("config.output_run_cost", False)
    yield
    settings.set("config.output_run_cost", previous)


def test_renders_all_fields_in_a_details_block_when_gfm_supported():
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=False)
    record_ai_call(_Usage(12340, 1205, 13545))

    output = show_run_details(gfm_supported=True)

    assert "<details>" in output
    assert "⚙️ Agent run details" in output
    assert "Model: openai/gpt-5.4" in output
    assert "Tokens: 12,340 in / 1,205 out / 13,545 total" in output
    assert re.search(r"Time cost: \d+\.\d+s", output)
    assert "AI calls: 1" in output


def test_marks_fallback_model():
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=True)
    record_ai_call(_Usage(1, 1, 2))

    output = show_run_details(gfm_supported=True)

    assert "Model: openai/gpt-5.4 (fallback)" in output


def test_omits_token_components_the_provider_did_not_report():
    """A provider that reports only a total must not render "0 in / 0 out"."""
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=False)
    record_ai_call({"total_tokens": 13545})

    output = show_run_details(gfm_supported=True)

    assert "Tokens: 13,545 total" in output
    assert "0 in" not in output
    assert "0 out" not in output


def test_omits_token_line_when_usage_unavailable():
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=False)
    record_ai_call(None)

    output = show_run_details(gfm_supported=True)

    assert "Tokens:" not in output
    assert "Model: openai/gpt-5.4" in output
    assert "AI calls: 1" in output


def test_plain_text_fallback_when_gfm_unsupported():
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=False)
    record_ai_call(_Usage(10, 2, 12))

    output = show_run_details(gfm_supported=False)

    assert "<details>" not in output
    assert "<summary>" not in output
    assert "⚙️ Agent run details" in output
    assert "Model: openai/gpt-5.4" in output
    assert "Tokens: 10 in / 2 out / 12 total" in output


def test_omits_ai_calls_line_when_no_calls_were_recorded():
    init_run_details()
    record_model_used("openai/gpt-5.4", is_fallback=False)

    output = show_run_details(gfm_supported=True)

    assert "Model: openai/gpt-5.4" in output
    assert "AI calls:" not in output


def test_returns_empty_string_when_no_model_was_recorded():
    init_run_details()

    assert show_run_details(gfm_supported=True) == ""


def test_returns_empty_string_when_collector_not_initialized():
    from pr_agent.algo import run_details

    token = run_details._run_details.set(None)
    try:
        assert get_run_details() is None
        assert show_run_details(gfm_supported=True) == ""
    finally:
        run_details._run_details.reset(token)


def test_disabled_cost_output_preserves_existing_run_details_byte_for_byte(monkeypatch):
    monkeypatch.setattr(run_details.time, "monotonic", lambda: 108.2)
    details = init_run_details()
    details.start_time = 100.0
    record_model_used("model-a", is_fallback=False)
    record_ai_call(_Usage(10, 2, 12), model="model-a", cost_usd=Decimal("0.0842"))

    output = show_run_details(gfm_supported=True)

    assert output == (
        "\n<hr>\n<details> <summary><strong>⚙️ Agent run details</strong></summary>\n\n"
        "- Model: model-a\n"
        "- Tokens: 10 in / 2 out / 12 total\n"
        "- Time cost: 8.2s\n"
        "- AI calls: 1\n\n"
        "</details>\n"
    )


def test_renders_complete_cost_and_rounded_multi_model_breakdown():
    get_settings().set("config.output_run_cost", True)
    init_run_details()
    record_model_used("model-b", is_fallback=True)
    record_ai_call(_Usage(10, 2, 12), model="model-a", cost_usd=Decimal("0.07104"))
    record_ai_call(_Usage(5, 1, 6), model="model-b", cost_usd=Decimal("0.01321"))

    output = show_run_details(gfm_supported=True)

    assert "Estimated API cost: $0.08 USD" in output
    assert "  - model-a: $0.07 USD" in output
    assert "  - model-b: $0.01 USD" in output


def test_renders_partial_cost_with_priced_call_count():
    get_settings().set("config.output_run_cost", True)
    init_run_details()
    record_model_used("model-a", is_fallback=False)
    record_ai_call(_Usage(10, 2, 12), model="model-a", cost_usd=Decimal("0.0042"))
    record_ai_call(None, model="model-b")

    output = show_run_details(gfm_supported=True)

    assert "Estimated API cost: <$0.01 USD (partial: 1 of 2 successful calls priced)" in output


def test_unavailable_cost_never_renders_as_zero():
    get_settings().set("config.output_run_cost", True)
    init_run_details()
    record_model_used("model-a", is_fallback=False)
    record_ai_call(None, model="model-a")

    output = show_run_details(gfm_supported=True)

    assert "Estimated API cost: unavailable (no calls could be priced)" in output
    assert "Estimated API cost: $0" not in output


def test_tiny_positive_cost_is_not_rounded_to_false_zero():
    get_settings().set("config.output_run_cost", True)
    init_run_details()
    record_model_used("model-a", is_fallback=False)
    record_ai_call(_Usage(1, 1, 2), model="model-a", cost_usd=Decimal("0.00001"))

    output = show_run_details(gfm_supported=True)

    assert "Estimated API cost: <$0.01 USD" in output
    assert "Estimated API cost: $0.00" not in output
