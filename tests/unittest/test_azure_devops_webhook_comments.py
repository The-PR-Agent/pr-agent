import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import pytest

from pr_agent.algo.utils import decode_user_text_args, update_settings_from_args
from pr_agent.servers import azuredevops_server_webhook as webhook

AGENT_ALIASES = {
    "agent-guid",
    "Build Service (organization)",
    "agent@example.com",
}

@pytest.mark.parametrize(("comment", "question"), [
    ("@<agent-guid> can this throw?", "can this throw?"),
    ("@<Build Service (organization)>: check the nullable value", "check the nullable value"),
    ("@<agent@example.com>, please review this", "please review this"),
])
def test_extract_agent_question_accepts_agent_identity_mentions(comment, question):
    assert webhook.extract_agent_question(comment, AGENT_ALIASES) == question


def test_extract_agent_question_accepts_legacy_html_mentions():
    comment = ('<a href="#" data-vss-mention="version:2.0,agent-guid">'
               '@Build Service (organization)</a> check this')

    assert webhook.extract_agent_question(comment, AGENT_ALIASES) == "check this"


@pytest.mark.parametrize("comment", [
    "We will move this to the backlog.",
    "The agent service is unavailable.",
    "This needs another review.",
    "PR-Agent: check this",
    "Hi agent, check this",
    "@<another-user-guid> check this",
    "Hi agent, this is already answered.\n\n<!-- pr-agent-response -->",
])
def test_extract_agent_question_ignores_normal_discussion(comment):
    assert webhook.extract_agent_question(comment, AGENT_ALIASES) is None


def test_extract_azure_mention_recognizes_valid_mention_syntax_before_identity_lookup():
    assert webhook.extract_azure_mention("@<another-user-guid> check this") == (
        {"another-user-guid"}, "check this"
    )


def test_addressed_line_comment_becomes_ask_line_with_history_ids():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=SimpleNamespace(line=8),
        right_file_end=SimpleNamespace(line=10),
    )

    command = webhook.handle_line_comment(
        "@<agent-guid> can this throw?", thread_id=22, comment_id=31, provider=provider
    )

    assert command == ("/ask_line --line_start=8 --line_end=10 --side=right "
                       "--file_name=%2Fsrc%2Fapp.cs --file_name_encoded=true --comment_id=22 "
                       "--origin_comment_id=31 __pr_agent_encoded_text__:can%20this%20throw%3F")


def test_addressed_pr_comment_becomes_threaded_ask():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = None

    command = webhook.handle_line_comment(
        "@<Build Service (organization)> check this PR", thread_id=22, comment_id=31, provider=provider
    )

    assert command == ("/ask --comment_id=22 --origin_comment_id=31 "
                       "__pr_agent_encoded_text__:check%20this%20PR")


def test_addressed_comment_without_line_range_becomes_threaded_ask():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=None,
        right_file_end=None,
    )

    command = webhook.handle_line_comment(
        "@<agent@example.com> check this thread", thread_id=22, comment_id=31, provider=provider
    )

    assert command == ("/ask --comment_id=22 --origin_comment_id=31 "
                       "__pr_agent_encoded_text__:check%20this%20thread")


def test_addressed_comment_uses_available_line_position():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=None,
        right_file_end=SimpleNamespace(line=10),
    )

    command = webhook.handle_line_comment("@<agent-guid> check this", 22, 31, provider)

    assert command == ("/ask_line --line_start=10 --line_end=10 --side=right "
                       "--file_name=%2Fsrc%2Fapp.cs --file_name_encoded=true --comment_id=22 "
                       "--origin_comment_id=31 __pr_agent_encoded_text__:check%20this")


def test_slash_ask_does_not_require_agent_identity_discovery():
    provider = MagicMock()
    provider.get_thread_context.return_value = None

    command = webhook.handle_line_comment("/ask check this PR", 22, 31, provider)

    assert command == ("/ask --comment_id=22 --origin_comment_id=31 "
                       "__pr_agent_encoded_text__:check%20this%20PR")
    provider.get_agent_mention_aliases.assert_not_called()


def test_slash_ask_keeps_explicit_cli_overrides_out_of_the_question():
    provider = MagicMock()
    provider.get_thread_context.return_value = None

    command = webhook.handle_line_comment(
        '/ask --pr_questions.extra_instructions="be terse" what does this do?', 22, 31, provider
    )
    lexer = shlex.shlex(command.replace("'", "\\'"), posix=True)
    lexer.whitespace_split = True
    _, *args = list(lexer)

    with patch("pr_agent.algo.utils.get_settings") as settings:
        question_args = update_settings_from_args(args)

    assert decode_user_text_args(question_args) == "what does this do?"
    assert any(call.args == ("PR_QUESTIONS.EXTRA_INSTRUCTIONS", "be terse")
               for call in settings.return_value.set.call_args_list)


def test_slash_ask_line_keeps_explicit_cli_overrides_out_of_the_question():
    provider = MagicMock()
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=SimpleNamespace(line=8),
        right_file_end=SimpleNamespace(line=8),
    )

    command = webhook.handle_line_comment("/ask --pr_questions.extra_instructions=terse why?", 22, 31, provider)

    assert "--pr_questions.extra_instructions=terse" in command
    assert command.endswith("__pr_agent_encoded_text__:why%3F")


def test_slash_ask_treats_valueless_flags_as_question_text():
    provider = MagicMock()
    provider.get_thread_context.return_value = None

    command = webhook.handle_line_comment("/ask --extended why is this?", 22, 31, provider)
    _, *args = shlex.split(command)
    question_args = update_settings_from_args(args)

    assert decode_user_text_args(question_args) == "--extended why is this?"


def test_non_question_command_is_preserved():
    provider = MagicMock()

    command = webhook.handle_line_comment("/improve -i", 22, 31, provider)

    assert command == "/improve -i"
    provider.get_agent_mention_aliases.assert_not_called()
    provider.get_thread_context.assert_not_called()


def test_unaddressed_comment_is_ignored():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES

    assert webhook.handle_line_comment("Move this to the backlog", 22, 31, provider) is None
    provider.get_thread_context.assert_not_called()


def test_non_string_comment_is_ignored():
    provider = MagicMock()

    assert webhook.handle_line_comment(123, 22, 31, provider) is None
    provider.get_agent_mention_aliases.assert_not_called()


def test_invalid_line_range_becomes_threaded_ask():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=SimpleNamespace(line=True),
        right_file_end=SimpleNamespace(line=10),
    )

    command = webhook.handle_line_comment("@<agent-guid> check this", 22, 31, provider)

    assert command == ("/ask --comment_id=22 --origin_comment_id=31 "
                       "__pr_agent_encoded_text__:check%20this")


def test_line_question_cannot_be_parsed_as_settings():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=SimpleNamespace(line=8),
        right_file_end=SimpleNamespace(line=8),
    )
    question = "does --some_key=1 change the request?"

    command = webhook.handle_line_comment(f"@<agent-guid> {question}", 22, 31, provider)
    lexer = shlex.shlex(command.replace("'", "\\'"), posix=True)
    lexer.whitespace_split = True
    _, *args = list(lexer)

    with patch("pr_agent.algo.utils.get_settings") as settings:
        question_args = update_settings_from_args(args)

    assert decode_user_text_args(question_args) == question
    assert all(call.args[0] != "SOME_KEY" for call in settings.return_value.set.call_args_list)


def test_line_comment_encodes_file_path():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/folder name/app.cs",
        left_file_start=None,
        left_file_end=None,
        right_file_start=SimpleNamespace(line=8),
        right_file_end=SimpleNamespace(line=8),
    )

    command = webhook.handle_line_comment("@<agent-guid> check this", 22, 31, provider)

    assert "--file_name=%2Fsrc%2Ffolder%20name%2Fapp.cs" in command
    assert "--file_name_encoded=true" in command


def test_line_comment_file_path_survives_request_parsing():
    provider = MagicMock()
    provider.get_agent_mention_aliases.return_value = AGENT_ALIASES
    provider.get_thread_context.return_value = SimpleNamespace(
        file_path="/src/folder name/don't.py",
        left_file_start=None,
        left_file_end=None,
        right_file_start=SimpleNamespace(line=8),
        right_file_end=SimpleNamespace(line=8),
    )

    command = webhook.handle_line_comment("@<agent-guid> check this", 22, 31, provider)
    lexer = shlex.shlex(command.replace("'", "\\'"), posix=True)
    lexer.whitespace_split = True
    _, *args = list(lexer)
    values = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)

    assert unquote(values["file_name"]) == "/src/folder name/don't.py"


def test_empty_slash_ask_is_preserved():
    provider = MagicMock()
    provider.get_thread_context.return_value = None

    assert webhook.handle_line_comment("/ask", 22, 31, provider) == "/ask --comment_id=22 --origin_comment_id=31"
