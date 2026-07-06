import pytest

from pr_agent.algo.types import EDIT_TYPE
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import _GIT_PROVIDERS
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider

# Diff-mode settings keys these tests mutate on the process-wide singleton.
_SETTINGS_KEYS = ["plain_diff.content", "plain_diff.output_path",
                  "config.git_provider", "config.publish_output"]


@pytest.fixture(autouse=True)
def cfg():
    """Restore all diff-mode settings keys after each test (autouse) and expose a
    setter so tests mutate settings through the fixture rather than bare set()
    calls. Keeps the process-wide settings singleton from leaking between tests."""
    s = get_settings()
    saved = {k: s.get(k, None) for k in _SETTINGS_KEYS}

    def _set(key, value):
        s.set(key, value)

    yield _set
    for key, value in saved.items():
        s.set(key, value)


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
    assert _GIT_PROVIDERS["plain-diff"] is PlainDiffGitProvider


def test_get_diff_files(cfg):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    files = provider.get_diff_files()
    assert len(files) == 1
    assert files[0].filename == "foo.py"
    assert files[0].edit_type == EDIT_TYPE.MODIFIED


def test_get_diff_files_patch_is_hunk_only(cfg):
    # The stored patch must not carry the 'diff --git'/'index'/'---'/'+++'
    # headers, which the shared hunk converter would misparse as a bogus hunk.
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    patch = provider.get_diff_files()[0].patch
    assert patch.startswith("@@")
    assert "diff --git" not in patch
    assert "+++ b/foo.py" not in patch


def test_publish_comment_to_stdout(cfg, capsys):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    provider.publish_comment("# Review\nlooks good")
    captured = capsys.readouterr()
    assert "looks good" in captured.out


def test_publish_comment_to_file(cfg, tmp_path):
    out = tmp_path / "review.md"
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", str(out))
    provider = PlainDiffGitProvider(None)
    provider.publish_comment("# Review\nsaved")
    assert "saved" in out.read_text(encoding="utf-8")


def test_empty_diff_raises(cfg):
    cfg("plain_diff.content", "")
    cfg("plain_diff.output_path", None)
    with pytest.raises(ValueError):
        PlainDiffGitProvider(None)


def test_temporary_comment_not_emitted(cfg, capsys):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    provider.publish_comment("Preparing review...", is_temporary=True)
    captured = capsys.readouterr()
    assert "Preparing review" not in captured.out


def test_publish_file_comments_not_supported(cfg):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    assert provider.is_supported("publish_file_comments") is False


def test_path_traversal_file_not_read(cfg, tmp_path, monkeypatch):
    # SENTINEL TEST: this test FAILS if the path-traversal guard in
    # PlainDiffGitProvider.get_diff_files() is removed.
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
    cfg("plain_diff.content", traversal_diff)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    files = provider.get_diff_files()
    assert len(files) == 1
    # Guard must block the read: both fields must remain empty strings.
    assert files[0].head_file == "", (
        "Path-traversal guard failed: head_file was read from ../secret.txt"
    )
    assert files[0].base_file == "", (
        "Path-traversal guard failed: base_file was populated from traversal path"
    )


def test_malformed_diff_raises_valueerror(cfg):
    # A hunk with no file header triggers UnidiffParseError inside parse_unified_diff,
    # which the provider must re-raise as ValueError with a clear message.
    cfg("plain_diff.content", "@@ -1,3 +1,3 @@\n line1\n-line2\n+line2-changed\n line3\n")
    cfg("plain_diff.output_path", None)
    with pytest.raises(ValueError):
        PlainDiffGitProvider(None)


def test_no_repo_root_disables_enrichment(cfg, tmp_path, monkeypatch):
    # When run outside any git repo (no .git ancestor), enrichment must be
    # disabled and the provider must not read working-tree files even if a
    # file with the diff's name happens to exist in the CWD.
    decoy = tmp_path / "foo.py"
    decoy.write_text("line1\nline2-changed\nline3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    files = provider.get_diff_files()
    assert files[0].head_file == "", (
        "Enrichment must be disabled when no .git root is found (patch-only)"
    )
    assert files[0].base_file == ""


def test_publish_code_suggestions_renders_to_stdout(cfg, capsys):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
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


def test_publish_code_suggestions_empty_is_noop(cfg, capsys):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    assert provider.publish_code_suggestions([]) is True
    assert capsys.readouterr().out.strip() == ""


def test_incremental_review_disabled(cfg):
    # -i has no meaning for a standalone diff; the provider must disable it so
    # PRReviewer never takes the incremental path (which would TypeError).
    from pr_agent.git_providers.git_provider import IncrementalPR
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)
    incremental = IncrementalPR(is_incremental=True)
    provider.get_incremental_commits(incremental)
    assert incremental.is_incremental is False


def test_diff_content_forces_diff_provider(cfg):
    # Even if config.git_provider points elsewhere (e.g. set by extra config),
    # the presence of loaded diff content must select the diff provider.
    from pr_agent.git_providers import get_git_provider_with_context
    cfg("config.git_provider", "github")
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = get_git_provider_with_context("local_diff")
    assert isinstance(provider, PlainDiffGitProvider)
