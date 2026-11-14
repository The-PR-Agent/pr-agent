"""Verify publication errors protect instruction-bearing data across providers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pr_agent.git_providers.azuredevops_provider as azure_module
import pr_agent.git_providers.bitbucket_provider as bitbucket_module
import pr_agent.git_providers.bitbucket_server_provider as bitbucket_server_module
import pr_agent.git_providers.codecommit_provider as codecommit_module
import pr_agent.git_providers.gerrit_provider as gerrit_module
import pr_agent.git_providers.git_provider as base_module
import pr_agent.git_providers.gitea_provider as gitea_module
from pr_agent.algo.ai_handlers.litellm_ai_handler import withhold_prompt_logging

SECRET = "Repository instructions echoed inside a suggestion or API exception"


@pytest.mark.parametrize(
    "module, provider_class",
    [
        (base_module, base_module.GitProvider),
        (bitbucket_module, bitbucket_module.BitbucketProvider),
        (bitbucket_server_module, bitbucket_server_module.BitbucketServerProvider),
    ],
)
def test_shared_and_bitbucket_publication_errors_withhold_sensitive_details(monkeypatch, module, provider_class):
    logger = MagicMock()
    monkeypatch.setattr(module, "get_logger", lambda: logger)

    with withhold_prompt_logging(True):
        provider_class._log_code_suggestion_publish_error(object(), RuntimeError(SECRET))

    logger.error.assert_called_once()
    assert SECRET not in str(logger.mock_calls)


def test_azure_inline_publish_failure_omits_structured_suggestion_and_exception(monkeypatch):
    logger = MagicMock()
    settings = SimpleNamespace(
        get=lambda *_args, **_kwargs: False,
        azure_devops=SimpleNamespace(get=lambda _key, default=None: default),
    )
    monkeypatch.setattr(azure_module, "get_logger", lambda: logger)
    monkeypatch.setattr(azure_module, "get_settings", lambda: settings)
    client = SimpleNamespace(create_thread=MagicMock(side_effect=RuntimeError(SECRET)))
    provider = SimpleNamespace(
        azure_devops_client=client,
        workspace_slug="workspace",
        repo_slug="repo",
        pr_num=123,
        _resolve_diff_file_path=lambda file_path: file_path,
    )
    suggestion = {
        "body": SECRET,
        "relevant_file": "src/app.py",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "fallback_to_pr_comment": False,
    }

    with withhold_prompt_logging(True):
        result = azure_module.AzureDevopsProvider.publish_code_suggestions(provider, [suggestion])

    assert result is False
    client.create_thread.assert_called_once()
    logger.error.assert_called_once()
    assert SECRET not in str(logger.mock_calls)
    assert not logger.exception.called


def test_bitbucket_invalid_inline_comment_withholds_payload(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(bitbucket_module, "get_logger", lambda: logger)

    with withhold_prompt_logging(True):
        result = bitbucket_module.BitbucketProvider.publish_inline_comments(object(), [{"body": SECRET}])

    assert result is True
    logger.error.assert_called_once()
    assert SECRET not in str(logger.mock_calls)


def test_codecommit_publication_failure_withholds_exception_and_model_file(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(codecommit_module, "get_logger", lambda: logger)
    client = SimpleNamespace(publish_comment=MagicMock(side_effect=RuntimeError(SECRET)))
    provider = SimpleNamespace(
        pr_num=123,
        codecommit_client=client,
        _is_valid_code_suggestion=lambda _suggestion: True,
        _prepare_comment_body=lambda body: body,
        _get_target_contexts_for_file=lambda _file: [{
            "repository_name": "repo",
            "destination_commit": "base",
            "source_commit": "head",
        }],
    )
    suggestion = {"body": SECRET, "relevant_file": SECRET, "relevant_lines_start": 1}

    with withhold_prompt_logging(True):
        result = codecommit_module.CodeCommitProvider.publish_code_suggestions(provider, [suggestion])

    assert result is False
    client.publish_comment.assert_called_once()
    assert SECRET not in str(logger.mock_calls)


def test_gitea_inline_failure_withholds_api_exception():
    logger = MagicMock()
    api = SimpleNamespace(create_inline_comment=MagicMock(side_effect=RuntimeError(SECRET)))
    provider = SimpleNamespace(
        repo_api=api,
        owner="owner",
        repo="repo",
        pr_number=123,
        enabled_pr=True,
        last_commit=None,
        logger=logger,
    )

    with withhold_prompt_logging(True):
        result = gitea_module.GiteaProvider.publish_inline_comments(provider, [{"body": SECRET}])

    assert result is False
    logger.error.assert_called_once()
    assert SECRET not in str(logger.mock_calls)


@pytest.mark.parametrize("guarded", [True, False])
def test_azure_repo_instruction_fetch_error_respects_request_log_guard(monkeypatch, guarded):
    """An instruction-derived import path must not appear in Azure diagnostics."""
    logger = MagicMock()
    monkeypatch.setattr(azure_module, "get_logger", lambda: logger)
    monkeypatch.setattr(azure_module, "get_verbosity_level", lambda: 2)
    path = f"private/{SECRET}.md"
    client = SimpleNamespace(get_item=MagicMock(side_effect=RuntimeError(f"Failed to read {path}: {SECRET}")))
    provider = SimpleNamespace(azure_devops_client=client, repo_slug="repo", workspace_slug="project")

    with withhold_prompt_logging(guarded):
        with pytest.raises(RuntimeError, match="Failed to read"):
            azure_module.AzureDevopsProvider.get_repo_file_content(provider, path, from_default_branch=True)

    assert client.get_item.call_count == 1
    assert (SECRET in str(logger.mock_calls)) is not guarded
    assert "Failed to load repo file" in str(logger.mock_calls)


@pytest.mark.parametrize("guarded", [True, False])
def test_gerrit_traversal_warning_withholds_generated_file_path(monkeypatch, tmp_path, guarded):
    logger = MagicMock()
    monkeypatch.setattr(gerrit_module, "get_logger", lambda: logger)
    provider = SimpleNamespace(repo_path=str(tmp_path))
    suggestion = {"relevant_file": f"../{SECRET}", "body": SECRET}

    with withhold_prompt_logging(guarded):
        result = gerrit_module.GerritProvider.publish_code_suggestions(provider, [suggestion])

    assert result is True
    logger.warning.assert_called_once()
    assert "Skipping suggestion with path traversal" in str(logger.mock_calls)
    assert (SECRET in str(logger.mock_calls)) is not guarded


@pytest.mark.parametrize("guarded", [True, False])
def test_bitbucket_server_missing_import_does_not_log_instruction_path(monkeypatch, guarded):
    logger = MagicMock()
    monkeypatch.setattr(bitbucket_server_module, "get_logger", lambda: logger)
    path = f"internal/{SECRET}.md"
    missing = bitbucket_server_module.HTTPError(
        "Not Found", response=SimpleNamespace(status_code=404)
    )
    client = SimpleNamespace(get_content_of_file=MagicMock(side_effect=missing))
    provider = SimpleNamespace(
        bitbucket_client=client, workspace_slug="org", repo_slug="repo"
    )

    with withhold_prompt_logging(guarded):
        result = bitbucket_server_module.BitbucketServerProvider.get_file(
            provider, path, "target-commit"
        )

    assert result == ""
    client.get_content_of_file.assert_called_once()
    logger.debug.assert_called_once()
    assert (SECRET in str(logger.mock_calls)) is not guarded
    assert "not found" in str(logger.mock_calls)


@pytest.mark.parametrize("guarded", [True, False])
def test_gerrit_publish_error_redacts_logs(monkeypatch, tmp_path, guarded):
    logger = MagicMock()
    monkeypatch.setattr(gerrit_module, "get_logger", lambda: logger)
    monkeypatch.setattr(gerrit_module, "add_suggestion", lambda *_args: None)
    monkeypatch.setattr(gerrit_module, "diff", lambda **_kwargs: "patch")
    monkeypatch.setattr(gerrit_module, "upload_patch", lambda *_args: "uploaded-path")
    monkeypatch.setattr(gerrit_module, "reset_local_changes", lambda *_args: None)
    monkeypatch.setattr(
        gerrit_module, "add_comment", MagicMock(side_effect=RuntimeError(SECRET))
    )
    provider = SimpleNamespace(
        repo_path=str(tmp_path),
        refspec="revision",
        parsed_url="https://example.com/review",
        _is_valid_code_suggestion=lambda _suggestion: True,
        split_suggestion=lambda _body: ("summary", "patch"),
    )
    suggestion = {
        "relevant_file": "file.py",
        "body": SECRET,
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }

    with withhold_prompt_logging(guarded):
        result = gerrit_module.GerritProvider.publish_code_suggestions(provider, [suggestion])

    assert result is False
    assert (SECRET in str(logger.mock_calls)) is not guarded
    assert logger.error.called is guarded
    assert logger.exception.called is not guarded


@pytest.mark.parametrize("guarded", [True, False])
def test_gerrit_reset_error_redacts_details(monkeypatch, tmp_path, guarded):
    logger = MagicMock()
    monkeypatch.setattr(gerrit_module, "get_logger", lambda: logger)
    monkeypatch.setattr(gerrit_module, "add_suggestion", lambda *_args: None)
    monkeypatch.setattr(gerrit_module, "diff", lambda **_kwargs: "patch")
    monkeypatch.setattr(
        gerrit_module, "upload_patch", MagicMock(side_effect=RuntimeError(SECRET))
    )
    cleanup_error = RuntimeError("reset failed")
    cleanup_error.stderr = SECRET
    monkeypatch.setattr(gerrit_module, "reset_local_changes", MagicMock(side_effect=cleanup_error))
    provider = SimpleNamespace(
        repo_path=str(tmp_path),
        refspec="revision",
        _is_valid_code_suggestion=lambda _suggestion: True,
        split_suggestion=lambda _body: ("summary", "patch"),
    )
    suggestion = {
        "relevant_file": "file.py",
        "body": SECRET,
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }

    with withhold_prompt_logging(guarded):
        with pytest.raises(RuntimeError, match="Repository instructions"):
            gerrit_module.GerritProvider.publish_code_suggestions(provider, [suggestion])

    logger.warning.assert_called_once()
    assert (SECRET in str(logger.mock_calls)) is not guarded
    assert (str(tmp_path) in str(logger.mock_calls)) is not guarded
