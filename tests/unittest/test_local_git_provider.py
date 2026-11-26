import json
import os
import stat
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import git
import pytest

from pr_agent import cli
from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.git_patch_processing import count_patch_lines
from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.commands.pr_add_docs import PRAddDocs
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import gerrit_provider
from pr_agent.git_providers.local_git_provider import LocalGitProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


def _make_repo(tmp_path, filenames):
    repo = git.Repo.init(tmp_path)
    for name in filenames:
        f = tmp_path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x\n")
        repo.index.add([str(f)])
    repo.index.commit("init")
    return repo


@pytest.fixture
def local_commit_description(tmp_path):
    repo = _make_repo(tmp_path, ["a.py"])
    target = repo.active_branch.name
    repo.git.checkout("-b", "feature")
    older = "OLDER: preserve the existing retry boundary"
    newer = (
        "NEWER: document the changed recovery path\n"
        + "Keep the full commit context for local review. " * 12
        + "TAIL: retain cancellation behavior"
    )
    for number, message in enumerate([older, newer], start=1):
        (tmp_path / "a.py").write_text(f"value = {number}\n")
        repo.index.add(["a.py"])
        repo.index.commit(message)
    provider = object.__new__(LocalGitProvider)
    provider.repo = repo
    provider.target_branch_name = target
    return provider, newer + " " + older


def test_local_description_preserves_commit_range_order_and_tail(local_commit_description):
    provider, expected = local_commit_description
    assert len(expected) > 200
    assert provider.get_pr_description_full() == expected


def test_local_description_is_empty_without_feature_commits(tmp_path):
    repo = _make_repo(tmp_path, ["a.py"])
    provider = object.__new__(LocalGitProvider)
    provider.repo = repo
    provider.target_branch_name = repo.active_branch.name
    assert provider.get_pr_description_full() == ""


@pytest.mark.parametrize("full", [True, False])
def test_local_description_uses_shared_token_budget(local_commit_description, full):
    provider, expected = local_commit_description
    settings = get_settings()
    snapshot = snapshot_settings(["CONFIG.MAX_DESCRIPTION_TOKENS"])
    encoder = TokenEncoder.get_token_encoder()
    try:
        settings.set("CONFIG.MAX_DESCRIPTION_TOKENS", 1000)
        assert provider.get_pr_description(full=full) == expected
        assert provider.get_pr_description(split_changes_walkthrough=True) == (expected, [])

        settings.set("CONFIG.MAX_DESCRIPTION_TOKENS", 20)
        clipped = provider.get_pr_description(full=full)
        assert clipped.endswith("...(truncated)")
        assert "TAIL: retain cancellation behavior" not in clipped
        clipped_tokens = len(encoder.encode(clipped, disallowed_special=()))
        expected_tokens = len(encoder.encode(expected, disallowed_special=()))
        assert clipped_tokens < expected_tokens
        assert provider.get_user_description() == expected

        settings.set("CONFIG.MAX_DESCRIPTION_TOKENS", 1000)
        assert provider.get_pr_description(full=full) == expected
    finally:
        restore_settings(snapshot)


def test_get_languages_returns_language_names(tmp_path):
    # get_languages() must key on language NAMES (e.g. "Python"), not raw
    # extensions ("py"): sort_files_by_main_languages() maps names back to
    # extensions, so extension keys would drop every file into "Other" and
    # defeat the hunk prioritisation this method exists for.
    repo = _make_repo(tmp_path, ["a.py", "b.py", "c.py", "d.js", "weird.zzz"])
    provider = object.__new__(LocalGitProvider)  # bypass heavy __init__
    provider.repo = repo

    languages = provider.get_languages()
    # 3 Python + 1 JavaScript known; .zzz is unknown and excluded from the total.
    assert languages == {"Python": 75.0, "JavaScript": 25.0}

    # Verify the values flow through the real consumer into proper buckets.
    from pr_agent.algo.language_handler import sort_files_by_main_languages

    class _F:
        def __init__(self, name):
            self.filename = name

    files = [_F("a.py"), _F("d.js"), _F("weird.zzz")]
    buckets = {b["language"]: {f.filename for f in b["files"]}
               for b in sort_files_by_main_languages(languages, files)}
    assert buckets["Python"] == {"a.py"}
    assert buckets["JavaScript"] == {"d.js"}
    assert buckets["Other"] == {"weird.zzz"}  # unknown extension falls through


def test_get_languages_matches_full_names_and_multipart_extensions(tmp_path):
    # Beyond simple ".ext", the language map also has full-filename rules
    # ("Dockerfile") and multi-part extensions (".cmake.in"); Path.suffix alone
    # would miss both. Match on the whole filename and dotted-suffix fallbacks.
    repo = _make_repo(tmp_path, ["Dockerfile", "build.cmake.in", "app.py"])
    provider = object.__new__(LocalGitProvider)
    provider.repo = repo

    languages = provider.get_languages()
    # One file each -> ~33.33% apiece, and none dropped as "unknown".
    assert set(languages) == {"Dockerfile", "CMake", "Python"}
    assert all(abs(v - 100 / 3) < 1e-6 for v in languages.values())


def test_get_languages_preserves_case_sensitive_extensions(tmp_path):
    repo = _make_repo(tmp_path, ["lower.c", "upper.C"])
    provider = object.__new__(LocalGitProvider)
    provider.repo = repo

    assert provider.get_languages() == {"C": 50.0, "C++": 50.0}


def test_get_files_returns_new_path_for_renamed_file(tmp_path):
    repo = _make_repo(tmp_path, ["old.py"])
    target_branch_name = repo.active_branch.name
    repo.git.checkout("-b", "feature")
    (tmp_path / "old.py").rename(tmp_path / "new.py")
    repo.index.remove(["old.py"])
    repo.index.add(["new.py"])
    repo.index.commit("rename old.py to new.py")

    provider = object.__new__(LocalGitProvider)
    provider.repo = repo
    provider.target_branch_name = target_branch_name

    assert provider.get_files() == ["new.py"]


def test_shallow_checkout_requires_history_for_local_comparison(tmp_path, monkeypatch):
    source = tmp_path / "source"
    repo = _make_repo(source, ["app.py"])
    target = repo.active_branch.name
    repo.git.checkout("-b", "feature")
    (source / "app.py").write_text("changed = True\n")
    repo.index.add(["app.py"])
    repo.index.commit("Change app.py")

    checkout = git.Repo.clone_from(source.as_uri(), tmp_path / "checkout", depth=1, no_single_branch=True)
    checkout.create_head(target, f"origin/{target}")
    monkeypatch.chdir(checkout.working_tree_dir)
    snapshot = snapshot_settings(["pr_reviewer.inline_code_comments"])
    try:
        with pytest.raises(ValueError, match="No merge base"):
            LocalGitProvider(target)

        checkout.git.fetch("--unshallow")
        assert LocalGitProvider(target).get_files() == ["app.py"]
    finally:
        restore_settings(snapshot)


def test_local_diff_applies_ignore_rules_to_files_and_patches(tmp_path):
    names = ["generated/schema.py", "generated/uv.lock", "src/app.py", "yarn.lock"]
    repo = _make_repo(tmp_path, names)
    target_branch_name = repo.active_branch.name
    repo.git.checkout("-b", "feature")
    for name in names:
        (tmp_path / name).write_text("changed\n")
    repo.index.add(names)
    repo.index.commit("change files")

    provider = object.__new__(LocalGitProvider)
    provider.repo = repo
    provider.target_branch_name = target_branch_name
    provider.diff_files = None
    snapshot = snapshot_settings(["IGNORE.GLOB", "IGNORE.REGEX"])
    try:
        get_settings().set("IGNORE.GLOB", ["generated/**"])
        get_settings().set("IGNORE.REGEX", [])
        first = provider.get_diff_files()
        assert provider.get_files() == ["src/app.py", "yarn.lock"]
        assert provider.get_filtered_diff_file_names() == ["yarn.lock"]
        assert provider.get_diff_files() is first
        assert first[1].head_file == "changed\n"
        assert first[1].patch
        assert (first[1].num_plus_lines, first[1].num_minus_lines) == count_patch_lines(first[1].patch)

        get_settings().set("IGNORE.GLOB", ["*.lock"])
        assert provider.get_diff_files() is first
        assert provider.get_filtered_diff_file_names() == ["yarn.lock"]
        get_settings().set("IGNORE.GLOB", [])
        get_settings().set("IGNORE.REGEX", ["["])
        provider.reset_diff_cache_for_command()
        assert [file.filename for file in provider.get_diff_files()] == names
        assert provider.get_filtered_diff_file_names() == []
        get_settings().set("IGNORE.GLOB", ["*.lock"])
        get_settings().set("IGNORE.REGEX", [])
        provider.reset_diff_cache_for_command()
        second = provider.get_diff_files()
        assert [file.filename for file in second] == ["generated/schema.py", "src/app.py"]
        assert second is not first
        assert provider.get_filtered_diff_file_names() == []
    finally:
        restore_settings(snapshot)


def test_get_diff_files_deleted_file_falls_back_to_old_path(tmp_path):
    # A plain deletion has no "new side": GitPython sets diff_item.b_path to None.
    # The filename must fall back to a_path (the old path) instead of None, or
    # downstream consumers keying on file.filename (e.g. set_file_languages'
    # file.filename.rsplit('.')) hit AttributeError on NoneType. See issue #2580.
    repo = _make_repo(tmp_path, ["keep.py", "gone.py"])
    target_branch_name = repo.active_branch.name  # the branch that still has gone.py
    repo.git.checkout("-b", "feature")
    (tmp_path / "gone.py").unlink()
    repo.index.remove(["gone.py"])
    repo.index.commit("remove gone.py")

    provider = object.__new__(LocalGitProvider)  # bypass heavy __init__
    provider.repo = repo
    provider.target_branch_name = target_branch_name

    diff_files = provider.get_diff_files()  # must not raise

    deleted = [f for f in diff_files if f.edit_type == EDIT_TYPE.DELETED]
    assert len(deleted) == 1
    # filename falls back to the old path rather than being None.
    assert deleted[0].filename == "gone.py"
    # every diff file exposes a usable filename for downstream consumers.
    assert all(f.filename is not None for f in diff_files)


@pytest.mark.parametrize("provider_name", ["local", "gerrit"])
def test_submodule_bump_preserves_gitlink_patch_and_sibling(tmp_path, monkeypatch, provider_name):
    dep = _make_repo(tmp_path / "dependency", ["lib.py"])
    root = tmp_path / "project"
    repo = _make_repo(root, ["app.py"])
    target = repo.active_branch.name
    module = repo.create_submodule("library", "library", url=dep.working_tree_dir)
    old = module.hexsha
    repo.index.commit("pin library")
    repo.git.checkout("-b", "feature")
    new = module.module().index.commit("update library").hexsha
    (root / "app.py").write_text("updated\n")
    repo.git.add("library", "app.py")
    repo.index.commit("bump library and update caller")
    repo.git.submodule("deinit", "--", "library")

    snapshot = snapshot_settings(["pr_reviewer.inline_code_comments", "gerrit.url", "gerrit.user"])
    try:
        if provider_name == "local":
            monkeypatch.chdir(root)
            provider = LocalGitProvider(target)
        else:
            get_settings().set("gerrit.url", "ssh://gerrit.example.test:29418")
            get_settings().set("gerrit.user", "reviewer")
            monkeypatch.setattr(gerrit_provider, "prepare_repo", lambda *args: str(root))
            provider = gerrit_provider.GerritProvider("project:refs/changes/01/1/1")
        files = {file.filename: file for file in provider.pr.diff_files}
    finally:
        restore_settings(snapshot)

    link = files["library"]
    assert link.base_file == link.head_file == ""
    assert f"-Subproject commit {old}\n+Subproject commit {new}" in link.patch
    assert (files["app.py"].base_file, files["app.py"].head_file) == ("x\n", "updated\n")


@pytest.mark.parametrize("change_type", ["added", "modified", "deleted"])
def test_get_diff_files_marks_non_utf8_file_unreadable_and_keeps_utf8_sibling(tmp_path, monkeypatch, change_type):
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
    target_branch_name = repo.active_branch.name

    repo.git.checkout("-b", "feature")
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

    snapshot = snapshot_settings(["pr_reviewer.inline_code_comments"])
    try:
        monkeypatch.chdir(tmp_path)
        provider = LocalGitProvider(target_branch_name)
    finally:
        restore_settings(snapshot)

    diff_files = provider.pr.diff_files

    # The undecodable file stays in the inventory as unreadable (as CodeCommit reports it), so
    # the shared diff renderer shows a manual-review notice instead of the review silently
    # looking complete without that file. The UTF-8 sibling still gets a normal diff.
    assert sorted(file.filename for file in diff_files) == ["good.py", "non_utf8.py"]
    good = next(file for file in diff_files if file.filename == "good.py")
    unreadable = next(file for file in diff_files if file.filename == "non_utf8.py")
    assert unreadable.content_fetch_failed is True
    assert unreadable.patch == ""
    assert unreadable.base_file == unreadable.head_file == ""
    # A deletion has no destination path, so the old path is reported as it is for readable files.
    assert unreadable.old_filename == ("non_utf8.py" if change_type == "deleted" else None)
    assert unreadable.edit_type == {
        "added": EDIT_TYPE.ADDED, "modified": EDIT_TYPE.MODIFIED, "deleted": EDIT_TYPE.DELETED,
    }[change_type]
    assert good.content_fetch_failed is False
    assert good.base_file == "before\n"
    assert good.head_file == "after\n"
    assert "-before" in good.patch
    assert "+after" in good.patch
    assert good.edit_type == EDIT_TYPE.MODIFIED
    assert provider.diff_files is diff_files


@pytest.fixture
def local_output_provider(tmp_path):
    provider = object.__new__(LocalGitProvider)
    provider.repo_path = tmp_path
    provider.description_path = tmp_path / "description.md"
    provider.review_path = tmp_path / "review.md"
    provider.improve_path = tmp_path / "improve.md"
    return provider


def test_publish_code_suggestions_writes_improve_file(local_output_provider):
    # /improve has no hosted PR to attach inline comments to, so the suggestions
    # built for inline publishing are rendered to improve.md, mirroring how
    # /review and /describe persist their output locally.
    provider = local_output_provider

    code_suggestions = [
        {"body": "**Suggestion:** rename x\n```suggestion\ny = 1\n```",
         "relevant_file": "a.py", "relevant_lines_start": 3, "relevant_lines_end": 5},
        {"body": "**Suggestion:** add guard\n```suggestion\nif y:\n```",
         "relevant_file": "b.py", "relevant_lines_start": 7, "relevant_lines_end": 7},
    ]

    assert provider.publish_code_suggestions(code_suggestions) is True
    content = provider.improve_path.read_text()
    # each suggestion's file, line range and rendered body make it into the file.
    assert "### a.py [3-5]" in content
    assert "### b.py [7]" in content  # single-line range collapses to one number
    assert "rename x" in content
    assert "add guard" in content


def test_publish_code_suggestions_no_suggestions(local_output_provider):
    provider = local_output_provider
    assert provider.publish_code_suggestions([]) is True
    assert "No code suggestions found" in provider.improve_path.read_text()


def test_publish_code_suggestions_artifact_includes_partial_coverage(local_output_provider):
    provider = local_output_provider
    assert provider.publish_code_suggestions_artifact(
        [],
        artifact_footer="\n\n⚠️ **Suggestion coverage:** 1 of 2 analysis chunks failed.",
        no_suggestions_message="No code suggestions found in the successfully analyzed chunks.",
    ) is True

    content = provider.improve_path.read_text()
    assert "No code suggestions found in the successfully analyzed chunks." in content
    assert "1 of 2 analysis chunks failed" in content


def test_publish_code_suggestions_uses_custom_heading_without_identity(local_output_provider):
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    provider = local_output_provider
    try:
        get_settings().set("pr_code_suggestions.suggestions_heading", "Team Suggestions")

        provider.publish_code_suggestions([])
    finally:
        restore_settings(snapshot)

    content = provider.improve_path.read_text()
    assert content.startswith("# Team Suggestions ✨\n\n")
    assert "<!-- pr-agent:improve" not in content


def test_publish_comment_skips_temporary(local_output_provider):
    # Temporary progress comments ("Preparing suggestions...") must not clobber
    # the persisted review.md; only real output is written.
    provider = local_output_provider
    review_path = provider.review_path

    provider.publish_comment("Preparing suggestions...", is_temporary=True)
    assert not review_path.exists()

    provider.publish_comment("real review body")
    assert review_path.read_text() == "real review body"


def test_publish_description_writes_utf8_regardless_of_locale(local_output_provider, monkeypatch):
    # Simulate a Windows cp1252 locale so an fdopen() without an explicit encoding
    # fails on the emoji that /describe output carries (e.g. the usage guide header).
    fdopen = os.fdopen

    def cp1252_default_open(file, mode="r", *args, **kwargs):
        if "b" not in mode:
            kwargs.setdefault("encoding", "cp1252")
        return fdopen(file, mode, *args, **kwargs)

    monkeypatch.setattr(os, "fdopen", cp1252_default_open)
    provider = local_output_provider
    provider.publish_description("my-branch", "✨ Describe command usage guide")
    assert provider.description_path.read_text(encoding="utf-8") == "my-branch\n✨ Describe command usage guide"


@pytest.fixture
def local_describe_cli(tmp_path, monkeypatch):
    from pr_agent.config_loader import safe_request_cycle_context

    repo = _make_repo(tmp_path, ["a.py"])
    repo.create_head("target")
    (tmp_path / "a.py").write_text("y\n")
    repo.index.add(["a.py"])
    repo.index.commit("Change the return value")
    monkeypatch.chdir(tmp_path)

    (tmp_path / "review.md").write_text("Previous review\n")
    response = "type: [Enhancement]\ntitle: Update the result\ndescription: Return the new value."
    handler = SimpleNamespace(chat_completion=AsyncMock(return_value=(response, "stop")))
    monkeypatch.setattr(cli, "PRAgent", lambda: PRAgent(ai_handler=lambda: handler))
    monkeypatch.delenv("PR_AGENT_EXTRA_CONFIG_URL", raising=False)
    get_settings().as_dict()
    with safe_request_cycle_context({"settings": deepcopy(get_settings())}):
        settings = get_settings()
        settings.set("config.git_provider", "local")
        settings.set("config.extra_config_url", "")
        settings.set("config.publish_output", True)
        settings.set("push_outputs.enable", False)
        settings.set("local.description_path", str(tmp_path / "summary.md"))
        settings.set("local.review_path", str(tmp_path / "review.md"))
        settings.set("pr_description.publish_description_as_comment", True)
        yield handler


@pytest.mark.parametrize("persistent", [False, True])
def test_describe_as_comment_keeps_local_review_file(tmp_path, local_describe_cli, persistent):
    get_settings().set("pr_description.publish_description_as_comment_persistent", persistent)
    assert cli.run(["--pr_url", "target", "--json-output", "summary.json", "describe"]) is None

    assert (tmp_path / "review.md").read_text() == "Previous review\n"
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["title"] == "Update the result"
    description = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert description.startswith("## Title\n\n")
    assert "Return the new value." in description


def test_describe_rejects_overlapping_outputs_before_writing(tmp_path, local_describe_cli):
    (tmp_path / "summary.md").write_text("Previous description\n")
    assert cli.run(["--pr_url", "target", "--json-output", "summary.md", "describe"]) == 1
    assert (tmp_path / "summary.md").read_text() == "Previous description\n"
    assert (tmp_path / "review.md").read_text() == "Previous review\n"
    local_describe_cli.chat_completion.assert_not_awaited()


def test_describe_keeps_json_when_alias_is_detected_after_creation(tmp_path, monkeypatch, local_describe_cli):
    result = tmp_path / "Summary.md"
    monkeypatch.setattr("pr_agent.git_providers.local_git_provider.same_output_file",
                        lambda markdown, json_path: Path(json_path).exists())
    assert cli.run(["--pr_url", "target", "--json-output", str(result), "describe"]) == 1
    assert json.loads(result.read_text(encoding="utf-8"))["title"] == "Update the result"
    assert (tmp_path / "review.md").read_text() == "Previous review\n"


def test_description_comment_preserves_existing_output_file(tmp_path):
    provider = object.__new__(LocalGitProvider)
    provider.repo_path = tmp_path
    provider.description_path = tmp_path / "description.md"
    provider.description_path.write_text("a longer previous description\n")
    previous = provider.description_path.stat()
    provider.publish_description_comment("new\n", persistent=True)
    assert provider.description_path.read_text() == "new\n"
    assert os.path.samestat(previous, provider.description_path.stat())


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX permission bits")
@pytest.mark.parametrize(
    ("method", "args", "output_attr"),
    [
        pytest.param("publish_description", ("title", "body"), "description_path", id="description"),
        pytest.param("publish_description_comment", ("body",), "description_path", id="description-comment"),
        pytest.param("publish_comment", ("body",), "review_path", id="review"),
        pytest.param("publish_command_failure", ("describe", "failure"), "description_path", id="failure"),
        pytest.param("publish_code_suggestions", ([],), "improve_path", id="suggestions"),
    ],
)
def test_local_output_limits_permissions_only_on_creation(local_output_provider, method, args, output_attr):
    provider = local_output_provider
    path = getattr(provider, output_attr)
    publish = getattr(provider, method)
    kwargs = {"persistent": False} if method == "publish_description_comment" else {}
    old_umask = os.umask(0o027)
    try:
        publish(*args, **kwargs)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    path.chmod(0o640)
    publish(*args, **kwargs)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_description_comment_refuses_changed_output_before_writing(tmp_path, monkeypatch):
    provider = object.__new__(LocalGitProvider)
    provider.repo_path = tmp_path
    provider.description_path = tmp_path / "description.md"
    provider.description_path.write_text("previous\n")
    monkeypatch.setattr(os.path, "samestat", lambda _before, _after: False)
    with pytest.raises(ValueError, match="changed"):
        provider.publish_description_comment("new\n", persistent=True)
    assert provider.description_path.read_text() == "previous\n"


@pytest.mark.parametrize(("mode", "links"), [(stat.S_IFLNK, 1), (stat.S_IFREG, 2)])
def test_description_comment_refuses_unsuitable_output_before_open(tmp_path, monkeypatch, mode, links):
    provider = object.__new__(LocalGitProvider)
    provider.repo_path = tmp_path
    provider.description_path = tmp_path / "summary.md"
    lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_mode=mode, st_nlink=links)
                        if path == provider.description_path else lstat(path))

    with pytest.raises(ValueError, match="regular file"):
        provider.publish_description_comment("new\n", persistent=False)
    assert not provider.description_path.exists()


@pytest.mark.parametrize("in_checkout", [False, True])
def test_description_comment_checks_directory_links_inside_checkout_only(tmp_path, monkeypatch, in_checkout):
    provider = object.__new__(LocalGitProvider)
    provider.repo_path = tmp_path / "checkout"
    provider.repo_path.mkdir()
    monkeypatch.chdir(provider.repo_path)
    parent = Path("artifacts") if in_checkout else Path("../artifacts")
    parent.mkdir()
    provider.description_path = parent / "summary.md"
    alias = parent.resolve()
    monkeypatch.setattr(Path, "is_symlink", lambda directory: directory == alias)
    target = tmp_path / "output"
    target.mkdir()
    monkeypatch.setattr(Path, "readlink", lambda directory: target)

    if in_checkout:
        with pytest.raises(ValueError, match="linked directory"):
            provider.publish_description_comment("new\n", persistent=False)
        assert not provider.description_path.exists()
    else:
        provider.publish_description_comment("new\n", persistent=False)
        assert (target / "summary.md").read_text() == "new\n"


def test_local_output_directory_checks_link_targets(local_output_provider, monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local_output_provider.repo_path = checkout
    external = tmp_path / "external"
    external.mkdir()
    links = {tmp_path / "alias": checkout / "artifacts", checkout / "artifacts": external}
    monkeypatch.setattr(Path, "is_symlink", lambda path: path in links)
    monkeypatch.setattr(Path, "readlink", lambda path: links[path])

    with pytest.raises(ValueError, match="linked directory in the checkout"):
        local_output_provider._resolve_output_directory(tmp_path / "alias")


def test_local_output_directory_uses_checkout_identity(local_output_provider, monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    alternate = tmp_path / "alternate-spelling"
    checkout.mkdir()
    alternate.mkdir()
    local_output_provider.repo_path = checkout
    link = alternate / "artifacts"
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == link)
    monkeypatch.setattr(Path, "samefile", lambda path, other: path == alternate and other == checkout)

    with pytest.raises(ValueError, match="linked directory in the checkout"):
        local_output_provider._resolve_output_directory(link)


@pytest.mark.parametrize(
    "command, output_attr",
    [("improve", "improve_path"), ("describe", "description_path"), ("add_docs", "improve_path")],
)
def test_publish_command_failure_keeps_review_file(local_output_provider, command, output_attr):
    # A failed /improve, /describe or /add_docs must not overwrite the persisted /review result.
    provider = local_output_provider
    review_path = provider.review_path
    review_path.write_text("previous review")

    provider.publish_command_failure(command, "Failed")

    assert review_path.read_text() == "previous review"
    assert getattr(provider, output_attr).read_text() == "Failed"


def test_add_docs_empty_result_keeps_review_file(local_output_provider):
    provider = local_output_provider
    review_path = provider.review_path
    review_path.write_text("previous review")
    command = PRAddDocs.__new__(PRAddDocs)
    command.git_provider = provider

    result = command.push_inline_docs({"Code Documentation": []})

    assert result is None
    assert review_path.read_text() == "previous review"
    assert provider.improve_path.read_text() == "No code documentation found to improve this PR."


def test_init_on_detached_head_falls_back_to_commit_sha(tmp_path, monkeypatch):
    # CI checkouts often point HEAD at a bare commit; repo.head.ref then raises
    # TypeError. The branch name is only used as the PR-mimic title, so fall
    # back to the short SHA and keep the diff working. See issue #2669.
    repo = _make_repo(tmp_path, ["a.py"])
    target_branch_name = repo.active_branch.name
    repo.git.checkout("-b", "feature")
    (tmp_path / "a.py").write_text("y\n")
    repo.index.add(["a.py"])
    commit = repo.index.commit("change a.py")
    repo.git.checkout(commit.hexsha)
    assert repo.head.is_detached

    monkeypatch.chdir(tmp_path)
    provider = LocalGitProvider(target_branch_name)

    assert provider.get_pr_title() == commit.hexsha[:7]
    assert [f.filename for f in provider.get_diff_files()] == ["a.py"]


def _make_feature_branch_provider(tmp_path, monkeypatch, branch_name):
    repo = _make_repo(tmp_path, ["a.py"])
    target_branch_name = repo.active_branch.name
    repo.git.checkout("-b", branch_name)
    (tmp_path / "a.py").write_text("y\n")
    repo.index.add(["a.py"])
    commit = repo.index.commit("change a.py")
    monkeypatch.chdir(tmp_path)
    return repo, commit, LocalGitProvider(target_branch_name)


def test_get_pr_branch_returns_branch_name_string(tmp_path, monkeypatch):
    # get_pr_branch() is consumed as text (prompt variables, ticket-key scanning),
    # so it must return the branch name rather than GitPython's HEAD object.
    _, _, provider = _make_feature_branch_provider(tmp_path, monkeypatch, "feature/PROJ-123-fix")

    assert provider.get_pr_branch() == "feature/PROJ-123-fix"


def test_get_pr_branch_on_detached_head_returns_commit_sha(tmp_path, monkeypatch):
    repo, commit, _ = _make_feature_branch_provider(tmp_path, monkeypatch, "feature")
    repo.git.checkout(commit.hexsha)
    assert repo.head.is_detached

    provider = LocalGitProvider("feature")

    assert provider.get_pr_branch() == commit.hexsha[:7]


def test_add_jira_tickets_scans_local_branch_name(tmp_path, monkeypatch):
    # Regression: add_jira_tickets() joins title, description and branch into one
    # string. A non-str branch made the join raise, which was logged as
    # "Error extracting Jira tickets: ... expected str instance, HEAD found" on
    # every local run, even with Jira unconfigured.
    from pr_agent.commands import ticket_pr_compliance_check as tickets

    _, _, provider = _make_feature_branch_provider(tmp_path, monkeypatch, "feature/PROJ-123-fix")
    scanned = []
    monkeypatch.setattr(tickets, "extract_jira_tickets",
                        lambda text, *args, **kwargs: scanned.append(text) or [])

    assert tickets.add_jira_tickets(provider, []) == []
    assert len(scanned) == 1
    assert "feature/PROJ-123-fix" in scanned[0]


def _make_repo_context_provider(tmp_path, monkeypatch):
    # Commit "base rules" to AGENTS.md on the target branch and "head rules" on the
    # feature branch so a test can tell which revision was read.
    repo = _make_repo(tmp_path, ["a.py", "docs/guide.md"])
    target_branch_name = repo.active_branch.name
    (tmp_path / "AGENTS.md").write_bytes(b"base rules\n")
    repo.index.add(["AGENTS.md"])
    target_commit = repo.index.commit("add AGENTS.md")
    repo.git.checkout("-b", "feature")
    (tmp_path / "AGENTS.md").write_bytes(b"head rules\n")
    repo.index.add(["AGENTS.md"])
    repo.index.commit("change AGENTS.md")
    monkeypatch.chdir(tmp_path)
    return target_commit, LocalGitProvider(target_branch_name)


def test_get_repo_file_content_reads_target_branch_not_head(tmp_path, monkeypatch):
    # Read repo context from the target branch, like the hosted providers, so the
    # reviewed changes cannot rewrite the instructions used to review them.
    target_commit, provider = _make_repo_context_provider(tmp_path, monkeypatch)

    assert provider.get_repo_file_content("AGENTS.md") == "base rules\n"
    assert provider.get_repo_file_content("AGENTS.md", from_default_branch=True) == "base rules\n"
    assert provider.get_repo_context_ref() == target_commit.hexsha


@pytest.mark.parametrize("file_path", ["MISSING.md", "docs", "docs/missing.md", "../AGENTS.md"])
def test_get_repo_file_content_returns_empty_for_non_file_paths(tmp_path, monkeypatch, file_path):
    _, provider = _make_repo_context_provider(tmp_path, monkeypatch)

    assert provider.get_repo_file_content(file_path) == ""


def test_build_repo_context_includes_local_agents_file(tmp_path, monkeypatch):
    from pr_agent.algo.repo_context import build_repo_context

    _, provider = _make_repo_context_provider(tmp_path, monkeypatch)
    snapshot = snapshot_settings(["config.repo_context_files"])
    try:
        get_settings().set("config.repo_context_files", ["AGENTS.md"])
        repo_context = build_repo_context(provider)
    finally:
        restore_settings(snapshot)

    assert "AGENTS.md" in repo_context
    assert "base rules" in repo_context
    assert "head rules" not in repo_context
