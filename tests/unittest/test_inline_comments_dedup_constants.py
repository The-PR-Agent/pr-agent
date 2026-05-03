"""Smoke tests for resolve-outdated constants and base GitProvider defaults."""

from pr_agent.algo.inline_comments_dedup import (
    RESOLVED_BODY_MARKER,
    RESOLVED_NOTE,
)


def test_resolved_marker_is_html_comment():
    assert RESOLVED_BODY_MARKER.startswith("<!--")
    assert RESOLVED_BODY_MARKER.endswith("-->")
    assert "\n" not in RESOLVED_BODY_MARKER


def test_resolved_marker_detectable_only_when_present():
    body_with = "some comment body\n\n---\n_" + RESOLVED_NOTE + "_\n" + RESOLVED_BODY_MARKER
    body_without = "an unrelated comment that quotes <!-- pr-agent-inline-id:abc123def456 -->"
    assert RESOLVED_BODY_MARKER in body_with
    assert RESOLVED_BODY_MARKER not in body_without


def test_base_provider_defaults_return_false():
    from pr_agent.git_providers.git_provider import GitProvider

    # GitProvider is abstract; verify defaults via the unbound methods.
    assert GitProvider.resolve_review_thread(None, {"id": 1}) is False
    assert GitProvider.unresolve_review_thread(None, {"id": 1}) is False
