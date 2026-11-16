import copy
import json
from unittest.mock import AsyncMock

import pytest
from starlette_context import request_cycle_context

from pr_agent.agent.pr_agent import PRAgent, parse_command
from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.review_json_output import review_json_output_path
from pr_agent.algo.run_details import command_failed
from pr_agent.algo.token_budget import AttemptTokenBudget
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.commands import ticket_pr_compliance_check as tickets_module
from pr_agent.commands.pr_questions import NO_ANALYZABLE_FILES_NOTE, PRQuestions
from pr_agent.config_loader import _find_repository_root, get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.plain_diff_provider import PlainDiffGitProvider

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


def test_emptied_working_tree_file_reconstructs_the_base(cfg, tmp_path, monkeypatch):
    """An empty working-tree file was read successfully; do not treat it as missing."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "foo.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg("plain_diff.content", "--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +0,0 @@\n-line1\n-line2\n")

    file = PlainDiffGitProvider(None).get_diff_files()[0]

    assert file.head_file == ""
    assert file.base_file == "line1\nline2\n"


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
@pytest.mark.parametrize("command,filename,response,expected_output", [
    ("/ask What changed?", "foo.py", "The second line changed.", "The second line changed."),
    ("/review", "foo.py", "review:\n  estimated_effort_to_review_[1-5]: 1\n"
                         "  relevant_tests: \"No\"\n  security_concerns: \"No\"\n"
                         "  key_issues_to_review: []\n", "No major issues detected"),
    pytest.param(
        "/review", "build.gradle",
        "review:\n  estimated_effort_to_review_[1-5]: 1\n"
        "  relevant_tests: \"No\"\n  security_concerns: \"No\"\n  key_issues_to_review: []\n",
        "No major issues detected",
        id="review-gradle-build-script",
    ),
    ("/describe", "foo.py", "title: Update the second line\ndescription: The second line changed.\n"
                  "type: [Enhancement]\npr_files: []\n", "The second line changed."),
    pytest.param(
        "/describe", "foo.py",
        "title: Update the second line\ndescription: The second line changed.\n"
        "type: [Enhancement, null]\npr_files:\n- filename: foo.py\n"
        "  changes_title: Update the second line\n  changes_summary: The second line changed.\n"
        "  label: enhancement\n",
        "### **PR Type**\nEnhancement\n",
        id="describe-mixed-type-with-file-summary",
    ),
    pytest.param(
        "/describe --pr_description.publish_labels=true", "foo.py",
        "title: Update the second line\ndescription: The second line changed.\n"
        "type: [Enhancement]\nlabels: [Enhancement, null, {name: Enhancement}, 123, false]\npr_files: []\n",
        "### **Labels**\nEnhancement\n",
        id="describe-mixed-labels-with-publishing-enabled",
    ),
])
async def test_commands_through_diff_provider_mocked_llm(
    tmp_path, monkeypatch, capsys, command, filename, response, expected_output
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
            "plain_diff.content": DIFF.replace("foo.py", filename),
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
    assert filename in calls[0]["user"]
    assert "line2-changed" in calls[0]["user"]
    assert expected_output in capsys.readouterr().out


@pytest.fixture
def ask_with_stubbed_ticket_source(tmp_path, monkeypatch):
    """Use local diff input; replace remote ticket retrieval and the model, not Ask's processing."""
    monkeypatch.chdir(tmp_path)
    calls = []
    source = AsyncMock(return_value=[])
    monkeypatch.setattr(tickets_module, "extract_tickets", source)

    class FakeAiHandler(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return "fake"

        def get_output_token_reserve(self, model, default):
            return 1600

        def build_request_messages(self, model, system_prompt, user_prompt, *, image_path=None):
            messages = super().build_request_messages(model, system_prompt, user_prompt)
            if image_path:
                messages[1]["content"] = [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_path}},
                ]
            return messages

        async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
            calls.append({"model": model, "system": system, "user": user, "image": img_path})
            if model == "gpt-4o":
                raise TimeoutError("Primary model unavailable")
            return "The change implements the requirement.", "stop"

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.model_weak": "gpt-4o-mini",
            "config.fallback_models": [],
            "config.repo_context_files": [],
            "config.publish_output": True,
            "config.extra_config_url": "",
            "config.image_input_token_allowance": 4096,
            "plain_diff.content": DIFF,
            "plain_diff.output_path": None,
            "plain_diff.json_output_path": None,
            "pr_reviewer.require_ticket_analysis_review": False,
            "related_tickets": [],
            "push_outputs.enable": False,
        }.items():
            get_settings().set(key, value)
        yield PRAgent(ai_handler=FakeAiHandler), calls, source


@pytest.mark.parametrize("enabled", [None, "false", True], ids=["default-off", "string-false", "opt-in"])
async def test_ask_ticket_context_is_opt_in(ask_with_stubbed_ticket_source, enabled, capsys):
    agent, calls, source = ask_with_stubbed_ticket_source
    ticket = {
        "ticket_url": "https://example.test/issues/123",
        "title": "Linked requirement",
        "body": "The ticket body specifies an acceptance criterion.",
        "requirements": "A separately supplied requirement.",
    }
    source.return_value = [ticket]
    foreign_ticket = {
        "ticket_url": "u/foreign", "title": "Another PR's ticket", "body": "Unrelated cached requirement."
    }
    get_settings().set("related_tickets", [foreign_ticket])
    command = "/ask Which requirement does this change implement?"
    if enabled is not None:
        get_settings().set("pr_questions.use_ticket_context", enabled)

    assert await agent.handle_request("local_diff", command) is True
    assert command_failed() is False
    assert len(calls) == 1
    assert "line2-changed" in calls[0]["user"]
    for value in ticket.values():
        assert (value in calls[0]["user"]) is (enabled is True)
    assert foreign_ticket["body"] not in calls[0]["user"]
    assert get_settings().get("related_tickets") == [foreign_ticket]
    assert source.await_count == int(enabled is True)
    assert "The change implements the requirement." in capsys.readouterr().out


async def test_ask_comment_cannot_enable_ticket_lookups(ask_with_stubbed_ticket_source):
    agent, calls, source = ask_with_stubbed_ticket_source

    assert await agent.handle_request(
        "local_diff", "/ask What changed? --pr_questions.use_ticket_context=true"
    ) is False
    source.assert_not_awaited()
    assert calls == []


async def test_ask_ticket_fetch_failure_preserves_diff_answer(ask_with_stubbed_ticket_source):
    agent, calls, source = ask_with_stubbed_ticket_source
    get_settings().set("pr_questions.use_ticket_context", True)
    get_settings().set("config.model_weak", "gpt-4o")
    get_settings().set("config.fallback_models", ["gpt-4o-mini"])
    source.side_effect = RuntimeError("Ticket source unavailable")

    assert await agent.handle_request("local_diff", "/ask What changed?") is True
    assert command_failed() is False
    assert [call["model"] for call in calls] == ["gpt-4o", "gpt-4o-mini"]
    assert all("line2-changed" in call["user"] for call in calls)
    assert all("Linked ticket context" not in call["user"] for call in calls)
    assert source.await_count == 1


@pytest.mark.parametrize("has_tickets", [True, False], ids=["bounded-tickets", "unavailable-tickets"])
async def test_ask_ticket_context_preserves_diff_across_image_fallback(
    ask_with_stubbed_ticket_source, monkeypatch, has_tickets
):
    agent, calls, source = ask_with_stubbed_ticket_source
    limits = {"gpt-4o": 20000, "gpt-4o-mini": 11000}
    monkeypatch.setattr("pr_agent.algo.token_budget.get_max_tokens", lambda model, **_: limits[model])
    monkeypatch.setattr(
        PRQuestions, "_load_conversation_history", lambda self: "1. Reviewer: Keep the original contract."
    )
    get_settings().set("config.model_weak", "gpt-4o")
    get_settings().set("config.fallback_models", ["gpt-4o-mini"])
    get_settings().set("plain_diff.content", "--- /dev/null\n+++ b/foo.py\n@@ -0,0 +1,200 @@\n" + "".join(
        f"+value_{index} = compute_requirement({index})\n" for index in range(200)
    ))
    source.return_value = [
        {"ticket_url": "u/1", "title": "Short ticket", "body": "Retain this acceptance criterion."},
        {"ticket_url": "u/2", "title": "Remaining-budget ticket", "body": "remaining_budget_requirement " * 500},
        {"ticket_url": "u/3", "title": "Long ticket", "body": "important_requirement " * 750},
    ] if has_tickets else []
    original_tickets = copy.deepcopy(source.return_value)
    command = "/ask Does the change meet the requirements? ![image](https://example.test/screenshot.png)"

    assert await agent.handle_request("local_diff", command) is True
    assert command_failed() is False
    baseline = list(calls)
    calls.clear()
    get_settings().set("pr_questions.use_ticket_context", True)
    assert await agent.handle_request("local_diff", command) is True
    assert command_failed() is False

    assert [call["model"] for call in calls] == ["gpt-4o", "gpt-4o-mini"]
    assert source.await_count == 1
    assert source.return_value == original_tickets
    for before, after in zip(baseline, calls, strict=True):
        assert after["image"] == "https://example.test/screenshot.png"
        assert after["system"] == before["system"]
        # All original context precedes the optional ticket section.
        assert after["user"].split("\n\nLinked ticket context")[0].split("\n\nResponse to")[0] == (
            before["user"].split("\n\nResponse to")[0]
        )
        assert "value_199" in after["user"]
        assert "Keep the original contract." in after["user"]
    if has_tickets:
        assert "Long ticket" in calls[0]["user"]
        assert "Short ticket" in calls[1]["user"]
        assert "Remaining-budget ticket" in calls[1]["user"]
        assert "Long ticket" not in calls[1]["user"]
        assert "1 additional related ticket(s) were omitted" in calls[1]["user"]
    else:
        assert calls == baseline
    assert get_settings().get("related_tickets") == []


async def test_ask_skips_ticket_source_without_analyzable_diff(ask_with_stubbed_ticket_source, capsys):
    agent, calls, source = ask_with_stubbed_ticket_source
    get_settings().set("plain_diff.content", DIFF.replace("foo.py", "package-lock.json"))

    get_settings().set("pr_questions.use_ticket_context", True)
    assert await agent.handle_request("local_diff", "/ask What changed?") is True

    source.assert_not_awaited()
    assert calls == []
    assert NO_ANALYZABLE_FILES_NOTE in capsys.readouterr().out


async def test_ask_keeps_answer_when_ticket_omission_notice_cannot_fit(ask_with_stubbed_ticket_source, monkeypatch):
    agent, calls, source = ask_with_stubbed_ticket_source
    monkeypatch.setattr("pr_agent.algo.token_budget.get_max_tokens", lambda model, **_: 5000)
    # Custom templates can make even the zero-ticket omission notice too large.
    get_settings().set("pr_questions_prompt.user", get_settings().pr_questions_prompt.user.replace(
        "Context notice:", "omission detail " * 5000 + "Context notice:"
    ))
    source.return_value = [{"ticket_url": "u/1", "title": "Large ticket", "body": "requirement " * 10000}]
    command = "/ask What changed?"
    assert await agent.handle_request("local_diff", command) is True
    baseline = list(calls)
    calls.clear()

    get_settings().set("pr_questions.use_ticket_context", True)
    assert await agent.handle_request("local_diff", command) is True

    assert command_failed() is False
    source.assert_awaited_once()
    assert calls == baseline


async def test_ask_keeps_ticket_prefix_when_full_context_exactly_fills_budget(
    ask_with_stubbed_ticket_source, monkeypatch
):
    agent, calls, source = ask_with_stubbed_ticket_source
    source.return_value = [
        {"ticket_url": "u/1", "title": "First requirement", "body": "Keep this useful requirement."},
        {"ticket_url": "u/2", "title": "Large requirement", "body": "requirement " * 2000},
    ]
    get_settings().set("pr_questions.use_ticket_context", True)
    command = "/ask What changed?"
    assert await agent.handle_request("local_diff", command) is True
    counter = AttemptTokenBudget.for_attempt("gpt-4o-mini", TokenHandler(None, {}, "", "", model="gpt-4o-mini"))
    exact_limit = counter.count_request_tokens(calls[0]["system"], calls[0]["user"]) + 1600
    monkeypatch.setattr("pr_agent.algo.token_budget.get_max_tokens", lambda model, **_: exact_limit)
    calls.clear()

    assert await agent.handle_request("local_diff", command) is True

    assert command_failed() is False
    assert len(calls) == 1
    assert "First requirement" in calls[0]["user"]
    assert "Large requirement" not in calls[0]["user"]
    assert "1 additional related ticket(s) were omitted" in calls[0]["user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,response,description", [
    ("/ask What changed?", "The approval threshold changed.",
     "Invoices above the limit need manager approval."),
    ("/review", "review:\n  estimated_effort_to_review_[1-5]: 1\n"
                "  relevant_tests: 'No'\n  security_concerns: 'No'\n  key_issues_to_review: []\n",
     "Invoices above the limit need manager approval."),
    pytest.param(
        "/review", "review:\n  estimated_effort_to_review_[1-5]: 1\n"
                   "  relevant_tests: 'No'\n  security_concerns: 'No'\n  key_issues_to_review: []\n",
        "Require displaying the label File Walkthrough in the UI",
        id="review-preserves-literal-file-walkthrough",
    ),
    ("/describe", "title: Generated title\ndescription: Approval now required.\n"
                  "type: [Enhancement]\npr_files: []\n",
     "### **Description**\nInvoices above the limit need manager approval."),
])
async def test_plain_diff_cli_context_reaches_command_prompts(tmp_path, monkeypatch, command, response, description):
    """Command settings must reach prompts even after the provider has been cached."""
    monkeypatch.chdir(tmp_path)
    prompts = []

    class FakeAiHandler(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return "fake"

        async def chat_completion(self, model: str, system: str, user: str,
                                  temperature: float = 0.2, img_path: str = None):
            prompts.append(system + "\n" + user)
            return response, "stop"

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.model_weak": "gpt-4o-mini",
            "config.fallback_models": [],
            "config.repo_context_files": [],
            "config.extra_config_url": "",
            "plain_diff.content": DIFF,
            "plain_diff.title": None,
            "plain_diff.description": None,
            "pr_description.use_description_markers": False,
            "pr_description.enable_pr_description": True,
            "push_outputs.enable": False,
        }.items():
            get_settings().set(key, value)

        cached_provider = get_git_provider_with_context("local_diff")
        assert cached_provider.pr.title == "Local diff review"
        request = [*parse_command(command),
                   f'--plain_diff.title={json.dumps("Require approval for invoices")}',
                   f'--plain_diff.description={json.dumps(description)}']

        assert await PRAgent(ai_handler=FakeAiHandler).handle_request("local_diff", request) is True
        assert cached_provider.pr.title == "Require approval for invoices"

    assert len(prompts) == 1
    assert "Require approval for invoices" in prompts[0]
    assert description in prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,partial,complete,partial_marker,complete_marker", [
    (
        "/describe",
        "type: [Enhancement]\ntitle: Increment\ndescription: TRUNCATED_DESCRIPTION",
        "type: [Enhancement]\ntitle: Increment\ndescription: COMPLETE_DESCRIPTION\npr_files: []\n",
        "TRUNCATED_DESCRIPTION", "COMPLETE_DESCRIPTION",
    ),
    (
        "/add_docs",
        'Code Documentation:\n- relevant file: foo.py\n  relevant line: 1\n  doc placement: after\n'
        '  documentation: |\n    """\n    TRUNCATED_DOCSTRING\n',
        'Code Documentation:\n- relevant file: foo.py\n  relevant line: 1\n  doc placement: after\n'
        '  documentation: |\n    """\n    COMPLETE_DOCSTRING\n    """\n',
        "TRUNCATED_DOCSTRING", "COMPLETE_DOCSTRING",
    ),
    (
        "/generate_labels", "labels: [Enhancement]\n", "labels: [Bug fix]\n",
        "## PR Labels:\nEnhancement", "## PR Labels:\nBug fix",
    ),
    (
        "/update_changelog", "### Fixed\n- TRUNCATED_CHANGELOG", "### Fixed\n- COMPLETE_CHANGELOG\n",
        "TRUNCATED_CHANGELOG", "COMPLETE_CHANGELOG",
    ),
], ids=["describe", "add-docs", "generate-labels", "update-changelog"])
@pytest.mark.parametrize("finish_reasons", [
    pytest.param(["stop"], id="complete-primary"),
    pytest.param(["length", "stop"], id="complete-fallback"),
    pytest.param(["length", "length"], id="fallback-exhausted"),
])
async def test_publishing_commands_reject_truncated_responses(
    tmp_path, monkeypatch, capsys, command, partial, complete, partial_marker, complete_marker, finish_reasons,
):
    """Keep parseable truncated output out of publication through the real command flow."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    head = "def increment(x):\n    return x + 1\n"
    (tmp_path / "foo.py").write_text(head, encoding="utf-8")
    diff = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n"
        " def increment(x):\n-    return x\n+    return x + 1\n"
    )
    calls = []

    class FakeAiHandler(BaseAiHandler):
        def __init__(self):
            pass

        @property
        def deployment_id(self):
            return "fake"

        async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
            reason = finish_reasons[len(calls)]
            calls.append((model, user))
            return (partial if reason == "length" else complete), reason

    succeeded = finish_reasons[-1] == "stop"
    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.model_weak": "gpt-4o-mini",
            "config.fallback_models": ["gpt-4o"],
            "config.repo_context_files": [],
            "config.publish_output": True,
            "config.publish_output_progress": False,
            "config.propagate_command_errors": False,
            "config.extra_config_url": "",
            "config.enable_custom_labels": False,
            "plain_diff.content": diff,
            "plain_diff.output_path": None,
            "plain_diff.json_output_path": None,
            "pr_description.use_description_markers": False,
            "pr_description.enable_pr_description": True,
            "pr_description.enable_semantic_files_types": True,
            "push_outputs.enable": False,
        }.items():
            get_settings().set(key, value)

        result = await PRAgent(ai_handler=FakeAiHandler).handle_request("local_diff", command)
        if succeeded:
            assert result is True
            assert command_failed() is False
        else:
            assert result is False or command_failed()

    assert [model for model, _ in calls] == ["gpt-4o-mini", "gpt-4o"][:len(finish_reasons)]
    assert all("return x + 1" in user for _, user in calls)
    output = capsys.readouterr().out
    assert partial_marker not in output
    assert (complete_marker in output) is succeeded
    if command == "/add_docs" and succeeded:
        suggestion = output.split("```suggestion\n", 1)[1].split("\n```", 1)[0]
        compile(suggestion + "\n" + head.split("\n", 1)[1], "foo.py", "exec")


_COMPLETE_REVIEW = (
    "review:\n  estimated_effort_to_review_[1-5]: '1'\n  score: '90'\n  relevant_tests: 'No'\n"
    "  key_issues_to_review: []\n  security_concerns: 'No'\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,finish_reason,expected_failure", [
    pytest.param(_COMPLETE_REVIEW, "stop", False, id="complete"),
    pytest.param(_COMPLETE_REVIEW, "length", True, id="truncated-complete-looking"),
    pytest.param(
        "review:\n  key_issues_to_review:\n  - relevant_file: foo.py\n    issue_header: Bug\n"
        "    issue_content: |\n      The call fails because",
        "length",
        True,
        id="truncated-parseable-yaml",
    ),
])
async def test_improve_alias_rejects_truncated_responses_through_diff_provider(
    tmp_path, monkeypatch, capsys, response, finish_reason, expected_failure
):
    """Verify /improve, which runs /review, rejects truncated output even when YAML parses."""
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
            calls.append(user)
            return response, finish_reason

    with request_cycle_context({"settings": copy.deepcopy(get_settings())}):
        for key, value in {
            "config.git_provider": "plain-diff",
            "config.model": "gpt-4o-mini",
            "config.model_weak": "gpt-4o-mini",
            "config.fallback_models": [],
            "config.repo_context_files": [],
            "config.publish_output": True,
            "config.publish_output_progress": False,
            "config.propagate_command_errors": False,
            "config.extra_config_url": "",
            "plain_diff.content": DIFF,
            "plain_diff.output_path": None,
            "plain_diff.json_output_path": None,
            "config.is_auto_command": False,
            "pr_reviewer.publish_review_failure_comment": True,
            "push_outputs.enable": False,
        }.items():
            get_settings().set(key, value)

        assert await PRAgent(ai_handler=FakeAiHandler).handle_request("local_diff", "/improve") is True
        assert command_failed() is expected_failure

    assert len(calls) == 1
    assert "line2-changed" in calls[0]
    output = capsys.readouterr().out
    assert ("Failed to review PR" in output) is expected_failure
