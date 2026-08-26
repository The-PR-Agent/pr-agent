import re

from pr_agent.algo.run_details import (get_run_details, init_run_details,
                                       record_ai_call, record_model_used)
from pr_agent.algo.utils import show_run_details


class _Usage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


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
