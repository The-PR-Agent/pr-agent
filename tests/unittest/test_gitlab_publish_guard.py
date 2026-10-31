"""Skip suggestions without a usable source line instead of indexing unavailable content."""
import re
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers import gitlab_provider
from pr_agent.git_providers.gitlab_provider import GitLabProvider

HEAD = "line1\nline2\nline3"


def _provider(monkeypatch, head=HEAD, patch=""):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_provider, "get_logger", lambda: logger)
    monkeypatch.setattr(gitlab_provider, "get_settings",
                        lambda: {"gitlab.publish_code_suggestions_as_review": False})
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.RE_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[ ]?(.*)")
    provider.resolve_outdated_inline_threads = lambda: None
    provider.get_diff_files = lambda: [FilePatchInfo(base_file="", head_file=head, patch=patch,
                                                     filename="a.py", edit_type=EDIT_TYPE.MODIFIED)]
    provider.sent = []
    def send(*args, **kwargs):
        provider.sent.append(args)
        return True
    provider.send_inline_comment = send
    return provider, logger


def _suggestion(start=2, end=3):
    return {'body': '```suggestion\nfixed\n```', 'relevant_file': 'a.py',
            'relevant_lines_start': start, 'relevant_lines_end': end}


@pytest.mark.parametrize("head,start,patch", [
    ("", 2, ""),
    (HEAD, 99, ""),
    (HEAD, 0, ""),
    ("", 2, "@@ -1 +1 @@\n-old\n+new\n"),
    (HEAD, 99, "@@ -99 +99 @@\n-old\n+new\n"),
])
def test_unpublishable_suggestion_is_skipped_with_warning(monkeypatch, head, start, patch):
    """Skip suggestions without a usable source line or with invalid line numbers."""
    provider, logger = _provider(monkeypatch, head, patch)

    assert provider.publish_code_suggestions([_suggestion(start=start, end=start)]) is False
    assert provider.sent == []
    warned = [c[0][0] for c in logger.warning.call_args_list]
    assert any("Skipping suggestion" in w for w in warned)
    logger.exception.assert_not_called()


def test_populated_head_file_still_publishes(monkeypatch):
    """Keep the existing behaviour where head content is present."""
    provider, _ = _provider(monkeypatch)

    assert provider.publish_code_suggestions([_suggestion()]) is True
    assert len(provider.sent) == 1
    assert provider.sent[0][4] == "line2"
