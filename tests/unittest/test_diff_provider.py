import pytest

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import _GIT_PROVIDERS
from pr_agent.git_providers.diff_provider import DiffGitProvider

# Keys these tests mutate on the process-wide settings singleton; saved and
# restored around every test so global state never leaks between tests.
_SETTINGS_KEYS = ["diff.content", "diff.output_path",
                  "config.git_provider", "config.publish_output"]


@pytest.fixture(autouse=True)
def _restore_settings():
    s = get_settings()
    saved = {k: s.get(k, None) for k in _SETTINGS_KEYS}
    yield
    for k, v in saved.items():
        s.set(k, v)


DIFF = """diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2-changed
 line3
"""


def test_registered():
    assert _GIT_PROVIDERS["diff"] is DiffGitProvider


def test_get_diff_files(monkeypatch):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    files = provider.get_diff_files()
    assert len(files) == 1
    assert files[0].filename == "foo.py"
    assert files[0].edit_type == EDIT_TYPE.MODIFIED


def test_publish_comment_to_stdout(capsys):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    provider.publish_comment("# Review\nlooks good")
    captured = capsys.readouterr()
    assert "looks good" in captured.out


def test_publish_comment_to_file(tmp_path):
    out = tmp_path / "review.md"
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", str(out))
    provider = DiffGitProvider(None)
    provider.publish_comment("# Review\nsaved")
    assert "saved" in out.read_text(encoding="utf-8")


def test_empty_diff_raises():
    get_settings().set("diff.content", "")
    get_settings().set("diff.output_path", None)
    try:
        DiffGitProvider(None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_temporary_comment_not_emitted(capsys):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    provider.publish_comment("Preparing review...", is_temporary=True)
    captured = capsys.readouterr()
    assert "Preparing review" not in captured.out


def test_publish_file_comments_not_supported():
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    assert provider.is_supported("publish_file_comments") is False


def test_path_traversal_file_not_read(tmp_path, monkeypatch):
    # SENTINEL TEST: this test FAILS if the path-traversal guard in
    # DiffGitProvider.get_diff_files() is removed.
    #
    # Without the guard, os.path.isfile("../secret.txt") would be True
    # (because we create the file below) and the provider would read its
    # contents into head_file.  With the guard in place the path escapes
    # the repo root so it is rejected and head_file stays "".
    #
    # Setup: an inner "repo" dir is a real repo root (has .git) and is the
    # working directory; the secret file lives one level up (reachable via
    # "../secret.txt" traversal). The .git marker ensures working-tree
    # enrichment is active, so this isolates the traversal guard itself.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")

    # Make the provider believe the repo root is `repo`.
    monkeypatch.chdir(repo)

    traversal_diff = (
        "diff --git a/../secret.txt b/../secret.txt\n"
        "index 0000000..1111111 100644\n"
        "--- a/../secret.txt\n"
        "+++ b/../secret.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-TOP SECRET\n"
        "+REPLACED\n"
    )
    get_settings().set("diff.content", traversal_diff)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    files = provider.get_diff_files()
    assert len(files) == 1
    # Guard must block the read: both fields must remain empty strings.
    assert files[0].head_file == "", (
        "Path-traversal guard failed: head_file was read from ../secret.txt"
    )
    assert files[0].base_file == "", (
        "Path-traversal guard failed: base_file was populated from traversal path"
    )


def test_malformed_diff_raises_valueerror():
    # A hunk with no file header triggers UnidiffParseError inside parse_unified_diff,
    # which the provider must re-raise as ValueError with a clear message.
    get_settings().set("diff.content", "@@ -1,3 +1,3 @@\n line1\n-line2\n+line2-changed\n line3\n")
    get_settings().set("diff.output_path", None)
    try:
        DiffGitProvider(None)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_no_repo_root_disables_enrichment(tmp_path, monkeypatch):
    # When run outside any git repo (no .git ancestor), enrichment must be
    # disabled and the provider must not read working-tree files even if a
    # file with the diff's name happens to exist in the CWD.
    decoy = tmp_path / "foo.py"
    decoy.write_text("line1\nline2-changed\nline3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    files = provider.get_diff_files()
    assert files[0].head_file == "", (
        "Enrichment must be disabled when no .git root is found (patch-only)"
    )
    assert files[0].base_file == ""


def test_publish_code_suggestions_renders_to_stdout(capsys):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    suggestions = [
        {"body": "**Suggestion:** use a constant", "relevant_file": "foo.py",
         "relevant_lines_start": 2, "relevant_lines_end": 2},
    ]
    # The 'improve' tool calls this unconditionally; it must not crash and must
    # render the suggestions to stdout.
    assert provider.publish_code_suggestions(suggestions) is True
    out = capsys.readouterr().out
    assert "Code suggestions" in out
    assert "foo.py:2-2" in out
    assert "use a constant" in out


def test_publish_code_suggestions_empty_is_noop(capsys):
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    assert provider.publish_code_suggestions([]) is True
    assert capsys.readouterr().out.strip() == ""


def test_incremental_review_disabled():
    # -i has no meaning for a standalone diff; the provider must disable it so
    # PRReviewer never takes the incremental path (which would TypeError).
    from pr_agent.git_providers.git_provider import IncrementalPR
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = DiffGitProvider(None)
    incremental = IncrementalPR(is_incremental=True)
    provider.get_incremental_commits(incremental)
    assert incremental.is_incremental is False


def test_diff_content_forces_diff_provider():
    # Even if config.git_provider points elsewhere (e.g. set by extra config),
    # the presence of loaded diff content must select the diff provider.
    from pr_agent.git_providers import get_git_provider_with_context
    get_settings().set("config.git_provider", "github")
    get_settings().set("diff.content", DIFF)
    get_settings().set("diff.output_path", None)
    provider = get_git_provider_with_context("local_diff")
    assert isinstance(provider, DiffGitProvider)
