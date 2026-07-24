from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _make_reviewer(git_provider=None):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = git_provider or MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    return reviewer


def test_should_publish_review_no_suggestions_respects_config():
    reviewer = _make_reviewer()
    settings = get_settings()
    original_publish_no_suggestions = settings.pr_reviewer.publish_output_no_suggestions

    try:
        settings.pr_reviewer.publish_output_no_suggestions = False
        assert reviewer._should_publish_review_no_suggestions("No major issues detected") is False
        assert reviewer._should_publish_review_no_suggestions("A major issue was detected") is True

        settings.pr_reviewer.publish_output_no_suggestions = True
        assert reviewer._should_publish_review_no_suggestions("No major issues detected") is True
    finally:
        settings.pr_reviewer.publish_output_no_suggestions = original_publish_no_suggestions


def test_can_run_incremental_review_skips_auto_mode_without_new_commit():
    reviewer = _make_reviewer()
    reviewer.is_auto = True
    reviewer.incremental = SimpleNamespace(first_new_commit_sha=None)

    assert reviewer._can_run_incremental_review() is False


def test_set_review_labels_replaces_stale_review_labels_and_keeps_user_labels():
    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "require_estimate_effort_to_review": settings.pr_reviewer.require_estimate_effort_to_review,
        "require_security_review": settings.pr_reviewer.require_security_review,
        "enable_review_labels_effort": settings.pr_reviewer.enable_review_labels_effort,
        "enable_review_labels_security": settings.pr_reviewer.enable_review_labels_security,
    }
    settings.config.publish_output = True
    settings.pr_reviewer.require_estimate_effort_to_review = True
    settings.pr_reviewer.require_security_review = True
    settings.pr_reviewer.enable_review_labels_effort = True
    settings.pr_reviewer.enable_review_labels_security = True
    git_provider = MagicMock()
    git_provider.get_pr_labels.return_value = ["Review effort 1/5", "Possible security concern", "keep-me"]
    reviewer = _make_reviewer(git_provider)
    data = {
        "review": {
            "estimated_effort_to_review_[1-5]": "3, moderate",
            "security_concerns": "yes",
        }
    }

    try:
        reviewer.set_review_labels(data)

        git_provider.publish_labels.assert_called_once_with([
            "Review effort 3/5",
            "Possible security concern",
            "keep-me",
        ])
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.pr_reviewer.require_estimate_effort_to_review = original["require_estimate_effort_to_review"]
        settings.pr_reviewer.require_security_review = original["require_security_review"]
        settings.pr_reviewer.enable_review_labels_effort = original["enable_review_labels_effort"]
        settings.pr_reviewer.enable_review_labels_security = original["enable_review_labels_security"]


def test_get_user_answers_collects_question_and_answer_from_issue_comments():
    git_provider = MagicMock()
    git_provider.get_issue_comments.return_value = SimpleNamespace(reversed=[
        SimpleNamespace(body="Unrelated"),
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ])
    reviewer = _make_reviewer(git_provider)
    reviewer.is_answer = True

    question, answer = reviewer._get_user_answers()

    assert question == "Questions to better understand the PR:\n- Why?"
    assert answer == "/answer Because it fixes production."


@pytest.mark.parametrize("persistent", [True, False])
@pytest.mark.parametrize("thread_enabled", [True, False])
async def test_run_threads_only_the_final_review_comment(monkeypatch, persistent, thread_enabled):
    """`as_thread` is forwarded to the review's final publish call only when the provider opts in
    (should_publish_review_as_thread), and is omitted entirely otherwise - other providers'
    publish methods don't accept it. Status/progress comments are never threaded.
    """
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    git_provider = MagicMock()
    git_provider.should_publish_review_as_thread.return_value = thread_enabled
    reviewer = _make_reviewer(git_provider)
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    reviewer.prediction = None
    review_text = "## PR Reviewer Guide 🔍\n\nsome findings"
    reviewer._prepare_pr_review = lambda: review_text

    async def fake_extract_tickets(git_provider, vars):
        return None

    async def fake_retry(prepare_fn, model_type=None):
        reviewer.prediction = "prediction"

    monkeypatch.setattr(pr_reviewer_module, "extract_and_cache_pr_tickets", fake_extract_tickets)
    monkeypatch.setattr(pr_reviewer_module, "retry_with_fallback_models", fake_retry)

    settings = get_settings()
    original = {
        "publish_output": settings.config.publish_output,
        "persistent_comment": settings.pr_reviewer.persistent_comment,
        "is_auto_command": settings.config.get("is_auto_command", False),
    }
    try:
        settings.config.publish_output = True
        settings.config.is_auto_command = False
        settings.pr_reviewer.persistent_comment = persistent

        await reviewer.run()
    finally:
        settings.config.publish_output = original["publish_output"]
        settings.config.is_auto_command = original["is_auto_command"]
        settings.pr_reviewer.persistent_comment = original["persistent_comment"]

    if persistent:
        publish = git_provider.publish_persistent_comment
        publish.assert_called_once()
    else:
        publish = git_provider.publish_comment
    assert publish.call_args.args[0] == review_text
    if thread_enabled:
        assert publish.call_args.kwargs.get("as_thread") is True
    else:
        assert "as_thread" not in publish.call_args.kwargs
    # The temporary progress comment is published without as_thread regardless of the flag.
    git_provider.publish_comment.assert_any_call("Preparing review...", is_temporary=True)


def test_init_maps_user_question_and_answer_to_correct_prompt_vars(monkeypatch):
    """Behavioral regression for the swapped-unpacking bug (#2496).

    The bug lived in ``PRReviewer.__init__``: ``_get_user_answers()`` returns
    ``(question, answer)`` but the tuple was unpacked as ``answer, question``,
    so the review prompt rendered the user's answer under ``{{ question_str }}``
    and the question under ``{{ answer_str }}``. This drives the real ``__init__``
    (external collaborators stubbed) and asserts each value lands in ``self.vars``
    under the correct key — so it fails if the unpack is ever swapped again,
    regardless of how the line is formatted.
    """
    from pr_agent.tools import pr_reviewer as pr_reviewer_module

    provider = MagicMock()
    provider.is_supported.return_value = True
    provider.get_languages.return_value = {}
    provider.get_files.return_value = []
    provider.get_issue_comments.return_value = SimpleNamespace(reversed=[
        SimpleNamespace(body="Questions to better understand the PR:\n- Why?"),
        SimpleNamespace(body="/answer Because it fixes production."),
    ])
    provider.get_pr_description.return_value = ("desc", [])

    monkeypatch.setattr(pr_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_reviewer_module, "TokenHandler", MagicMock())

    reviewer = PRReviewer(
        "https://example/pr/1",
        is_answer=True,
        ai_handler=lambda: SimpleNamespace(main_pr_language=None),
    )

    assert reviewer.vars["question_str"] == "Questions to better understand the PR:\n- Why?"
    assert reviewer.vars["answer_str"] == "/answer Because it fixes production."
