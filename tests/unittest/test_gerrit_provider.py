import git
import pytest
import urllib3.util

from pr_agent.algo.language_handler import sort_files_by_main_languages
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import gerrit_provider
from pr_agent.git_providers.gerrit_provider import GerritProvider
from tests.unittest import _settings_helpers as settings_helpers


def _make_repo(tmp_path, filenames):
    repo = git.Repo.init(tmp_path)
    for name in filenames:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n")
        repo.index.add([str(path)])
    repo.index.commit("initial files")
    return repo


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
