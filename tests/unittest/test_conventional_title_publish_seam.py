from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.tools import pr_description as pr_description_module
from pr_agent.tools.pr_description import PRDescription, _ANGULAR_TITLE_INSTRUCTIONS

_MISSING = object()


class _Config(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


def _settings(*, generate_ai_title: bool, enable_conventional_title: bool, extra_instructions: str = ""):
    settings = _Config()
    settings.config = _Config(
        publish_output=True,
        is_auto_command=False,
        output_relevant_configurations=False,
        enable_custom_labels=False,
    )
    settings.pr_description = _Config(
        generate_ai_title=generate_ai_title,
        enable_conventional_title=enable_conventional_title,
        extra_instructions=extra_instructions,
        enable_semantic_files_types=False,
        publish_labels=False,
        use_description_markers=False,
        inline_file_summary=False,
        enable_help_text=False,
        enable_help_comment=False,
        publish_description_as_comment=False,
        final_update_message=False,
    )
    settings.pr_description_prompt = SimpleNamespace(system="", user="")
    return settings


def _make_instance(*, ai_title=_MISSING, pr_title: str) -> PRDescription:
    obj = PRDescription.__new__(PRDescription)
    obj.pr_id = "1"
    obj.prediction = "title: x"
    obj.vars = {"title": "Human supplied title"}
    obj.data = {}
    if ai_title is not _MISSING:
        obj.data["title"] = ai_title
    obj.ai_title = ai_title if ai_title is not _MISSING else None
    obj.git_provider = MagicMock()
    obj.git_provider.is_supported.return_value = False
    obj._prepare_data = MagicMock()
    obj._prepare_pr_answer = MagicMock(return_value=(pr_title, "body", "", []))
    return obj


async def _noop_extract_tickets(*_args, **_kwargs):
    return None


async def _noop_retry(*_args, **_kwargs):
    return None


@pytest.mark.parametrize(
    ("generate_ai_title", "enable_conventional_title", "ai_title", "pr_title", "expected_title",
     "expected_validator_calls"),
    [
        (False, False, "Feature(auth): add SSO support", "Human supplied title", None, 0),
        (True, False, "Feature(auth): add SSO support", "  Feature(auth): add SSO support  ",
         "Feature(auth): add SSO support", 0),
        (False, True, "Feature(auth): add SSO support", "Human supplied title",
         "feat(auth): add SSO support", 1),
        (False, True, "WIP: whatever", "Human supplied title", None, 1),
        (True, True, "Feature(auth): add SSO support", "Human supplied title",
         "feat(auth): add SSO support", 1),
    ],
)
@patch("pr_agent.tools.pr_description.retry_with_fallback_models", side_effect=_noop_retry)
@patch("pr_agent.tools.pr_description.extract_and_cache_pr_tickets", side_effect=_noop_extract_tickets)
@patch("pr_agent.tools.pr_description.get_settings")
async def test_publish_description_title_argument_matrix(
    mock_get_settings,
    _mock_extract_tickets,
    _mock_retry,
    generate_ai_title,
    enable_conventional_title,
    ai_title,
    pr_title,
    expected_title,
    expected_validator_calls,
):
    mock_get_settings.return_value = _settings(
        generate_ai_title=generate_ai_title,
        enable_conventional_title=enable_conventional_title,
    )
    obj = _make_instance(ai_title=ai_title, pr_title=pr_title)

    with patch(
        "pr_agent.tools.pr_description._normalize_angular_title",
        wraps=pr_description_module._normalize_angular_title,
    ) as mock_normalize:
        await obj.run()

    title_arg = obj.git_provider.publish_description.call_args[0][0]
    if expected_title is None:
        assert title_arg is None
    else:
        assert title_arg == expected_title
    assert mock_normalize.call_count == expected_validator_calls


@patch("pr_agent.tools.pr_description.TokenHandler")
@patch("pr_agent.tools.pr_description.get_main_pr_language", return_value="Python")
@patch("pr_agent.tools.pr_description.get_git_provider_with_context")
@patch("pr_agent.tools.pr_description.get_settings")
def test_conventional_title_augments_effective_extra_instructions(
    mock_get_settings,
    mock_get_git_provider,
    _mock_get_main_pr_language,
    _mock_token_handler,
):
    settings = _settings(
        generate_ai_title=False,
        enable_conventional_title=True,
        extra_instructions="Keep existing guidance.",
    )
    mock_get_settings.return_value = settings
    git_provider = MagicMock()
    git_provider.pr = SimpleNamespace(title="Human supplied title")
    git_provider.get_languages.return_value = {}
    git_provider.get_files.return_value = []
    git_provider.get_pr_id.return_value = "1"
    git_provider.get_pr_branch.return_value = "feature/auth"
    git_provider.get_pr_description.return_value = ""
    git_provider.get_commit_messages.return_value = []
    git_provider.get_diff_files.return_value = []
    git_provider.get_user_description.return_value = ""
    mock_get_git_provider.return_value = git_provider

    obj = PRDescription("https://gitlab.example/org/repo/-/merge_requests/1", ai_handler=lambda: SimpleNamespace())

    effective_extra_instructions = obj.vars["extra_instructions"]
    assert "Keep existing guidance." in effective_extra_instructions
    assert "type(scope): summary" in effective_extra_instructions
    assert "feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert" in effective_extra_instructions
    assert settings.pr_description.extra_instructions == "Keep existing guidance."

    second_obj = PRDescription(
        "https://gitlab.example/org/repo/-/merge_requests/1",
        ai_handler=lambda: SimpleNamespace(),
    )
    assert obj.vars["extra_instructions"].count(_ANGULAR_TITLE_INSTRUCTIONS) == 1
    assert second_obj.vars["extra_instructions"].count(_ANGULAR_TITLE_INSTRUCTIONS) == 1
    assert settings.pr_description.extra_instructions == "Keep existing guidance."


@pytest.mark.parametrize("generate_ai_title", [False, True])
@pytest.mark.parametrize("ai_title", [_MISSING, "", "   ", ["bad"]])
@patch("pr_agent.tools.pr_description.retry_with_fallback_models", side_effect=_noop_retry)
@patch("pr_agent.tools.pr_description.extract_and_cache_pr_tickets", side_effect=_noop_extract_tickets)
@patch("pr_agent.tools.pr_description.get_settings")
async def test_conventional_title_missing_or_malformed_ai_title_publishes_body_without_title(
    mock_get_settings,
    _mock_extract_tickets,
    _mock_retry,
    generate_ai_title,
    ai_title,
):
    mock_get_settings.return_value = _settings(
        generate_ai_title=generate_ai_title,
        enable_conventional_title=True,
    )
    obj = _make_instance(ai_title=ai_title, pr_title="Human supplied title")

    await obj.run()

    assert obj.git_provider.publish_description.call_args[0][0] is None
    assert obj.git_provider.publish_description.call_args[0][1].startswith("body")
