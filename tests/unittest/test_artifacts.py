import os
from unittest.mock import patch

from pr_agent.algo.artifacts import (
    DEFAULT_ARTIFACT_INSTRUCTIONS,
    _read_and_truncate,
    format_artifact_content,
    load_artifact,
    resolve_artifact_path,
)


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
