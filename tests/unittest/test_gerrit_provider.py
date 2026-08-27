import git

from pr_agent.algo.language_handler import sort_files_by_main_languages
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
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
