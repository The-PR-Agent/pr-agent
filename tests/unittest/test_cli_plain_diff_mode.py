import builtins
import io
import json
import os
from unittest.mock import AsyncMock

import pytest
from git import Repo

from pr_agent import git_providers
from pr_agent.agent.request_policy import RequestOutcome
from pr_agent.algo.artifacts import get_artifact_context
from pr_agent.algo.review_json_output import (
    hosted_markdown_artifact,
    hosted_markdown_config_error,
    hosted_markdown_export,
)
from pr_agent.cli import _resolve_output_option, commands, run, set_parser
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.local_git_provider import LocalGitProvider

# Keys run() mutates on the process-wide settings singleton, directly or via the
# diff-mode CLI path. Snapshotted and restored around every test (autouse) so
# state never leaks, even when run() sets keys the test never touches itself.
_SETTINGS_KEYS = [
    "ignore.glob",
    "plain_diff.content",
    "plain_diff.output_path",
    "plain_diff.json_output_path",
    "config.git_provider",
    "config.publish_output",
    "config.propagate_command_errors",
    "config.cli_mode",
    "config.config_branch",
    "config.extra_config_url",
    "data.artifact",
    "pr_reviewer.per_commit_review",
]

_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n-line2\n+line2-changed\n line3\n"
)
_REVIEW_MARKER = "plain-diff review output"
_CANNED_REVIEW = f"""\
review:
  estimated_effort_to_review_[1-5]: '1'
  score: '90'
  relevant_tests: 'No'
  key_issues_to_review:
    - relevant_file: foo.py
      issue_header: '{_REVIEW_MARKER}'
      issue_content: 'Verify the changed line.'
      start_line: 2
      end_line: 2
  security_concerns: 'No'
"""
_OUTPUT_OPTION_PREFIXES = [
    (option[:length], option)
    for option in ("--output", "--json-output")
    for length in range(3, len(option) + 1)
]

_MARKDOWN_COMMANDS = [
    "review",
    "review_pr",
    "describe",
    "describe_pr",
    "improve",
    "improve_code",
    "ask",
    "ask_question",
    "config",
    "settings",
    "help",
]

_NON_MARKDOWN_COMMANDS = [
    "ask_line",
    "update_changelog",
    "similar_issue",
    "add_docs",
    "generate_labels",
]


def test_output_command_matrix_covers_cli_commands():
    assert set(_MARKDOWN_COMMANDS) | set(_NON_MARKDOWN_COMMANDS) == set(commands)
    assert set(_MARKDOWN_COMMANDS).isdisjoint(_NON_MARKDOWN_COMMANDS)


def _rejecting_input_args(input_mode, monkeypatch):
    class UnreadableStdin:
        def read(self):
            pytest.fail("stdin must not be read for invalid output options")

    if input_mode == "stdin":
        monkeypatch.setattr("sys.stdin", UnreadableStdin())
        return ["--stdin"]

    original_open = builtins.open

    def guarded_open(path, *args, **kwargs):
        if path == "changes.diff":
            pytest.fail("the diff file must not be opened for invalid output options")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded_open)
    return ["--diff-file", "changes.diff"]


def _valid_input_args(input_mode, monkeypatch, tmp_path):
    if input_mode == "stdin":
        monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
        return ["--stdin"]

    diff_file = tmp_path / "changes.diff"
    diff_file.write_text(_DIFF, encoding="utf-8")
    return ["--diff-file", str(diff_file)]


def _fail_if_agent_constructed(monkeypatch):
    class NeverAgent:
        def __init__(self):
            pytest.fail("PRAgent must not be constructed for invalid output options")

    monkeypatch.setattr("pr_agent.cli.PRAgent", NeverAgent)


@pytest.fixture(autouse=True)
def cfg():
    """Restore all diff-mode settings keys after each test, and expose a setter
    so tests mutate settings through the fixture rather than bare set() calls."""
    s = get_settings()
    saved = {k: s.get(k, None) for k in _SETTINGS_KEYS}

    def _set(key, value):
        s.set(key, value)

    yield _set
    for key, value in saved.items():
        s.set(key, value)


def test_parser_has_diff_flags():
    parser = set_parser()
    args = parser.parse_args([
        "--diff-file", "x.diff", "--output", "out.md",
        "--json-output", "out.json", "review",
    ])
    assert args.diff_file == "x.diff"
    assert args.output == "out.md"
    assert args.json_output == "out.json"
    assert args.command == "review"


def test_parser_stdin_flag():
    parser = set_parser()
    args = parser.parse_args(["--stdin", "review"])
    assert args.stdin is True


@pytest.mark.parametrize(
    ("option_spelling", "option"),
    _OUTPUT_OPTION_PREFIXES,
)
def test_parser_accepts_unambiguous_output_abbreviations_before_command(
    option_spelling, option,
):
    parser = set_parser()
    args = parser.parse_args(["--stdin", option_spelling, "result", "review"])

    destination = option[2:].replace("-", "_")
    assert getattr(args, destination) == "result"


def test_misplaced_output_abbreviation_must_be_unambiguous():
    parser = set_parser()
    parser.add_argument("--outcome")

    assert _resolve_output_option(parser, "--out=value") is None
    assert _resolve_output_option(parser, "--output=value") == "--output"


def test_missing_diff_file_fails_fast(tmp_path, monkeypatch, capsys):
    """A non-existent --diff-file must exit cleanly via parser.error (SystemExit)
    with a clear message, not crash with an uncaught OSError traceback."""
    missing = tmp_path / "does-not-exist.diff"

    def fail_if_entered():
        pytest.fail("missing diff-file errors must not enter the settings scope")

    monkeypatch.setattr("pr_agent.cli._cli_settings_scope", fail_if_entered)

    with pytest.raises(SystemExit):
        run(inargs=["--diff-file", str(missing), "review"])
    err = capsys.readouterr().err
    assert "Could not read --diff-file" in err


@pytest.mark.parametrize("command", [
    "review", "review_pr", "improve", "improve_code", "describe", "describe_pr", "generate_labels",
    "ask", "ask_question",
])
def test_json_output_accepts_pr_review(command, monkeypatch, tmp_path):
    from pr_agent.algo.review_json_output import review_json_output_path

    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured.update(target=target, request=request, path=review_json_output_path.get())
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.json"
    run(inargs=["--pr_url", "https://example/pr/1", "--json-output", str(output), command])

    assert captured == {"target": "https://example/pr/1", "request": [command], "path": str(output)}
    assert review_json_output_path.get() is None


@pytest.mark.parametrize("command", ["describe", "describe_pr", "ask", "ask_question"])
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_json_output_accepts_plain_diff_command_aliases(command, input_mode, monkeypatch, tmp_path):
    from pr_agent.algo.review_json_output import review_json_output_path

    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured.update(target=target, request=request, path=review_json_output_path.get())
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    input_args = _valid_input_args(input_mode, monkeypatch, tmp_path)
    output = tmp_path / "result.json"

    run(inargs=[*input_args, "--json-output", str(output), command])

    assert captured == {"target": "local_diff", "request": [command], "path": str(output)}
    assert review_json_output_path.get() is None


@pytest.mark.parametrize("target_args", [[], ["--issue_url", "https://example/issue/1"]])
def test_json_output_requires_pr_or_diff(target_args, capsys):
    with pytest.raises(SystemExit, match="2"):
        run(inargs=[*target_args, "--json-output", "out.json", "review"])
    assert "--json-output requires --pr_url, --stdin, or --diff-file" in capsys.readouterr().err


def test_json_output_rejects_non_review_pr_command(monkeypatch, capsys):
    _fail_if_agent_constructed(monkeypatch)
    with pytest.raises(SystemExit, match="2"):
        run(inargs=["--pr_url", "https://example/pr/1", "--json-output", "out.json", "config"])
    error = capsys.readouterr().err
    assert "--json-output is only supported for review, improve, describe, generate_labels, or ask commands" in error


@pytest.mark.parametrize("result", [False, RequestOutcome.SKIPPED])
def test_ask_json_output_propagates_failed_or_skipped_command(result, monkeypatch, tmp_path):
    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            return result

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "answer.json"

    assert run(inargs=["--pr_url", "https://example/pr/1", "--json-output", str(output), "ask"]) == 1
    assert not output.exists()


@pytest.mark.parametrize("command", ["review", "review_pr", "describe", "improve", "ask", "ask_question"])
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_plain_diff_rejects_shared_markdown_and_json_path_before_read(
    command, input_mode, monkeypatch, capsys,
):
    _fail_if_agent_constructed(monkeypatch)
    args = _rejecting_input_args(input_mode, monkeypatch)

    with pytest.raises(SystemExit, match="2"):
        run(inargs=[*args, "--output", "result.txt", "--json-output", "result.txt", command])

    assert "--output and --json-output must use different files" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["review", "describe", "improve", "ask"])
def test_plain_diff_rejects_hardlinked_markdown_and_json_paths(command, monkeypatch, tmp_path, capsys):
    _fail_if_agent_constructed(monkeypatch)
    markdown_output = tmp_path / "result.md"
    markdown_output.touch()
    json_output = tmp_path / "result.json"
    json_output.hardlink_to(markdown_output)

    with pytest.raises(SystemExit, match="2"):
        run(inargs=["--stdin", "--output", str(markdown_output), "--json-output", str(json_output), command])

    assert "--output and --json-output must use different files" in capsys.readouterr().err


@pytest.mark.parametrize("target_args", [["--issue_url", "https://example/issue/1"], []])
def test_markdown_output_outside_plain_diff_fails_before_dispatch(target_args, monkeypatch, capsys):
    _fail_if_agent_constructed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*target_args, "--output", "out.md", "review"])

    assert exc_info.value.code == 2
    assert "--output requires plain-diff mode or a hosted --pr_url" in capsys.readouterr().err


def test_markdown_output_rejects_local_git_mode_before_dispatch(monkeypatch, capsys):
    _fail_if_agent_constructed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[
            "--pr_url", "main",
            "--output", "out.md",
            "review",
            "--config.git_provider=local",
        ])

    assert exc_info.value.code == 2
    assert "--output requires plain-diff mode or a hosted --pr_url" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["review", "review_pr", "improve", "improve_code"])
def test_hosted_review_writes_markdown_without_publishing(command, monkeypatch, tmp_path, cfg):
    """Exercise real command argument handling, including repo settings overrides."""
    captured = {}

    def repo_settings_override(_):
        get_settings().set("config.publish_output", True)
        get_settings().set("pr_reviewer.per_commit_review", True)

    class FakeReview:
        async def run(self):
            captured["publish_output"] = get_settings().config.publish_output
            captured["per_commit_review"] = get_settings().pr_reviewer.per_commit_review
            captured["hosted_markdown_export"] = hosted_markdown_export.get()
            hosted_markdown_artifact.set("# Review\n\nNo issues found.\n")

    async def make_command(self, command_cls, pr_url, args=None, **kwargs):
        captured["pr_url"] = pr_url
        return FakeReview()

    cfg("config.publish_output", True)
    cfg("pr_reviewer.per_commit_review", True)
    monkeypatch.setattr("pr_agent.agent.pr_agent.apply_repo_settings", repo_settings_override)
    monkeypatch.setattr("pr_agent.agent.pr_agent.enforce_request_policy", lambda _: True)
    monkeypatch.setattr("pr_agent.agent.pr_agent.PRAgent._create_command", make_command)
    output = tmp_path / "review.md"

    assert run(inargs=[
        "--pr_url", "https://github.com/example/repo/pull/1",
        "--output", str(output), command, "--config.publish_output=true",
    ]) is None

    assert captured == {
        "publish_output": False,
        "per_commit_review": True,
        "hosted_markdown_export": True,
        "pr_url": "https://github.com/example/repo/pull/1",
    }
    assert output.read_text(encoding="utf-8") == "# Review\n\nNo issues found.\n"
    assert get_settings().config.publish_output is True


@pytest.mark.parametrize("command", ["ask", "describe", "generate_labels"])
def test_hosted_markdown_rejects_other_commands(command, monkeypatch, capsys):
    _fail_if_agent_constructed(monkeypatch)

    with pytest.raises(SystemExit, match="2"):
        run(inargs=["--pr_url", "https://example.com/pr/1", "--output", "out.md", command])

    assert "--output with --pr_url is not supported" in capsys.readouterr().err


def test_hosted_markdown_rejects_malformed_url(monkeypatch, capsys):
    _fail_if_agent_constructed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=["--pr_url", "http://[::1", "--output", "review.md", "review"])

    assert exc_info.value.code == 2
    assert "--output requires plain-diff mode or a hosted --pr_url" in capsys.readouterr().err


@pytest.mark.parametrize("outcome,artifact,failed", [
    (False, "# Incomplete review", False),
    (True, None, False),
    (True, "# Recorded failure", True),
])
def test_hosted_review_failure_preserves_existing_file(
    outcome, artifact, failed, monkeypatch, tmp_path,
):
    from pr_agent.algo.run_details import record_command_failure

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            if artifact is not None:
                hosted_markdown_artifact.set(artifact)
            if failed:
                record_command_failure()
            return outcome

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"
    output.write_text("previous review", encoding="utf-8")

    assert run(inargs=["--pr_url", "https://example.com/pr/1", "--output", str(output), "review"]) == 1
    assert output.read_text(encoding="utf-8") == "previous review"


@pytest.mark.asyncio
async def test_hosted_markdown_export_uses_full_review_with_per_commit_enabled(monkeypatch, cfg):
    from unittest.mock import AsyncMock

    from pr_agent.commands.pr_reviewer import PRReviewer

    cfg("pr_reviewer.per_commit_review", True)
    reviewer = object.__new__(PRReviewer)
    full_review = AsyncMock()
    per_commit = AsyncMock()
    monkeypatch.setattr(reviewer, "_run_review", full_review)
    monkeypatch.setattr(reviewer, "_run_per_commit", per_commit)
    assert reviewer.parse_incremental(["-i"]).is_incremental
    token = hosted_markdown_export.set(True)
    try:
        assert not reviewer.parse_incremental(["-i"]).is_incremental
        await reviewer.run()
    finally:
        hosted_markdown_export.reset(token)

    assert reviewer.parse_incremental(["-i"]).is_incremental
    full_review.assert_awaited_once()
    per_commit.assert_not_awaited()



def test_hosted_review_with_invalid_repo_settings_preserves_output(monkeypatch, tmp_path):
    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            hosted_markdown_config_error.set(True)
            hosted_markdown_artifact.set("# Review using fallback settings")
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"
    output.write_text("# Existing review", encoding="utf-8")

    assert run(inargs=[
        "--pr_url", "https://example.com/pr/1",
        "--output", str(output), "review",
    ]) == 1
    assert output.read_text(encoding="utf-8") == "# Existing review"


def test_hosted_review_rejects_caller_supplied_artifact(monkeypatch, tmp_path):
    """A request with no generated review must not export configurable artifact text."""

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            # Simulate configuration or CLI arguments that populate unrelated settings data.
            get_settings().data = {"artifact": "# Not a real review"}
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"

    assert run(inargs=[
        "--pr_url", "https://example.com/pr/1", "--output", str(output),
        "review", "--data.artifact=# Not a real review",
    ]) == 1
    assert not output.exists()


@pytest.mark.skipif(os.name == "nt", reason="symlink and mode semantics differ on Windows")
def test_hosted_review_preserves_symlink_and_existing_mode(monkeypatch, tmp_path):
    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            hosted_markdown_artifact.set("# New review")
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    target = tmp_path / "review.md"
    target.write_text("# Old review", encoding="utf-8")
    target.chmod(0o640)
    link = tmp_path / "linked.md"
    link.symlink_to(target.name)

    assert run(inargs=["--pr_url", "https://example.com/pr/1", "--output", str(link), "review"]) is None
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "# New review"
    assert target.stat().st_mode & 0o777 == 0o640


def test_hosted_review_ignores_previous_settings_artifact(monkeypatch, tmp_path, cfg):
    """A successful command that never creates a review must not export old data."""
    cfg("data.artifact", "# Stale review")

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"

    assert run(inargs=["--pr_url", "https://example.com/pr/1", "--output", str(output), "review"]) == 1
    assert not output.exists()


def test_hosted_review_failed_replace_preserves_existing_output(monkeypatch, tmp_path):
    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            hosted_markdown_artifact.set("# New review")
            return True

    def fail_replace(src, dst):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"
    output.write_text("# Existing review", encoding="utf-8")
    monkeypatch.setattr("pr_agent.cli.os.replace", fail_replace)

    assert run(inargs=["--pr_url", "https://example.com/pr/1", "--output", str(output), "review"]) == 1
    assert output.read_text(encoding="utf-8") == "# Existing review"
    assert sorted(tmp_path.iterdir()) == [output]


def test_hosted_review_encoding_failure_preserves_existing_output(monkeypatch, tmp_path):
    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            hosted_markdown_artifact.set("# Invalid UTF-8 surrogate: " + chr(0xD800))
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"
    output.write_text("# Existing review", encoding="utf-8")

    assert run(inargs=["--pr_url", "https://example.com/pr/1", "--output", str(output), "review"]) == 1
    assert output.read_text(encoding="utf-8") == "# Existing review"
    assert sorted(tmp_path.iterdir()) == [output]


def test_hosted_review_policy_skip_leaves_no_markdown(monkeypatch, tmp_path):
    from pr_agent.agent.request_policy import RequestOutcome

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            return RequestOutcome.SKIPPED

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / "review.md"

    assert run(inargs=["--pr_url", "https://example.com/pr/1", "--output", str(output), "review"]) == 1
    assert not output.exists()


@pytest.mark.parametrize("output_name", ["missing/review.md", "invalid\0review.md"])
def test_hosted_review_unwritable_output_returns_failure(monkeypatch, tmp_path, output_name):
    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            hosted_markdown_artifact.set("# Review")
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    output = tmp_path / output_name

    args = ["--pr_url", "https://example.com/pr/1", "--output", str(output)]
    if chr(0) in output_name:
        args.extend(["--json-output", str(tmp_path / "review.json")])
    assert run(inargs=[*args, "review"]) == 1
    assert list(tmp_path.iterdir()) == []


def test_hosted_review_rejects_shared_markdown_and_json_destination(monkeypatch, tmp_path, capsys):
    _fail_if_agent_constructed(monkeypatch)
    output = tmp_path / "review.md"

    with pytest.raises(SystemExit, match="2"):
        run(inargs=[
            "--pr_url", "https://example.com/pr/1",
            "--output", str(output), "--json-output", str(output), "review",
        ])

    assert "--output and --json-output must use different files" in capsys.readouterr().err


def test_hosted_review_preserves_late_json_alias(monkeypatch, tmp_path):
    markdown = tmp_path / "review.md"
    json_path = tmp_path / "review.json"

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            json_path.write_text('{"ok": true}', encoding="utf-8")
            markdown.hardlink_to(json_path)
            hosted_markdown_artifact.set("# Review")
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    assert run(inargs=[
        "--pr_url", "https://example.com/pr/1",
        "--output", str(markdown), "--json-output", str(json_path), "review",
    ]) == 1
    assert json_path.read_text(encoding="utf-8") == '{"ok": true}'
    assert markdown.samefile(json_path)


@pytest.mark.parametrize("option", ["--output", "--json-output"])
@pytest.mark.parametrize("spelling", ["equals", "separate"])
@pytest.mark.parametrize("mode", ["hosted", "stdin"])
def test_empty_output_paths_fail_before_input_or_dispatch(
    option, spelling, mode, monkeypatch, capsys,
):
    _fail_if_agent_constructed(monkeypatch)
    target_args = ["--pr_url", "https://example/pr/1"]
    if mode == "stdin":
        target_args = _rejecting_input_args("stdin", monkeypatch)
    option_args = [f"{option}="] if spelling == "equals" else [option, ""]

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*target_args, *option_args, "review"])

    assert exc_info.value.code == 2
    assert f"{option} requires a non-empty path" in capsys.readouterr().err


@pytest.mark.parametrize("command", _NON_MARKDOWN_COMMANDS)
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_markdown_output_rejects_unsupported_plain_diff_commands_before_read(
    command, input_mode, monkeypatch, capsys,
):
    input_args = _rejecting_input_args(input_mode, monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*input_args, "--output", "out.md", command])

    assert exc_info.value.code == 2
    assert "--output is not supported for" in capsys.readouterr().err


@pytest.mark.parametrize("command", _MARKDOWN_COMMANDS)
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_markdown_output_accepts_compatible_plain_diff_commands(
    command, input_mode, monkeypatch, tmp_path,
):
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["target"] = target
            captured["request"] = request
            captured["output_path"] = get_settings().plain_diff.output_path
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    input_args = _valid_input_args(input_mode, monkeypatch, tmp_path)

    output = tmp_path / "result.md"
    run(inargs=[*input_args, "--output", str(output), command])

    assert captured == {
        "target": "local_diff",
        "request": [command],
        "output_path": str(output),
    }


@pytest.mark.parametrize("command", ["config", "settings", "help"])
def test_compatible_non_model_commands_write_markdown_artifacts(
    command, monkeypatch, tmp_path,
):
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / f"{command}.md"

    run(inargs=["--stdin", "--output", str(output), command])

    assert output.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize(
    ("generate_ai_title", "expected_title"),
    [(False, "Local diff review"), (True, "Update foo.py")],
)
def test_describe_title_in_stdout_and_output_file(
    generate_ai_title, expected_title, monkeypatch, tmp_path, capsys,
):
    async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
        return """\
title: Update foo.py
type: [Enhancement]
description: Update the second line.
pr_files:
  - filename: foo.py
    changes_title: Update the second line
    changes_summary: Replace line2 with line2-changed.
    label: Enhancement
""", "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        fake_chat_completion,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / "description.md"
    overrides = ["--pr_description.generate_ai_title=true"] if generate_ai_title else []

    run(inargs=["--stdin", "--output", str(output), "describe", *overrides])

    markdown = output.read_text(encoding="utf-8")
    assert markdown.startswith(f"{expected_title}\n\n")
    assert "Update the second line." in markdown
    assert "+1/-1" in markdown
    assert capsys.readouterr().out == markdown + "\n"


@pytest.mark.parametrize(("command", "prediction", "expected_key", "expected_value"), [
    (
        "describe",
        "title: Update foo.py\ntype: [Enhancement]\n"
        "description: Update the second line.\npr_files: []\n",
        "description", "Update the second line.",
    ),
])
def test_plain_diff_describe_writes_json_from_model_response(
    command, prediction, expected_key, expected_value, monkeypatch, tmp_path,
):
    async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
        return prediction, "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        fake_chat_completion,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / f"{command}.json"

    run(inargs=["--stdin", "--json-output", str(output), command])

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result[expected_key] == expected_value
    assert isinstance(result["usage"], dict)


def test_plain_diff_improve_writes_the_review_json_from_model_response(monkeypatch, tmp_path):
    async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
        return _CANNED_REVIEW, "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        fake_chat_completion,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / "improve.json"

    run(inargs=["--stdin", "--json-output", str(output), "improve"])

    result = json.loads(output.read_text(encoding="utf-8"))
    assert "review" in result
    assert "code_suggestions" not in result


@pytest.mark.parametrize("command", ["review", "improve"])
def test_plain_diff_review_with_all_files_ignored_calls_no_model(command, monkeypatch, tmp_path, cfg):
    async def unexpected_model_call(self, model, system, user, temperature=0.2, **kwargs):
        pytest.fail("A fully ignored diff must not call the model")

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        unexpected_model_call,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    cfg("ignore.glob", ["foo.py"])
    output = tmp_path / "review.json"

    run(inargs=["--stdin", "--json-output", str(output), command])

    assert not output.exists()


def test_plain_diff_describe_still_publishes_markdown_when_json_unwritable(
    monkeypatch, tmp_path, capsys,
):
    async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
        return (
            "title: Update foo.py\ntype: [Enhancement]\n"
            "description: Update the second line.\npr_files: []\n",
            "stop",
        )

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        fake_chat_completion,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    unreachable_path = tmp_path / "missing" / "description.json"

    assert run(inargs=["--stdin", "--json-output", str(unreachable_path), "describe"]) == 1
    assert "Update the second line." in capsys.readouterr().out
    assert not unreachable_path.exists()


def test_plain_diff_describe_fails_when_all_files_are_ignored(monkeypatch, tmp_path, cfg):
    from unittest.mock import Mock

    from pr_agent.algo.run_details import record_command_failure

    async def unexpected_model_call(self, model, system, user, temperature=0.2, **kwargs):
        pytest.fail("A fully ignored diff must not call the model")

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        unexpected_model_call,
    )
    failure_recorder = Mock(wraps=record_command_failure)
    monkeypatch.setattr("pr_agent.commands.pr_description.record_command_failure", failure_recorder)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    cfg("ignore.glob", ["foo.py"])
    output = tmp_path / "description.json"

    assert run(inargs=["--stdin", "--json-output", str(output), "describe"]) == 1
    failure_recorder.assert_called_once_with()
    assert not output.exists()


@pytest.mark.parametrize("command", ["describe", "review", "improve"])
def test_plain_diff_json_command_failure_returns_nonzero_without_error_propagation(
        monkeypatch, tmp_path, cfg, command):
    from pr_agent.algo.run_details import record_command_failure

    class FailedAgent:
        async def handle_request(self, target, request, notify=None):
            record_command_failure()
            return True

    cfg("config.propagate_command_errors", False)
    monkeypatch.setattr("pr_agent.cli.PRAgent", FailedAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))

    assert run(inargs=["--stdin", "--json-output", str(tmp_path / "result.json"), command]) == 1


def test_plain_diff_ask_keeps_output_after_command_settings(monkeypatch, tmp_path, capsys):
    async def answer(self, model, system, user, temperature=0.2, **kwargs):
        return "The changed line is correct.", "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion", answer,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / "answer.md"
    json_output = tmp_path / "answer.json"

    run(inargs=["--stdin", "--output", str(output), "--json-output", str(json_output),
                "ask", "Explain.", "--config.publish_output=false"])

    assert "The changed line is correct." in output.read_text(encoding="utf-8")
    assert "The changed line is correct." in capsys.readouterr().out
    assert json.loads(json_output.read_text(encoding="utf-8")) == {
        "question": "Explain.", "answer": "The changed line is correct.", "usage": {},
    }


def test_plain_diff_ask_keeps_markdown_if_json_output_fails(monkeypatch, tmp_path):
    async def answer(self, model, system, user, temperature=0.2, **kwargs):
        return "The change is correct.", "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion", answer,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    markdown = tmp_path / "answer.md"
    missing_json = tmp_path / "missing" / "answer.json"

    assert run(inargs=["--stdin", "--output", str(markdown), "--json-output", str(missing_json),
                       "ask", "Explain."]) == 1
    assert "The change is correct." in markdown.read_text(encoding="utf-8")
    assert not missing_json.exists()


@pytest.mark.parametrize(("title", "description"), [
    ("Fix: invoice approvals", "Acceptance criteria: require approval"),
    ("true", "123"),
    ("DEL\x7f title", "C1\x80\x9f description"),
    ("繁體中文標題", "內容：需審核"),
    ("Fix 🚀", "Ship 🎯 when ready"),
])
def test_plain_diff_cli_preserves_context_text_in_prompts(monkeypatch, tmp_path, title, description):
    captured = {}

    async def answer(self, model, system, user, temperature=0.2, **kwargs):
        captured["title"] = get_settings().get("plain_diff.title")
        captured["description"] = get_settings().get("plain_diff.description")
        captured["prompt"] = system + "\n" + user
        return "The changed line is correct.", "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion", answer,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))

    run(inargs=["--stdin", "ask", "Explain.",
                f"--plain_diff.title={title}", f"--plain_diff.description={description}"])

    assert captured["title"] == title
    assert captured["description"] == description
    assert title in captured["prompt"]
    assert description in captured["prompt"]


def test_plain_diff_cli_encodes_context_with_issue_target(monkeypatch):
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured.update(target=target, request=request)
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))

    run(inargs=["--stdin", "--issue_url", "https://example/issue/1", "ask",
                "--plain_diff.title=true", "--plain_diff.description=Acceptance: ready"])

    assert captured == {
        "target": "https://example/issue/1",
        "request": ["ask", '--plain_diff.title="true"',
                    '--plain_diff.description="Acceptance: ready"'],
    }


def test_plain_diff_ask_applies_command_ignore_filter(monkeypatch):
    diff = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/bar.py b/bar.py\n--- a/bar.py\n+++ b/bar.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    seen = []

    async def capture(self):
        seen.extend(f.filename for f in self.git_provider.get_diff_files())

    monkeypatch.setattr("pr_agent.commands.pr_questions.PRQuestions.run", capture)
    monkeypatch.setattr("sys.stdin", io.StringIO(diff))
    run(inargs=["--stdin", "ask", "Explain.", '--ignore.glob=["foo.py"]'])

    assert seen == ["bar.py"]


@pytest.mark.parametrize("registered_provider", [False, True])
def test_local_git_ask_applies_command_ignore_filter(monkeypatch, tmp_path, cfg, registered_provider):
    repo = Repo.init(tmp_path, initial_branch="main")
    with repo.config_writer() as config:
        config.set_value("user", "name", "Test User")
        config.set_value("user", "email", "test@example.com")

    for filename in ("foo.py", "bar.py"):
        (tmp_path / filename).write_text("old\n", encoding="utf-8")
    repo.index.add(["foo.py", "bar.py"])
    repo.index.commit("base")

    repo.git.checkout("-b", "feature")
    for filename in ("foo.py", "bar.py"):
        (tmp_path / filename).write_text("new\n", encoding="utf-8")
    repo.index.add(["foo.py", "bar.py"])
    repo.index.commit("change")

    provider_id = "local"
    if registered_provider:
        class RegisteredLocalProvider(LocalGitProvider):
            pass

        provider_id = "registered-local"
        monkeypatch.setitem(git_providers._GIT_PROVIDERS, provider_id, RegisteredLocalProvider)

    cfg("config.git_provider", provider_id)
    cfg("plain_diff.content", None)
    monkeypatch.chdir(tmp_path)
    seen = []

    async def capture(self):
        seen.extend(f.filename for f in self.git_provider.get_diff_files())

    monkeypatch.setattr("pr_agent.commands.pr_questions.PRQuestions.run", capture)
    run(inargs=["--pr_url", "main", "ask", "Explain.", '--ignore.glob=["foo.py"]'])

    assert seen == ["bar.py"]


def test_ask_line_output_is_rejected_without_creating_artifact(monkeypatch, tmp_path, capsys):
    input_args = _rejecting_input_args("stdin", monkeypatch)
    output = tmp_path / "ask-line.md"

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*input_args, "--output", str(output), "ask_line"])

    assert exc_info.value.code == 2
    assert "--output is not supported for" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("command", ["review", "review_pr", "improve", "improve_code"])
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_json_output_accepts_review_aliases(command, input_mode, monkeypatch, tmp_path):
    from pr_agent.algo.review_json_output import review_json_output_path

    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["request"] = request
            captured["json_output_path"] = get_settings().plain_diff.json_output_path
            captured["shared_writer_path"] = review_json_output_path.get()
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    input_args = _valid_input_args(input_mode, monkeypatch, tmp_path)

    output = tmp_path / "review.json"
    run(inargs=[*input_args, "--json-output", str(output), command])

    assert captured == {
        "request": [command],
        "json_output_path": str(output),
        "shared_writer_path": None,
    }


@pytest.mark.parametrize("command", [
    *(name for name in _NON_MARKDOWN_COMMANDS if name != "generate_labels"),
    "config", "settings", "help",
])
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_json_output_rejects_non_review_commands_before_read(
    command, input_mode, monkeypatch, capsys,
):
    input_args = _rejecting_input_args(input_mode, monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*input_args, "--json-output", "out.json", command])

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "--json-output is only supported for review, improve, describe, generate_labels, or ask commands" in error


@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_plain_diff_generate_labels_writes_filtered_json(input_mode, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    input_args = _valid_input_args(input_mode, monkeypatch, tmp_path)
    monkeypatch.setattr(
        "pr_agent.commands.pr_generate_labels.PRGenerateLabels._get_prediction",
        AsyncMock(return_value="labels: [Bug fix, not-a-supported-label]"),
    )
    output = tmp_path / "labels.json"

    assert run(inargs=[*input_args, "--json-output", str(output), "generate_labels"]) is None

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["labels"] == ["Bug fix"]
    assert isinstance(result["usage"], dict)
    assert "## PR Labels:" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["hosted", "plain_diff"])
@pytest.mark.parametrize("json_requested", [False, True])
def test_generate_labels_policy_skip_only_fails_when_json_is_required(
    mode, json_requested, monkeypatch, tmp_path, capsys,
):
    from pr_agent.agent.request_policy import RequestOutcome

    class SkippedAgent:
        async def handle_request(self, target, request, notify=None):
            return RequestOutcome.SKIPPED

    monkeypatch.setattr("pr_agent.cli.PRAgent", SkippedAgent)
    args = ["--pr_url", "https://example/pr/1"] if mode == "hosted" else ["--stdin"]
    if mode == "plain_diff":
        monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / "labels.json"
    if json_requested:
        args.extend(["--json-output", str(output)])

    assert run(inargs=[*args, "generate_labels"]) == (1 if json_requested else None)
    assert not output.exists()
    assert "Usage:" not in capsys.readouterr().out


def test_plain_diff_generate_labels_failure_does_not_write_success_json(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    monkeypatch.setattr(
        "pr_agent.commands.pr_generate_labels.retry_with_fallback_models",
        AsyncMock(side_effect=RuntimeError("model unavailable")),
    )
    output = tmp_path / "labels.json"

    assert run(inargs=["--stdin", "--json-output", str(output), "generate_labels"]) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    ("option_spelling", "option"),
    _OUTPUT_OPTION_PREFIXES,
)
@pytest.mark.parametrize("value_form", ["separate", "equals"])
def test_output_options_after_command_fail_before_read(
    option_spelling, option, value_form, monkeypatch, capsys,
):
    class UnreadableStdin:
        def read(self):
            pytest.fail("stdin must not be read for misplaced output options")

    monkeypatch.setattr("sys.stdin", UnreadableStdin())
    value = "out.json" if option == "--json-output" else "out.md"
    trailing_args = (
        [option_spelling, value]
        if value_form == "separate"
        else [f"{option_spelling}={value}"]
    )

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=["--stdin", "review", *trailing_args])

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert f"{option} must appear before the command" in err


def test_near_match_after_command_remains_a_command_argument(monkeypatch):
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["request"] = request
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))

    run(inargs=["--stdin", "review", "--output-format=markdown"])

    assert captured["request"] == ["review", "--output-format=markdown"]


def test_diff_mode_forces_publish_output(cfg, monkeypatch):
    """Diff mode must force config.publish_output=True so stdout/--output is
    never suppressed by a config/env that disabled publishing."""
    cfg("config.publish_output", False)
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["publish_output"] = get_settings().config.publish_output
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    run(inargs=["--stdin", "review"])
    assert captured["publish_output"] is True


def test_diff_mode_sets_json_output_path(cfg, monkeypatch, tmp_path):
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["json_output_path"] = get_settings().plain_diff.json_output_path
            return True

    output = tmp_path / "review.json"
    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))

    run(inargs=["--stdin", "--json-output", str(output), "review"])

    assert captured["json_output_path"] == str(output)


def test_plain_diff_fake_handler_sees_eager_artifact_without_new_keyword(monkeypatch, tmp_path):
    """Keep the three-argument fake-handler contract while CLI owns the scope."""
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("PLAIN_DIFF_ARTIFACT", encoding="utf-8")
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["target"] = target
            captured["request"] = request
            captured["instructions"] = str(get_settings().pr_reviewer.extra_instructions)
            captured["artifact_context"] = get_artifact_context("pr_reviewer")
            return True

    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_PATH", str(artifact))
    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))

    run(inargs=["--stdin", "review"])

    assert captured["target"] == "local_diff"
    assert captured["request"] == ["review"]
    assert "PLAIN_DIFF_ARTIFACT" not in captured["instructions"]
    assert captured["artifact_context"]["content"].count("PLAIN_DIFF_ARTIFACT") == 1
