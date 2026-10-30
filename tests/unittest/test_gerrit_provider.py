import os
import subprocess
import sys
import traceback
from types import SimpleNamespace

import git
import pytest
import requests
import urllib3.util

from pr_agent.algo.language_handler import sort_files_by_main_languages
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import gerrit_provider
from pr_agent.git_providers.gerrit_provider import GerritProvider
from pr_agent.tools.ticket_pr_compliance_check import add_jira_tickets
from tests.unittest import _settings_helpers as settings_helpers


@pytest.fixture
def command_timeout_setting():
    snapshot = settings_helpers.snapshot_settings(["gerrit.command_timeout_seconds"])
    try:
        yield
    finally:
        settings_helpers.restore_settings(snapshot)


@pytest.mark.parametrize(
    ("operation", "invoke"),
    [
        ("clone", lambda url, ref, cwd: gerrit_provider.clone(url, cwd)),
        ("fetch", gerrit_provider.fetch),
        ("checkout", lambda url, ref, cwd: gerrit_provider.checkout(cwd)),
        ("show", lambda url, ref, cwd: gerrit_provider.show("HEAD", cwd=cwd)),
        ("diff", lambda url, ref, cwd: gerrit_provider.diff("HEAD", cwd=cwd)),
        ("reset", lambda url, ref, cwd: gerrit_provider.reset_local_changes(cwd)),
        ("query", lambda url, ref, cwd: gerrit_provider.list_comments(url, ref)),
        ("review", lambda url, ref, cwd: gerrit_provider.add_comment(url, ref, "private comment")),
    ],
)
def test_direct_gerrit_children_use_safe_per_call_timeout(
    monkeypatch, command_timeout_setting, operation, invoke, tmp_path
):
    url = urllib3.util.parse_url("ssh://secret-user:secret-pass@example.com:29418/project")
    ref = "refs/changes/01/1/1"
    calls = []

    def fail_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(
            cmd="private argv", timeout=120, output=b"private stdout", stderr=b"private stderr"
        )

    monkeypatch.setattr(gerrit_provider.subprocess, "run", fail_run)
    with pytest.raises(gerrit_provider.GerritCommandTimeoutError) as caught:
        invoke(url, ref, tmp_path)

    error = caught.value
    assert isinstance(error, TimeoutError)
    assert str(error) == f"Gerrit {operation} timed out after 120 seconds"
    assert error.__cause__ is None and error.__suppress_context__
    assert all(not hasattr(error, field) for field in ("cmd", "stdout", "stderr", "output"))
    rendered = "".join(traceback.format_exception_only(type(error), error))
    assert all(secret not in rendered for secret in (
        "secret-user", "secret-pass", "private argv", "private stdout", "private stderr", "private comment"
    ))
    assert len(calls) == 1
    expected = {
        "clone": ("git", "clone", "--depth", "1", url, tmp_path),
        "fetch": ("git", "fetch", "--depth", "2", url, ref),
        "checkout": ("git", "checkout", "FETCH_HEAD"),
        "show": ("git", "show", "HEAD"),
        "diff": ("git", "diff", "HEAD"),
        "reset": ("git", "checkout", "--force"),
        "query": ("ssh", "-p", "29418", "secret-user:secret-pass@example.com", "gerrit", "query",
                  "--comments", "--current-patch-set", "1", "--format", "JSON"),
        "review": ("ssh", "-p", "29418", "secret-user:secret-pass@example.com", "gerrit", "review",
                   "--message", "'private comment'", "1,1"),
    }
    assert calls[0][0] == expected[operation]
    assert calls[0][1].get("cwd") == (None if operation in {"clone", "query", "review"} else tmp_path)
    assert calls[0][1]["timeout"] == 120
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["check"] is True


@pytest.mark.parametrize("value", [None, True, False, 0, -2, float("nan"), float("inf"), "private-invalid", {}])
def test_invalid_gerrit_timeout_falls_back_without_logging_value(monkeypatch, command_timeout_setting, value):
    from loguru import logger as loguru_logger

    get_settings().set("gerrit.command_timeout_seconds", value)
    captured, sink_id = _capture_logs()
    calls = []
    monkeypatch.setattr(
        gerrit_provider.subprocess, "run",
        lambda command, **kwargs: (calls.append(kwargs), SimpleNamespace(stdout=b"ok"))[1],
    )
    try:
        assert gerrit_provider.show("HEAD") == "ok"
    finally:
        loguru_logger.remove(sink_id)

    assert calls[0]["timeout"] == 120
    combined = "\n".join(captured)
    assert "Ignoring invalid gerrit.command_timeout_seconds" in combined
    assert "private-invalid" not in combined


def test_gerrit_timeout_reads_host_setting_for_each_direct_child(monkeypatch, command_timeout_setting):
    calls = []
    monkeypatch.setattr(
        gerrit_provider.subprocess, "run",
        lambda command, **kwargs: (calls.append(kwargs), SimpleNamespace(stdout=b"ok"))[1],
    )
    get_settings().set("gerrit.command_timeout_seconds", 15.5)
    assert gerrit_provider.show("HEAD") == "ok"
    get_settings().set("gerrit.command_timeout_seconds", 100000)
    assert gerrit_provider.show("HEAD") == "ok"
    assert [call["timeout"] for call in calls] == [15.5, 100000]


def test_gerrit_call_preserves_success_decoding_and_process_failure(monkeypatch, command_timeout_setting):
    error = subprocess.CalledProcessError(1, ["git", "show"], stderr=b"failure")
    monkeypatch.setattr(
        gerrit_provider.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b"caf\xc3\xa9")
    )
    assert gerrit_provider.show("HEAD") == "caf\u00e9"
    monkeypatch.setattr(gerrit_provider.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(error))
    with pytest.raises(subprocess.CalledProcessError) as caught:
        gerrit_provider.show("HEAD")
    assert caught.value is error


@pytest.mark.parametrize("failing_step", ["clone", "fetch", "checkout"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_prepare_repo_timeout_attempts_cleanup_once_without_leaking_child_data(
    tmp_path, monkeypatch, command_timeout_setting, failing_step, cleanup_fails
):
    from loguru import logger as loguru_logger

    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    monkeypatch.setattr(gerrit_provider, "mkdtemp", lambda: str(repo_path))
    child_calls = []

    def run(command, **kwargs):
        child_calls.append(command)
        if command[1] == failing_step:
            raise subprocess.TimeoutExpired(
                "private argv", kwargs["timeout"], output=b"private stdout", stderr=b"private stderr"
            )
        return SimpleNamespace(stdout=b"")

    monkeypatch.setattr(gerrit_provider.subprocess, "run", run)
    real_rmtree = gerrit_provider.shutil.rmtree
    removed = []

    def remove(path):
        removed.append(path)
        if cleanup_fails:
            raise OSError("cleanup busy")
        real_rmtree(path)

    monkeypatch.setattr(gerrit_provider.shutil, "rmtree", remove)
    captured, sink_id = _capture_logs()
    try:
        with pytest.raises(gerrit_provider.GerritCommandTimeoutError, match=f"Gerrit {failing_step} timed out"):
            gerrit_provider.prepare_repo(
                urllib3.util.parse_url("ssh://secret-user:secret-pass@example.com:29418"),
                "project", "refs/changes/01/1/1",
            )
    finally:
        loguru_logger.remove(sink_id)

    assert removed == [repo_path]
    assert len(child_calls) == {"clone": 1, "fetch": 2, "checkout": 3}[failing_step]
    assert repo_path.exists() is cleanup_fails
    combined = "\n".join(captured)
    assert all(secret not in combined for secret in (
        "secret-user", "secret-pass", "private argv", "private stdout", "private stderr"
    ))
    assert any(
        message.endswith(f"Cloning ssh://example.com:29418/project to {repo_path}\n")
        for message in captured
    )
    assert ("cleanup busy" in combined) is cleanup_fails


@pytest.mark.parametrize("error", [
    gerrit_provider.GerritCommandTimeoutError("Gerrit clone timed out after 120 seconds"),
    KeyboardInterrupt(),
])
def test_prepare_repo_reraises_primary_base_exception_after_cleanup(tmp_path, monkeypatch, error):
    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    monkeypatch.setattr(gerrit_provider, "mkdtemp", lambda: str(repo_path))
    monkeypatch.setattr(gerrit_provider, "clone", lambda *args: (_ for _ in ()).throw(error))

    with pytest.raises(type(error)) as caught:
        gerrit_provider.prepare_repo(
            urllib3.util.parse_url("ssh://reviewer@example.com:29418"),
            "project", "refs/changes/01/1/1",
        )

    assert caught.value is error
    assert not repo_path.exists()


def test_factory_preserves_existing_resource_error_with_safe_timeout_cause(monkeypatch):
    from pr_agent import git_providers as providers

    snapshot = settings_helpers.snapshot_settings(["config.git_provider", "gerrit.url", "gerrit.user"])
    get_settings().set("config.git_provider", "gerrit")
    get_settings().set("gerrit.url", "ssh://example.com:29418")
    get_settings().set("gerrit.user", "reviewer")
    monkeypatch.setattr(gerrit_provider, "prepare_repo", lambda *args: (
        (_ for _ in ()).throw(gerrit_provider.GerritCommandTimeoutError("Gerrit clone timed out after 120 seconds"))
    ))
    try:
        with pytest.raises(ValueError, match="project:refs/changes/01/1/1") as caught:
            providers.get_git_provider_with_context("project:refs/changes/01/1/1")
    finally:
        settings_helpers.restore_settings(snapshot)

    assert type(caught.value.__cause__) is gerrit_provider.GerritCommandTimeoutError


@pytest.mark.parametrize("consumer", ["cached_server", "fresh_cli", "fresh_questions"])
def test_gerrit_consumers_keep_host_timeout_after_repo_overlay(
    monkeypatch, tmp_path, consumer
):
    from starlette_context import request_cycle_context

    from pr_agent import git_providers as providers
    from pr_agent.algo.cli_args import CliArgs
    from pr_agent.git_providers.utils import apply_repo_settings
    from pr_agent.tools import pr_questions

    key = "project:refs/changes/01/1/1"
    snapshot = settings_helpers.snapshot_settings([
        "config.git_provider", "config.use_repo_settings_file", "gerrit.url", "gerrit.user",
        "gerrit.command_timeout_seconds",
    ])
    settings = get_settings()
    settings.set("config.git_provider", "gerrit")
    settings.set("config.use_repo_settings_file", True)
    settings.set("gerrit.url", "ssh://example.com:29418")
    settings.set("gerrit.user", "reviewer")
    settings.set("gerrit.command_timeout_seconds", 120)

    calls = []
    made_dirs = []

    def make_temp_directory():
        path = tmp_path / f"repo-{len(made_dirs)}"
        path.mkdir()
        made_dirs.append(path)
        return str(path)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=b'{"currentPatchSet":{"comments":[]}}\n')

    monkeypatch.setattr(gerrit_provider, "mkdtemp", make_temp_directory)
    monkeypatch.setattr(gerrit_provider, "Repo", lambda path: SimpleNamespace())
    monkeypatch.setattr(gerrit_provider.subprocess, "run", run)
    monkeypatch.setattr(GerritProvider, "get_pr_title", lambda self: "change")
    monkeypatch.setattr(GerritProvider, "get_diff_files", lambda self: [])
    monkeypatch.setattr(
        GerritProvider, "get_repo_settings",
        lambda self: b"[gerrit]\ncommand_timeout_seconds = 999\n",
    )
    try:
        if consumer == "cached_server":
            with request_cycle_context({"settings": settings}):
                apply_repo_settings(key)
                early = providers.get_git_provider_with_context(key)
                assert providers.get_git_provider_with_context(key) is early
                assert early.get_issue_comments() == []
        else:
            apply_repo_settings(key)
            if consumer == "fresh_cli":
                assert isinstance(providers.get_git_provider()(key), GerritProvider)
            else:
                monkeypatch.setattr(GerritProvider, "get_languages", lambda self: {})
                monkeypatch.setattr(GerritProvider, "get_files", lambda self: [])
                monkeypatch.setattr(GerritProvider, "get_pr_branch", lambda self: "change")
                monkeypatch.setattr(GerritProvider, "get_pr_description", lambda self: "")
                monkeypatch.setattr(GerritProvider, "get_commit_messages", lambda self: "")
                monkeypatch.setattr(pr_questions, "get_main_pr_language", lambda *args: "")
                monkeypatch.setattr(pr_questions, "TokenHandler", lambda *args: None)
                monkeypatch.setattr(pr_questions.PRQuestions, "_load_conversation_history", lambda self: "")
                question = pr_questions.PRQuestions(key, args=[], ai_handler=SimpleNamespace)
                assert isinstance(question.git_provider, GerritProvider)
        assert settings.get("gerrit.command_timeout_seconds") == 120
        assert CliArgs.validate_user_args(["--gerrit.command_timeout_seconds=999"])[0] is False
        assert len(calls) == (4 if consumer == "cached_server" else 6)
        assert all(kwargs["timeout"] == 120 for _, kwargs in calls)
    finally:
        settings_helpers.restore_settings(snapshot)


def _make_repo(tmp_path, filenames):
    repo = git.Repo.init(tmp_path)
    for name in filenames:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n")
        repo.index.add([str(path)])
    repo.index.commit("initial files")
    return repo


def test_get_commit_messages_returns_text(tmp_path):
    provider = object.__new__(GerritProvider)
    provider.repo = _make_repo(tmp_path, ["app.py"])

    assert provider.get_commit_messages() == "initial files"


def test_detached_gerrit_branch_keeps_commit_message_in_jira_context(tmp_path, monkeypatch):
    repo_path = tmp_path / "clone"
    repo = _make_repo(repo_path, ["app.py"])
    repo.git.checkout("--detach")
    repo.index.commit("Implement ABC-123 from Gerrit change")
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = repo, str(repo_path)
    provider.pr = gerrit_provider.PullRequestMimic("Review change", [])
    contexts = []

    def extract(context, max_characters, remaining):
        contexts.append(context)
        return [{"ticket_url": "https://example.atlassian.net/browse/ABC-123"}]

    monkeypatch.setattr("pr_agent.tools.ticket_pr_compliance_check.extract_jira_tickets", extract)
    try:
        assert repo.head.is_detached
        assert provider.get_user_description() == "Implement ABC-123 from Gerrit change"
        tickets = add_jira_tickets(provider, [])
        assert contexts == ["Review change\nImplement ABC-123 from Gerrit change"]
        assert tickets == [{"ticket_url": "https://example.atlassian.net/browse/ABC-123"}]
        assert provider.get_pr_branch() == ""
    finally:
        repo.close()
        provider.cleanup()


def test_get_repo_settings_reads_the_default_branch_not_the_change(tmp_path):
    repo = _make_repo(tmp_path, [".pr_agent.toml"])
    repo.git.checkout("--detach")
    (tmp_path / ".pr_agent.toml").write_text("from the change\n")
    repo.index.add([".pr_agent.toml"])
    repo.index.commit("change edits settings")
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = repo, tmp_path

    assert provider.get_repo_settings() == b".pr_agent.toml\n"


def test_get_repo_settings_is_empty_when_the_default_branch_has_none(tmp_path):
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = _make_repo(tmp_path, ["app.py"]), tmp_path

    assert provider.get_repo_settings() == b""


def test_get_repo_settings_is_empty_when_the_settings_path_is_a_directory(tmp_path):
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = _make_repo(tmp_path, [".pr_agent.toml/nested"]), tmp_path

    assert provider.get_repo_settings() == b""


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("settings/review.toml", b"settings/review.toml\n"),
        (".pr_agent.toml", b""),
        ("../outside.toml", b""),
    ],
)
def test_get_repo_settings_resolves_only_safe_symlinks(tmp_path, target, expected):
    repo = _make_repo(tmp_path, ["settings/review.toml"])
    (tmp_path / ".pr_agent.toml").symlink_to(target)
    repo.index.add([".pr_agent.toml"])
    repo.index.commit("link settings")
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = repo, tmp_path

    assert provider.get_repo_settings() == expected


def test_get_repo_settings_resolves_linked_directories(tmp_path):
    repo = _make_repo(tmp_path, ["actual/review.toml"])
    (tmp_path / "settings").symlink_to("actual", target_is_directory=True)
    (tmp_path / ".pr_agent.toml").symlink_to("settings/review.toml")
    repo.index.add(["settings", ".pr_agent.toml"])
    repo.index.commit("link settings directory")
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = repo, tmp_path

    assert provider.get_repo_settings() == b"actual/review.toml\n"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("alias/../settings.toml", b"nested/settings.toml\n"),
        ("alias/../../settings.toml", b"settings.toml\n"),
        ("alias/../../../outside.toml", b""),
    ],
)
def test_get_repo_settings_resolves_parent_segments_after_linked_directories(tmp_path, target, expected):
    repo = _make_repo(tmp_path, ["settings.toml", "nested/settings.toml", "nested/subdir/keep"])
    (tmp_path / "alias").symlink_to("nested/subdir", target_is_directory=True)
    (tmp_path / ".pr_agent.toml").symlink_to(target)
    repo.index.add(["alias", ".pr_agent.toml"])
    repo.index.commit("link settings through parent segment")
    provider = object.__new__(GerritProvider)
    provider.repo, provider.repo_path = repo, tmp_path

    assert provider.get_repo_settings() == expected


def test_get_diff_files_preserves_deleted_filename(tmp_path):
    repo = _make_repo(tmp_path, ["keep.py", "gone.py"])
    (tmp_path / "gone.py").unlink()
    repo.index.remove(["gone.py"])
    repo.index.commit("delete gone.py")

    provider = object.__new__(GerritProvider)
    provider.repo = repo

    diff_files = provider.get_diff_files()

    deleted = [file for file in diff_files if file.edit_type == EDIT_TYPE.DELETED]
    assert len(deleted) == 1
    assert deleted[0].filename == "gone.py"


@pytest.mark.parametrize("change_type", ["added", "modified", "deleted"])
def test_get_diff_files_skips_non_utf8_file_and_keeps_utf8_sibling(tmp_path, change_type):
    repo = git.Repo.init(tmp_path)
    good_file = tmp_path / "good.py"
    non_utf8_file = tmp_path / "non_utf8.py"
    good_file.write_text("before\n", encoding="utf-8")
    files_to_add = ["good.py"]
    if change_type in {"modified", "deleted"}:
        non_utf8_file.write_bytes(b"\xffbefore\n")
        files_to_add.append("non_utf8.py")
    repo.index.add(files_to_add)
    repo.index.commit("base")

    good_file.write_text("after\n", encoding="utf-8")
    repo.index.add(["good.py"])
    if change_type == "added":
        non_utf8_file.write_bytes(b"\xffafter\n")
        repo.index.add(["non_utf8.py"])
    elif change_type == "modified":
        non_utf8_file.write_bytes(b"\xfeafter\n")
        repo.index.add(["non_utf8.py"])
    else:
        non_utf8_file.unlink()
        repo.index.remove(["non_utf8.py"])
    repo.index.commit(f"{change_type} non-UTF-8 file")

    provider = object.__new__(GerritProvider)
    provider.repo = repo

    diff_files = provider.get_diff_files()

    assert [file.filename for file in diff_files] == ["good.py"]
    assert diff_files[0].base_file == "before\n"
    assert diff_files[0].head_file == "after\n"
    assert "-before" in diff_files[0].patch
    assert "+after" in diff_files[0].patch
    assert diff_files[0].edit_type == EDIT_TYPE.MODIFIED
    assert provider.diff_files is diff_files


def test_get_languages_returns_names_used_for_hunk_prioritization(tmp_path):
    repo = _make_repo(tmp_path, ["a.py", "b.py", "c.py", "app.js", "notes.unknown"])
    provider = object.__new__(GerritProvider)
    provider.repo = repo

    languages = provider.get_languages()

    assert languages == {"Python": 75.0, "JavaScript": 25.0}

    files = [type("File", (), {"filename": name})() for name in ["a.py", "app.js", "notes.unknown"]]
    buckets = {
        bucket["language"]: {file.filename for file in bucket["files"]}
        for bucket in sort_files_by_main_languages(languages, files)
    }
    assert buckets == {
        "Python": {"a.py"},
        "JavaScript": {"app.js"},
        "Other": {"notes.unknown"},
    }


def test_get_languages_matches_filenames_and_multipart_extensions(tmp_path):
    repo = _make_repo(tmp_path, ["Dockerfile", "build.cmake.in", "app.py", "notes.unknown"])
    provider = object.__new__(GerritProvider)
    provider.repo = repo

    languages = provider.get_languages()

    assert set(languages) == {"Dockerfile", "CMake", "Python"}
    assert all(abs(percentage - 100 / 3) < 1e-6 for percentage in languages.values())

    files = [
        type("File", (), {"filename": name})()
        for name in ["Dockerfile", "build.cmake.in", "app.py", "notes.unknown"]
    ]
    buckets = {
        bucket["language"]: {file.filename for file in bucket["files"]}
        for bucket in sort_files_by_main_languages(languages, files)
    }
    assert buckets == {
        "Dockerfile": {"Dockerfile"},
        "CMake": {"build.cmake.in"},
        "Python": {"app.py"},
        "Other": {"notes.unknown"},
    }


def test_get_languages_preserves_case_sensitive_extensions(tmp_path):
    repo = _make_repo(tmp_path, ["lower.c", "upper.C"])
    provider = object.__new__(GerritProvider)
    provider.repo = repo

    languages = provider.get_languages()
    assert languages == {"C": 50.0, "C++": 50.0}

    files = [
        type("File", (), {"filename": name})()
        for name in ["lower.c", "upper.C"]
    ]
    buckets = {
        bucket["language"]: {file.filename for file in bucket["files"]}
        for bucket in sort_files_by_main_languages(languages, files)
    }
    assert buckets == {
        "C": {"lower.c"},
        "C++": {"upper.C"},
        "Other": set(),
    }


def test_language_prioritization_falls_back_for_unambiguous_case(tmp_path):
    repo = _make_repo(tmp_path, ["module.PY"])
    provider = object.__new__(GerritProvider)
    provider.repo = repo

    languages = provider.get_languages()
    assert languages == {"Python": 100.0}

    file = type("File", (), {"filename": "module.PY"})()
    assert sort_files_by_main_languages(languages, [file]) == [
        {"language": "Python", "files": [file]},
        {"language": "Other", "files": []},
    ]


def test_get_diff_files_applies_glob_and_regex_ignore_rules(tmp_path):
    repo = _make_repo(tmp_path, ["src/keep.py", "generated/skip.py", "notes.ignore.py"])
    for name in ["src/keep.py", "generated/skip.py", "notes.ignore.py"]:
        (tmp_path / name).write_text("changed\n")
    repo.index.add(["src/keep.py", "generated/skip.py", "notes.ignore.py"])
    repo.index.commit("change files")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    settings_snapshot = settings_helpers.snapshot_settings(["ignore.glob", "ignore.regex"])
    try:
        get_settings().set("ignore.glob", ["generated/**"])
        get_settings().set("ignore.regex", [r"^notes\."])

        diff_files = provider.get_diff_files()
    finally:
        settings_helpers.restore_settings(settings_snapshot)

    assert [file.filename for file in diff_files] == ["src/keep.py"]


def test_get_diff_files_filters_each_gitpython_path_shape(tmp_path):
    repo = _make_repo(
        tmp_path,
        [
            "src/keep.py",
            "generated/delete.py",
            "src/rename_into.py",
            "generated/rename_out.py",
        ],
    )
    (tmp_path / "src/keep.py").write_text("keep changed\n")
    (tmp_path / "generated/delete.py").unlink()
    repo.index.remove(["generated/delete.py"])
    repo.git.mv("src/rename_into.py", "generated/rename_into.py")
    repo.git.mv("generated/rename_out.py", "src/rename_out.py")
    (tmp_path / "generated/new.py").write_text("new file\n")
    repo.index.add(["src/keep.py", "generated/new.py"])
    repo.index.commit("mix changed paths")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    settings_snapshot = settings_helpers.snapshot_settings(["ignore.glob", "ignore.regex"])
    try:
        get_settings().set("ignore.glob", ["generated/**"])
        get_settings().set("ignore.regex", [])

        diff_files = provider.get_diff_files()
    finally:
        settings_helpers.restore_settings(settings_snapshot)

    assert {file.filename for file in diff_files} == {"src/keep.py", "src/rename_out.py"}
    renamed = next(file for file in diff_files if file.filename == "src/rename_out.py")
    assert renamed.edit_type == EDIT_TYPE.RENAMED
    assert renamed.old_filename == "generated/rename_out.py"


def _capture_logs():
    from loguru import logger as loguru_logger

    captured = []
    sink_id = loguru_logger.add(lambda msg: captured.append(str(msg)), level="DEBUG")
    return captured, sink_id


def test_git_remote_logs_redact_credentials(tmp_path, monkeypatch):
    from loguru import logger as loguru_logger

    monkeypatch.setattr(gerrit_provider, "_call", lambda *args, **kwargs: "")
    captured, sink_id = _capture_logs()
    try:
        url = "https://secret-user@example.com/project"
        gerrit_provider.clone(url, tmp_path)
        gerrit_provider.fetch(url, "refs/changes/01/1/1", tmp_path)
    finally:
        loguru_logger.remove(sink_id)

    combined = "\n".join(captured)
    assert "secret-user" not in combined
    assert "https://example.com/project" in combined


@pytest.mark.parametrize("failing_step", ["clone", "fetch", "checkout"])
def test_prepare_repo_removes_temp_directory_when_setup_fails(tmp_path, monkeypatch, failing_step):
    repo_path = tmp_path / "clone"

    def make_temp_directory():
        repo_path.mkdir()
        return str(repo_path)

    def fail(*args, **kwargs):
        raise RuntimeError(f"{failing_step} failed")

    monkeypatch.setattr(gerrit_provider, "mkdtemp", make_temp_directory)
    monkeypatch.setattr(gerrit_provider, "clone", lambda *args, **kwargs: None)
    monkeypatch.setattr(gerrit_provider, "fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(gerrit_provider, "checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(gerrit_provider, failing_step, fail)

    with pytest.raises(RuntimeError, match=f"{failing_step} failed"):
        gerrit_provider.prepare_repo(
            urllib3.util.parse_url("https://user@example.com:443"),
            "project",
            "refs/changes/01/1/1",
        )

    assert not repo_path.exists()


def test_prepare_repo_reports_a_failed_cleanup_and_keeps_the_setup_error(tmp_path, monkeypatch):
    """A cleanup that cannot remove the directory must not replace the original setup error."""
    from loguru import logger as loguru_logger

    repo_path = tmp_path / "clone"

    def make_temp_directory():
        repo_path.mkdir()
        return str(repo_path)

    def failing_checkout(*args, **kwargs):
        raise RuntimeError("checkout failed")

    def failing_rmtree(path, **kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(gerrit_provider, "mkdtemp", make_temp_directory)
    monkeypatch.setattr(gerrit_provider, "clone", lambda *args, **kwargs: None)
    monkeypatch.setattr(gerrit_provider, "fetch", lambda *args, **kwargs: None)
    monkeypatch.setattr(gerrit_provider, "checkout", failing_checkout)
    monkeypatch.setattr("pr_agent.git_providers.gerrit_provider.shutil.rmtree", failing_rmtree)

    captured, sink_id = _capture_logs()
    try:
        with pytest.raises(RuntimeError, match="checkout failed"):
            gerrit_provider.prepare_repo(
                urllib3.util.parse_url("https://user@example.com:443"),
                "project",
                "refs/changes/01/1/1",
            )
    finally:
        loguru_logger.remove(sink_id)

    combined = "\n".join(captured)
    assert repo_path.exists()
    assert "after setup failed" in combined
    assert str(repo_path) in combined


def test_cleanup_removes_the_temp_repo_and_names_it_in_the_log(tmp_path):
    from loguru import logger as loguru_logger

    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    provider = object.__new__(GerritProvider)
    provider.repo_path = str(repo_path)

    captured, sink_id = _capture_logs()
    try:
        provider.cleanup()
    finally:
        loguru_logger.remove(sink_id)

    assert not repo_path.exists()
    assert str(repo_path) in "\n".join(captured)


@pytest.mark.parametrize("error_type", [requests.Timeout, requests.HTTPError])
@pytest.mark.parametrize("reset_fails", [False, True])
def test_suggestion_upload_failure_preserves_error_and_attempts_cleanup(tmp_path, monkeypatch, error_type, reset_fails):
    from loguru import logger as loguru_logger

    repo = _make_repo(tmp_path, ["app.py"])
    provider = object.__new__(GerritProvider)
    provider.repo_path = str(tmp_path)
    provider.refspec = "refs/changes/01/1/1"
    error = error_type("upload failed")

    def fail_upload(patch, path):
        assert "+replacement" in patch
        raise error

    def reject_comment(*args, **kwargs):
        pytest.fail("A failed upload must not publish a suggestion comment")

    monkeypatch.setattr(gerrit_provider, "upload_patch", fail_upload)
    monkeypatch.setattr(gerrit_provider, "add_comment", reject_comment)
    if reset_fails:
        def fail_reset(path):
            raise gerrit_provider.subprocess.CalledProcessError(
                1, ["git", "checkout", "--force"], stderr=b"fatal: index.lock exists",
            )

        monkeypatch.setattr(gerrit_provider, "reset_local_changes", fail_reset)
    suggestion = {
        "relevant_file": "app.py",
        "body": "Replace the line\n```suggestion\nreplacement\n```",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }

    captured, sink_id = _capture_logs()
    try:
        with pytest.raises(error_type, match="upload failed") as caught:
            provider.publish_code_suggestions([suggestion])
    finally:
        loguru_logger.remove(sink_id)

    assert caught.value is error
    if reset_fails:
        assert repo.is_dirty()
        combined = "\n".join(captured)
        assert "Failed to reset Gerrit edits after upload failed" in combined
        assert str(tmp_path) in combined
        assert "fatal: index.lock exists" in combined
    else:
        assert (tmp_path / "app.py").read_text() == "app.py\n"
        assert not repo.is_dirty()


def test_successful_suggestion_upload_propagates_reset_failure(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path, ["app.py"])
    provider = object.__new__(GerritProvider)
    provider.repo_path = str(tmp_path)
    provider.refspec = "refs/changes/01/1/1"
    error = gerrit_provider.subprocess.CalledProcessError(1, ["git", "checkout", "--force"])

    def fail_reset(path):
        raise error

    monkeypatch.setattr(gerrit_provider, "upload_patch", lambda patch, path: "https://patch.example/1")
    monkeypatch.setattr(gerrit_provider, "reset_local_changes", fail_reset)
    suggestion = {
        "relevant_file": "app.py",
        "body": "Replace the line\n```suggestion\nreplacement\n```",
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
    }

    with pytest.raises(gerrit_provider.subprocess.CalledProcessError) as caught:
        provider.publish_code_suggestions([suggestion])

    assert caught.value is error
    assert repo.is_dirty()


def test_cleanup_reports_a_failed_removal_instead_of_claiming_success(tmp_path, monkeypatch):
    """ignore_errors=True would swallow the error, leaving a 'Cleaned up' line for a repo still on disk."""
    from loguru import logger as loguru_logger

    def failing_rmtree(path, ignore_errors=False, **kwargs):
        if ignore_errors:
            return
        raise OSError("device busy")

    repo_path = tmp_path / "clone"
    repo_path.mkdir()
    provider = object.__new__(GerritProvider)
    provider.repo_path = str(repo_path)
    monkeypatch.setattr("pr_agent.git_providers.gerrit_provider.shutil.rmtree", failing_rmtree)

    captured, sink_id = _capture_logs()
    try:
        provider.cleanup()
    finally:
        loguru_logger.remove(sink_id)

    combined = "\n".join(captured)
    assert repo_path.exists()
    assert "Cleaned up temp repo" not in combined
    assert "Failed to clean up temp repo" in combined
    assert str(repo_path) in combined
    assert "device busy" in combined


def _patch_gerrit_provider_initialization(monkeypatch):
    settings_values = {
        "gerrit.url": "https://gerrit.example:29418",
        "gerrit.user": "bot",
    }
    settings = SimpleNamespace(get=lambda key: settings_values[key])
    prepare_calls = []

    def prepare_repo(url, project, refspec):
        prepare_calls.append((url, project, refspec))
        return "repo-path"

    monkeypatch.setattr(gerrit_provider, "get_settings", lambda: settings)
    monkeypatch.setattr(gerrit_provider, "prepare_repo", prepare_repo)
    monkeypatch.setattr(gerrit_provider, "Repo", lambda _path: object())
    monkeypatch.setattr(gerrit_provider, "PullRequestMimic", lambda title, files: (title, files))
    monkeypatch.setattr(GerritProvider, "get_pr_title", lambda _self: "change")
    monkeypatch.setattr(GerritProvider, "get_diff_files", lambda _self: [])
    return prepare_calls


@pytest.mark.parametrize("refspec", ["refs/changes/01/1/1", "refs/changes/23/123/4"])
def test_init_accepts_canonical_change_refspec(monkeypatch, refspec):
    prepare_calls = _patch_gerrit_provider_initialization(monkeypatch)

    provider = GerritProvider(f"my/project:{refspec}")

    assert provider.refspec == refspec
    assert prepare_calls[0][1:] == ("my/project", refspec)


@pytest.mark.parametrize(
    "refspec",
    [
        "refs/changes/1/1/1",
        "refs/changes/001/1/1",
        "refs/heads/main",
        "refs/changes/01/1",
        "refs/changes/01/1/1/extra",
        "refs/changes/ab/1/1",
    ],
)
def test_init_rejects_malformed_change_refspec_before_preparing_repo(monkeypatch, refspec):
    prepare_calls = _patch_gerrit_provider_initialization(monkeypatch)

    with pytest.raises(ValueError, match="refspec"):
        GerritProvider(f"my/project:{refspec}")

    assert prepare_calls == []


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_add_suggestion_replaces_only_the_suggested_lines(tmp_path, eol):
    src = tmp_path / "app.py"
    src.write_bytes(f"a{eol}b{eol}c{eol}".encode())

    gerrit_provider.add_suggestion(src, "B1\nB2\n", 2, 2)

    # Keep the file's line endings so the uploaded diff touches only the suggestion.
    assert src.read_bytes() == f"a{eol}B1{eol}B2{eol}c{eol}".encode()


def test_add_suggestion_matches_the_replaced_lines_in_a_mixed_ending_file(tmp_path):
    src = tmp_path / "app.py"
    src.write_bytes(b"a\nb\r\nc\n")

    gerrit_provider.add_suggestion(src, "B\n", 2, 2)

    assert src.read_bytes() == b"a\nB\r\nc\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_add_suggestion_keeps_the_file_mode(tmp_path):
    src = tmp_path / "run.sh"
    src.write_bytes(b"echo a\necho b\n")
    os.chmod(src, 0o700)

    gerrit_provider.add_suggestion(src, "echo B\n", 2, 2)

    # Check the executable bit survives, so the patch carries no mode change.
    assert os.stat(src).st_mode & 0o777 == 0o700
    assert src.read_bytes() == b"echo a\necho B\n"


def test_get_diff_files_walks_the_commit_once(tmp_path, monkeypatch):
    # Walk the commit once: a command asks for the diff several times (get_num_of_files,
    # inline comments, pr_processing) and each walk redoes rename detection.
    repo = _make_repo(tmp_path, ["a.py", "b.py"])
    for name in ["a.py", "b.py"]:
        (tmp_path / name).write_text("changed\n")
    repo.index.add(["a.py", "b.py"])
    repo.index.commit("change files")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    calls = []
    real_diff = git.Commit.diff

    def counting_diff(self, *args, **kwargs):
        calls.append(1)
        return real_diff(self, *args, **kwargs)

    monkeypatch.setattr(git.Commit, "diff", counting_diff)

    first = provider.get_diff_files()
    second = provider.get_diff_files()

    assert len(calls) == 1
    # Verify cached Diff objects reproduce the same base/head content and patch.
    assert first == second
    assert first[0].base_file != ""
    assert first[0].patch


def test_get_diff_files_reflects_ignore_rules_merged_after_construction(tmp_path):
    # Read the diff before loading repository settings, then apply the new ignore rules.
    names = ["generated.py", "generated/uv.lock", "keep.py", "yarn.lock"]
    repo = _make_repo(tmp_path, names)
    for name in names:
        (tmp_path / name).write_text("changed\n")
    repo.index.add(names)
    repo.index.commit("change files")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    settings_snapshot = settings_helpers.snapshot_settings(["ignore.glob", "ignore.regex"])
    try:
        # Simulate provider construction before repository settings are loaded.
        get_settings().set("ignore.glob", [])
        get_settings().set("ignore.regex", [])
        before_merge = provider.get_diff_files()
        raw_diffs = provider._commit_diffs
        assert [file.filename for file in before_merge] == names
        assert provider.get_filtered_diff_file_names() == ["generated/uv.lock", "yarn.lock"]

        get_settings().set("ignore.glob", ["generated.py", "generated/**"])
        after_merge = provider.get_diff_files()
        assert [file.filename for file in after_merge] == ["keep.py", "yarn.lock"]
        assert provider.get_files() == ["keep.py", "yarn.lock"]
        assert provider.get_filtered_diff_file_names() == ["yarn.lock"]
        assert provider.get_diff_files() == after_merge
        assert provider.get_filtered_diff_file_names() == ["yarn.lock"]

        get_settings().set("ignore.glob", [])
        get_settings().set("ignore.regex", ["["])
        assert [file.filename for file in provider.get_diff_files()] == names
        assert provider.get_filtered_diff_file_names() == []
        assert provider._commit_diffs is raw_diffs
        get_settings().set("ignore.regex", [])
        get_settings().set("ignore.glob", ["*.lock"])
        assert [file.filename for file in provider.get_diff_files()] == ["generated.py", "keep.py"]
        assert provider.get_filtered_diff_file_names() == []
    finally:
        settings_helpers.restore_settings(settings_snapshot)


def test_get_files_derives_names_from_the_diff(tmp_path, monkeypatch):
    # Share the cached walk with get_files(), so both agree on which files changed.
    repo = _make_repo(tmp_path, ["keep.py"])
    (tmp_path / "keep.py").write_text("changed\n")
    repo.index.add(["keep.py"])
    repo.index.commit("change keep.py")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    calls = []
    real_diff = git.Commit.diff

    def counting_diff(self, *args, **kwargs):
        calls.append(1)
        return real_diff(self, *args, **kwargs)

    monkeypatch.setattr(git.Commit, "diff", counting_diff)

    assert provider.get_files() == ["keep.py"]
    assert provider.get_files() == ["keep.py"]
    assert [file.filename for file in provider.get_diff_files()] == provider.get_files()
    # Reuse the cached walk for every filename lookup.
    assert len(calls) == 1


def test_get_files_reports_the_post_rename_path(tmp_path):
    # Report renamed files under their destination paths.
    repo = _make_repo(tmp_path, ["old_name.py"])
    repo.git.mv("old_name.py", "new_name.py")
    repo.index.commit("rename old_name.py")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    assert provider.get_files() == ["new_name.py"]


def test_get_files_applies_the_same_ignore_rules_as_the_diff(tmp_path):
    # Exclude ignored paths from language detection and the no-files guard.
    repo = _make_repo(tmp_path, ["src/keep.py", "generated/skip.py"])
    for name in ["src/keep.py", "generated/skip.py"]:
        (tmp_path / name).write_text("changed\n")
    repo.index.add(["src/keep.py", "generated/skip.py"])
    repo.index.commit("change files")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None
    settings_snapshot = settings_helpers.snapshot_settings(["ignore.glob", "ignore.regex"])
    try:
        get_settings().set("ignore.glob", ["generated/**"])
        get_settings().set("ignore.regex", [])

        files = provider.get_files()
    finally:
        settings_helpers.restore_settings(settings_snapshot)

    assert files == ["src/keep.py"]


def test_get_files_includes_files_that_diff_files_skips_as_non_utf8(tmp_path):
    # List undecodable files by name for language detection; omit their content from
    # the model diff.
    repo = _make_repo(tmp_path, ["good.py", "bad.py"])
    (tmp_path / "bad.py").write_bytes(b"\xffbefore\n")
    repo.index.add(["good.py", "bad.py"])
    repo.index.commit("add bad bytes")
    (tmp_path / "good.py").write_text("after\n")
    (tmp_path / "bad.py").write_bytes(b"\xfeafter\n")
    repo.index.add(["good.py", "bad.py"])
    repo.index.commit("change both")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    assert provider.get_files() == ["bad.py", "good.py"]
    assert [file.filename for file in provider.get_diff_files()] == ["good.py"]


def test_diff_files_attribute_refreshes_on_every_call(tmp_path):
    # Refresh diff_files for direct readers after the ignore rules change.
    repo = _make_repo(tmp_path, ["keep.py", "generated.py"])
    for name in ["keep.py", "generated.py"]:
        (tmp_path / name).write_text("changed\n")
    repo.index.add(["keep.py", "generated.py"])
    repo.index.commit("change files")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    provider.get_diff_files()
    assert {file.filename for file in provider.diff_files} == {"keep.py", "generated.py"}

    settings_snapshot = settings_helpers.snapshot_settings(["ignore.glob", "ignore.regex"])
    try:
        get_settings().set("ignore.glob", ["generated.py"])
        get_settings().set("ignore.regex", [])

        provider.get_diff_files()
    finally:
        settings_helpers.restore_settings(settings_snapshot)

    assert [file.filename for file in provider.diff_files] == ["keep.py"]


def test_get_files_includes_deleted_files(tmp_path):
    # Retain a deleted file's a_path when b_path is None so deletion-only changes
    # are not treated as empty.
    repo = _make_repo(tmp_path, ["gone.py"])
    (tmp_path / "gone.py").unlink()
    repo.index.remove(["gone.py"])
    repo.index.commit("delete gone.py")

    provider = object.__new__(GerritProvider)
    provider.repo = repo
    provider.repo_path = None

    assert provider.get_files() == ["gone.py"]
    assert provider.get_diff_files()[0].edit_type == EDIT_TYPE.DELETED


def _provider_for_suggestions(tmp_path):
    repo = git.Repo.init(tmp_path)
    (tmp_path / "app.py").write_text("print(1)\n")
    repo.index.add(["app.py"])
    repo.index.commit("initial")
    provider = object.__new__(GerritProvider)
    provider.repo_path = str(tmp_path)
    return provider


def _suggestion(relevant_file):
    return {
        "relevant_file": relevant_file,
        "relevant_lines_start": 1,
        "relevant_lines_end": 1,
        "body": "```suggestion\nprint(2)\n```",
    }


def test_publish_code_suggestions_applies_a_file_inside_the_checkout(tmp_path, monkeypatch):
    provider = _provider_for_suggestions(tmp_path)
    applied = []
    monkeypatch.setattr(gerrit_provider, "add_suggestion", lambda *args, **kwargs: applied.append(args))
    monkeypatch.setattr(gerrit_provider, "diff", lambda **kwargs: "patch")
    monkeypatch.setattr(gerrit_provider, "upload_patch", lambda patch, path: "https://patches.example/" + path)
    monkeypatch.setattr(gerrit_provider, "reset_local_changes", lambda cwd: None)
    monkeypatch.setattr(gerrit_provider, "add_comment", lambda *args, **kwargs: None)
    provider.parsed_url = None
    provider.refspec = "refs/changes/01/1/1"

    assert provider.publish_code_suggestions([_suggestion("app.py")]) is True
    assert applied and applied[0][0].name == "app.py"


def test_publish_code_suggestions_skips_a_symlink(tmp_path, monkeypatch):
    provider = _provider_for_suggestions(tmp_path)
    link = tmp_path / "linked.py"
    try:
        link.symlink_to(tmp_path / ".git" / "config")
    except OSError:
        pytest.skip("creating a symlink is not permitted here")
    applied = []
    monkeypatch.setattr(gerrit_provider, "add_suggestion", lambda *args, **kwargs: applied.append(args))

    assert provider.publish_code_suggestions([_suggestion("linked.py")]) is True
    assert applied == []
    assert (tmp_path / ".git" / "config").exists()


@pytest.mark.parametrize("relevant_file", [".git/config", ".GIT/config"])
def test_publish_code_suggestions_skips_git_metadata(tmp_path, monkeypatch, relevant_file):
    provider = _provider_for_suggestions(tmp_path)
    applied = []
    monkeypatch.setattr(gerrit_provider, "add_suggestion", lambda *args, **kwargs: applied.append(args))

    assert provider.publish_code_suggestions([_suggestion(relevant_file)]) is True
    assert applied == []
