"""Tests for the Phase 4 '## Changes' embed and enable_pr_agent_output suppression.

Covers:
- _render_org_template_block fills the walkthrough + diagram into a '## Changes'
  section.
- The filled block contains no forbidden literals ('File Walkthrough' /
  'Diagram Walkthrough' — Phase 3 SC#5) and no unfilled marker literals.
- _prepend_org_template suppresses PR-Agent's default body when
  enable_pr_agent_output=false (default) and the org template is active.
- _prepend_org_template renders the default body below the block when
  enable_pr_agent_output=true.
- enable_org_template=false leaves pr_body unchanged (byte-identical-when-off).
"""

from unittest.mock import MagicMock, patch

from pr_agent.tools.pr_description import (
    PRDescription,
    _ORG_TEMPLATE_END,
    _ORG_TEMPLATE_START,
    _render_org_template_block,
)


def _settings(*, enable_org_template=True, enable_pr_agent_output=False, use_description_markers=False):
    settings = MagicMock()
    pd = settings.pr_description
    pd.use_description_markers = use_description_markers
    pd.get.side_effect = lambda key, default=None: {
        "enable_org_template": enable_org_template,
        "enable_conventional_title": False,
        "enable_pr_agent_output": enable_pr_agent_output,
        "use_description_markers": use_description_markers,
    }.get(key, default)
    # Make the [config] section behave as empty so _fork_toggle falls through
    # to pr_description.* (the Phase 4 MagicMock landmine).
    config_section = MagicMock()
    config_section.get.side_effect = lambda key, default=None: default
    settings.get.side_effect = lambda key, default=None: (
        config_section if key == "config" else default
    )
    return settings


def _make_instance():
    obj = PRDescription.__new__(PRDescription)
    obj.git_provider = MagicMock()
    # _prepend_org_template fetches the existing MR description for checkbox
    # preservation; mock it to return empty (no prior block).
    obj.git_provider.get_pr_description.return_value = ""
    # _prepend_org_template reads self.git_provider and self.org_template_fields
    obj.org_template_fields = {
        "what_why": "- change A",
        "note_risk": "None",
        "walkthrough": "| File | Action |\n|---|---|\n| a.py | add |",
        "diagram": "```mermaid\nsequenceDiagram\n```",
    }
    return obj


TEMPLATE_BODY = (
    "## What does this MR do? Why?\n\n\n"
    "## Note / Risk\n\n\n"
    "## Changes\n\npr_agent:walkthrough\n\npr_agent:diagram\n\n"
    "## Checklist\n- [ ] Self-reviewed\n"
)


def test_render_fills_walkthrough_and_diagram():
    block = _render_org_template_block(
        TEMPLATE_BODY,
        what_why="- change A",
        note_risk="None",
        existing_description="",
        walkthrough="| File | Action |\n|---|---|\n| a.py | add |",
        diagram="```mermaid\nsequenceDiagram\n```",
    )
    assert "## Changes" in block
    assert "| a.py | add |" in block
    assert "```mermaid" in block


def test_render_has_no_unfilled_markers():
    block = _render_org_template_block(
        TEMPLATE_BODY,
        what_why="- change A",
        note_risk="None",
        existing_description="",
        walkthrough="WT",
        diagram="DG",
    )
    assert "pr_agent:walkthrough" not in block
    assert "pr_agent:diagram" not in block


def test_render_has_no_forbidden_literals():
    """Phase 3 SC#5: the org block must not contain 'File Walkthrough' /
    'Diagram Walkthrough' literals (process_description splits on them)."""
    block = _render_org_template_block(
        TEMPLATE_BODY,
        what_why="- change A",
        note_risk="None",
        existing_description="",
        walkthrough="WT content",
        diagram="DG content",
    )
    assert "File Walkthrough" not in block
    assert "Diagram Walkthrough" not in block


@patch("pr_agent.tools.pr_description.load_org_template", return_value=TEMPLATE_BODY)
@patch("pr_agent.tools.pr_description._is_gitlab_provider", return_value=True)
@patch("pr_agent.tools.pr_description.get_settings")
def test_suppression_on_returns_only_block(mock_get_settings, _mock_is_gitlab, _mock_load_template):
    """enable_pr_agent_output=false (default) -> only the org block renders."""
    mock_get_settings.return_value = _settings(enable_org_template=True, enable_pr_agent_output=False)
    obj = _make_instance()
    result = obj._prepend_org_template("PR-Agent default body")
    # Block present, default body suppressed.
    assert _ORG_TEMPLATE_START in result
    assert _ORG_TEMPLATE_END in result
    assert "## Changes" in result
    assert "PR-Agent default body" not in result


@patch("pr_agent.tools.pr_description.load_org_template", return_value=TEMPLATE_BODY)
@patch("pr_agent.tools.pr_description._is_gitlab_provider", return_value=True)
@patch("pr_agent.tools.pr_description.get_settings")
def test_suppression_off_renders_default_body(mock_get_settings, _mock_is_gitlab, _mock_load_template):
    """enable_pr_agent_output=true -> default body renders below the org block."""
    mock_get_settings.return_value = _settings(enable_org_template=True, enable_pr_agent_output=True)
    obj = _make_instance()
    result = obj._prepend_org_template("PR-Agent default body")
    assert _ORG_TEMPLATE_START in result
    assert "## Changes" in result
    assert "PR-Agent default body" in result


@patch("pr_agent.tools.pr_description.load_org_template", return_value=TEMPLATE_BODY)
@patch("pr_agent.tools.pr_description._is_gitlab_provider", return_value=True)
@patch("pr_agent.tools.pr_description.get_settings")
def test_byte_identical_when_org_template_off(mock_get_settings, _mock_is_gitlab, _mock_load_template):
    """enable_org_template=false -> pr_body returned unchanged regardless of
    enable_pr_agent_output (byte-identical-when-off regression)."""
    mock_get_settings.return_value = _settings(enable_org_template=False, enable_pr_agent_output=False)
    obj = _make_instance()
    result = obj._prepend_org_template("untouched body")
    assert result == "untouched body"


@patch("pr_agent.tools.pr_description._is_gitlab_provider", return_value=True)
@patch("pr_agent.tools.pr_description.get_settings")
def test_stash_skips_walkthrough_when_file_label_dict_none(mock_get_settings, _mock_is_gitlab):
    """Regression: when file_label_dict is None (e.g. enable_semantic_files_types
    off), _stash_org_template_fields must NOT call process_pr_files_prediction
    with None (which would throw 'NoneType' object has no attribute 'keys' and
    yield a partial table). Walkthrough falls back to empty."""
    mock_get_settings.return_value = _settings(enable_org_template=True)
    obj = PRDescription.__new__(PRDescription)
    obj.git_provider = MagicMock()
    obj.file_label_dict = None  # the regression condition
    obj.data = {
        "what_why": "- change",
        "note_risk": "None",
        "pr_files": [{"filename": "a.py", "changes_title": "t", "label": "enh"}],
    }

    obj._stash_org_template_fields()

    assert obj.org_template_fields["walkthrough"] == ""
    assert "diagram" in obj.org_template_fields
