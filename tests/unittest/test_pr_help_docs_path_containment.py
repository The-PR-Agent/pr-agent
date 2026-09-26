from types import SimpleNamespace

from pr_agent.tools.pr_help_docs import PRHelpDocs


def _help_docs_with_clone(tmp_path, docs_path, include_root_readme_file=False):
    clone_root = tmp_path / "clone"
    clone_root.mkdir(parents=True, exist_ok=True)
    (clone_root / "docs" / "guide.md").parent.mkdir(parents=True, exist_ok=True)
    (clone_root / "docs" / "guide.md").write_text("docs content", encoding="utf-8")
    provider = PRHelpDocs.__new__(PRHelpDocs)
    provider.repo_url = "https://github.com/org/repo"
    provider.ctx_url = "https://github.com/org/repo/pull/1"
    provider.include_root_readme_file = include_root_readme_file
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
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("HOST SECRET CONTENT", encoding="utf-8")
    provider = _help_docs_with_clone(tmp_path, str(outside))
    assert provider._gen_filenames_to_contents_map_from_repo() == {}


def test_traversing_docs_path_escaping_clone_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("HOST SECRET CONTENT", encoding="utf-8")
    provider = _help_docs_with_clone(tmp_path, "../outside")
    assert provider._gen_filenames_to_contents_map_from_repo() == {}


def test_symlinked_doc_file_escaping_clone_is_skipped(tmp_path):
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("HOST SECRET CONTENT", encoding="utf-8")
    provider = _help_docs_with_clone(tmp_path, "docs")
    (tmp_path / "clone" / "docs" / "leak.md").symlink_to(secret)

    result = provider._gen_filenames_to_contents_map_from_repo()

    assert any("docs content" in v for v in result.values())
    assert not any("HOST SECRET" in v for v in result.values())


def test_symlinked_root_readme_escaping_clone_is_skipped(tmp_path):
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("HOST SECRET CONTENT", encoding="utf-8")
    provider = _help_docs_with_clone(tmp_path, "docs", include_root_readme_file=True)
    clone_root = tmp_path / "clone"
    (clone_root / "readme.txt").write_text("clone readme", encoding="utf-8")
    (clone_root / "README.md").symlink_to(secret)

    result = provider._gen_filenames_to_contents_map_from_repo()

    assert any("clone readme" in v for v in result.values())
    assert not any("HOST SECRET" in v for v in result.values())


def test_symlink_to_in_clone_file_is_kept(tmp_path):
    provider = _help_docs_with_clone(tmp_path, "docs")
    clone_root = tmp_path / "clone"
    (clone_root / "docs" / "guide.md").write_text("original content", encoding="utf-8")
    (clone_root / "docs" / "alias.md").symlink_to(clone_root / "docs" / "guide.md")

    result = provider._gen_filenames_to_contents_map_from_repo()

    assert sum("original content" in v for v in result.values()) >= 1
