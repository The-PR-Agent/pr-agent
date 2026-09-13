from types import SimpleNamespace

from pr_agent.tools.pr_help_docs import PRHelpDocs


def _help_docs_with_clone(tmp_path, docs_path):
    clone_root = tmp_path / "clone"
    clone_root.mkdir(parents=True, exist_ok=True)
    (clone_root / "docs" / "guide.md").parent.mkdir(parents=True, exist_ok=True)
    (clone_root / "docs" / "guide.md").write_text("docs content", encoding="utf-8")
    provider = PRHelpDocs.__new__(PRHelpDocs)
    provider.repo_url = "https://github.com/org/repo"
    provider.ctx_url = "https://github.com/org/repo/pull/1"
    provider.include_root_readme_file = False
    provider.supported_doc_exts = [".md"]
    provider.docs_path = docs_path
    provider.git_provider = SimpleNamespace(clone=lambda url, dst, remove_dest_folder: SimpleNamespace(
        path=str(clone_root)))
    return provider


def test_docs_path_within_clone_is_read(tmp_path):
    provider = _help_docs_with_clone(tmp_path, "docs")
    result = provider._gen_filenames_to_contents_map_from_repo()
    assert result and "docs content" in next(iter(result.values()))


def test_absolute_docs_path_escaping_clone_is_rejected(tmp_path):
    provider = _help_docs_with_clone(tmp_path, "/tmp")
    assert provider._gen_filenames_to_contents_map_from_repo() == {}


def test_traversing_docs_path_escaping_clone_is_rejected(tmp_path):
    provider = _help_docs_with_clone(tmp_path, "../../escape")
    assert provider._gen_filenames_to_contents_map_from_repo() == {}
