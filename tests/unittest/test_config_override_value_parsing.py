"""A `--key=value` override must reach the tool whatever the value looks like.

`update_settings_from_args` tries to read the value as YAML and falls back to the raw string.
The fallback logs at DEBUG - the level every webhook server runs at, because
`configuration.toml` ships `log_level="DEBUG"` - so the log call is on the hot path for any
value YAML rejects.
"""
import pytest

import pr_agent.agent.pr_agent as agent_module
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.log import setup_logger

PR_URL = "https://github.com/org/repo/pull/1"

# None of these can start a YAML plain scalar, so each takes the raw-string fallback.
UNPARSEABLE_VALUES = [
    "`{}` is preferred over dict()",
    "*Always* check {placeholders} in f-strings",
    "@decorator usage: verify {kwargs} handling",
    "%s and {0} are both positional",
    "&anchor {name} lookups",
    "unbalanced { brace",
]


@pytest.fixture
def debug_logging():
    """The level every webhook server passes to setup_logger."""
    setup_logger(level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
    yield
    setup_logger(level="INFO")


@pytest.fixture
def review_tool(monkeypatch):
    ran = {}

    class FakeReviewTool:
        def __init__(self, pr_url, ai_handler=None, args=None):
            ran["args"] = args

        async def run(self):
            ran["ran"] = True

    monkeypatch.setitem(agent_module.command2class, "review", FakeReviewTool)
    monkeypatch.setattr(agent_module, "apply_repo_settings", lambda pr_url: None)
    return ran


@pytest.mark.parametrize("value", UNPARSEABLE_VALUES)
def test_an_unparseable_value_is_stored_as_the_raw_string(monkeypatch, debug_logging, value):
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "extra_instructions", "")

    update_settings_from_args([f"--pr_reviewer.extra_instructions={value}"])

    assert settings.pr_reviewer.extra_instructions == value


def test_a_parseable_value_is_still_converted(monkeypatch, debug_logging):
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "num_max_findings", 3)

    update_settings_from_args(["--pr_reviewer.num_max_findings=5"])

    assert settings.pr_reviewer.num_max_findings == 5


async def test_a_braced_instruction_reaches_the_tool(monkeypatch, debug_logging, review_tool):
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "extra_instructions", "")

    handled = await agent_module.PRAgent().handle_request(
        PR_URL, '/review --pr_reviewer.extra_instructions="`{}` is preferred over dict()"')

    assert handled is True
    assert review_tool.get("ran") is True
    assert settings.pr_reviewer.extra_instructions == "`{}` is preferred over dict()"


async def test_a_plain_instruction_reaches_the_tool(monkeypatch, debug_logging, review_tool):
    """Control: the same request with a value YAML accepts always worked."""
    settings = get_settings()
    monkeypatch.setattr(settings.pr_reviewer, "extra_instructions", "")

    handled = await agent_module.PRAgent().handle_request(
        PR_URL, '/review --pr_reviewer.extra_instructions="focus on the retry loop"')

    assert handled is True
    assert review_tool.get("ran") is True
