from unittest.mock import MagicMock, patch

from pr_agent.algo.inline_comments_dedup import generate_marker
from pr_agent.tools.pr_add_docs import PRAddDocs


def test_push_inline_docs_emits_markerable_original_suggestion():
    add_docs = PRAddDocs.__new__(PRAddDocs)
    add_docs.git_provider = MagicMock()
    add_docs.git_provider.publish_code_suggestions = MagicMock(return_value=True)
    add_docs.dedent_code = MagicMock(return_value='"""Explain foo."""\nfoo()')

    fake_settings = MagicMock()
    fake_settings.config.verbosity_level = 0

    data = {
        "Code Documentation": [
            {
                "relevant file": "src/app.py",
                "relevant line": 12,
                "documentation": '"""Explain foo."""',
                "doc placement": "before",
            }
        ]
    }

    with patch("pr_agent.tools.pr_add_docs.get_settings", return_value=fake_settings):
        add_docs.push_inline_docs(data)

    add_docs.git_provider.publish_code_suggestions.assert_called_once()
    docs = add_docs.git_provider.publish_code_suggestions.call_args[0][0]
    assert len(docs) == 1
    suggestion = docs[0]
    original = suggestion["original_suggestion"]
    assert original["relevant_file"] == "src/app.py"
    assert original["relevant_lines_start"] == 12
    assert original["relevant_lines_end"] == 12
    assert original["label"] == "documentation"
    assert original["suggestion_content"] == "Proposed documentation"
    assert original["improved_code"] == '"""Explain foo."""\nfoo()'
    assert generate_marker(original) is not None
