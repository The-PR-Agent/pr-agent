import os
from contextlib import nullcontext
from contextvars import copy_context
from copy import deepcopy
from unittest.mock import patch

import pytest
from starlette_context import request_cycle_context

from pr_agent.algo import artifacts
from pr_agent.algo.artifacts import (
    DEFAULT_ARTIFACT_INSTRUCTIONS,
    _read_and_truncate,
    artifact_context_scope,
    format_artifact_content,
    inject_artifact_context,
    load_artifact,
    reapply_artifact_context,
    resolve_artifact_path,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class TestResolveArtifactPathRobustness:
    def test_whitespace_path_returns_none(self):
        assert resolve_artifact_path("   ") is None

    def test_oserror_during_resolve_returns_none(self, tmp_path):
        with patch("pr_agent.algo.artifacts.Path") as mock_path_cls:
            mock_path_cls.return_value.is_absolute.return_value = True
            mock_path_cls.return_value.resolve.side_effect = OSError("symlink loop")
            result = resolve_artifact_path("/some/path/file.txt")
            assert result is None


class TestFormatArtifactContentRobustness:
    def test_whitespace_only_instructions_uses_default(self):
        result = format_artifact_content("output", "file.txt", "   ")
        assert DEFAULT_ARTIFACT_INSTRUCTIONS in result

    def test_none_instructions_uses_default(self):
        result = format_artifact_content("output", "file.txt", None)
        assert DEFAULT_ARTIFACT_INSTRUCTIONS in result


class TestLoadArtifactEnableFlag:
    def test_string_true_enables(self, tmp_path):
        f = tmp_path / "artifact.txt"
        f.write_text("content")
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": "true",
                "artifact_path": str(f),
                "artifact_instructions": "",
                "artifact_label": "",
                "max_artifact_size": 50000,
            }
            with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
                result = load_artifact()
            assert result != ""

    def test_string_false_disables(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": "false",
                "artifact_path": "some/path.txt",
            }
            assert load_artifact() == ""

    def test_string_True_capitalised_disables(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": "True",
                "artifact_path": "some/path.txt",
            }
            # "True".lower() == "true" → should enable; but file won't exist → returns ""
            assert load_artifact() == ""


class TestResolveArtifactPath:
    def test_empty_path_returns_none(self):
        assert resolve_artifact_path("") is None
        assert resolve_artifact_path(None) is None

    def test_absolute_path_existing_file(self, tmp_path):
        f = tmp_path / "plan.txt"
        f.write_text("content")
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
            assert resolve_artifact_path(str(f)) == f.resolve()

    def test_absolute_path_missing_file(self, tmp_path):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
            assert resolve_artifact_path(str(tmp_path / "nonexistent.txt")) is None

    def test_relative_path_with_github_workspace(self, tmp_path):
        f = tmp_path / "output" / "plan.txt"
        f.parent.mkdir(parents=True)
        f.write_text("terraform plan")

        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
            result = resolve_artifact_path("output/plan.txt")
            assert result == f.resolve()

    def test_relative_path_without_workspace_falls_back_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "plan.txt"
        f.write_text("content")

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_WORKSPACE", None)
            result = resolve_artifact_path("plan.txt")
            assert result == f.resolve()

    def test_relative_path_not_found_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/tmp/nonexistent_workspace_xyz"}):
            assert resolve_artifact_path("missing.txt") is None

    def test_rejects_path_traversal_above_workspace(self, tmp_path):
        outside = tmp_path / "outside" / "secret.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("secret")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace)}):
            result = resolve_artifact_path("../outside/secret.txt")
            assert result is None

    def test_rejects_absolute_path_outside_workspace(self, tmp_path):
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace)}):
            result = resolve_artifact_path(str(outside))
            assert result is None

    def test_root_workspace_does_not_reject_valid_paths(self, tmp_path):
        f = tmp_path / "artifact.txt"
        f.write_text("data")

        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/"}):
            result = resolve_artifact_path(str(f))
            assert result == f.resolve()


class TestReadAndTruncate:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "artifact.txt"
        f.write_text("hello world")
        assert _read_and_truncate(f, 50000) == "hello world"

    def test_truncates_large_content(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 1000)
        result = _read_and_truncate(f, 100)
        assert len(result) <= 100
        assert result.startswith("x")
        assert "[... content truncated due to size limit ...]" in result

    def test_returns_empty_on_read_error(self, tmp_path):
        missing = tmp_path / "no_such_file.txt"
        assert _read_and_truncate(missing, 50000) == ""

    def test_does_not_read_entire_large_file(self, tmp_path):
        f = tmp_path / "huge.txt"
        f.write_text("x" * 1_000_000)
        result = _read_and_truncate(f, 100)
        # Should contain exactly 100 chars of content + truncation marker
        assert len(result) < 200

    def test_result_never_exceeds_max_size_when_limit_smaller_than_marker(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("x" * 100)
        result = _read_and_truncate(f, 30)
        assert len(result) <= 30


class TestFormatArtifactContent:
    def test_with_label_and_custom_instructions(self):
        result = format_artifact_content("plan output", "plan.txt", "Check for deletions.")
        assert "CI Artifact: plan.txt" in result
        assert "plan output" in result
        assert "Check for deletions." in result

    def test_with_label_uses_default_instructions_when_empty(self):
        result = format_artifact_content("some output", "build.log", "")
        assert "CI Artifact: build.log" in result
        assert DEFAULT_ARTIFACT_INSTRUCTIONS in result

    def test_without_label(self):
        result = format_artifact_content("output", "", "")
        assert "CI Artifact\n" in result
        assert DEFAULT_ARTIFACT_INSTRUCTIONS in result


class TestLoadArtifact:
    def test_returns_empty_when_no_config(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {}
            assert load_artifact() == ""

    def test_returns_empty_when_disabled(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {"enable": False, "artifact_path": "plan.txt"}
            assert load_artifact() == ""

    def test_returns_empty_when_no_path(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {"enable": True, "artifact_path": ""}
            assert load_artifact() == ""

    def test_returns_empty_when_file_not_found(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": "/nonexistent/file.txt",
            }
            assert load_artifact() == ""

    def test_loads_and_formats_with_default_instructions(self, tmp_path):
        f = tmp_path / "plan.txt"
        f.write_text("+ aws_s3_bucket.data")

        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": str(f),
                "artifact_instructions": "",
                "artifact_label": "",
                "max_artifact_size": 50000,
            }
            with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
                result = load_artifact()
            assert "CI Artifact: plan.txt" in result
            assert "+ aws_s3_bucket.data" in result
            assert DEFAULT_ARTIFACT_INSTRUCTIONS in result

    def test_loads_and_formats_with_custom_instructions(self, tmp_path):
        f = tmp_path / "results.xml"
        f.write_text("FAILED: test_login")

        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": str(f),
                "artifact_instructions": "Flag any test failures.",
                "artifact_label": "Test Results",
                "max_artifact_size": 50000,
            }
            with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
                result = load_artifact()
            assert "CI Artifact: Test Results" in result
            assert "FAILED: test_login" in result
            assert "Flag any test failures." in result


class TestInjectArtifactContext:
    """The injection step shared by the GitHub Action runner and the CLI."""

    _KEYS = (
        "artifacts.enable",
        "artifacts.artifact_path",
        "artifacts.artifact_instructions",
        "artifacts.target_tools",
        "pr_reviewer.extra_instructions",
        "pr_description.extra_instructions",
        "pr_code_suggestions.extra_instructions",
    )

    @pytest.fixture
    def settings(self):
        snapshot = snapshot_settings(self._KEYS)
        s = get_settings()
        s.set("artifacts.enable", False)
        s.set("artifacts.artifact_path", "")
        s.set("artifacts.artifact_instructions", "")
        s.set("artifacts.target_tools", ["pr_reviewer", "pr_description", "pr_code_suggestions"])
        for tool in ("pr_reviewer", "pr_description", "pr_code_suggestions"):
            s.set(f"{tool}.extra_instructions", "")
        yield s
        restore_settings(snapshot)

    @pytest.fixture
    def report(self, tmp_path):
        f = tmp_path / "report.xml"
        f.write_text("FAILED: test_login")
        return f

    def test_disabled_leaves_extra_instructions_alone(self, settings, report):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(report.parent)}):
            os.environ.pop("ARTIFACT_PATH", None)
            os.environ.pop("PR_AGENT_ARTIFACT_PATH", None)
            inject_artifact_context()
        assert settings.get("pr_reviewer.extra_instructions") == ""

    def test_a_value_that_is_neither_bool_nor_string_stays_disabled(self, settings, report):
        """ARTIFACTS__ENABLE=1 from the environment is off, as it was in the GitHub Action runner."""
        settings.set("artifacts.enable", 1)
        settings.set("artifacts.artifact_path", str(report))
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(report.parent)}):
            os.environ.pop("ARTIFACT_PATH", None)
            os.environ.pop("PR_AGENT_ARTIFACT_PATH", None)
            inject_artifact_context()
        assert settings.get("pr_reviewer.extra_instructions") == ""

    def test_env_path_enables_and_appends_to_every_target_tool(self, settings, report):
        env = {"GITHUB_WORKSPACE": str(report.parent), "ARTIFACT_PATH": str(report),
               "ARTIFACT_INSTRUCTIONS": "Flag any test failures."}
        with patch.dict(os.environ, env):
            inject_artifact_context()

        assert settings.get("artifacts.enable") is True
        for tool in ("pr_reviewer", "pr_description", "pr_code_suggestions"):
            extra = settings.get(f"{tool}.extra_instructions")
            assert "CI Artifact: report.xml" in extra
            assert "FAILED: test_login" in extra
            assert "Flag any test failures." in extra

    def test_settings_alone_are_enough_without_the_env_var(self, settings, report):
        settings.set("artifacts.enable", True)
        settings.set("artifacts.artifact_path", str(report))
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(report.parent)}):
            os.environ.pop("ARTIFACT_PATH", None)
            os.environ.pop("PR_AGENT_ARTIFACT_PATH", None)
            inject_artifact_context()
        assert "FAILED: test_login" in settings.get("pr_reviewer.extra_instructions")

    def test_only_target_tools_get_it_and_existing_instructions_are_kept(self, settings, report):
        settings.set("artifacts.target_tools", ["pr_reviewer"])
        settings.set("pr_reviewer.extra_instructions", "Be terse.")
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(report.parent), "ARTIFACT_PATH": str(report)}):
            inject_artifact_context()

        extra = settings.get("pr_reviewer.extra_instructions")
        assert extra.startswith("Be terse.\n======\n\n")
        assert "FAILED: test_login" in extra
        assert settings.get("pr_description.extra_instructions") == ""

    def test_running_twice_does_not_duplicate_the_artifact(self, settings, report):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(report.parent), "ARTIFACT_PATH": str(report)}):
            inject_artifact_context()
            inject_artifact_context()
        assert settings.get("pr_reviewer.extra_instructions").count("FAILED: test_login") == 1


@pytest.fixture
def scoped_artifact_settings(monkeypatch, tmp_path):
    for key in ("ARTIFACT_PATH", "PR_AGENT_ARTIFACT_PATH", "ARTIFACT_INSTRUCTIONS", "PR_AGENT_ARTIFACT_INSTRUCTIONS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    with request_cycle_context({"settings": deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("ARTIFACTS", {"enable": False, "target_tools": ["pr_reviewer"]})
        settings.set("PR_REVIEWER.EXTRA_INSTRUCTIONS", "original")
        yield settings


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt])
def test_artifact_scope_restores_replaced_settings_and_invalidates_copied_context(
    monkeypatch, tmp_path, scoped_artifact_settings, error
):
    settings = scoped_artifact_settings
    original_artifacts = deepcopy(settings.get("ARTIFACTS"))
    report = tmp_path / "report.txt"
    report.write_text("SCOPED_ARTIFACT")
    monkeypatch.setenv("ARTIFACT_PATH", str(report))
    with pytest.raises(error) if error else nullcontext():
        with artifact_context_scope():
            inject_artifact_context()
            copied = copy_context()
            settings.set("PR_REVIEWER", {"extra_instructions": "replacement", "other": "keep"})
            reapply_artifact_context()
            reapply_artifact_context()
            assert settings.pr_reviewer.extra_instructions.startswith("replacement")
            assert settings.pr_reviewer.extra_instructions.count("SCOPED_ARTIFACT") == 1
            if error:
                raise error("primary failure")
    assert settings.get("ARTIFACTS") == original_artifacts
    assert settings.pr_reviewer.extra_instructions == "original"
    assert settings.pr_reviewer.other == "keep"
    with patch.object(artifacts, "load_artifact", side_effect=AssertionError("late read")):
        copied.run(reapply_artifact_context)
        copied.run(inject_artifact_context)
    assert settings.pr_reviewer.extra_instructions == "original"


@pytest.mark.parametrize("original, replacement", [
    ({}, {"extra_instructions": "new", "other": "keep"}),
    ({"extra_instructions": None}, None),
    ({"extra_instructions": ""}, {"extra_instructions": "new"}),
    ({"extra_instructions": "original"}, "malformed"),
])
def test_artifact_scope_restores_instruction_presence(
    monkeypatch, tmp_path, scoped_artifact_settings, original, replacement
):
    settings = scoped_artifact_settings
    settings.set("PR_REVIEWER", original)
    report = tmp_path / "report.txt"
    report.write_text("SCOPED_ARTIFACT")
    monkeypatch.setenv("ARTIFACT_PATH", str(report))
    with artifact_context_scope():
        inject_artifact_context()
        if replacement is None:
            settings.unset("PR_REVIEWER", force=True)
        else:
            settings.set("PR_REVIEWER", replacement)
        reapply_artifact_context()
    restored = settings.get("PR_REVIEWER")
    assert ("extra_instructions" in restored) == ("extra_instructions" in original)
    if "extra_instructions" in original:
        assert restored.extra_instructions == original["extra_instructions"]
    else:
        assert restored.other == "keep"


def test_nested_artifact_scopes_do_not_leak_into_a_later_invocation(monkeypatch, tmp_path, scoped_artifact_settings):
    settings = scoped_artifact_settings
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("FIRST_ARTIFACT")
    second.write_text("SECOND_ARTIFACT")
    with patch.object(artifacts, "_read_and_truncate", wraps=_read_and_truncate) as read:
        monkeypatch.setenv("ARTIFACT_PATH", str(first))
        with artifact_context_scope():
            inject_artifact_context()
            outer = settings.pr_reviewer.extra_instructions
            monkeypatch.setenv("ARTIFACT_PATH", str(second))
            with artifact_context_scope():
                inject_artifact_context()
                assert "SECOND_ARTIFACT" in settings.pr_reviewer.extra_instructions
            assert settings.pr_reviewer.extra_instructions == outer
            inject_artifact_context()
            assert settings.pr_reviewer.extra_instructions == outer
        monkeypatch.delenv("ARTIFACT_PATH")
        with artifact_context_scope():
            inject_artifact_context()
            reapply_artifact_context()
            assert settings.pr_reviewer.extra_instructions == "original"
        assert read.call_count == 2
    assert settings.get("ARTIFACTS.ENABLE") is False


def test_artifact_scope_does_not_prepare_or_reapply_into_different_settings(
    monkeypatch, tmp_path, scoped_artifact_settings
):
    report = tmp_path / "report.txt"
    report.write_text("SCOPED_ARTIFACT")
    monkeypatch.setenv("ARTIFACT_PATH", str(report))
    other_settings = deepcopy(scoped_artifact_settings)
    with artifact_context_scope():
        inject_artifact_context()
        with request_cycle_context({"settings": other_settings}):
            with patch.object(artifacts, "load_artifact", side_effect=AssertionError("unexpected read")):
                reapply_artifact_context()
                inject_artifact_context()
            assert other_settings.pr_reviewer.extra_instructions == "original"


def test_artifact_scope_does_not_retry_a_skipped_preparation(monkeypatch, tmp_path, scoped_artifact_settings):
    report = tmp_path / "later.txt"
    report.write_text("LATE_ARTIFACT")
    with artifact_context_scope():
        inject_artifact_context()
        monkeypatch.setenv("ARTIFACT_PATH", str(report))
        with patch.object(artifacts, "load_artifact", side_effect=AssertionError("late read")):
            inject_artifact_context()
            reapply_artifact_context()
    assert scoped_artifact_settings.pr_reviewer.extra_instructions == "original"


def test_artifact_cleanup_does_not_mask_primary_failure(monkeypatch, tmp_path, scoped_artifact_settings):
    settings = scoped_artifact_settings
    report = tmp_path / "report.txt"
    report.write_text("SCOPED_ARTIFACT")
    monkeypatch.setenv("ARTIFACT_PATH", str(report))

    def fail_restore(*_args, **_kwargs):
        raise ValueError("cleanup")

    with pytest.raises(KeyboardInterrupt, match="primary failure"):
        with artifact_context_scope():
            inject_artifact_context()
            copied = copy_context()
            monkeypatch.setattr(settings, "set", fail_restore)
            raise KeyboardInterrupt("primary failure")
    assert settings.pr_reviewer.extra_instructions == "original"
    copied.run(reapply_artifact_context)
    assert settings.pr_reviewer.extra_instructions == "original"


def test_artifact_scope_restores_absent_artifacts_section(monkeypatch, tmp_path, scoped_artifact_settings):
    settings = scoped_artifact_settings
    settings.unset("ARTIFACTS", force=True)
    report = tmp_path / "report.txt"
    report.write_text("SCOPED_ARTIFACT")
    monkeypatch.setenv("ARTIFACT_PATH", str(report))
    with artifact_context_scope():
        inject_artifact_context()
        assert settings.get("ARTIFACTS.ENABLE") is True
        assert "SCOPED_ARTIFACT" in settings.pr_reviewer.extra_instructions
    assert "ARTIFACTS" not in settings
    assert settings.pr_reviewer.extra_instructions == "original"
