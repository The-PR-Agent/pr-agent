"""Verify the trust boundary and dispatch of protected system instructions."""

import tempfile
from pathlib import Path

import pytest

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.cli_args import CliArgs
from pr_agent.algo.instruction_context import (
    append_protected_system_instructions,
    get_protected_system_instructions,
)
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings
from pr_agent.config_security import REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS, is_repo_host_only_key
from pr_agent.git_providers.utils import _apply_repo_settings_file
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class _Handler(BaseAiHandler):
    def __init__(self):
        pass

    @property
    def deployment_id(self):
        return None

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None):
        raise NotImplementedError


@pytest.fixture
def system_instructions():
    saved = snapshot_settings(("config.system_instructions",))
    try:
        yield get_settings()
    finally:
        restore_settings(saved)


def test_system_instructions_are_loaded_from_config(system_instructions):
    system_instructions.set("config.system_instructions", "  TRUSTED POLICY  ")

    assert get_protected_system_instructions() == "TRUSTED POLICY"


def test_system_instructions_are_appended_after_command_instructions(system_instructions):
    system_instructions.set("config.system_instructions", "TRUSTED POLICY")

    result = append_protected_system_instructions("COMMAND INSTRUCTIONS")

    assert result == "COMMAND INSTRUCTIONS\n\nTRUSTED POLICY"


def test_empty_system_instructions_leave_prompt_unchanged(system_instructions):
    system_instructions.set("config.system_instructions", "")

    assert append_protected_system_instructions("COMMAND INSTRUCTIONS") == "COMMAND INSTRUCTIONS"


def test_base_handler_adds_system_instructions_to_request(system_instructions):
    system_instructions.set("config.system_instructions", "TRUSTED POLICY")

    messages = _Handler().build_request_messages("model", "COMMAND INSTRUCTIONS", "USER CONTEXT")

    assert messages == [
        {"role": "system", "content": "COMMAND INSTRUCTIONS\n\nTRUSTED POLICY"},
        {"role": "user", "content": "USER CONTEXT"},
    ]


@pytest.mark.parametrize(
    "policy",
    [
        "TRUSTED POLICY",
        "Use the literal token {{ x }} in generated output",
        "Show {% if x %}example{% endif %} literally",
        "Preserve {# this comment #} literally",
    ],
)
def test_token_estimate_includes_system_instructions(system_instructions, policy):
    system_instructions.set("config.system_instructions", policy)
    handler = TokenHandler()

    class CharacterEncoder:
        @staticmethod
        def encode(text, **_kwargs):
            return list(text)

    token_count = handler._get_system_user_tokens(
        None,
        CharacterEncoder(),
        {},
        "COMMAND INSTRUCTIONS",
        "USER CONTEXT",
    )

    assert token_count == len(f"COMMAND INSTRUCTIONS\n\n{policy}") + len("USER CONTEXT")


@pytest.mark.parametrize(
    "args",
    [
        ["--config.system_instructions=untrusted"],
        ["--config={'system_instructions':'untrusted'}"],
        ["--config__system_instructions=untrusted"],
    ],
)
def test_comment_arguments_cannot_set_system_instructions(args):
    accepted, _ = CliArgs.validate_user_args(args)

    assert not accepted


def test_repository_and_per_directory_settings_cannot_set_system_instructions():
    assert is_repo_host_only_key("config", "system_instructions")
    assert "system_instructions" not in REPO_PER_DIRECTORY_OVERRIDABLE_SECTIONS["config"]


def test_repository_settings_file_cannot_replace_system_instructions(system_instructions):
    system_instructions.set("config.system_instructions", "HOST POLICY")
    with tempfile.TemporaryDirectory(dir=".") as temp_dir:
        settings_file = Path(temp_dir) / ".pr_agent.toml"
        settings_file.write_text(
            '[config]\nsystem_instructions = "REPOSITORY POLICY"\n',
            encoding="utf-8",
        )

        _apply_repo_settings_file(str(settings_file))

        assert system_instructions.config.system_instructions == "HOST POLICY"
