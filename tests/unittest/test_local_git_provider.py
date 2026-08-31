import git
import pytest

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.local_git_provider import LocalGitProvider
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
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


@pytest.mark.parametrize("change_type", ["added", "modified", "deleted"])
def test_get_diff_files_skips_non_utf8_file_and_keeps_utf8_sibling(tmp_path, monkeypatch, change_type):
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

    assert [file.filename for file in diff_files] == ["good.py"]
    assert diff_files[0].base_file == "before\n"
    assert diff_files[0].head_file == "after\n"
    assert "-before" in diff_files[0].patch
    assert "+after" in diff_files[0].patch
    assert diff_files[0].edit_type == EDIT_TYPE.MODIFIED
    assert provider.diff_files is diff_files


def test_publish_code_suggestions_writes_improve_file(tmp_path):
    # /improve has no hosted PR to attach inline comments to, so the suggestions
    # built for inline publishing are rendered to improve.md, mirroring how
    # /review and /describe persist their output locally.
    improve_path = tmp_path / "improve.md"
    provider = object.__new__(LocalGitProvider)  # bypass heavy __init__
    provider.improve_path = improve_path

    code_suggestions = [
        {"body": "**Suggestion:** rename x\n```suggestion\ny = 1\n```",
         "relevant_file": "a.py", "relevant_lines_start": 3, "relevant_lines_end": 5},
        {"body": "**Suggestion:** add guard\n```suggestion\nif y:\n```",
         "relevant_file": "b.py", "relevant_lines_start": 7, "relevant_lines_end": 7},
    ]

    assert provider.publish_code_suggestions(code_suggestions) is True
    content = improve_path.read_text()
    # each suggestion's file, line range and rendered body make it into the file.
    assert "### a.py [3-5]" in content
    assert "### b.py [7]" in content  # single-line range collapses to one number
    assert "rename x" in content
    assert "add guard" in content


def test_publish_code_suggestions_no_suggestions(tmp_path):
    improve_path = tmp_path / "improve.md"
    provider = object.__new__(LocalGitProvider)
    provider.improve_path = improve_path

    assert provider.publish_code_suggestions([]) is True
    assert "No code suggestions found" in improve_path.read_text()


def test_publish_code_suggestions_artifact_includes_partial_coverage(tmp_path):
    improve_path = tmp_path / "improve.md"
    provider = object.__new__(LocalGitProvider)
    provider.improve_path = improve_path

    assert provider.publish_code_suggestions_artifact(
        [],
        artifact_footer="\n\n⚠️ **Suggestion coverage:** 1 of 2 analysis chunks failed.",
        no_suggestions_message="No code suggestions found in the successfully analyzed chunks.",
    ) is True

    content = improve_path.read_text()
    assert "No code suggestions found in the successfully analyzed chunks." in content
    assert "1 of 2 analysis chunks failed" in content


def test_publish_code_suggestions_uses_custom_heading_without_identity(tmp_path):
    snapshot = snapshot_settings(["pr_code_suggestions.suggestions_heading"])
    improve_path = tmp_path / "improve.md"
    provider = object.__new__(LocalGitProvider)
    provider.improve_path = improve_path
    try:
        get_settings().set("pr_code_suggestions.suggestions_heading", "Team Suggestions")

        provider.publish_code_suggestions([])
    finally:
        restore_settings(snapshot)

    content = improve_path.read_text()
    assert content.startswith("# Team Suggestions ✨\n\n")
    assert "<!-- pr-agent:improve" not in content


@pytest.mark.asyncio
async def test_publish_no_suggestions_routes_local_git_output_to_improve_file(tmp_path, monkeypatch):
    snapshot = snapshot_settings([
        "config.output_run_details",
        "config.publish_output",
        "pr_code_suggestions.publish_output_no_suggestions",
        "pr_code_suggestions.suggestions_heading",
    ])
    improve_path = tmp_path / "improve.md"
    review_path = tmp_path / "review.md"
    provider = object.__new__(LocalGitProvider)
    provider.improve_path = improve_path
    provider.review_path = review_path
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = provider
    tool.progress_response = None
    try:
        get_settings().set("config.output_run_details", True)
        get_settings().set("config.publish_output", True)
        get_settings().set("pr_code_suggestions.publish_output_no_suggestions", True)
        get_settings().set("pr_code_suggestions.suggestions_heading", "Team Suggestions")
        monkeypatch.setattr(
            "pr_agent.git_providers.local_git_provider.show_run_details",
            lambda gfm_supported: "\n\nRun details" if not gfm_supported else "",
        )

        await tool.publish_no_suggestions()
    finally:
        restore_settings(snapshot)

    assert provider.supports_code_suggestions_artifact() is True
    content = improve_path.read_text()
    assert content.startswith("# Team Suggestions ✨\n\n")
    assert "No code suggestions found for the PR." in content
    assert "Run details" in content
    assert "<!-- pr-agent:improve" not in content
    assert not review_path.exists()


def test_publish_comment_skips_temporary(tmp_path):
    # Temporary progress comments ("Preparing suggestions...") must not clobber
    # the persisted review.md; only real output is written.
    review_path = tmp_path / "review.md"
    provider = object.__new__(LocalGitProvider)
    provider.review_path = review_path

    provider.publish_comment("Preparing suggestions...", is_temporary=True)
    assert not review_path.exists()

    provider.publish_comment("real review body")
    assert review_path.read_text() == "real review body"


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
