"""Apply the forbidden-argument denylist to auto commands, as PRAgent already does."""
import pytest

from pr_agent.agent.pr_agent import prepare_command
from pr_agent.algo.cli_args import CliArgs
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

FORBIDDEN = [
    "--config.secret_provider=attacker_controlled",
    "--config.extra_config_url=http://example.com/evil.toml",
    "--config.review_path=/etc/cron.d/x",
    "--openai.key=sk-leaked",
]


@pytest.fixture
def settings():
    return get_settings(use_context=False)


@pytest.mark.parametrize("argument", FORBIDDEN)
def test_the_denylist_already_rejects_the_argument(argument):
    """Pin the shared contract these arguments are measured against."""
    is_valid, _ = CliArgs.validate_user_args([argument])

    assert is_valid is False


@pytest.mark.parametrize("argument", FORBIDDEN)
def test_a_forbidden_argument_is_not_applied(settings, argument):
    """An auto command must not set a key a PR comment is forbidden from setting."""
    key = argument[2:].split("=", 1)[0]
    original = settings.get(key, None)
    try:
        prepare_command(f"/review {argument}")

        assert settings.get(key, None) == original
    finally:
        settings.set(key, original)


def test_an_allowed_argument_is_still_applied(settings):
    """Ordinary auto-command overrides must keep working."""
    original = settings.get("pr_reviewer.extra_instructions", "")
    try:
        prepare_command('/review --pr_reviewer.extra_instructions="focus on tests"')

        assert settings.get("pr_reviewer.extra_instructions") == "focus on tests"
    finally:
        settings.set("pr_reviewer.extra_instructions", original)


def test_the_action_is_still_returned_for_a_forbidden_argument():
    """The command still runs; only the forbidden override is dropped."""
    assert prepare_command("/review --config.secret_provider=x")[0] == "/review"


def test_quoted_values_keep_their_boundaries():
    """Keep the quoting behaviour the tokenizer exists to preserve."""
    assert prepare_command('/describe --pr_description.extra_instructions="a b"')[0] == "/describe"


# The denylist matched the whole argument, value included, so an ordinary review instruction
# that merely mentions a forbidden key was dropped along with the genuinely forbidden ones.
INSTRUCTIONS_MENTIONING_A_FORBIDDEN_KEY = [
    "Flag any hardcoded openai.key in the diff",
    "Verify the litellm.base_url override",
    "Explain the .system prompt handling",
    "Check every config.url is not hardcoded",
    "Reject a hardcoded config.secret_provider value",
]


@pytest.mark.parametrize("instruction", INSTRUCTIONS_MENTIONING_A_FORBIDDEN_KEY)
def test_an_instruction_that_mentions_a_forbidden_key_is_still_applied(settings, instruction):
    """Only the key can set a setting; the value is free text a reviewer may write anything in."""
    original = settings.get("pr_reviewer.extra_instructions", "")
    try:
        prepare_command(f'/review --pr_reviewer.extra_instructions="{instruction}"')

        assert settings.get("pr_reviewer.extra_instructions") == instruction
    finally:
        settings.set("pr_reviewer.extra_instructions", original)


def test_a_forbidden_key_is_dropped_even_with_an_innocent_value(settings):
    """Control: the protection itself is unchanged."""
    original = settings.get("openai.key", None)
    try:
        prepare_command('/review --openai.key="a harmless looking sentence"')

        assert settings.get("openai.key", None) == original
    finally:
        settings.set("openai.key", original)


def test_every_forbidden_param_is_named_in_the_log(settings):
    """All offending arguments are dropped, so all of them are reported."""
    logged = []
    # loguru does not propagate to the stdlib logging caplog captures, so read its own sink.
    sink = get_logger().add(lambda message: logged.append(message.record["message"]), level="ERROR")
    try:
        prepare_command("/review --openai.key=a --config.secret_provider=b")
    finally:
        get_logger().remove(sink)

    reported = " ".join(logged)
    assert "openai.key" in reported
    assert "secret_provider" in reported


def test_a_forbidden_and_an_allowed_argument_are_separated(settings):
    """A dropped argument must not take an allowed one with it."""
    original = settings.get("pr_reviewer.extra_instructions", "")
    try:
        prepare_command('/review --openai.key=sk-leaked '
                        '--pr_reviewer.extra_instructions="focus on retries"')

        assert settings.get("pr_reviewer.extra_instructions") == "focus on retries"
    finally:
        settings.set("pr_reviewer.extra_instructions", original)
