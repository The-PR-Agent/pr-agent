import builtins
import io

import pytest

from pr_agent.cli import commands, run, set_parser
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

_MARKDOWN_COMMANDS = [
    "review",
    "review_pr",
    "describe",
    "describe_pr",
    "improve",
    "improve_code",
    "ask",
    "ask_question",
]

_NON_MARKDOWN_COMMANDS = [
    "auto_review",
    "answer",
    "ask_line",
    "update_changelog",
    "config",
    "settings",
    "help",
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
def test_markdown_output_accepts_documented_plain_diff_commands(
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
    ("trailing_args", "option"),
    [
        (["--output", "out.md"], "--output"),
        (["--output=out.md"], "--output"),
        (["--json-output", "out.json"], "--json-output"),
        (["--json-output=out.json"], "--json-output"),
    ],
)
def test_output_options_after_command_fail_before_read(trailing_args, option, monkeypatch, capsys):
    class UnreadableStdin:
        def read(self):
            pytest.fail("stdin must not be read for misplaced output options")

    monkeypatch.setattr("sys.stdin", UnreadableStdin())

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
