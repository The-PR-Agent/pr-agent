"""The base-class diff-cache reset every provider inherits (#4372).

#3724 gave GitHub an unconditional reset; the contract lives in GitProvider now, so
any provider's auto-command boundary drops the whole diff cache — non-empty ones
included — and the incremental anchor, plus the request-scoped diff cache.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from starlette_context import context, request_cycle_context

from pr_agent.git_providers.git_provider import GitProvider, IncrementalPR


def _provider_with_state():
    provider = SimpleNamespace(
        diff_files=[MagicMock()],
        incremental=IncrementalPR(True),
    )
    return provider


def test_reset_drops_non_empty_cache_and_incremental_anchor():
    provider = _provider_with_state()

    GitProvider.reset_diff_cache_for_command(provider)

    assert provider.diff_files is None
    assert provider.incremental.is_incremental is False


def test_reset_pops_the_request_scoped_diff_cache():
    provider = _provider_with_state()

    with request_cycle_context({"diff_files": provider.diff_files}):
        GitProvider.reset_diff_cache_for_command(provider)
        assert context.get("diff_files", "gone") == "gone"


def test_reset_is_safe_outside_a_request_and_without_diff_state():
    provider = SimpleNamespace()

    # No request context and no diff/incremental attributes: still a clean no-op.
    GitProvider.reset_diff_cache_for_command(provider)

    assert provider.diff_files is None
    assert getattr(provider, "incremental", None) is None
