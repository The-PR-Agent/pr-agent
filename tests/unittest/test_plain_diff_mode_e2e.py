import pytest

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.config_loader import get_settings
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


def test_provider_end_to_end_files_and_output(cfg, capsys):
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)

    # files parsed and content reconstructed where working tree is absent
    files = provider.get_diff_files()
    assert files[0].filename == "foo.py"
    # The causal condition: head_file is empty because foo.py is not on disk
    assert files[0].head_file == ""
    # Consequence: base_file is also empty (patch-only fallback, no reconstruction)
    assert files[0].base_file == ""  # foo.py not on disk -> patch-only fallback

    # output reaches stdout
    provider.publish_comment("## PR Review\n- finding one")
    assert "finding one" in capsys.readouterr().out


def test_base_file_reconstructed_from_working_tree(cfg, tmp_path, monkeypatch):
    """When the working-tree file exists, head_file is populated and base_file
    is reconstructed from it by reversing the diff patch."""
    # Mark tmp_path as a repository root so _find_repository_root() resolves
    # diff paths against it.
    (tmp_path / ".git").mkdir()
    # The HEAD (current) content of foo.py — line2 has already been changed
    head_content = "line1\nline2-changed\nline3\n"
    foo = tmp_path / "foo.py"
    foo.write_text(head_content, encoding="utf-8")

    # Change cwd so the provider's relative-path lookup resolves into tmp_path
    monkeypatch.chdir(tmp_path)

    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)

    files = provider.get_diff_files()
    assert files[0].filename == "foo.py"

    # head_file must be the working-tree content (non-empty)
    assert files[0].head_file != "", "head_file should be populated from the working-tree file"
    assert "line2-changed" in files[0].head_file

    # base_file must be reconstructed (non-empty)
    assert files[0].base_file != "", "base_file should be reconstructed by reversing the patch"
    # The original (pre-change) content must contain the old line, not the new one
    assert "line2" in files[0].base_file
    assert "line2-changed" not in files[0].base_file


def test_base_file_reconstructed_when_run_from_subdirectory(cfg, tmp_path, monkeypatch):
    """Regression for the CWD-vs-repo-root bug: enrichment must still find
    working-tree files when the CLI is run from a subdirectory of the repo."""
    # tmp_path is the repo root; foo.py lives at the root, matching the diff path.
    (tmp_path / ".git").mkdir()
    foo = tmp_path / "foo.py"
    foo.write_text("line1\nline2-changed\nline3\n", encoding="utf-8")

    # Run from a nested subdirectory, not the repo root.
    subdir = tmp_path / "pkg" / "sub"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    provider = PlainDiffGitProvider(None)

    files = provider.get_diff_files()
    assert files[0].filename == "foo.py"
    # Despite running from a subdir, the file is resolved against the repo root.
    assert files[0].head_file != "", "head_file should be found via repo root, not CWD"
    assert files[0].base_file != "", "base_file should be reconstructed from repo-root file"
    assert "line2" in files[0].base_file


@pytest.mark.asyncio
async def test_review_command_through_diff_provider_mocked_llm(cfg, monkeypatch):
    """Integration test: drives PRReviewer with a fake AI handler through the
    diff provider.  We assert that the AI handler is invoked with a prompt that
    contains the changed content from the diff, proving end-to-end wiring from
    diff -> PlainDiffGitProvider -> PRReviewer -> LLM boundary.

    We do NOT attempt to parse back a full schema-valid review YAML from the
    fake response, because the exact expected schema is prone to breaking with
    prompt changes.  The 'handler was called with the diff content' assertion is
    the meaningful claim here.
    """
    from pr_agent.tools.pr_reviewer import PRReviewer

    # --- configure provider ---
    cfg("config.git_provider", "plain-diff")
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    # Disable publish so we don't need a real comment sink
    cfg("config.publish_output", False)

    # --- fake AI handler ---
    # PRReviewer calls ai_handler() (a factory/partial), so we pass a class
    # whose constructor returns the fake instance.
    calls = []  # records (system, user) for each chat_completion call

    class FakeAiHandler(BaseAiHandler):
        def __init__(self):
            # No super().__init__() — BaseAiHandler is abstract, nothing to call
            self.main_pr_language = None

        @property
        def deployment_id(self):
            return "fake"

        async def chat_completion(self, model: str, system: str, user: str,
                                  temperature: float = 0.2, img_path: str = None):
            calls.append({"system": system, "user": user})
            # Return a minimal response; PRReviewer will store this as self.prediction
            # and then attempt _prepare_pr_review().  If parsing fails the run()
            # method catches the exception gracefully, so the test won't error out.
            return ("## Review\nNo major issues detected.", "stop")

    reviewer = PRReviewer("local_diff", ai_handler=FakeAiHandler, args=[])
    await reviewer.run()

    # The AI handler must have been called at least once
    assert len(calls) >= 1, "FakeAiHandler.chat_completion was never called — end-to-end wiring is broken"

    # The prompt sent to the LLM must contain content derived from the diff
    # (the changed line "line2-changed" appears in the diff hunk)
    all_prompt_text = " ".join(c["system"] + c["user"] for c in calls)
    assert "line2" in all_prompt_text, (
        "The diff content ('line2') was not found in the prompt sent to the AI handler. "
        "End-to-end wiring from diff -> provider -> reviewer -> LLM is broken."
    )
