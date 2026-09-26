import git

from pr_agent.git_providers.local_git_provider import LocalGitProvider


def test_get_diff_files_reuses_cached_result(tmp_path, monkeypatch):
    repo = git.Repo.init(tmp_path)
    file_path = tmp_path / "example.py"
    file_path.write_text("before\n", encoding="utf-8")
    repo.index.add(["example.py"])
    repo.index.commit("base")
    target_branch_name = repo.active_branch.name

    repo.git.checkout("-b", "feature")
    file_path.write_text("after\n", encoding="utf-8")
    repo.index.add(["example.py"])
    repo.index.commit("change example.py")

    provider = object.__new__(LocalGitProvider)
    provider.repo = repo
    provider.target_branch_name = target_branch_name

    first = provider.get_diff_files()
    assert [file.filename for file in first] == ["example.py"]

    monkeypatch.setattr(
        repo,
        "merge_base",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("diff recomputed")),
    )

    assert provider.get_diff_files() is first
