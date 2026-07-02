import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pr_agent.algo.artifacts import (
    resolve_artifact_path,
    load_artifact_content,
    _read_and_truncate,
    ARTIFACT_PARSERS,
    parse_generic,
    parse_terraform_plan,
    parse_test_report,
)


class TestResolveArtifactPath:
    def test_empty_path_returns_none(self):
        assert resolve_artifact_path("") is None
        assert resolve_artifact_path(None) is None

    def test_absolute_path_existing_file(self, tmp_path):
        f = tmp_path / "plan.txt"
        f.write_text("content")
        assert resolve_artifact_path(str(f)) == f

    def test_absolute_path_missing_file(self):
        assert resolve_artifact_path("/nonexistent/path/file.txt") is None

    def test_relative_path_with_github_workspace(self, tmp_path):
        f = tmp_path / "output" / "plan.txt"
        f.parent.mkdir(parents=True)
        f.write_text("terraform plan")

        with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(tmp_path)}):
            result = resolve_artifact_path("output/plan.txt")
            assert result == f

    def test_relative_path_without_workspace_falls_back_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        f = tmp_path / "plan.txt"
        f.write_text("content")

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GITHUB_WORKSPACE", None)
            result = resolve_artifact_path("plan.txt")
            assert result == f

    def test_relative_path_not_found_returns_none(self):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE": "/tmp/nonexistent_workspace_xyz"}):
            assert resolve_artifact_path("missing.txt") is None


class TestReadAndTruncate:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "artifact.txt"
        f.write_text("hello world")
        assert _read_and_truncate(f, 50000) == "hello world"

    def test_truncates_large_content(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 1000)
        result = _read_and_truncate(f, 100)
        assert len(result) < 1000
        assert result.startswith("x" * 100)
        assert "[... content truncated due to size limit ...]" in result

    def test_returns_empty_on_read_error(self, tmp_path):
        missing = tmp_path / "no_such_file.txt"
        assert _read_and_truncate(missing, 50000) == ""


class TestParsers:
    def test_parser_registry_has_expected_types(self):
        assert "generic" in ARTIFACT_PARSERS
        assert "terraform_plan" in ARTIFACT_PARSERS
        assert "test_report" in ARTIFACT_PARSERS

    def test_generic_parser(self):
        result = parse_generic("some output", "build.log")
        assert "CI Artifact: build.log" in result
        assert "some output" in result
        assert "additional context" in result

    def test_generic_parser_no_label(self):
        result = parse_generic("output", "")
        assert "CI Artifact\n" in result

    def test_terraform_plan_parser(self):
        result = parse_terraform_plan("+ aws_instance.web", "plan.txt")
        assert "Terraform Plan Output: plan.txt" in result
        assert "+ aws_instance.web" in result
        assert "infrastructure" in result

    def test_test_report_parser(self):
        result = parse_test_report("FAILED: test_login", "results.xml")
        assert "Test Results: results.xml" in result
        assert "FAILED: test_login" in result
        assert "failures" in result


class TestLoadArtifactContent:
    def _mock_settings(self, artifacts_config):
        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            artifacts_config if key == "ARTIFACTS" else default
        )
        return settings

    def test_returns_empty_when_no_config(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {}
            assert load_artifact_content("pr_reviewer") == ""

    def test_returns_empty_when_disabled(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {"enable": False, "artifact_path": "plan.txt"}
            assert load_artifact_content("pr_reviewer") == ""

    def test_returns_empty_when_no_path(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {"enable": True, "artifact_path": ""}
            assert load_artifact_content("pr_reviewer") == ""

    def test_returns_empty_when_tool_not_targeted(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": "plan.txt",
                "target_tools": ["pr_reviewer"],
            }
            assert load_artifact_content("pr_description") == ""

    def test_returns_empty_when_file_not_found(self):
        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": "/nonexistent/file.txt",
                "target_tools": ["pr_reviewer"],
            }
            assert load_artifact_content("pr_reviewer") == ""

    def test_loads_and_formats_artifact(self, tmp_path):
        f = tmp_path / "plan.txt"
        f.write_text("+ aws_s3_bucket.data")

        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": str(f),
                "artifact_type": "terraform_plan",
                "artifact_label": "",
                "target_tools": ["pr_reviewer", "pr_description", "pr_code_suggestions"],
                "max_artifact_size": 50000,
            }
            result = load_artifact_content("pr_reviewer")
            assert "Terraform Plan Output: plan.txt" in result
            assert "+ aws_s3_bucket.data" in result

    def test_falls_back_to_generic_for_unknown_type(self, tmp_path):
        f = tmp_path / "output.log"
        f.write_text("some output")

        with patch("pr_agent.algo.artifacts.get_settings") as mock_gs:
            mock_gs.return_value.get.return_value = {
                "enable": True,
                "artifact_path": str(f),
                "artifact_type": "unknown_type",
                "artifact_label": "",
                "target_tools": ["pr_reviewer"],
                "max_artifact_size": 50000,
            }
            result = load_artifact_content("pr_reviewer")
            assert "CI Artifact: output.log" in result
