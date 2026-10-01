"""Regression tests for the default label set used when ``enable_custom_labels`` is on.

With no ``[custom_labels]`` section configured, ``set_custom_labels`` built a
fallback list and wrote it to a ``custom_labels`` key that no template reads. The
prompts read ``custom_labels_class``, so it stayed empty and the model was handed
``labels: List[Label]`` with no ``Label`` class defined at all.

``get_user_labels`` hardcoded a five-entry list that omitted "Bug fix with
tests", so that bot-applied label was mistaken for a user label and preserved
forever.
"""

import pytest
from jinja2 import Environment, StrictUndefined

from pr_agent.algo.utils import get_user_labels, set_custom_labels
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = ("config.enable_custom_labels", "custom_labels")

_DEFAULT_LABELS = [
    "Bug fix",
    "Tests",
    "Bug fix with tests",
    "Enhancement",
    "Documentation",
    "Other",
]

_PROMPT_VARS = {
    "enable_custom_labels": True,
    "custom_labels_class": "",
    "extra_instructions": "",
    "title": "",
    "branch": "",
    "description": "",
    "language": "",
    "diff": "",
    "commit_messages_str": "",
}


@pytest.fixture
def settings():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    yield get_settings()
    restore_settings(snapshot)


def _render(template, variables):
    return Environment(undefined=StrictUndefined, autoescape=True).from_string(template).render(
        **variables
    )


def test_fallback_defines_the_label_enum(settings):
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {})
    variables = dict(_PROMPT_VARS)

    set_custom_labels(variables)

    assert variables["custom_labels_class"].startswith("class Label(str, Enum):")
    for label in _DEFAULT_LABELS:
        key = label.lower().replace(" ", "_")
        assert f"{key} = '{label}'" in variables["custom_labels_class"]


def test_fallback_maps_keys_back_to_label_names(settings):
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {})
    variables = dict(_PROMPT_VARS)

    set_custom_labels(variables)

    assert variables["labels_minimal_to_labels_dict"] == {
        label.lower().replace(" ", "_"): label for label in _DEFAULT_LABELS
    }


def test_fallback_does_not_write_the_unused_key(settings):
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {})
    variables = dict(_PROMPT_VARS)

    set_custom_labels(variables)

    assert "custom_labels" not in variables


def test_custom_labels_prompt_declares_a_label_type(settings):
    """The template asks for List[Label], so a Label class has to be rendered."""
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {})
    variables = dict(_PROMPT_VARS)
    set_custom_labels(variables)

    template = get_settings().pr_custom_labels_prompt.system
    rendered = _render(template, variables)

    assert "class Label(str, Enum):" in rendered
    assert "labels: List[Label]" in rendered


def test_configured_labels_are_unchanged(settings):
    settings.set("config.enable_custom_labels", True)
    settings.set(
        "custom_labels",
        {"Bug fix": {"description": "Fixes a bug"}, "Tests": {"description": "Adds tests"}},
    )
    variables = dict(_PROMPT_VARS)

    set_custom_labels(variables)

    assert variables["labels_minimal_to_labels_dict"] == {"bug_fix": "Bug fix", "tests": "Tests"}
    assert "bug_fix = 'Fixes a bug'" in variables["custom_labels_class"]


def test_disabled_switch_writes_nothing(settings):
    settings.set("config.enable_custom_labels", False)
    variables = dict(_PROMPT_VARS)

    set_custom_labels(variables)

    assert variables["custom_labels_class"] == ""


def test_get_user_labels_drops_every_default_bot_label(settings):
    """The unconfigured fallback is the full six, so all six are bot-owned."""
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {})

    result = get_user_labels(_DEFAULT_LABELS + ["P0", "needs design review"])

    assert result == ["P0", "needs design review"]


def test_get_user_labels_keeps_bug_fix_with_tests_when_custom_labels_disabled(settings):
    """With the switch off the prompt only offers five labels, so the sixth is a user label."""
    settings.set("config.enable_custom_labels", False)
    settings.set("custom_labels", {})

    assert get_user_labels(["Bug fix with tests", "P0"]) == ["Bug fix with tests", "P0"]


def test_get_user_labels_keeps_labels_excluded_by_a_configured_set(settings):
    """A configured set that omits a label leaves it user-owned, not bot-owned."""
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {"Feature": {"description": "new feature"}})

    assert get_user_labels(["Bug fix with tests", "Feature", "P0"]) == ["Bug fix with tests", "P0"]


def test_get_user_labels_ignores_case(settings):
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {})

    assert get_user_labels(["bug fix WITH tests", "P1"]) == ["P1"]


def test_get_user_labels_drops_describe_types_with_a_configured_set(settings):
    """/describe still publishes the built-in PR types when a custom set is configured."""
    settings.set("config.enable_custom_labels", True)
    settings.set("custom_labels", {"Feature": {"description": "new feature"}})

    assert get_user_labels(["Bug fix", "Enhancement", "Feature", "P0"]) == ["P0"]
