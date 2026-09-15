"""Validate and read the labels list inside each model attempt."""
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_generate_labels import PRGenerateLabels
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = (
    "config.model",
    "config.fallback_models",
    "config.publish_output",
    "config.enable_custom_labels",
    "custom_labels",
    "model_routing.enable",
    "openai.deployment_id",
    "openai.fallback_deployments",
)

_VALID_FALLBACK = "labels:\n- bug fix\n"


@pytest.fixture
def fallback_models():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    settings = get_settings()
    settings.set("config.model", "primary-model")
    settings.set("config.fallback_models", ["fallback-model"])
    settings.set("config.publish_output", True)
    settings.set("config.enable_custom_labels", False)
    settings.set("custom_labels", [])
    settings.set("model_routing.enable", False)
    settings.set("openai.deployment_id", None)
    settings.set("openai.fallback_deployments", [])
    yield
    restore_settings(snapshot)


def labels(prediction, variables=None):
    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.prediction = prediction
    tool.variables = variables or {"title": "a title"}
    tool.pr_id = "repo#1"
    tool._prepare_data()
    return tool._prepare_labels()


def label_tool(git_provider=None):
    tool = PRGenerateLabels.__new__(PRGenerateLabels)
    tool.git_provider = git_provider or MagicMock()
    tool.token_handler = MagicMock()
    tool.pr_id = "repo#1"
    tool.patches_diff = None
    tool.prediction = None
    tool.data = None
    tool.variables = {}
    return tool


def test_read_the_documented_list_of_strings():
    """Keep the documented shape working exactly as before."""
    assert labels("labels:\n- bug fix\n- tests\n") == ["bug fix", "tests"]


def test_read_a_comma_separated_string():
    """Keep the string form the parser already accepts."""
    assert labels("labels: |\n  bug fix, tests\n") == ["bug fix", "tests"]


@pytest.mark.parametrize("prediction, expected", [
    ("labels:\n- name: bug fix\n", ["bug fix"]),
    ("labels:\n- label: bug fix\n", ["bug fix"]),
    ("labels:\n- 1\n", ["1"]),
])
def test_read_an_entry_that_is_not_a_plain_string(prediction, expected):
    """A mapping or a number must not fail the whole /generate_labels run."""
    assert labels(prediction) == expected


def test_drop_an_entry_that_carries_no_name():
    """An unusable entry is skipped, not turned into an empty label."""
    assert labels("labels:\n- {}\n- bug fix\n") == ["bug fix"]


def test_keep_custom_label_case_mapping():
    assert labels(
        "labels:\n- bug_fix\n",
        {"labels_minimal_to_labels_dict": {"bug_fix": "Bug Fix"}},
    ) == ["Bug Fix"]


def test_no_labels_key_is_not_a_successful_attempt():
    with pytest.raises(ValueError, match="labels"):
        labels("types:\n- bug fix\n")


@pytest.mark.asyncio
async def test_malformed_primary_uses_valid_fallback_and_preserves_user_labels(fallback_models):
    provider = MagicMock()
    provider.publish_comment.return_value = MagicMock()
    provider.is_supported.return_value = True
    provider.get_pr_labels.return_value = ["keep-me"]
    tool = label_tool(provider)
    tool._get_prediction = AsyncMock(side_effect=["not valid labels yaml", _VALID_FALLBACK])

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await tool.run()

    assert tool._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    provider.publish_labels.assert_called_once_with(["bug fix", "keep-me"])
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.asyncio
async def test_explicit_empty_labels_is_valid_and_keeps_publication_path(fallback_models):
    provider = MagicMock()
    provider.publish_comment.return_value = MagicMock()
    provider.is_supported.return_value = True
    provider.get_pr_labels.return_value = ["keep-me"]
    tool = label_tool(provider)
    tool._get_prediction = AsyncMock(return_value="labels: []\n")

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await tool.run()

    tool._get_prediction.assert_awaited_once_with("primary-model")
    provider.publish_labels.assert_called_once_with(["keep-me"])
    provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.parametrize(
    "invalid_prediction",
    [
        "",
        "not valid labels yaml",
        "- bug fix\n",
        "types:\n- bug fix\n",
        "labels:\n  name: bug fix\n",
        "labels:\n",
        "labels: 7\n",
        "labels: false\n",
        "labels: ' , '\n",
        "labels:\n- {}\n- false\n",
    ],
)
@pytest.mark.asyncio
async def test_invalid_attempt_shapes_reach_fallback(invalid_prediction, fallback_models):
    tool = label_tool()
    tool._get_prediction = AsyncMock(side_effect=[invalid_prediction, _VALID_FALLBACK])

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await retry_with_fallback_models(tool._prepare_prediction, git_provider=tool.git_provider)

    assert tool._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert tool.prediction == _VALID_FALLBACK
    assert tool._prepare_labels() == ["bug fix"]


@pytest.mark.asyncio
async def test_usable_entries_survive_malformed_siblings_without_fallback(fallback_models):
    tool = label_tool()
    tool._get_prediction = AsyncMock(return_value="labels:\n- {}\n- bug fix\n- false\n")

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await retry_with_fallback_models(tool._prepare_prediction, git_provider=tool.git_provider)

    tool._get_prediction.assert_awaited_once_with("primary-model")
    assert tool._prepare_labels() == ["bug fix"]


@pytest.mark.asyncio
async def test_all_invalid_attempts_clear_state_and_cleanup_progress_once(fallback_models):
    provider = MagicMock()
    provider.publish_comment.return_value = MagicMock()
    provider.is_supported.return_value = True
    tool = label_tool(provider)
    tool._get_prediction = AsyncMock(
        side_effect=["types:\n- bug fix\n", "labels:\n- {}\n- false\n"]
    )

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await tool.run()

    assert tool._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert tool.prediction is None
    assert tool.data is None
    provider.publish_labels.assert_not_called()
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.remove_initial_comment.assert_called_once_with()


@pytest.mark.asyncio
async def test_all_invalid_attempts_do_not_cleanup_without_progress_handle(fallback_models):
    provider = MagicMock()
    provider.publish_comment.return_value = None
    provider.is_supported.return_value = True
    tool = label_tool(provider)
    tool._get_prediction = AsyncMock(
        side_effect=["types:\n- bug fix\n", "labels:\n- {}\n- false\n"]
    )

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await tool.run()

    assert tool._get_prediction.await_args_list == [call("primary-model"), call("fallback-model")]
    assert tool.prediction is None
    assert tool.data is None
    provider.publish_labels.assert_not_called()
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.remove_initial_comment.assert_not_called()
    provider.remove_comment.assert_not_called()


@pytest.mark.asyncio
async def test_valid_attempt_does_not_cleanup_without_progress_handle(fallback_models):
    provider = MagicMock()
    provider.publish_comment.return_value = None
    provider.is_supported.return_value = True
    provider.get_pr_labels.return_value = []
    tool = label_tool(provider)
    tool._get_prediction = AsyncMock(return_value=_VALID_FALLBACK)

    with patch("pr_agent.tools.pr_generate_labels.get_pr_diff", return_value="diff"):
        await tool.run()

    tool._get_prediction.assert_awaited_once_with("primary-model")
    provider.publish_labels.assert_called_once_with(["bug fix"])
    provider.publish_comment.assert_called_once_with("Preparing PR labels...", is_temporary=True)
    provider.remove_initial_comment.assert_not_called()
    provider.remove_comment.assert_not_called()
