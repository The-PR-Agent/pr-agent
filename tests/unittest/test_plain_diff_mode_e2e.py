import copy
import json
from unittest.mock import patch

import pytest
from starlette_context import request_cycle_context

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.review_json_output import review_json_output_path
from pr_agent.algo.run_details import command_failed
from pr_agent.config_loader import _find_repository_root, get_settings
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

# Diff-mode settings keys these tests mutate on the process-wide singleton.
_SETTINGS_KEYS = ["plain_diff.content", "plain_diff.output_path", "plain_diff.json_output_path",
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


def test_find_repository_root_supports_git_worktree_file(tmp_path, monkeypatch):
    repo_root = tmp_path / "worktree"
    nested = repo_root / "src"
    nested.mkdir(parents=True)
    (repo_root / ".git").write_text("gitdir: /tmp/main/.git/worktrees/worktree\n")
    monkeypatch.chdir(nested)

    assert _find_repository_root() == repo_root


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


def test_provider_writes_structured_review(cfg, tmp_path):
    output = tmp_path / "review.json"
    other_output = tmp_path / "pr-review.json"
    cfg("plain_diff.content", DIFF)
    cfg("plain_diff.output_path", None)
    cfg("plain_diff.json_output_path", str(output))
    provider = PlainDiffGitProvider(None)

    token = review_json_output_path.set(str(other_output))
    try:
        provider.publish_structured_review({"review": {"key_issues_to_review": []}, "usage": {}})
    finally:
        review_json_output_path.reset(token)

    assert json.loads(output.read_text()) == {
        "review": {"key_issues_to_review": []},
        "usage": {},
    }
    assert not other_output.exists()


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


def test_drifted_working_tree_uses_patch_only_context(cfg, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "foo.py").write_text("unrelated working-tree content\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg("plain_diff.content", DIFF)

    file = PlainDiffGitProvider(None).get_diff_files()[0]

    assert file.head_file == ""
    assert file.base_file == ""
    assert "+line2-changed" in file.patch


def test_added_file_with_empty_base_keeps_matching_head(cfg, tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "new.py").write_text("hello\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg("plain_diff.content", "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+hello\n")

    file = PlainDiffGitProvider(None).get_diff_files()[0]

    assert file.head_file == "hello\n"
    assert file.base_file == ""


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
@pytest.mark.parametrize("command,response,expected_output", [
    ("/ask What changed?", "The second line changed.", "The second line changed."),
    ("/review", "review:\n  key_issues_to_review: []\n", "No major issues detected"),
    ("/describe", "title: Update the second line\ndescription: The second line changed.\n"
                  "type: [Enhancement]\npr_files: []\n", "The second line changed."),
])
async def test_commands_through_diff_provider_mocked_llm(
    tmp_path, monkeypatch, capsys, command, response, expected_output
):
    """Exercise command dispatch, prompt budgeting and final output with only the model stubbed."""
    monkeypatch.chdir(tmp_path)
    calls = []

    class FakeAiHandler(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return "fake"

        async def chat_completion(self, model: str, system: str, user: str,
                                  temperature: float = 0.2, img_path: str = None):
            calls.append({"system": system, "user": user})
            return response, "stop"

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.model_weak": "gpt-4o-mini",
            "config.fallback_models": [],
            "config.repo_context_files": [],
            "config.publish_output": True,
            "config.extra_config_url": "",
            "plain_diff.content": DIFF,
            "plain_diff.output_path": None,
            "plain_diff.json_output_path": None,
            "pr_reviewer.publish_output_no_suggestions": True,
            "pr_description.use_description_markers": False,
            "pr_description.enable_pr_description": True,
            "push_outputs.enable": False,
        }.items():
            get_settings().set(key, value)

        assert await PRAgent(ai_handler=FakeAiHandler).handle_request("local_diff", command) is True
        assert command_failed() is False

    assert len(calls) == 1
    assert "line2-changed" in calls[0]["user"]
    assert expected_output in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("decouple_hunks,line_count,max_tokens,remove_calls", [
    pytest.param(True, 1, 32_000, 1, id="decoupled"),
    pytest.param(False, 1, 32_000, 0, id="ordinary-default"),
    pytest.param(False, 400, 10_000, 1, id="default-numbered-budget-fallback"),
])
async def test_improve_preserves_inline_separator_in_model_prompt(
    tmp_path, monkeypatch, decouple_hunks, line_count, max_tokens, remove_calls
):
    monkeypatch.chdir(tmp_path)
    diff = (
        "diff --git a/message.py b/message.py\n--- a/message.py\n+++ b/message.py\n"
        f"@@ -1,{line_count} +1,{line_count} @@\n"
        + "".join(f'-VALUE_{i} = "old"\n' for i in range(line_count))
        + "".join(f'+VALUE_{i} = "part\u2028123"\n' for i in range(line_count))
    )
    calls = []

    class RecordingAI(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return None

        async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
            calls.append(user)
            return "code_suggestions: []", "stop"

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.max_model_tokens": max_tokens,
            "config.fallback_models": [],
            "config.repo_context_files": [],
            "config.publish_output_progress": False,
            "plain_diff.content": diff,
            "plain_diff.output_path": None,
            "plain_diff.json_output_path": None,
            "pr_code_suggestions.decouple_hunks": decouple_hunks,
        }.items():
            get_settings().set(key, value)

        # Spy on the real method: the default case must reach the fallback through token packing.
        with patch.object(PRCodeSuggestions, "remove_line_numbers", autospec=True,
                          side_effect=PRCodeSuggestions.remove_line_numbers) as remove:
            assert await PRAgent(ai_handler=RecordingAI).handle_request("local_diff", "/improve") is True

    assert remove.call_count == remove_calls
    assert len(calls) == 1
    assert '+VALUE_0 = "part\u2028123"' in calls[0]
