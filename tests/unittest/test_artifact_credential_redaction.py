"""Keep synthetic CI credentials out of model context and diagnostics."""

from time import perf_counter
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
    ("key=" + "A3TQ" + "B" * 16, "A3TQ" + "B" * 16, "aws_access_key"),
    ("AWS_SECRET_ACCESS_KEY=" + "x" * 40, "x" * 40, "credential_assignment"),
    ('"aws_session_token": "synthetic-session-value"', "synthetic-session-value", "credential_assignment"),
    ('"SecretAccessKey": "synthetic-secret-value"', "synthetic-secret-value", "credential_assignment"),
    ('"SessionToken": "synthetic-session-value"', "synthetic-session-value", "credential_assignment"),
    ("OPENAI_KEY=sk-proj-synthetic-value", "sk-proj-synthetic-value", "credential_assignment"),
    ('"openai_api_key": "synthetic-openai-value"', "synthetic-openai-value", "credential_assignment"),
    ("Authorization: AWS4-HMAC-SHA256 Credential=synthetic-key, Signature=synthetic-signature",
     "synthetic-signature", "authorization_header"),
])
def test_load_artifact_context_redacts_credentials_and_reports_only_counts(
    tmp_path, monkeypatch, content, credential, kind,
):
    path = tmp_path / "ci.log"
    path.write_text(content + "\nFAILED test_boundary: expected 3, got 4", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(artifacts, "get_settings", lambda: SimpleNamespace(get=lambda *_: {
        "enable": True, "artifact_path": str(path), "max_artifact_size": 2000,
    }))
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts.load_artifact_context()["content"]

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


@pytest.mark.parametrize("key", [
    "user_token", "personal_access_token", "bearer_token", "basic_token", "api_token",
    "api_key", "gemini_api_key", "jira_api_token", "pat", "client_secret", "webhook_secret",
    "shared_secret", "webhook_password", "github.user_token", "GITHUB__USER_TOKEN", "BITBUCKET_BEARER_TOKEN",
    "_api_key", "__API_KEY", "github._api_key", "_github.user_token", "GITHUB___USER_TOKEN",
    "github2.api_key", "git-hub.api_key",
])
@pytest.mark.parametrize("assignment", ["{key}=synthetic-opaque-secret", '{key} = "synthetic-opaque-secret"',
                                        '"{key}": "synthetic-opaque-secret"'])
def test_configured_credential_assignments_are_redacted(tmp_path, monkeypatch, key, assignment):
    path = tmp_path / "ci.log"
    path.write_text(assignment.format(key=key) + "\nFAILED test_boundary", encoding="utf-8")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 2000)

    assert "synthetic-opaque-secret" not in context
    assert "FAILED test_boundary" in context
    assert key in context
    logger.warning.assert_called_once_with("Redacted CI artifact credentials by type: {'credential_assignment': 1}")
    repeated_counts = {}
    assert redact_credentials(context, redaction_counts=repeated_counts) == context
    assert repeated_counts == {}


def test_long_dotted_noncredential_assignment_is_unchanged_and_fast():
    content = "a." * 5000 + "ordinary=value\nFAILED test_boundary"
    counts = {}
    started = perf_counter()

    redacted = redact_credentials(content, redaction_counts=counts)
    elapsed = perf_counter() - started

    assert redacted == content
    assert counts == {}
    assert elapsed < 1.0


@pytest.mark.parametrize("content", [
    'webhook_password = "synthetic first second third"',
    "webhook_password = 'synthetic first second third'",
    '"client_secret": "synthetic first second third"',
    'api_token="<redacted> synthetic first second third"',
    'api_token="synthetic first \\"second\\" third"',
    "api_token='synthetic first \\'second\\' third'",
])
def test_whole_quoted_credential_is_redacted(tmp_path, monkeypatch, content):
    path = tmp_path / "ci.log"
    path.write_text(content + "\nFAILED test_boundary", encoding="utf-8")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 2000)

    for part in ("synthetic", "first", "second", "third"):
        assert part not in context
    assert "FAILED test_boundary" in context
    logger.warning.assert_called_once_with("Redacted CI artifact credentials by type: {'credential_assignment': 1}")
    repeated_counts = {}
    assert redact_credentials(context, redaction_counts=repeated_counts) == context
    assert repeated_counts == {}


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_credential_crossing_read_boundary_is_redacted(tmp_path, quote):
    path = tmp_path / "ci.log"
    path.write_text("webhook_password = " + quote + "synthetic first second third " * 1000 + quote,
                    encoding="utf-8")

    context = artifacts._read_and_truncate(path, 120)

    for part in ("synthetic", "first", "second", "third"):
        assert part not in context
    assert "truncated" in context
    assert len(context) <= 120
    repeated_counts = {}
    assert redact_credentials(context, redaction_counts=repeated_counts) == context
    assert repeated_counts == {}


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("suffix", ["", "\\"])
def test_unclosed_quoted_credential_preserves_later_log_lines(tmp_path, monkeypatch, quote, ending, suffix):
    path = tmp_path / "ci.log"
    path.write_text("api_token=" + quote + "synthetic first second" + suffix + ending +
                    "FAILED test_boundary: expected 3, got 4" + ending +
                    'webhook_password="synthetic next secret"', encoding="utf-8", newline="")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 2000)

    assert "synthetic" not in context
    assert "first" not in context
    assert "second" not in context
    assert "next secret" not in context
    assert "FAILED test_boundary: expected 3, got 4" in context
    logger.warning.assert_called_once_with("Redacted CI artifact credentials by type: {'credential_assignment': 2}")
    repeated_counts = {}
    assert redact_credentials(context, redaction_counts=repeated_counts) == context
    assert repeated_counts == {}


@pytest.mark.parametrize("content", [
    "Authorization: Bearer <redacted>synthetic-secret-suffix",
    "Authorization: Bearer <redacted> synthetic-secret-suffix",
    "Authorization: Basic  <redacted>\tsynthetic-secret-suffix",
    'api_token="<redacted>synthetic-secret-suffix"',
    'api_token="<redacted>,synthetic-secret-suffix"',
    'api_token="<redacted>}synthetic-secret-suffix"',
    'api_token="<redacted>]synthetic-secret-suffix"',
])
def test_partial_redaction_markers_do_not_hide_credential_suffixes(tmp_path, monkeypatch, content):
    path = tmp_path / "ci.log"
    path.write_text(content + "\nFAILED test_boundary", encoding="utf-8")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 2000)

    assert "synthetic-secret-suffix" not in context
    assert "FAILED test_boundary" in context
    assert "<redacted>" in context
    logger.warning.assert_called_once()
    repeated_counts = {}
    assert redact_credentials(context, redaction_counts=repeated_counts) == context
    assert repeated_counts == {}


@pytest.mark.parametrize("content", [
    'api_token="<redacted>"', '"api_token": "<redacted>"', "api_token=<redacted>\n",
    "Authorization: Bearer <redacted>  \n", "Authorization: Bearer <redacted>\t\r\n",
    "api_key_count=3\nuser_token_length=40\nkey=expected-value",
    "xapi_key=synthetic-value", "1api_key=synthetic-value", "githubxapi_key=synthetic-value",
    'api_token=""', "api_token='  '", 'api_token=" <redacted> "',
])
def test_masked_values_and_noncredential_assignments_are_not_counted(content):
    counts = {}

    assert redact_credentials(content, redaction_counts=counts) == content
    assert counts == {}


@pytest.mark.parametrize("ending", ["  ", "\t", "\n\n", "\r\n\r\n", "  \n \n"])
@pytest.mark.parametrize("max_size", [2000, 120])
def test_incomplete_userinfo_before_trailing_whitespace_is_masked(tmp_path, monkeypatch, ending, max_size):
    path = tmp_path / "ci.log"
    path.write_text("Build log: https://ci-user:synthetic-password" + "x" * 80 + ending,
                    encoding="utf-8", newline="")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, max_size)

    assert "ci-user" not in context
    assert "synthetic-password" not in context
    assert "Build log:" in context
    assert len(context) <= max_size
    logger.warning.assert_called_once_with("Redacted CI artifact credentials by type: {'incomplete_url_userinfo': 1}")
    if max_size == 2000:
        assert context == "Build log: <redacted>" + ending.replace("\r\n", "\n")
    else:
        assert "truncated" in context


@pytest.mark.parametrize("whitespace", [" ", "  ", "\t", " \t  "])
def test_masked_authorization_headers_are_unchanged_and_not_counted(whitespace):
    content = f"Authorization: Bearer{whitespace}<redacted>\nFAILED test_boundary"
    counts = {}

    assert redact_credentials(content, redaction_counts=counts) == content
    assert counts == {}


@pytest.mark.parametrize("whitespace", ["  ", "\t\t", " \t  "])
def test_authorization_headers_with_extra_whitespace_are_redacted_once(whitespace):
    content = f"Authorization: Bearer{whitespace}synthetic-header\nFAILED test_boundary"
    counts = {}
    redacted = redact_credentials(content, redaction_counts=counts)

    assert redacted == f"Authorization: Bearer{whitespace}<redacted>\nFAILED test_boundary"
    assert counts == {"authorization_header": 1}
    repeated_counts = {}
    assert redact_credentials(redacted, redaction_counts=repeated_counts) == redacted
    assert repeated_counts == {}


@pytest.mark.parametrize("url", [
    "https://localhost:8080", "http://127.0.0.1:3000", "https://ci.example.com:443",
    "https://localhost:8080?healthy=true", "https://localhost:8080#status",
    "http://[::1]:8080", "http://[::1]",
])
@pytest.mark.parametrize("ending", ["", "\n", "  ", "\t", "\n\n", "  \n \n"])
def test_valid_urls_at_end_of_artifact_are_preserved(tmp_path, monkeypatch, url, ending):
    path = tmp_path / "ci.log"
    content = f"Build endpoint: {url}{ending}"
    path.write_text(content, encoding="utf-8")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    assert artifacts._read_and_truncate(path, 2000) == content
    logger.warning.assert_not_called()


def test_injected_artifacts_retain_redacted_context_after_settings_change(tmp_path, monkeypatch):
    keys = ("artifacts", "pr_reviewer.extra_instructions", "pr_description.extra_instructions",
            "pr_code_suggestions.extra_instructions")
    snapshot = snapshot_settings(keys)
    token = artifacts._artifact_context.set(None)
    path = tmp_path / "ci.log"
    path.write_text("Authorization: Bearer synthetic-bearer-value\n"
                    "OPENAI_KEY=sk-proj-synthetic-value\nFAILED test_boundary", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_PATH", str(path))
    try:
        artifacts.inject_artifact_context()
        monkeypatch.setattr(artifacts, "_read_and_truncate", MagicMock(side_effect=AssertionError("Unexpected reread")))
        for tool in ("pr_reviewer", "pr_description", "pr_code_suggestions"):
            context = artifacts.get_artifact_context(tool)["content"]
            assert "synthetic-bearer-value" not in context
            assert "sk-proj-synthetic-value" not in context
            assert "FAILED test_boundary" in context
        get_settings().set("pr_reviewer.extra_instructions", "Repository guidance")
        context = artifacts.get_artifact_context("pr_reviewer")["content"]
        assert get_settings().pr_reviewer.extra_instructions == "Repository guidance"
        assert "synthetic-bearer-value" not in context
        assert "sk-proj-synthetic-value" not in context
        assert "FAILED test_boundary" in context
    finally:
        restore_settings(snapshot)
        artifacts._artifact_context.reset(token)


@pytest.mark.parametrize("template, kind", [
    ("api_key={value}", "credential_assignment"),
    ('webhook_password="{value} second part"', "credential_assignment"),
    ("webhook_password='{value} second part'", "credential_assignment"),
    ("Authorization: Bearer {value}", "authorization_header"),
    ("glpat-{value}", "gitlab_token"),
    ("https://ci-user:{value}@example.com/log", "url_userinfo"),
    ('AWS_SECRET_ACCESS_KEY="{value}"', "credential_assignment"),
])
@pytest.mark.parametrize("max_size", [30, 120])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_tail_cut_redacts_credentials_before_retaining_suffix(tmp_path, monkeypatch, template, kind, max_size, ending):
    value = "synthetic-" + "x" * 160 + "-leaked-suffix"
    verdict = "FAILED test_tail_boundary"
    path = tmp_path / "tail.log"
    path.write_text(("noise" + ending) * 2000 + template.format(value=value) + ending + verdict,
                    encoding="utf-8", newline="")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, max_size, truncate_from="end")

    assert "leaked-suffix" not in context
    assert "xxxxxxxx" not in context
    assert context.endswith(verdict)
    assert len(context) <= max_size
    if max_size > len(artifacts._TRUNCATION_MARKER_START):
        assert context.startswith(artifacts._TRUNCATION_MARKER_START)
    logger.warning.assert_called_once_with(f"Redacted CI artifact credentials by type: {{{kind!r}: 1}}")
    counts = {}
    assert redact_credentials(context, redaction_counts=counts) == context
    assert counts == {}


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_tail_cut_redacts_aws_key_when_budget_is_smaller_than_marker(tmp_path, monkeypatch, ending):
    path = tmp_path / "aws-tail.log"
    path.write_text(("noise" + ending) * 2000 + "AKIA" + "B" * 16 + ending + "FAILED tail test",
                    encoding="utf-8", newline="")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 30, truncate_from="end")

    assert "BBBB" not in context
    assert context.endswith("FAILED tail test")
    assert len(context) <= 30
    logger.warning.assert_called_once_with("Redacted CI artifact credentials by type: {'aws_access_key': 1}")


@pytest.mark.parametrize("ending", ["", "  ", "\r\n"])
def test_tail_cut_redacts_incomplete_userinfo_at_eof(tmp_path, monkeypatch, ending):
    path = tmp_path / "url-tail.log"
    path.write_text("noise\n" * 2000 + "https://ci-user:synthetic-" + "x" * 160 + "-leaked-suffix" + ending,
                    encoding="utf-8", newline="")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 120, truncate_from="end")

    assert "leaked-suffix" not in context
    assert "xxxxxxxx" not in context
    assert context.startswith(artifacts._TRUNCATION_MARKER_START)
    assert len(context) <= 120
    logger.warning.assert_called_once_with("Redacted CI artifact credentials by type: {'incomplete_url_userinfo': 1}")


def test_load_artifact_context_keeps_redacted_tail_from_settings(tmp_path, monkeypatch):
    path = tmp_path / "tail.log"
    path.write_text("noise\n" * 2000 + "api_key=synthetic-" + "x" * 160 + "-leaked-suffix\nFAILED tail test",
                    encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(artifacts, "get_settings", lambda: SimpleNamespace(get=lambda *_: {
        "enable": True, "artifact_path": str(path), "max_artifact_size": 120, "truncate_from": " END ",
    }))

    context = artifacts.load_artifact_context()["content"]

    assert "leaked-suffix" not in context
    assert "xxxxxxxx" not in context
    assert context.startswith(artifacts._TRUNCATION_MARKER_START)
    assert context.endswith("FAILED tail test")
    assert len(context) <= 120
