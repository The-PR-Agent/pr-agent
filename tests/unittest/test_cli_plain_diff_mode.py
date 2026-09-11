import builtins
import io

import pytest

from pr_agent.cli import _resolve_output_option, commands, run, set_parser
from pr_agent.config_loader import get_settings

# Keys run() mutates on the process-wide settings singleton, directly or via the
# diff-mode CLI path. Snapshotted and restored around every test (autouse) so
# state never leaks, even when run() sets keys the test never touches itself.
_SETTINGS_KEYS = [
    "plain_diff.content",
    "plain_diff.output_path",
    "plain_diff.json_output_path",
    "config.git_provider",
    "config.publish_output",
    "config.cli_mode",
    "config.config_branch",
    "config.extra_config_url",
]

_DIFF = (
    "diff --git a/foo.py b/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,3 +1,3 @@\n"
    " line1\n-line2\n+line2-changed\n line3\n"
)
_REVIEW_MARKER = "plain-diff auto-review output"
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
    "auto_review",
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
    "answer",
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


def test_missing_diff_file_fails_fast(tmp_path, capsys):
    """A non-existent --diff-file must exit cleanly via parser.error (SystemExit)
    with a clear message, not crash with an uncaught OSError traceback."""
    missing = tmp_path / "does-not-exist.diff"
    with pytest.raises(SystemExit):
        run(inargs=["--diff-file", str(missing), "review"])
    err = capsys.readouterr().err
    assert "Could not read --diff-file" in err


def test_json_output_outside_diff_mode_fails_fast(capsys):
    """Reject --json-output in hosted-provider mode via parser.error instead of
    silently dropping the explicitly requested artifact."""
    with pytest.raises(SystemExit) as exc_info:
        run(inargs=["--pr_url", "https://example/pr/1", "--json-output", "out.json", "review"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--json-output is only supported in plain-diff mode" in err


@pytest.mark.parametrize(
    "target_args",
    [
        ["--pr_url", "https://example/pr/1"],
        ["--issue_url", "https://example/issue/1"],
        [],
    ],
)
def test_markdown_output_outside_plain_diff_fails_before_dispatch(target_args, monkeypatch, capsys):
    _fail_if_agent_constructed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*target_args, "--output", "out.md", "review"])

    assert exc_info.value.code == 2
    assert "--output is only supported in plain-diff mode" in capsys.readouterr().err


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
    assert "--output is only supported in plain-diff mode" in capsys.readouterr().err


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


def test_auto_review_writes_markdown_artifact_with_stubbed_model(
    monkeypatch, tmp_path,
):
    async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
        return _CANNED_REVIEW, "stop"

    monkeypatch.setattr(
        "pr_agent.algo.ai_handlers.litellm_ai_handler.LiteLLMAIHandler.chat_completion",
        fake_chat_completion,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_DIFF))
    output = tmp_path / "auto-review.md"

    run(inargs=["--stdin", "--output", str(output), "auto_review"])

    assert _REVIEW_MARKER in output.read_text(encoding="utf-8")


def test_answer_output_is_rejected_without_creating_artifact(monkeypatch, tmp_path, capsys):
    input_args = _rejecting_input_args("stdin", monkeypatch)
    output = tmp_path / "answer.md"

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*input_args, "--output", str(output), "answer"])

    assert exc_info.value.code == 2
    assert "--output is not supported for" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("command", ["review", "review_pr"])
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_json_output_accepts_review_aliases(command, input_mode, monkeypatch, tmp_path):
    captured = {}

    class FakeAgent:
        async def handle_request(self, target, request, notify=None):
            captured["request"] = request
            captured["json_output_path"] = get_settings().plain_diff.json_output_path
            return True

    monkeypatch.setattr("pr_agent.cli.PRAgent", FakeAgent)
    input_args = _valid_input_args(input_mode, monkeypatch, tmp_path)

    output = tmp_path / "review.json"
    run(inargs=[*input_args, "--json-output", str(output), command])

    assert captured == {
        "request": [command],
        "json_output_path": str(output),
    }


@pytest.mark.parametrize("command", [*_NON_MARKDOWN_COMMANDS, *_MARKDOWN_COMMANDS[2:]])
@pytest.mark.parametrize("input_mode", ["stdin", "file"])
def test_json_output_rejects_non_review_commands_before_read(
    command, input_mode, monkeypatch, capsys,
):
    input_args = _rejecting_input_args(input_mode, monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        run(inargs=[*input_args, "--json-output", "out.json", command])

    assert exc_info.value.code == 2
    assert "--json-output is only supported for plain-diff review commands" in capsys.readouterr().err


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


def test_near_match_after_command_remains_a_tool_argument(monkeypatch):
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
