"""Fork-safe seam invariants for /describe.

Phase 01 gating proof (CFG-05): with both fork toggles off (shipped defaults),
`PRDescription._prepare_pr_answer` returns exactly the same `(title, body)` as
upstream. ALL inputs are pinned (fixed ``self.data``, fixed ``self.vars``,
deterministic git-provider capabilities, exact settings). The assertion is
against an exact string literal — any Phase 2/3 change that leaks behavior
into the toggles-off path fails this test.
"""

from unittest.mock import MagicMock, patch

from pr_agent.tools.pr_description import PRDescription


def _make_instance() -> PRDescription:
    """Construct a ``PRDescription`` instance without running ``__init__``.

    Mirrors the bypass pattern from ``test_pr_description_output_core.py`` so
    the golden characterization exercises only ``_prepare_pr_answer`` — no
    LLM calls, no live git provider, no dynaconf initialization.
    """
    with patch.object(PRDescription, "__init__", lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.vars = {}
    obj.data = {}
    obj.pr_id = "1"
    obj.file_label_dict = {}
    obj.COLLAPSIBLE_FILE_LIST_THRESHOLD = 8
    obj.user_description = ""
    return obj


def _pinned_settings() -> MagicMock:
    """Build a settings mock with every pr_description key ``_prepare_pr_answer``
    reads pinned to a fixed value. Both fork toggles are read via ``.get`` and
    resolve to ``False`` (the shipped defaults).
    """
    settings = MagicMock()
    pd = settings.pr_description
    # Attribute-access reads inside _prepare_pr_answer.
    pd.enable_pr_type = True
    pd.generate_ai_title = True
    pd.enable_semantic_files_types = False

    # `.get(...)` reads — including the two fork toggles (defaults off).
    pd.get.side_effect = lambda key, default=None: {
        "file_table_collapsible_open_by_default": False,
        "enable_conventional_title": False,
        "enable_org_template": False,
    }.get(key, default)
    return settings


def _pinned_git_provider() -> MagicMock:
    """Deterministic git provider: no gfm_markdown, no native labels."""
    gp = MagicMock()
    gp.is_supported.side_effect = lambda cap: {
        "gfm_markdown": False,
        "get_labels": False,
    }.get(cap, False)
    gp.get_diff_files.return_value = []
    gp.get_line_link.return_value = ""
    return gp


class TestByteIdenticalWhenToggleOff:
    """Golden characterization: with both fork toggles off, `_prepare_pr_answer`
    output is byte-identical to upstream for a fully-pinned input.
    """

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_prepare_pr_answer_is_byte_identical_when_toggles_off(self, mock_get_settings):
        mock_get_settings.return_value = _pinned_settings()

        obj = _make_instance()
        # Pin every input the method reads. Order matters for pr_body assembly.
        obj.vars = {"title": "Original title"}
        obj.data = {
            "title": "AI Title",
            "type": ["Enhancement"],
            "description": "Adds a feature",
        }
        obj.git_provider = _pinned_git_provider()

        # Sanity-check the fork toggles are OFF via `.get` (defaults-off contract).
        pd = mock_get_settings.return_value.pr_description
        assert pd.get("enable_conventional_title", False) is False
        assert pd.get("enable_org_template", False) is False

        title, pr_body, changes_walkthrough, pr_file_changes = obj._prepare_pr_answer()

        # Golden literals — captured from upstream `_prepare_pr_answer` output
        # for the exact pinned input above. Any drift here means fork behavior
        # is leaking into the toggles-off path (CFG-05 violation).
        expected_title = "AI Title"
        expected_body = (
            "### **PR Type**\n"
            "Enhancement\n"
            "\n\n___\n\n"
            "### **Description**\n"
            "Adds a feature\n"
        )

        assert title == expected_title, (
            f"title drifted from golden.\n"
            f"expected: {expected_title!r}\n"
            f"actual:   {title!r}"
        )
        assert pr_body == expected_body, (
            f"pr_body drifted from golden.\n"
            f"expected: {expected_body!r}\n"
            f"actual:   {pr_body!r}"
        )
        # No walkthrough path exercised (no pr_files in data, no gfm).
        assert changes_walkthrough == ""
        assert pr_file_changes == []
