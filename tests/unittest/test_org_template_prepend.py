from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.utils import load_yaml
from pr_agent.tools.pr_description import (
    PRDescription,
    _ORG_TEMPLATE_END,
    _ORG_TEMPLATE_START,
    _render_org_template_block,
    _strip_org_template_block,
)

TEMPLATE = """## What does this MR do? Why?


## Note / Risk


## Checklist
- [ ] Self-reviewed the changes
- [ ] Added or updated tests
- [ ] Updated documentation where needed
"""


def _settings(*, enable_org_template=True, use_description_markers=False):
    settings = MagicMock()
    pd = settings.pr_description
    pd.use_description_markers = use_description_markers
    pd.get.side_effect = lambda key, default=None: {
        "enable_org_template": enable_org_template,
        "enable_conventional_title": False,
    }.get(key, default)
    return settings


def _make_instance():
    obj = PRDescription.__new__(PRDescription)
    obj.git_provider = MagicMock()
    obj.git_provider.get_pr_description.return_value = ""
    obj.org_template_fields = {}
    return obj


def test_render_org_template_block_wraps_ai_sections_and_template_checklist():
    block = _render_org_template_block(TEMPLATE, "- Adds template prepend\n- Keeps walkthrough", "", "")

    assert block.startswith(_ORG_TEMPLATE_START)
    assert block.endswith(_ORG_TEMPLATE_END)
    assert block.count(_ORG_TEMPLATE_START) == 1
    assert block.count(_ORG_TEMPLATE_END) == 1
    assert "## What does this MR do? Why?\n\n- Adds template prepend" in block
    assert "## Note / Risk\n\nNone" in block
    assert "- [ ] Added or updated tests" in block
    assert "File Walkthrough" not in block
    assert "Diagram Walkthrough" not in block


def test_render_org_template_block_preserves_matching_checkbox_states():
    existing = _render_org_template_block(TEMPLATE, "- old", "None", "")
    existing = existing.replace("- [ ] Self-reviewed the changes", "- [x] Self-reviewed the changes")
    existing = existing.replace("- [ ] Updated documentation where needed", "- [X] Updated documentation where needed")

    block = _render_org_template_block(TEMPLATE, "- new", "Fresh risk", existing)

    assert "- [x] Self-reviewed the changes" in block
    assert "- [ ] Added or updated tests" in block
    assert "- [X] Updated documentation where needed" in block
    assert "- new" in block
    assert "- old" not in block


def test_strip_org_template_block_removes_only_sentinel_block():
    block = _render_org_template_block(TEMPLATE, "- old", "None", "")
    stripped = _strip_org_template_block(f"Intro\n\n{block}\n\n### **PR Description**\nDefault")

    assert _ORG_TEMPLATE_START not in stripped
    assert "Intro" in stripped
    assert "### **PR Description**\nDefault" in stripped


@patch("pr_agent.tools.pr_description.load_org_template", return_value=TEMPLATE)
@patch("pr_agent.tools.pr_description.get_settings")
def test_prepend_org_template_replaces_existing_block_and_preserves_checkboxes(mock_get_settings, _):
    mock_get_settings.return_value = _settings()
    old_block = _render_org_template_block(TEMPLATE, "- old", "None", "")
    old_block = old_block.replace("- [ ] Added or updated tests", "- [x] Added or updated tests")
    obj = _make_instance()
    obj.git_provider.get_pr_description.return_value = f"{old_block}\n\n### **PR Description**\nOld"
    obj.org_template_fields = {"what_why": "- fresh", "note_risk": "None"}

    body = obj._prepend_org_template(f"{old_block}\n\n### **PR Description**\nDefault")

    assert body.count(_ORG_TEMPLATE_START) == 1
    assert body.startswith(_ORG_TEMPLATE_START)
    assert "- fresh" in body
    assert "- old" not in body
    assert "- [x] Added or updated tests" in body
    assert body.index(_ORG_TEMPLATE_END) < body.index("### **PR Description**\nDefault")


@patch("pr_agent.tools.pr_description.get_settings")
def test_stash_org_template_fields_consumes_ai_keys(mock_get_settings):
    mock_get_settings.return_value = _settings()
    obj = _make_instance()
    obj.data = {
        "title": "AI title",
        "what_why": ["Adds org template", "Keeps PR-Agent body"],
        "note_risk": "None",
        "description": "Default body",
    }

    obj._stash_org_template_fields()

    assert obj.org_template_fields == {
        "what_why": ["Adds org template", "Keeps PR-Agent body"],
        "note_risk": "None",
    }
    assert "what_why" not in obj.data
    assert "note_risk" not in obj.data
    assert obj.data["description"] == "Default body"


@patch("pr_agent.tools.pr_description.load_org_template", return_value=TEMPLATE)
@patch("pr_agent.tools.pr_description.get_settings")
def test_marker_mode_skips_prepend(mock_get_settings, _):
    mock_get_settings.return_value = _settings(use_description_markers=True)
    obj = _make_instance()
    obj.org_template_fields = {"what_why": "- fresh", "note_risk": "None"}

    assert obj._prepend_org_template("marker body") == "marker body"


@pytest.mark.parametrize(
    "what_why,note_risk",
    [
        ("- Add org template: preserve defaults", "None"),
        ("- Keep `## PR Description` below template", "Rollout: GitLab only"),
        ("- Handle markdown **bold** text", "- Risk: malformed YAML"),
        ("- Preserve checklist states", "User can tick boxes"),
        ("- Support colon: values in bullets", "None"),
        ("- Support emoji 🚦 in content", "None"),
        ("- Keep file walkthrough separate", "No File Walkthrough literal in template"),
        ("- Replace previous sentinel block", "Risk: duplicate block prevented"),
        ("- Render Note / Risk header always", "None"),
        ("- Preserve default PR-Agent body", "Manual verification recommended"),
    ],
)
def test_yaml_block_scalar_keys_parse_with_org_template_keys(what_why, note_risk):
    prediction = f"""
title: Feature(template): add org block
what_why: |
  {what_why}
note_risk: |
  {note_risk}
description: |
  Default PR-Agent description.
pr_files:
  - filename: pr_agent/tools/pr_description.py
    changes_title: Org template prepend
    changes_summary: Adds sentinel block rendering
    label: enhancement
"""

    parsed = load_yaml(
        prediction,
        keys_fix_yaml=[
            "filename:",
            "language:",
            "changes_summary:",
            "changes_title:",
            "description:",
            "title:",
            "what_why:",
            "note_risk:",
        ],
    )

    assert isinstance(parsed, dict)
    assert parsed["what_why"]
    assert parsed["note_risk"]
