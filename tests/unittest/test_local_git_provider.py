import git

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.git_providers.local_git_provider import LocalGitProvider


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
