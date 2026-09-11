"""Tests for dashboard run launcher — never spawn a real process."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pr_agent.algo.utils import encode_user_text_arg
from pr_dashboard import registry, runner, store
from pr_dashboard.registry import Repo


def _conn(tmp_path: Path):
    return store.connect(tmp_path / "usage.db")


def _register(tmp_path: Path, provider: str = "github", slug: str = "owner/repo") -> Path:
    path = tmp_path / "pr_dashboard.toml"
    registry.save([Repo(provider=provider, slug=slug)], path)
    return path


class TestBuildArgv:
    def test_command_outside_whitelist_is_refused(self):
        """A command not in ALLOWED_COMMANDS raises RunError"""
        repo = Repo(provider="github", slug="owner/repo")
        with pytest.raises(runner.RunError, match="unsupported command"):
            runner.build_argv(repo, 1, "rm")

    def test_question_on_non_ask_command_is_refused(self):
        """A free-text question on review raises RunError"""
        repo = Repo(provider="github", slug="owner/repo")
        with pytest.raises(runner.RunError, match="does not take free text"):
            runner.build_argv(repo, 1, "review", question="why?")

    def test_ask_question_is_encoded_and_raw_string_absent_from_argv(self):
        """An ask question beginning -- reaches argv only in encoded form"""
        repo = Repo(provider="github", slug="owner/repo")
        raw = "--pr_questions.extra_instructions=x"
        argv = runner.build_argv(repo, 7, "ask", question=raw)
        encoded = encode_user_text_arg(raw)
        assert encoded in argv
        # Assert the raw unencoded question appears in NO argv element — asserting only
        # that the encoded form is present would pass an implementation that appended both.
        assert all(raw not in element for element in argv)

    def test_pr_url_is_rebuilt_from_registry_entry(self):
        """argv --pr_url is the rebuilt canonical URL, not a caller-supplied string"""
        repo = Repo(provider="github", slug="owner/repo")
        argv = runner.build_argv(repo, 3, "review")
        assert argv[argv.index("--pr_url") + 1] == "https://github.com/owner/repo/pull/3"


class TestBuildEnv:
    def test_excludes_hostile_ambient_variables(self, monkeypatch):
        """build_env drops PR_AGENT_EXTRA_CONFIG_URL, HTTP_PROXY, and OTEL exporter vars"""
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/tmp/home")
        monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_URL", "https://evil.example/config.toml")
        monkeypatch.setenv("HTTP_PROXY", "http://evil.example:8080")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://evil.example/otel")
        env = runner.build_env()
        assert "PR_AGENT_EXTRA_CONFIG_URL" not in env
        assert "HTTP_PROXY" not in env
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
        assert env.get("PATH") == "/usr/bin"
        assert env.get("HOME") == "/tmp/home"


class TestValidateTarget:
    def test_suffix_host_is_refused(self):
        """A host of github.com.evil.test is refused by assert_safe_pr_url"""
        with pytest.raises(runner.RunError, match="host"):
            runner.assert_safe_pr_url(
                "https://github.com.evil.test/owner/repo/pull/1",
                provider="github",
                slug="owner/repo",
                number=1,
            )

    def test_userinfo_is_refused(self):
        """A URL with userinfo is refused"""
        with pytest.raises(runner.RunError, match="userinfo"):
            runner.assert_safe_pr_url(
                "https://user:pass@github.com/owner/repo/pull/1",
                provider="github",
                slug="owner/repo",
                number=1,
            )

    def test_unregistered_slug_is_refused(self, tmp_path):
        """An unregistered slug is refused by validate_target"""
        path = _register(tmp_path, slug="owner/repo")
        with pytest.raises(runner.RunError, match="not registered"):
            runner.validate_target(path, "github", "other/repo", 1)

    def test_registered_target_returns_repo(self, tmp_path):
        """validate_target returns the matching registry Repo"""
        path = _register(tmp_path, slug="owner/repo")
        repo = runner.validate_target(path, "github", "owner/repo", 4)
        # Compare fields: registry tests reload the module, so dataclass identity can diverge.
        assert (repo.provider, repo.slug) == ("github", "owner/repo")


class TestLaunch:
    def test_popen_called_with_list_and_shell_false(self, tmp_path, monkeypatch):
        """Popen receives an argv list and shell=False"""
        conn = _conn(tmp_path)
        repo = Repo(provider="github", slug="owner/repo")
        calls = {}

        def fake_popen(argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            proc = MagicMock()
            proc.pid = 4242
            return proc

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        token = runner.launch(
            conn, repo=repo, number=1, command="review", log_dir=tmp_path / "logs",
            cwd=tmp_path,
        )
        assert isinstance(calls["argv"], list)
        assert calls["kwargs"].get("shell") is False
        assert store.get_ui_run(conn, token)["status"] == "running"

    def test_popen_receives_the_allowlisted_env_not_the_ambient_one(self, tmp_path, monkeypatch):
        """Popen is given the built env, so the child cannot inherit hostile configuration"""
        # build_env() is tested in isolation elsewhere, but that proves nothing about whether
        # launch() actually passes it: dropping `env=env` from the Popen call would restore
        # full ambient inheritance while every other test still passed. This pins the wiring.
        conn = _conn(tmp_path)
        repo = Repo(provider="github", slug="owner/repo")
        monkeypatch.setenv("PR_AGENT_EXTRA_CONFIG_URL", "https://attacker.test/config.toml")
        monkeypatch.setenv("HTTP_PROXY", "http://attacker.test:8080")
        calls = {}

        def fake_popen(argv, **kwargs):
            calls["kwargs"] = kwargs
            proc = MagicMock()
            proc.pid = 4242
            return proc

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        token = runner.launch(
            conn, repo=repo, number=1, command="review", log_dir=tmp_path / "logs",
            cwd=tmp_path,
        )
        passed_env = calls["kwargs"].get("env")
        assert passed_env is not None, "Popen must be given an explicit env, never the ambient one"
        assert "PR_AGENT_EXTRA_CONFIG_URL" not in passed_env
        assert "HTTP_PROXY" not in passed_env
        assert "PR_DASHBOARD_RUN_TOKEN" in passed_env
        assert store.get_ui_run(conn, token)["pid"] == 4242

    def test_cap_refuses_third_concurrent_run(self, tmp_path, monkeypatch):
        """A third concurrent run is refused when MAX_CONCURRENT_RUNS is 2"""
        conn = _conn(tmp_path)
        repo = Repo(provider="github", slug="owner/repo")

        def fake_popen(argv, **kwargs):
            proc = MagicMock()
            proc.pid = 100
            return proc

        monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
        runner.launch(conn, repo=repo, number=1, command="review", log_dir=tmp_path / "logs", cwd=tmp_path)
        runner.launch(conn, repo=repo, number=2, command="review", log_dir=tmp_path / "logs", cwd=tmp_path)
        with pytest.raises(runner.RunError, match="concurrent"):
            runner.launch(conn, repo=repo, number=3, command="review", log_dir=tmp_path / "logs", cwd=tmp_path)

    def test_queued_row_exists_when_popen_raises(self, tmp_path, monkeypatch):
        """A queued ui_runs row exists even when Popen then raises"""
        conn = _conn(tmp_path)
        repo = Repo(provider="github", slug="owner/repo")

        def boom(*args, **kwargs):
            raise OSError("spawn failed")

        monkeypatch.setattr(runner.subprocess, "Popen", boom)
        with pytest.raises(OSError, match="spawn failed"):
            runner.launch(
                conn, repo=repo, number=9, command="describe",
                log_dir=tmp_path / "logs", cwd=tmp_path,
            )
        rows = store.list_ui_runs(conn)
        assert len(rows) == 1
        assert rows[0]["status"] == "queued"
        assert rows[0]["command"] == "describe"
        assert rows[0]["pr_number"] == 9
