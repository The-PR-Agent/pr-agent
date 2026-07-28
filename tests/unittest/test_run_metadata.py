from pr_agent.algo.run_metadata import (RunMetadata, add_token_usage,
                                        get_run_metadata, init_run_metadata,
                                        record_ai_call, record_model_used)


class _Usage:
    """Stand-in for litellm's usage object (attribute access)."""

    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


def test_init_returns_fresh_instance_with_zeroed_counters():
    metadata = init_run_metadata()

    assert isinstance(metadata, RunMetadata)
    assert metadata.model_used is None
    assert metadata.fallback_used is False
    assert metadata.prompt_tokens == 0
    assert metadata.completion_tokens == 0
    assert metadata.total_tokens == 0
    assert metadata.num_ai_calls == 0
    assert metadata.has_token_usage is False
    assert metadata.duration_seconds >= 0


def test_init_replaces_previous_instance():
    first = init_run_metadata()
    record_model_used("model-a", is_fallback=True)

    second = init_run_metadata()

    assert second is not first
    assert get_run_metadata() is second
    assert second.model_used is None
    assert second.fallback_used is False


def test_record_model_used_tracks_model_and_fallback_flag():
    init_run_metadata()

    record_model_used("openai/gpt-5.4", is_fallback=True)

    metadata = get_run_metadata()
    assert metadata.model_used == "openai/gpt-5.4"
    assert metadata.fallback_used is True


def test_fallback_flag_is_sticky_once_a_fallback_was_used():
    init_run_metadata()

    record_model_used("fallback-model", is_fallback=True)
    record_model_used("primary-model", is_fallback=False)

    metadata = get_run_metadata()
    # last successful model wins, but the fallback flag must not be cleared
    assert metadata.model_used == "primary-model"
    assert metadata.fallback_used is True


def test_add_token_usage_accumulates_across_calls():
    init_run_metadata()

    add_token_usage(_Usage(100, 10, 110))
    add_token_usage(
        {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}
    )

    metadata = get_run_metadata()
    assert metadata.prompt_tokens == 105
    assert metadata.completion_tokens == 11
    assert metadata.total_tokens == 116
    assert metadata.has_token_usage is True


def test_add_token_usage_derives_total_when_missing():
    init_run_metadata()

    add_token_usage({"prompt_tokens": 20, "completion_tokens": 5})

    assert get_run_metadata().total_tokens == 25


def test_add_token_usage_ignores_none_and_partial_objects():
    init_run_metadata()

    add_token_usage(None)
    add_token_usage(object())

    metadata = get_run_metadata()
    assert metadata.total_tokens == 0
    assert metadata.has_token_usage is False


def test_record_ai_call_counts_calls_even_without_usage():
    init_run_metadata()

    record_ai_call(_Usage(10, 2, 12))
    record_ai_call(None)

    metadata = get_run_metadata()
    assert metadata.num_ai_calls == 2
    assert metadata.total_tokens == 12


def test_helpers_are_noops_when_not_initialized():
    from pr_agent.algo import run_metadata

    token = run_metadata._run_metadata.set(None)
    try:
        assert get_run_metadata() is None
        record_model_used("m", is_fallback=False)  # must not raise
        record_ai_call(_Usage(1, 1, 2))  # must not raise
        add_token_usage({"total_tokens": 5})  # must not raise
    finally:
        run_metadata._run_metadata.reset(token)
