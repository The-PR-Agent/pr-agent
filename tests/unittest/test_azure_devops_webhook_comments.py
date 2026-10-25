import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import pytest

from pr_agent.agent import pr_agent as pr_agent_module
from pr_agent.algo.run_details import record_command_failure
from pr_agent.algo.utils import decode_user_text_args, update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.servers import azuredevops_server_webhook as webhook

URL = "https://dev.azure.com/org/proj/_git/repo/pullrequest/1"
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


class _SwallowingReview:
    """Stand-in for a command that hits its own error and returns normally anyway, which is what
    the real tools do while `propagate_tool_errors` is false (see pr_reviewer.py:458-464)."""

    def __init__(self, pr_url, ai_handler=None, args=None):
        self.observed_propagation = None

    async def run(self):
        self.observed_propagation = get_settings().config.get("propagate_tool_errors", False)
        record_command_failure()
        if self.observed_propagation:
            raise RuntimeError("the model call failed")


class _CleanReview:
    def __init__(self, pr_url, ai_handler=None, args=None):
        self.ran = False

    async def run(self):
        self.ran = True


async def _run_comment_command(body, tool_class, monkeypatch):
    """Drive the comment endpoint through the real agent so the settings the tool sees are the
    ones the handler asked for, and return the provider plus the tool instance."""
    provider = MagicMock()
    provider.get_thread_context.return_value = None
    built = []

    def build(*args, **kwargs):
        tool = tool_class(*args, **kwargs)
        built.append(tool)
        return tool

    monkeypatch.setitem(pr_agent_module.command2class, "review", build)
    monkeypatch.setattr(pr_agent_module, "enforce_request_policy", lambda _url: None)
    monkeypatch.setattr(pr_agent_module, "apply_repo_settings", lambda pr_url: None)
    with patch.object(webhook, "get_git_provider_with_context", return_value=provider):
        await webhook.handle_request_comment(URL, body, 7, 42, {})
    return provider, built[0]


async def test_swallowed_tool_failure_does_not_resolve_the_thread(monkeypatch):
    """Resolving the discussion on a run that raised inside the tool leaves the author a closed
    thread and no review, so the handler must demand an honest verdict the way the GitHub comment
    path does. The "On it!" reply is posted before the command runs, so it still has to be cleaned
    up on that failed run, like the temporary comments the tools remove in their finally blocks."""

    provider, tool = await _run_comment_command("/review", _SwallowingReview, monkeypatch)

    provider.set_thread_status.assert_not_called()
    provider.remove_initial_comment.assert_called_once()
    assert tool.observed_propagation is True


async def test_successful_command_still_resolves_the_thread(monkeypatch):
    """The companion guard: a run that raised nothing must keep closing the thread, so the fix is
    not just "never close"."""

    provider, tool = await _run_comment_command("/review", _CleanReview, monkeypatch)

    assert tool.ran is True
    provider.set_thread_status.assert_called_once_with(7, "closed")
    provider.remove_initial_comment.assert_called_once()


@pytest.fixture
def created_event():
    # Azure's documented sample has no web link and a misleading remoteUrl project segment.
    return {
        "eventType": "git.pullrequest.created",
        "resource": {
            "repository": {
                "name": "Fabrikam", "project": {"name": "Fabrikam"},
                "remoteUrl": "https://dev.azure.com/fabrikam/DefaultCollection/_git/Fabrikam",
            },
            "pullRequestId": 1,
        },
        "resourceContainers": {"collection": {"baseUrl": "https://dev.azure.com/fabrikam/"}},
    }


@pytest.mark.parametrize("links", [{}, {"_links": {}}, {"_links": {"web": {}}}, {"_links": {"web": {"href": ""}}}])
async def test_created_event_without_web_link_dispatches_documented_identity(created_event, links):
    created_event["resource"].update(links)
    context = {}
    with patch.object(webhook, "_perform_commands_azure", new_callable=AsyncMock) as perform, \
            patch.object(webhook, "PRAgent") as agent:
        response = await webhook.handle_request_azure(created_event, context)
    expected = "https://dev.azure.com/fabrikam/Fabrikam/_git/Fabrikam/pullrequest/1"
    perform.assert_awaited_once_with("pr_commands", agent.return_value, expected, context)
    agent.assert_called_once_with()
    assert webhook.AzureDevopsProvider._parse_pr_url(expected) == ("Fabrikam", "Fabrikam", 1)
    assert context == {"event": "git.pullrequest.created", "api_url": expected}
    assert response.status_code == 202


async def test_created_event_fallback_preserves_structured_names_and_collection_prefix(created_event):
    repository = created_event["resource"]["repository"]
    repository.update(name="repo/part%20", project={"name": "Dev Project"}, remoteUrl="not-an-identity")
    created_event["resourceContainers"]["collection"]["baseUrl"] = "http://azure.example.test:8080/tfs/Collection/"
    with patch.object(webhook, "_perform_commands_azure", new_callable=AsyncMock) as perform, \
            patch.object(webhook, "PRAgent"):
        await webhook.handle_request_azure(created_event, {})
    perform.assert_awaited_once()
    url = perform.await_args.args[2]
    assert url == "http://azure.example.test:8080/tfs/Collection/Dev%20Project/_git/repo%2Fpart%2520/pullrequest/1"
    assert webhook.AzureDevopsProvider._parse_pr_url(url) == ("Dev Project", "repo/part%20", 1)


async def test_created_event_nonempty_web_link_retains_legacy_precedence():
    href = "https://dev.azure.com/org/Dev%20Project/_apis/git/repositories/repo/pullrequest/7"
    data = {"eventType": "git.pullrequest.created", "resource": {
        "_links": {"web": {"href": href}}, "repository": None, "pullRequestId": False,
    }}
    with patch.object(webhook, "_perform_commands_azure", new_callable=AsyncMock) as perform, \
            patch.object(webhook, "PRAgent"):
        await webhook.handle_request_azure(data, {})
    perform.assert_awaited_once()
    assert perform.await_args.args[2] == unquote(href.replace("_apis/git/repositories", "_git"))


@pytest.mark.parametrize(("path", "value"), [
    ("resource", None),
    ("resource._links", None),
    ("resource._links", {"web": []}),
    ("resource._links", {"web": {"href": 7}}),
    ("resource.repository", {}),
    ("resource.repository.project", None),
    ("resource.repository.project.name", ""),
    ("resource.repository.name", 7),
    ("resource.repository.name", " "),
    ("resource.pullRequestId", "1"),
    ("resource.pullRequestId", True),
    ("resource.pullRequestId", 0),
    ("resource.pullRequestId", -1),
    ("resourceContainers", {}),
    ("resourceContainers.collection", None),
    ("resourceContainers.collection.baseUrl", None),
    ("resourceContainers.collection.baseUrl", ""),
    ("resourceContainers.collection.baseUrl", "/collection/"),
    ("resourceContainers.collection.baseUrl", "ftp://azure.example.test/collection/"),
    ("resourceContainers.collection.baseUrl", "https://[broken/"),
    ("resourceContainers.collection.baseUrl", "https://azure.example.test:/collection/"),
    ("resourceContainers.collection.baseUrl", "https://azure.example.test/collection/?"),
    ("resourceContainers.collection.baseUrl", "https://azure.example.test/collection/#"),
    ("resourceContainers.collection.baseUrl", "https://azure.example.test:70000/collection/"),
    ("resourceContainers.collection.baseUrl", "https://user:secret@azure.example.test/collection/"),
    ("resourceContainers.collection.baseUrl", "https://azure.example.test/collection/?token=secret"),
    ("resourceContainers.collection.baseUrl", "https://azure.example.test/collection/#secret"),
])
async def test_created_event_invalid_identity_does_not_dispatch_or_log_payload(created_event, path, value):
    target = created_event
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    context = {}
    with patch.object(webhook, "_perform_commands_azure", new_callable=AsyncMock) as perform, \
            patch.object(webhook, "PRAgent") as agent, patch.object(webhook, "get_logger") as logger:
        result = await webhook.handle_request_azure(created_event, context)
    perform.assert_not_awaited()
    agent.assert_not_called()
    logger.return_value.warning.assert_called_once_with(
        "Ignoring Azure created event with invalid pull request identity"
    )
    assert logger.return_value.method_calls == [
        ("warning", ("Ignoring Azure created event with invalid pull request identity",), {})
    ]
    assert context == {}
    assert result is None
