"""Check request-scoped logging during GitLab documentation publication."""

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

from gitlab.exceptions import GitlabError

import pr_agent.git_providers.gitlab_provider as gitlab_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import withhold_prompt_logging
from pr_agent.algo.prompt_log_guard import prompt_logs_withheld

SECRET = "Sensitive repository instruction echoed in documentation"


def test_gitlab_inline_publication_withholds_suggestion_body_from_logs(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        gitlab_module, "get_settings", lambda: SimpleNamespace(get=lambda *_args, **_kwargs: False)
    )
    diff = SimpleNamespace(base_commit_sha="base", start_commit_sha="start", head_commit_sha="head")
    provider = SimpleNamespace(
        id_mr=123,
        get_relevant_diff=MagicMock(return_value=diff),
        _create_suggestion_note=MagicMock(return_value=True),
    )
    target = SimpleNamespace(filename="src/app.py", old_filename=None)
    arguments = (SECRET, "addition", True, "src/app.py", "def f():", 1, target, 1)

    with withhold_prompt_logging(True):
        assert prompt_logs_withheld()
        assert gitlab_module.GitLabProvider.send_inline_comment(provider, *arguments) is True

    assert SECRET not in str(logger.mock_calls)
    assert provider._create_suggestion_note.call_args.args[1] == SECRET
    assert not prompt_logs_withheld()

    logger.reset_mock()
    assert gitlab_module.GitLabProvider.send_inline_comment(provider, *arguments) is True
    assert SECRET in str(logger.debug.call_args_list)


def test_gitlab_publication_failure_withholds_suggestion_and_exception(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        gitlab_module, "get_settings", lambda: SimpleNamespace(get=lambda *_args, **_kwargs: False)
    )
    patch = "@@ -0,0 +1,1 @@\n+value\n"
    file = SimpleNamespace(filename="src/app.py", head_file="value\n", patch=patch)
    provider = SimpleNamespace(
        id_mr=123,
        RE_HUNK_HEADER=re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"),
        resolve_outdated_inline_threads=MagicMock(),
        reconcile_code_suggestion_threads=MagicMock(),
        get_diff_files=MagicMock(return_value=[file]),
        send_inline_comment=MagicMock(side_effect=RuntimeError(SECRET)),
    )
    suggestion = {
        "body": SECRET,
        "relevant_file": "src/app.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }

    with withhold_prompt_logging(True):
        assert gitlab_module.GitLabProvider.publish_code_suggestions(provider, [suggestion]) is False

    provider.send_inline_comment.assert_called_once()
    assert SECRET not in str(logger.mock_calls)
    logger.error.assert_called_once()
    assert "RuntimeError at " in str(logger.error.call_args)
    assert not logger.warning.called
    assert not logger.exception.called


def test_gitlab_failed_inline_comment_keeps_error_severity_without_sensitive_content(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_module, "get_logger", lambda: logger)
    provider = SimpleNamespace(
        id_mr=123,
        mr=SimpleNamespace(draft_notes=SimpleNamespace(create=MagicMock(side_effect=GitlabError(SECRET)))),
        get_line_link=MagicMock(return_value="https://example.test/src/app.py"),
    )
    diff = SimpleNamespace(base_commit_sha="base", start_commit_sha="start", head_commit_sha="head")
    target = SimpleNamespace(filename="src/app.py")
    original = {
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "existing_code": "old",
        "improved_code": "new",
        "suggestion_content": "Documentation",
        "label": "docs",
    }

    with withhold_prompt_logging(True):
        assert gitlab_module.GitLabProvider._create_suggestion_note(
            provider, True, SECRET, {}, diff, target, "src/app.py", original, None, None, None
        ) is False

    logger.error.assert_called_once()
    assert "GitlabError at " in str(logger.error.call_args)
    assert SECRET not in str(logger.mock_calls)
    assert not logger.warning.called
    assert not logger.exception.called


def test_gitlab_draft_bulk_publish_failure_keeps_error_severity(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        gitlab_module,
        "get_settings",
        lambda: SimpleNamespace(get=lambda key, default=False: key == "gitlab.publish_code_suggestions_as_review"),
    )
    patch = "@@ -0,0 +1,1 @@\n+value\n"
    file = SimpleNamespace(filename="src/app.py", head_file="value\n", patch=patch)
    provider = SimpleNamespace(
        id_mr=123,
        RE_HUNK_HEADER=re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"),
        resolve_outdated_inline_threads=MagicMock(),
        reconcile_code_suggestion_threads=MagicMock(),
        get_diff_files=MagicMock(return_value=[file]),
        send_inline_comment=MagicMock(return_value=True),
        _publish_suggestion_drafts=MagicMock(side_effect=GitlabError(SECRET)),
        mr=SimpleNamespace(draft_notes=SimpleNamespace(list=MagicMock(return_value=[SimpleNamespace(note=SECRET)]))),
    )
    suggestion = {
        "body": SECRET,
        "relevant_file": "src/app.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }

    with withhold_prompt_logging(True):
        assert gitlab_module.GitLabProvider.publish_code_suggestions(provider, [suggestion]) is False

    provider.send_inline_comment.assert_called_once()
    logger.error.assert_called_once()
    assert "remain pending" in str(logger.error.call_args)
    assert not logger.warning.called
    assert not logger.exception.called
    assert SECRET not in str(logger.mock_calls)


def test_gitlab_validation_warnings_withhold_generated_filenames(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(gitlab_module, "get_logger", lambda: logger)
    monkeypatch.setattr(
        gitlab_module, "get_settings", lambda: SimpleNamespace(get=lambda *_args, **_kwargs: False)
    )
    file = SimpleNamespace(filename=SECRET, head_file="value\n", patch="")
    provider = SimpleNamespace(
        id_mr=123,
        resolve_outdated_inline_threads=MagicMock(),
        reconcile_code_suggestion_threads=MagicMock(),
        get_diff_files=MagicMock(return_value=[file]),
        send_inline_comment=MagicMock(),
    )
    suggestion = {
        "body": "Generated documentation",
        "relevant_file": SECRET,
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }
    for kind in ("missing", "invalid_range", "out_of_range", "no_source"):
        provider.get_diff_files.return_value = [] if kind == "missing" else [file]
        file.head_file = "" if kind == "no_source" else "value\n"
        suggestion["relevant_lines_start"] = {
            "missing": 1, "invalid_range": 0, "out_of_range": 10, "no_source": 1,
        }[kind]
        logger.reset_mock()
        with withhold_prompt_logging(True):
            assert gitlab_module.GitLabProvider.publish_code_suggestions(provider, [suggestion]) is False
        assert logger.warning.called, kind
        assert SECRET not in str(logger.mock_calls), kind
        assert not provider.send_inline_comment.called

        logger.reset_mock()
        assert gitlab_module.GitLabProvider.publish_code_suggestions(provider, [suggestion]) is False
        assert SECRET in str(logger.warning.call_args), kind
