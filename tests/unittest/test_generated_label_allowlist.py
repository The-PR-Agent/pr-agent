"""Model-generated labels must stay inside the configured label vocabulary."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.algo.utils import get_user_labels, set_custom_labels
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_generate_labels import PRGenerateLabels
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.fixture(autouse=True)
def label_settings():
    snapshot = snapshot_settings(("config.enable_custom_labels", "custom_labels", "pr_description.publish_labels"))
    get_settings().set("config.enable_custom_labels", False)
    get_settings().set("custom_labels", {})
    get_settings().set("pr_description.publish_labels", True)
    yield
    restore_settings(snapshot)


@pytest.mark.parametrize("tool_class", [PRGenerateLabels, PRDescription])
@pytest.mark.parametrize("enabled,custom,labels,expected", [
    (False, {}, ["Bug fix", "deploy-production", "tests"], ["Bug fix", "tests"]),
    (True, {"Release ready": "ready"}, ["release_ready", "Other", "invented"], ["Release ready", "Other"]),
    (False, {"Release ready": "ready"}, ["Release ready", "Other"], ["Other"]),
    (True, {}, ["bug_fix_with_tests", "invented"], ["Bug fix with tests"]),
    (False, {}, ["invented"], []),
    (False, {}, ["bug_fix", "BUG_FIX", "invented"], ["Bug fix", "Bug fix"]),
    (True, {"Release ready": "ready"}, [" RELEASE_READY "], ["Release ready"]),
    (True, {}, ["BUG_FIX_WITH_TESTS"], ["Bug fix with tests"]),
    (False, {"Release ready": "ready"}, ["RELEASE_READY"], []),
])
def test_prepare_labels_filters_model_output(tool_class, enabled, custom, labels, expected):
    get_settings().set("config.enable_custom_labels", enabled)
    get_settings().set("custom_labels", custom)
    tool = tool_class.__new__(tool_class)
    tool.pr_id = "repo#1"
    tool.data = {"labels": labels}
    tool.variables = {}
    set_custom_labels(tool.variables)

    assert tool._prepare_labels() == expected


def test_describe_type_fallback_is_filtered_and_dropped_values_are_logged():
    tool = PRDescription.__new__(PRDescription)
    tool.pr_id = "repo#1"
    tool.data = {"type": "Bug fix, deploy-production"}
    tool.variables = {}
    with patch("pr_agent.algo.utils.get_logger") as logger:
        assert tool._prepare_labels() == ["Bug fix"]
    assert "deploy-production" in str(logger.return_value.warning.call_args)


def test_existing_human_labels_are_not_subject_to_model_allowlist():
    assert get_user_labels(["Bug fix", "deploy-production", "P0"]) == ["deploy-production", "P0"]


@pytest.mark.parametrize("supports_labels", [True, False])
@pytest.mark.asyncio
async def test_generate_labels_filters_before_publication(supports_labels):
    snapshot = snapshot_settings(("config.publish_output",))
    get_settings().set("config.publish_output", True)
    try:
        tool = PRGenerateLabels.__new__(PRGenerateLabels)
        tool.pr_id = "repo#1"
        tool.prediction = "labels: [Bug fix, deploy-production]"
        tool.data = {"labels": ["Bug fix", "deploy-production"]}
        tool.variables = {}
        tool.git_provider = MagicMock()
        tool.git_provider.is_supported.return_value = supports_labels
        tool.git_provider.get_pr_labels.return_value = ["P0"]
        with patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new=AsyncMock()):
            await tool.run()
        if supports_labels:
            tool.git_provider.publish_labels.assert_called_once_with(["Bug fix", "P0"])
        else:
            tool.git_provider.publish_labels.assert_not_called()
            tool.git_provider.publish_comment.assert_any_call("## PR Labels:\nBug fix\n", is_temporary=False)
    finally:
        restore_settings(snapshot)


@pytest.mark.parametrize("generated,expected", [
    (["deploy-production"], None),
    ([], ["P0"]),
    (["bug_fix", "deploy-production"], ["Bug fix", "P0"]),
])
@pytest.mark.asyncio
async def test_rejected_output_preserves_existing_labels_but_explicit_empty_can_clear(generated, expected):
    snapshot = snapshot_settings(("config.publish_output",))
    get_settings().set("config.publish_output", True)
    try:
        tool = PRGenerateLabels.__new__(PRGenerateLabels)
        tool.pr_id = "repo#1"
        tool.prediction = "model response"
        tool.data = {"labels": generated}
        tool.variables = {}
        tool.git_provider = MagicMock()
        tool.git_provider.is_supported.return_value = True
        tool.git_provider.get_pr_labels.return_value = ["Enhancement", "P0"]
        with patch("pr_agent.tools.pr_generate_labels.retry_with_fallback_models", new=AsyncMock()):
            await tool.run()
        if expected is None:
            tool.git_provider.publish_labels.assert_not_called()
        else:
            tool.git_provider.publish_labels.assert_called_once_with(expected)
        tool.git_provider.remove_initial_comment.assert_called_once_with()
    finally:
        restore_settings(snapshot)
