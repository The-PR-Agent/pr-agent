"""Keep synthetic CI credentials out of model context and diagnostics."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import pr_agent.algo.artifacts as artifacts
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import redact_credentials
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.mark.parametrize("content, credential, kind", [
    ("Authorization: Bearer synthetic-bearer-value", "synthetic-bearer-value", "authorization_header"),
    ("https://ci-user:synthetic-password@example.com/log", "synthetic-password", "url_userinfo"),
    ("token=glpat-synthetic_token_for_tests", "glpat-synthetic_token_for_tests", "gitlab_token"),
    ("key=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    ("AWS_SECRET_ACCESS_KEY=" + "x" * 40, "x" * 40, "credential_assignment"),
    ('"aws_session_token": "synthetic-session-value"', "synthetic-session-value", "credential_assignment"),
    ('"SecretAccessKey": "synthetic-secret-value"', "synthetic-secret-value", "credential_assignment"),
    ('"SessionToken": "synthetic-session-value"', "synthetic-session-value", "credential_assignment"),
    ("Authorization: AWS4-HMAC-SHA256 Credential=synthetic-key, Signature=synthetic-signature",
     "synthetic-signature", "authorization_header"),
])
def test_load_artifact_redacts_credentials_and_reports_only_counts(tmp_path, monkeypatch, content, credential, kind):
    path = tmp_path / "ci.log"
    path.write_text(content + "\nFAILED test_boundary: expected 3, got 4", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(artifacts, "get_settings", lambda: SimpleNamespace(get=lambda *_: {
        "enable": True, "artifact_path": str(path), "max_artifact_size": 2000,
    }))
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts.load_artifact()

    assert credential not in context
    assert "FAILED test_boundary: expected 3, got 4" in context
    assert "<redacted>" in context or kind == "url_userinfo"
    logger.warning.assert_called_once()
    diagnostics = str(logger.warning.call_args)
    assert kind in diagnostics
    assert "1" in diagnostics
    assert credential not in diagnostics
    assert "FAILED test_boundary" not in diagnostics


def test_redaction_happens_before_truncating_a_credential(tmp_path):
    path = tmp_path / "ci.log"
    path.write_text("glpat-" + "synthetic" * 40 + " tail", encoding="utf-8")

    context = artifacts._read_and_truncate(path, 55)

    assert "glpat-" not in context
    assert "truncated" in context
    assert len(context) <= 55


def test_safe_artifact_is_unchanged_and_has_no_redaction_warning(tmp_path, monkeypatch):
    path = tmp_path / "ci.log"
    content = "FAILED test_boundary: expected 3, got 4\nBuild finished."
    path.write_text(content, encoding="utf-8")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    assert artifacts._read_and_truncate(path, 2000) == content
    logger.warning.assert_not_called()


def test_truncated_long_url_does_not_expose_partial_userinfo(tmp_path):
    path = tmp_path / "ci.log"
    path.write_text("Build log: https://ci-user:" + "synthetic-password" * 1000 + "@example.com/log", encoding="utf-8")

    context = artifacts._read_and_truncate(path, 120)

    assert "ci-user" not in context
    assert "synthetic-password" not in context
    assert "Build log:" in context
    assert "truncated" in context
    assert len(context) <= 120


@pytest.mark.parametrize("length", [80, 120 + 511])
def test_log_ending_inside_url_userinfo_is_masked_at_any_length(tmp_path, length):
    path = tmp_path / "ci.log"
    content = "Build log: https://ci-user:synthetic-password"
    path.write_text(content + "x" * (length - len(content)), encoding="utf-8")

    context = artifacts._read_and_truncate(path, 120)

    assert "ci-user" not in context
    assert "synthetic-password" not in context
    assert "Build log:" in context
    assert len(context) <= 120


def test_shared_redactor_counts_each_type_and_does_not_recount_masked_headers():
    content = (
        "Authorization: Bearer synthetic-header\n"
        "Authorization: Basic synthetic-basic\n"
        "glpat-synthetic_one glpat-synthetic_two\n"
        "ASIAIOSFODNN7EXAMPLE\n"
        "AWS_SECRET_ACCESS_KEY=synthetic-secret"
    )
    counts = {}
    redacted = redact_credentials(content, redaction_counts=counts)

    assert counts == {"authorization_header": 2, "gitlab_token": 2,
                      "aws_access_key": 1, "credential_assignment": 1}
    assert "synthetic" not in redacted
    repeated_counts = {}
    assert redact_credentials(redacted, redaction_counts=repeated_counts) == redacted
    assert repeated_counts == {}


def test_injected_and_reapplied_artifacts_use_redacted_context(tmp_path, monkeypatch):
    keys = ("artifacts", "pr_reviewer.extra_instructions", "pr_description.extra_instructions",
            "pr_code_suggestions.extra_instructions")
    snapshot = snapshot_settings(keys)
    token = artifacts._artifact_context.set(None)
    path = tmp_path / "ci.log"
    path.write_text("Authorization: Bearer synthetic-bearer-value\nFAILED test_boundary", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_PATH", str(path))
    try:
        artifacts.inject_artifact_context()
        for tool in ("pr_reviewer", "pr_description", "pr_code_suggestions"):
            context = get_settings().get(tool).extra_instructions
            assert "synthetic-bearer-value" not in context
            assert "FAILED test_boundary" in context
        get_settings().set("pr_reviewer.extra_instructions", "Repository guidance")
        artifacts.reapply_artifact_context()
        context = get_settings().pr_reviewer.extra_instructions
        assert "Repository guidance" in context
        assert "synthetic-bearer-value" not in context
        assert "FAILED test_boundary" in context
    finally:
        restore_settings(snapshot)
        artifacts._artifact_context.reset(token)
