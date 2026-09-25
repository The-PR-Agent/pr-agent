"""Skip suggestions that point past blanked head content instead of indexing the file."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers import gitlab_provider
from pr_agent.git_providers.gitlab_provider import GitLabProvider

HEAD = "line1\nline2\nline3"


def _provider(monkeypatch, head=HEAD):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_provider, "get_logger", lambda: logger)
    monkeypatch.setattr(gitlab_provider, "get_settings",
                        lambda: {"gitlab.publish_code_suggestions_as_review": False})
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.resolve_outdated_inline_threads = lambda: None
    provider.get_diff_files = lambda: [FilePatchInfo(base_file="", head_file=head, patch="",
                                                     filename="a.py", edit_type=EDIT_TYPE.MODIFIED)]
    provider.mr = MagicMock()
    provider.id_mr = 1
    provider.get_relevant_diff = MagicMock(return_value=SimpleNamespace(
        base_commit_sha="base", start_commit_sha="start", head_commit_sha="head"))
    return provider, logger


def _suggestion(start=2, end=3):
    return {'body': '```suggestion\nfixed\n```', 'relevant_file': 'a.py',
            'relevant_lines_start': start, 'relevant_lines_end': end}


@pytest.mark.parametrize("head,start", [("", 2), (HEAD, 99), (HEAD, 0)])
def test_unpublishable_suggestion_is_skipped_with_warning(monkeypatch, head, start):
    """Blank head, out-of-range, or zero line numbers must not raise IndexError."""
    provider, logger = _provider(monkeypatch, head)

    assert provider.publish_code_suggestions([_suggestion(start=start, end=start)]) is False
    provider.mr.discussions.create.assert_not_called()
    warned = [c[0][0] for c in logger.warning.call_args_list]
    assert any("Skipping suggestion" in w for w in warned)


def test_populated_head_file_still_publishes(monkeypatch):
    """Keep the existing behaviour where head content is present."""
    provider, _ = _provider(monkeypatch)

    assert provider.publish_code_suggestions([_suggestion()]) is True
    provider.mr.discussions.create.assert_called_once()
    provider.get_relevant_diff.assert_called_once_with("a.py", "line2")
