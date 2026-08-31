import tracemalloc

from pr_agent.algo.language_handler import build_language_file_matcher


def _assert_bounded_match(matcher, filename, expected_language):
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    baseline_memory, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        assert matcher(filename) == expected_language
        _, peak_memory = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    assert peak_memory - baseline_memory < 2_000_000


def test_matcher_uses_bounded_memory_for_heavily_dotted_filename():
    matcher = build_language_file_matcher({"Python": [".py"]})
    filename = ".".join(["segment"] * 1000) + ".py"

    _assert_bounded_match(matcher, filename, "Python")


def test_full_filename_tokens_do_not_expand_suffix_candidates():
    configured_filename = ".".join(["configured"] * 1000)
    matcher = build_language_file_matcher(
        {"Config": [configured_filename], "Python": [".py"]}
    )
    filename = ".".join(["segment"] * 1000) + ".py"

    _assert_bounded_match(matcher, filename, "Python")
