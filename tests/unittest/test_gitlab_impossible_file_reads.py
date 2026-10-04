from types import SimpleNamespace

import pytest

import pr_agent.git_providers.gitlab_provider as gitlab_provider


@pytest.mark.parametrize(
    ("flags", "expected_calls", "expected_base", "expected_head"),
    [
        (
            {"new_file": True, "deleted_file": False, "renamed_file": False},
            [("new.py", "head")],
            "",
            "new\n",
        ),
        (
            {"new_file": False, "deleted_file": True, "renamed_file": False},
            [("old.py", "base")],
            "old\n",
            "",
        ),
        (
            {"new_file": False, "deleted_file": False, "renamed_file": False},
            [("old.py", "base"), ("new.py", "head")],
            "old\n",
            "new\n",
        ),
        (
            {"new_file": False, "deleted_file": False, "renamed_file": True},
            [("old.py", "base"), ("new.py", "head")],
            "old\n",
            "new\n",
        ),
    ],
)
def test_full_diff_skips_file_revisions_that_cannot_exist(
    monkeypatch, flags, expected_calls, expected_base, expected_head
):
    provider = object.__new__(gitlab_provider.GitLabProvider)
    provider.diff_files = None
    provider.incremental = False
    provider.id_mr = 1

    change = {
        "old_path": "old.py",
        "new_path": "new.py",
        "diff": "@@ -1 +1 @@\n-old\n+new\n",
        **flags,
    }

    def get_merge_request_changes():
        return {
            "changes": [change],
            "diff_refs": {"base_sha": "base", "head_sha": "head"},
        }

    def expand_submodule_changes(changes, _diff_refs):
        return changes

    provider._get_merge_request_changes = get_merge_request_changes
    provider._expand_submodule_changes = expand_submodule_changes

    calls = []

    def get_content(path, revision):
        calls.append((path, revision))
        return "old\n" if revision == "base" else "new\n"

    provider.get_pr_file_content = get_content
    monkeypatch.setattr(gitlab_provider, "filter_ignored", lambda changes, _provider: changes)
    monkeypatch.setattr(gitlab_provider, "is_valid_file", lambda _path: True)

    files = provider.get_diff_files()

    assert len(files) == 1
    assert calls == expected_calls
    assert files[0].base_file == expected_base
    assert files[0].head_file == expected_head


@pytest.mark.parametrize(
    "flags",
    [
        {"new_file": True, "deleted_file": False, "renamed_file": False},
        {"new_file": False, "deleted_file": True, "renamed_file": False},
    ],
)
def test_incremental_diff_keeps_both_content_reads(monkeypatch, flags):
    provider = object.__new__(gitlab_provider.GitLabProvider)
    provider.diff_files = None
    provider.id_mr = 1
    provider.incremental = SimpleNamespace(is_incremental=True, last_seen_commit_sha="base")
    provider._incremental_head_sha = "head"
    provider.unreviewed_files_map = {
        "new.py": {
            "old_path": "old.py",
            "new_path": "new.py",
            "diff": "@@ -1 +1 @@\n-old\n+new\n",
            **flags,
        }
    }

    def expand_submodule_changes(changes, _diff_refs):
        return changes

    def get_content(path, revision):
        calls.append((path, revision))
        return "content\n"

    provider._expand_submodule_changes = expand_submodule_changes

    calls = []
    provider.get_pr_file_content = get_content
    monkeypatch.setattr(gitlab_provider, "filter_ignored", lambda changes, _provider: changes)
    monkeypatch.setattr(gitlab_provider, "is_valid_file", lambda _path: True)

    files = provider.get_diff_files()

    assert calls == [("old.py", "base"), ("new.py", "head")]
    assert files[0].base_file == "content\n"
    assert files[0].head_file == "content\n"
