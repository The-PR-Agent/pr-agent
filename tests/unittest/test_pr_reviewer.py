from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pr_agent.tools.pr_reviewer import PRReviewer


def _make_reviewer():
    with patch.object(PRReviewer, "__init__", lambda self, *a, **kw: None):
        reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    return reviewer


class TestPRReviewerReviewRules:
    @patch("pr_agent.tools.pr_reviewer.get_settings")
    def test_get_review_rules_disabled_returns_empty(self, mock_get_settings):
        mock_get_settings.return_value = SimpleNamespace(
            pr_reviewer={"enable_review_rules": False}
        )
        reviewer = _make_reviewer()

        assert reviewer._get_review_rules() == ""

    @patch("pr_agent.tools.pr_reviewer.get_settings")
    def test_get_review_rules_invalid_paths_type_returns_empty(self, mock_get_settings):
        mock_get_settings.return_value = SimpleNamespace(
            pr_reviewer={
                "enable_review_rules": True,
                "review_rules_paths": 123,
                "max_review_rules_tokens": 0,
            }
        )
        reviewer = _make_reviewer()
        reviewer.git_provider.get_pr_base_ref.return_value = "main"

        assert reviewer._get_review_rules() == ""
        reviewer.git_provider.get_pr_file_content.assert_not_called()

    @patch("pr_agent.tools.pr_reviewer.get_settings")
    def test_get_review_rules_missing_base_ref_returns_empty(self, mock_get_settings):
        mock_get_settings.return_value = SimpleNamespace(
            pr_reviewer={
                "enable_review_rules": True,
                "review_rules_paths": [".pr_agent/review_rules.md"],
                "max_review_rules_tokens": 0,
            }
        )
        reviewer = _make_reviewer()
        reviewer.git_provider.get_pr_base_ref.return_value = ""

        assert reviewer._get_review_rules() == ""
        reviewer.git_provider.get_pr_file_content.assert_not_called()

    @patch("pr_agent.tools.pr_reviewer.get_settings")
    def test_get_review_rules_loads_and_concatenates_files(self, mock_get_settings):
        mock_get_settings.return_value = SimpleNamespace(
            pr_reviewer={
                "enable_review_rules": True,
                "review_rules_paths": [
                    ".pr_agent/review_rules.md",
                    ".github/review_rules.md",
                ],
                "max_review_rules_tokens": 0,
            }
        )
        reviewer = _make_reviewer()
        reviewer.git_provider.get_pr_base_ref.return_value = "main"

        def _get_file_content(path, ref):
            if path == ".pr_agent/review_rules.md":
                return "Rule A"
            if path == ".github/review_rules.md":
                return "Rule B"
            return ""

        reviewer.git_provider.get_pr_file_content.side_effect = _get_file_content

        review_rules = reviewer._get_review_rules()

        assert "File: `.pr_agent/review_rules.md`\nRule A" in review_rules
        assert "File: `.github/review_rules.md`\nRule B" in review_rules
        assert "\n\n---\n\n" in review_rules
