"""A whole-section comment argument must merge into the section, not replace it.

Regression tests for #4450: `--pr_description={'publish_labels':false}` used to
replace the whole [pr_description] section (23 keys -> 1), and a non-mapping
section value such as `--pr_description=x` replaced the section with a string.
"""
import copy

import pytest

from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings


@pytest.fixture
def restore_sections():
    settings = get_settings()
    snapshot = {
        name: copy.deepcopy(settings.as_dict().get(name, {}))
        for name in ("PR_DESCRIPTION", "PR_REVIEWER")
    }
    yield
    for name, section in snapshot.items():
        settings.unset(name)
        settings.set(name, section, merge=False)


def _section(name):
    return get_settings().as_dict().get(name, {})


def test_mapping_arg_merges_into_section_leaving_other_keys(restore_sections):
    before = _section("PR_DESCRIPTION")
    assert len(before) == 23

    update_settings_from_args(["--pr_description={'publish_labels':false}"])

    after = _section("PR_DESCRIPTION")
    assert len(after) == 23
    assert after["publish_labels"] is False
    assert after["enable_pr_diagram"] is True
    assert after["collapsible_file_list_threshold"] == 6


def test_mapping_arg_changes_only_the_given_key(restore_sections):
    update_settings_from_args(["--pr_description={'enable_pr_diagram':false}"])

    after = _section("PR_DESCRIPTION")
    assert after["enable_pr_diagram"] is False
    assert after["publish_labels"] is False
    assert after["collapsible_file_list_threshold"] == 6


def test_empty_mapping_arg_leaves_section_unchanged(restore_sections):
    before = _section("PR_DESCRIPTION")

    update_settings_from_args(["--pr_description={}"])

    assert _section("PR_DESCRIPTION") == before


def test_non_mapping_section_arg_is_rejected(restore_sections):
    before = _section("PR_DESCRIPTION")

    with pytest.raises(ValueError):
        update_settings_from_args(["--pr_description=x"])

    assert _section("PR_DESCRIPTION") == before


def test_non_mapping_list_section_arg_is_rejected(restore_sections):
    before = _section("PR_DESCRIPTION")

    with pytest.raises(ValueError):
        update_settings_from_args(["--pr_description=[1, 2]"])

    assert _section("PR_DESCRIPTION") == before


def test_dotted_arg_still_sets_a_single_key(restore_sections):
    before = _section("PR_REVIEWER")

    update_settings_from_args(["--pr_reviewer.extra_instructions=be concise"])

    after = _section("PR_REVIEWER")
    assert after["extra_instructions"] == "be concise"
    assert len(after) == len(before)
