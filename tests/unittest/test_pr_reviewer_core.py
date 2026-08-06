from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _make_reviewer(git_provider=None):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = git_provider or MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    return reviewer


def _make_prediction_reviewer(git_provider=None):
    reviewer = _make_reviewer(git_provider)
    reviewer.token_handler = MagicMock()
    reviewer.remaining_files_list = []
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.prediction = None
    return reviewer


async def test_prepare_prediction_requests_remaining_files_and_preserves_tuple_result():
    reviewer = _make_prediction_reviewer()
    reviewer._get_prediction = AsyncMock(return_value="prediction")

    with patch(
        "pr_agent.tools.pr_reviewer.get_pr_diff",
        return_value=("diff", ["src/one.py", "docs/two.md"]),
    ) as get_pr_diff:
        await reviewer._prepare_prediction("model")

    get_pr_diff.assert_called_once_with(
        reviewer.git_provider,
        reviewer.token_handler,
        "model",
        add_line_numbers_to_hunks=True,
        disable_extra_lines=False,
        return_remaining_files=True,
    )
    assert reviewer.patches_diff == "diff"
    assert reviewer.remaining_files_list == ["src/one.py", "docs/two.md"]
    assert reviewer.prediction == "prediction"


async def test_prepare_prediction_accepts_full_diff_string_when_token_budget_is_sufficient():
    reviewer = _make_prediction_reviewer()
    reviewer._get_prediction = AsyncMock(return_value="prediction")

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value="diff"):
        await reviewer._prepare_prediction("model")

    assert reviewer.patches_diff == "diff"
    assert reviewer.remaining_files_list == []
    assert reviewer.prediction == "prediction"


async def test_prepare_prediction_keeps_incremental_review_compatible_with_tuple_result():
    reviewer = _make_prediction_reviewer()
    reviewer.incremental = SimpleNamespace(is_incremental=True)
    reviewer._get_prediction = AsyncMock(return_value="prediction")

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", ["skipped.py"])):
        await reviewer._prepare_prediction("model")

    assert reviewer.patches_diff == "diff"
    assert reviewer.remaining_files_list == ["skipped.py"]
    assert reviewer.prediction == "prediction"


def _render_review(reviewer, remaining_files, supports_gfm_markdown=False):
    reviewer.prediction = "review: {}"
    reviewer.remaining_files_list = remaining_files
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = supports_gfm_markdown
    reviewer.set_review_labels = MagicMock()

    with (
        patch("pr_agent.tools.pr_reviewer.load_yaml", return_value={"review": {}}),
        patch("pr_agent.tools.pr_reviewer.github_action_output"),
        patch("pr_agent.tools.pr_reviewer.convert_to_markdown_v2", return_value="original review"),
    ):
        return reviewer._prepare_pr_review()


def test_prepare_pr_review_appends_complete_coverage_footer():
    reviewer = _make_prediction_reviewer()

    review = _render_review(reviewer, ["src/one.py", "nested/two.md"])

    assert review.startswith("original review")
    assert "⚠️ **Review coverage:**" in review
    assert "- `src/one.py`" in review
    assert "- `nested/two.md`" in review


def test_prepare_pr_review_places_coverage_footer_before_help_text():
    reviewer = _make_prediction_reviewer()
    settings = get_settings()
    original_enable_help_text = settings.pr_reviewer.enable_help_text
    settings.pr_reviewer.enable_help_text = True

    try:
        with patch("pr_agent.tools.pr_reviewer.HelpMessage.get_review_usage_guide", return_value="help text"):
            review = _render_review(reviewer, ["skipped.py"], supports_gfm_markdown=True)
    finally:
        settings.pr_reviewer.enable_help_text = original_enable_help_text

    assert review.index("⚠️ **Review coverage:**") < review.index("help text")


def test_prepare_pr_review_leaves_original_content_unchanged_without_remaining_files():
    reviewer = _make_prediction_reviewer()

    review = _render_review(reviewer, [])

    assert review == "original review"
    assert "Review coverage" not in review


def test_prepare_pr_review_limits_coverage_footer_to_50_files():
    reviewer = _make_prediction_reviewer()
    remaining_files = [f"file_{index}.py" for index in range(51)]

    review = _render_review(reviewer, remaining_files)

    assert review.count("- `file_") == 50
    assert "- `file_0.py`" in review
    assert "- `file_49.py`" in review
    assert "- `file_50.py`" not in review


def test_prepare_pr_review_reports_number_of_files_beyond_coverage_limit():
    reviewer = _make_prediction_reviewer()
    remaining_files = [f"file_{index}.py" for index in range(53)]

    review = _render_review(reviewer, remaining_files)

    assert "... and 3 more" in review
    assert "- `file_50.py`" not in review


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
