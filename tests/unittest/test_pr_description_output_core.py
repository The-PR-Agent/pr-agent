"""Focused unit tests for /describe output behavior.

These tests target stable helper seams on ``PRDescription`` and the
``process_description`` helper. They avoid LLM/network calls by bypassing
``__init__`` and providing minimal in-memory state.

Coverage:
* ``_prepare_data`` key reordering, diagram sanitization removal, and
  ``add_original_user_description`` injection.
* ``_prepare_labels`` list/string parsing, fallback-to-type behavior, and
  ``labels_minimal_to_labels_dict`` re-casing.
* ``_prepare_pr_answer_with_markers`` HTML-comment guards, generated-by
  header injection, list-type joining, and the diagram marker dual-format.
* ``_prepare_pr_answer`` non-gfm vs gfm branching, ``enable_pr_type``
  toggling, ``get_labels`` removal, and description bullet formatting.
* ``process_pr_files_prediction`` gfm-only table rendering.
* Round-trip: ``process_description`` recovers files from a rendered
  walkthrough produced by ``process_pr_files_prediction``.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import PRDescriptionHeader, process_description
from pr_agent.tools.pr_description import PRDescription

KEYS_FIX = ["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]


def _make_instance(prediction_yaml: str = "") -> PRDescription:
    """Construct a ``PRDescription`` instance without running ``__init__``."""
    with patch.object(PRDescription, "__init__", lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.prediction = prediction_yaml
    obj.keys_fix = KEYS_FIX
    obj.user_description = ""
    obj.vars = {}
    obj.data = {}
    obj.pr_id = "1"
    obj.file_label_dict = {}
    obj.COLLAPSIBLE_FILE_LIST_THRESHOLD = 8
    return obj


def _settings(
    *,
    add_original_user_description: bool = False,
    publish_labels: bool = False,
    enable_pr_type: bool = True,
    generate_ai_title: bool = True,
    include_generated_by_header: bool = False,
    enable_semantic_files_types: bool = True,
    collapsible_file_list: str = "adaptive",
    file_table_collapsible_open_by_default: bool = False,
) -> MagicMock:
    """Build a settings mock with all PR-description knobs the SUT reads."""
    settings = MagicMock()
    pd = settings.pr_description
    pd.add_original_user_description = add_original_user_description
    pd.publish_labels = publish_labels
    pd.enable_pr_type = enable_pr_type
    pd.generate_ai_title = generate_ai_title
    pd.include_generated_by_header = include_generated_by_header
    pd.enable_semantic_files_types = enable_semantic_files_types
    pd.collapsible_file_list = collapsible_file_list
    pd.get.side_effect = lambda key, default=None: {
        "file_table_collapsible_open_by_default": file_table_collapsible_open_by_default,
    }.get(key, default)
    return settings


# ---------------------------------------------------------------------------
# _prepare_data
# ---------------------------------------------------------------------------
class TestPrepareData:
    @patch("pr_agent.tools.pr_description.get_settings")
    def test_keys_are_reordered_in_canonical_sequence(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = _make_instance(yaml.dump({
            "pr_files": [],
            "description": "desc",
            "labels": ["bug"],
            "type": "Bug fix",
            "title": "AI title",
        }))

        obj._prepare_data()

        # Order matters: title, type, labels, description, pr_files
        assert list(obj.data.keys()) == ["title", "type", "labels", "description", "pr_files"]

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_empty_diagram_key_is_dropped(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = _make_instance(yaml.dump({
            "title": "t",
            "description": "d",
            "changes_diagram": "graph LR\nA --> B",  # no mermaid fence -> sanitized to ''
        }))

        obj._prepare_data()

        assert "changes_diagram" not in obj.data

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_user_description_is_injected_when_enabled(self, mock_get_settings):
        mock_get_settings.return_value = _settings(add_original_user_description=True)
        obj = _make_instance(yaml.dump({"title": "t", "description": "d"}))
        obj.user_description = "Original body from user"

        obj._prepare_data()

        assert obj.data["User Description"] == "Original body from user"


# ---------------------------------------------------------------------------
# _prepare_labels
# ---------------------------------------------------------------------------
class TestPrepareLabels:
    @patch("pr_agent.tools.pr_description.get_settings")
    def test_labels_list_is_returned_stripped(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = _make_instance()
        obj.data = {"labels": ["  bug ", "perf"]}
        obj.variables = {}

        assert obj._prepare_labels() == ["bug", "perf"]

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_labels_comma_string_is_split(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = _make_instance()
        obj.data = {"labels": "bug, perf , docs"}
        obj.variables = {}

        assert obj._prepare_labels() == ["bug", "perf", "docs"]

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_falls_back_to_type_only_when_publish_labels_enabled(self, mock_get_settings):
        mock_get_settings.return_value = _settings(publish_labels=True)
        obj = _make_instance()
        obj.data = {"type": "Bug fix, Refactor"}
        obj.variables = {}

        assert obj._prepare_labels() == ["Bug fix", "Refactor"]

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_does_not_fall_back_to_type_when_publish_labels_disabled(self, mock_get_settings):
        mock_get_settings.return_value = _settings(publish_labels=False)
        obj = _make_instance()
        obj.data = {"type": "Bug fix"}
        obj.variables = {}

        assert obj._prepare_labels() == []

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_labels_minimal_dict_remaps_case(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = _make_instance()
        obj.data = {"labels": ["bug fix", "perf"]}
        obj.variables = {"labels_minimal_to_labels_dict": {"bug fix": "Bug Fix"}}

        assert obj._prepare_labels() == ["Bug Fix", "perf"]


# ---------------------------------------------------------------------------
# _prepare_pr_answer_with_markers
# ---------------------------------------------------------------------------
class TestPrepareAnswerWithMarkers:
    def _obj_with_user_description(self, user_description: str, data: dict) -> PRDescription:
        obj = _make_instance()
        obj.vars = {"title": "Original title"}
        obj.user_description = user_description
        obj.data = data
        obj.git_provider = MagicMock()
        obj.git_provider.last_commit_id.sha = "deadbeef"
        return obj

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_html_comment_guard_prevents_type_replacement(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        body_in = "<!-- pr_agent:type -->\npr_agent:type stays raw"
        obj = self._obj_with_user_description(body_in, {"title": "AI", "type": "Bug fix"})

        _, body, _, _ = obj._prepare_pr_answer_with_markers()

        # Guard present -> the plain marker is NOT replaced.
        assert "pr_agent:type stays raw" in body
        assert "Bug fix" not in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_plain_summary_marker_is_replaced(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = self._obj_with_user_description(
            "Intro\npr_agent:summary\nOutro",
            {"title": "AI", "description": "Adds caching layer."},
        )

        _, body, _, _ = obj._prepare_pr_answer_with_markers()

        assert "Adds caching layer." in body
        assert "pr_agent:summary" not in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_generated_by_header_prefixes_replacements(self, mock_get_settings):
        mock_get_settings.return_value = _settings(include_generated_by_header=True)
        obj = self._obj_with_user_description(
            "pr_agent:type\npr_agent:summary",
            {"title": "AI", "type": "Bug fix", "description": "Fix bug."},
        )

        _, body, _, _ = obj._prepare_pr_answer_with_markers()

        assert "### 🤖 Generated by PR Agent at deadbeef" in body
        # Header appears for both replaced markers.
        assert body.count("### 🤖 Generated by PR Agent at deadbeef") == 2

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_list_type_is_joined_with_comma(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = self._obj_with_user_description(
            "pr_agent:type",
            {"title": "AI", "type": ["Bug fix", "Refactor"]},
        )

        _, body, _, _ = obj._prepare_pr_answer_with_markers()

        assert "Bug fix, Refactor" in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_diagram_marker_replaces_both_plain_and_html_comment(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        diagram = "\n```mermaid\ngraph LR\nA --> B\n```"
        obj = self._obj_with_user_description(
            "First: pr_agent:diagram\nSecond: <!-- pr_agent:diagram -->",
            {"title": "AI", "changes_diagram": diagram},
        )

        _, body, _, _ = obj._prepare_pr_answer_with_markers()

        # Both forms are substituted with the diagram.
        assert body.count("```mermaid") == 2
        assert "<!-- pr_agent:diagram -->" not in body
        assert "pr_agent:diagram" not in body.replace("```mermaid", "")

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_title_falls_back_when_generate_ai_title_disabled(self, mock_get_settings):
        mock_get_settings.return_value = _settings(generate_ai_title=False)
        obj = self._obj_with_user_description(
            "pr_agent:summary",
            {"title": "AI Title", "description": "x"},
        )

        title, _, _, _ = obj._prepare_pr_answer_with_markers()

        assert title == "Original title"


# ---------------------------------------------------------------------------
# _prepare_pr_answer (non-marker rendering path)
# ---------------------------------------------------------------------------
class TestPrepareAnswer:
    def _obj(self, data: dict, *, gfm: bool = True) -> PRDescription:
        obj = _make_instance()
        obj.vars = {"title": "Original title"}
        obj.data = data
        obj.file_label_dict = {}
        obj.git_provider = MagicMock()
        obj.git_provider.is_supported.side_effect = lambda cap: {
            "gfm_markdown": gfm,
            "get_labels": False,
        }.get(cap, False)
        obj.git_provider.get_diff_files.return_value = []
        obj.git_provider.get_line_link.return_value = ""
        return obj

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_labels_removed_when_provider_supports_get_labels(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = self._obj({"title": "t", "labels": ["bug"], "description": "d"})
        obj.git_provider.is_supported.side_effect = lambda cap: cap in {"gfm_markdown", "get_labels"}

        _, body, _, _ = obj._prepare_pr_answer()

        # The Labels section is suppressed for providers with native label support.
        assert "Labels" not in body
        assert "bug" not in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_type_section_removed_when_disabled(self, mock_get_settings):
        mock_get_settings.return_value = _settings(enable_pr_type=False)
        obj = self._obj({"title": "t", "type": "Bug fix", "description": "d"})

        _, body, _, _ = obj._prepare_pr_answer()

        assert "PR Type" not in body
        assert "Bug fix" not in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_description_list_value_is_joined_and_bullets_spaced(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = self._obj({
            "title": "t",
            "description": "Intro\n- one\n- two",
        })

        _, body, _, _ = obj._prepare_pr_answer()

        # Bullet readability: single newline before "-" becomes double newline.
        assert "Intro\n\n- one\n\n- two" in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_diagram_section_uses_header_enum(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        diagram = "\n```mermaid\ngraph LR\nA --> B\n```"
        obj = self._obj({"title": "t", "description": "d", "changes_diagram": diagram})

        _, body, _, _ = obj._prepare_pr_answer()

        assert f"### {PRDescriptionHeader.DIAGRAM_WALKTHROUGH.value}" in body
        assert "```mermaid" in body

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_title_uses_vars_title_when_data_has_no_title(self, mock_get_settings):
        mock_get_settings.return_value = _settings(generate_ai_title=False)
        obj = self._obj({"description": "d"})

        title, _, _, _ = obj._prepare_pr_answer()

        assert title == "Original title"


# ---------------------------------------------------------------------------
# process_pr_files_prediction (gfm vs non-gfm)
# ---------------------------------------------------------------------------
class TestProcessPRFilesPrediction:
    def _obj(self, *, gfm: bool, diff_files=None) -> PRDescription:
        obj = _make_instance()
        obj.git_provider = MagicMock()
        obj.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown" and gfm
        obj.git_provider.get_diff_files.return_value = diff_files or []
        obj.git_provider.get_line_link.return_value = "https://example/blob/main/src/app.py#L1"
        return obj

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_non_gfm_provider_skips_table_rendering(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        obj = self._obj(gfm=False)
        value = {"backend": [("src/app.py", "Add cache", "Adds a bounded cache.")]}

        body, comments = obj.process_pr_files_prediction("PRE", value)

        assert body == "PRE"
        assert comments == []

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_gfm_provider_emits_table_with_file_row(self, mock_get_settings):
        mock_get_settings.return_value = _settings()
        diff = FilePatchInfo("", "", "", "src/app.py")
        diff.num_plus_lines = 5
        diff.num_minus_lines = 2
        obj = self._obj(gfm=True, diff_files=[diff])
        value = {"backend": [("src/app.py", "Add cache", "Adds a bounded cache.")]}

        body, comments = obj.process_pr_files_prediction("", value)

        assert body.startswith("<table>")
        assert body.rstrip().endswith("</table>")
        assert "<strong>Backend</strong>" in body
        assert "<strong>app.py</strong>" in body
        assert "+5/-2" in body
        assert comments == []

    @patch("pr_agent.tools.pr_description.get_settings")
    def test_adaptive_collapsible_triggers_above_threshold(self, mock_get_settings):
        mock_get_settings.return_value = _settings(collapsible_file_list="adaptive")
        obj = self._obj(gfm=True)
        obj.COLLAPSIBLE_FILE_LIST_THRESHOLD = 1  # force collapsible behavior with 2 files
        value = {
            "backend": [
                ("a.py", "t1", "s1"),
                ("b.py", "t2", "s2"),
            ]
        }

        body, _ = obj.process_pr_files_prediction("", value)

        assert "<details><summary>2 files</summary>" in body


# ---------------------------------------------------------------------------
# Round-trip: process_description recovers structured files from rendering
# ---------------------------------------------------------------------------
class TestRoundTripWithProcessDescription:
    @patch("pr_agent.tools.pr_description.get_settings")
    def test_walkthrough_table_round_trips_through_process_description(self, mock_get_settings):
        mock_get_settings.return_value = _settings(collapsible_file_list=False)
        obj = _make_instance()
        diff = FilePatchInfo("", "", "", "src/app.py")
        diff.num_plus_lines = 3
        diff.num_minus_lines = 1
        obj.git_provider = MagicMock()
        obj.git_provider.is_supported.side_effect = lambda cap: cap == "gfm_markdown"
        obj.git_provider.get_diff_files.return_value = [diff]
        obj.git_provider.get_line_link.return_value = "https://example/blob/main/src/app.py#L1"

        value = {"backend": [("src/app.py", "Add cache", "Adds a bounded cache.")]}
        table, _ = obj.process_pr_files_prediction("", value)

        full_description = (
            "Some intro text.\n\n___\n\n"
            f"<details> <summary><h3> {PRDescriptionHeader.FILE_WALKTHROUGH.value}</h3></summary>\n\n"
            f"{table}\n\n</details>\n\n___\n\nFooter"
        )

        base, files = process_description(full_description)

        assert base.startswith("Some intro text.")
        # At least one structured file entry was recovered.
        assert files, "expected process_description to recover at least one file entry"
        recovered = files[0]
        assert recovered["short_file_name"] == "app.py"
        assert recovered["full_file_name"] == "src/app.py"
        assert "Add cache" in recovered["short_summary"]

    def test_process_description_returns_empty_on_empty_input(self):
        assert process_description("") == ("", [])

    def test_process_description_without_walkthrough_returns_full_text(self):
        text = "Just a description without any walkthrough section."
        base, files = process_description(text)
        assert base == text
        assert files == []


# ---------------------------------------------------------------------------
# _prepare_file_labels edge cases not covered elsewhere
# ---------------------------------------------------------------------------
class TestPrepareFileLabelsEdgeCases:
    def test_returns_empty_when_data_missing_pr_files(self):
        obj = _make_instance()
        obj.data = {"title": "t"}
        assert obj._prepare_file_labels() == {}

    def test_returns_empty_when_data_is_not_a_dict(self):
        obj = _make_instance()
        obj.data = None
        assert obj._prepare_file_labels() == {}

    def test_filename_quotes_are_normalized(self):
        obj = _make_instance()
        obj.vars = {"include_file_summary_changes": True}
        obj.data = {
            "pr_files": [
                {
                    "filename": "src/it's a \"file\".py",
                    "changes_title": "T",
                    "changes_summary": "S",
                    "label": "Backend",
                },
            ]
        }

        labels = obj._prepare_file_labels()

        # Single and double quotes in filenames are replaced with backticks;
        # labels are lower-cased for grouping.
        assert list(labels.keys()) == ["backend"]
        recovered_name = labels["backend"][0][0]
        assert "'" not in recovered_name
        assert '"' not in recovered_name


# Ensure SimpleNamespace import is used (kept for potential future fixtures);
# referenced here to avoid an unused-import warning without changing semantics.
_ = SimpleNamespace


class TestSafePublishLabels:
    """``PRDescription._safe_publish_labels`` must isolate label failures.

    /describe wraps the entire publish flow in a single try/except; if the
    label step raises, the description publish is aborted. The helper exists
    so a transient refresh failure during ``publish_managed_labels``
    degrades to a logged warning instead of skipping the description.
    """

    def _obj_with_provider(self, provider):
        obj = _make_instance()
        obj.git_provider = provider
        return obj

    def test_swallows_expected_provider_errors(self):
        provider = MagicMock()
        # Each of these is a failure mode we expect from the provider/network
        # path and explicitly catch in _safe_publish_labels.
        for err in (
            ConnectionError("network down"),
            TimeoutError("read timeout"),
            OSError("socket closed"),
            ValueError("bad payload"),
            KeyError("missing key on MR"),
            AttributeError("stale attr"),
        ):
            provider.reset_mock()
            provider.publish_managed_labels.side_effect = err

            obj = self._obj_with_provider(provider)
            # Must not raise.
            obj._safe_publish_labels(["Bug fix"])

            provider.publish_managed_labels.assert_called_once()

    def test_does_not_swallow_unexpected_errors(self):
        # Programmer errors (e.g. a typo causing TypeError) must surface,
        # not be silently logged. _safe_publish_labels catches only the
        # narrow set above; anything else propagates so /describe's outer
        # handler logs it with a traceback.
        provider = MagicMock()
        provider.publish_managed_labels.side_effect = TypeError("unexpected kwarg")

        obj = self._obj_with_provider(provider)

        try:
            obj._safe_publish_labels(["Bug fix"])
        except TypeError:
            pass
        else:
            raise AssertionError("TypeError should not be swallowed by _safe_publish_labels")

    def test_passes_managed_labels_and_predicate(self):
        provider = MagicMock()
        provider.publish_managed_labels.return_value = ["Bug fix", "area/backend"]

        obj = self._obj_with_provider(provider)
        obj._safe_publish_labels(["Bug fix"])

        provider.publish_managed_labels.assert_called_once()
        managed_arg, predicate_arg = provider.publish_managed_labels.call_args[0]
        assert managed_arg == ["Bug fix"]
        # Predicate is the shared ``is_describe_managed_label`` so the
        # classification stays in one place alongside ``get_user_labels``.
        # The base set is always managed; user labels stay user.
        assert predicate_arg("Bug fix") is True
        assert predicate_arg("BUG FIX") is True
        assert predicate_arg("tests") is True
        assert predicate_arg("Enhancement") is True
        assert predicate_arg("Documentation") is True
        assert predicate_arg("Other") is True
        assert predicate_arg("area/backend") is False
        assert predicate_arg(None) is False

    def test_predicate_includes_bug_fix_with_tests_when_custom_labels_default(self):
        # Reproduces Qodo #3: when enable_custom_labels=True and no
        # custom_labels are configured, set_custom_labels injects defaults
        # that include "Bug fix with tests". The managed predicate must
        # classify that label as managed so it can be removed later;
        # otherwise it gets stuck on the MR forever.
        from pr_agent.config_loader import get_settings
        settings = get_settings()
        original_enable = settings.config.get("enable_custom_labels", False)
        original_custom = settings.get("custom_labels", None)
        try:
            settings.config.enable_custom_labels = True
            # Clear any pre-existing custom_labels; we want the
            # default-injection branch of set_custom_labels.
            settings.set("custom_labels", [])

            provider = MagicMock()
            provider.publish_managed_labels.return_value = ["Bug fix"]
            obj = self._obj_with_provider(provider)
            obj._safe_publish_labels(["Bug fix"])

            _, predicate_arg = provider.publish_managed_labels.call_args[0]
            assert predicate_arg("Bug fix with tests") is True
            assert predicate_arg("bug fix with tests") is True
            # Base set still managed.
            assert predicate_arg("Tests") is True
            # User labels still user.
            assert predicate_arg("area/backend") is False
        finally:
            settings.config.enable_custom_labels = original_enable
            if original_custom is not None:
                settings.set("custom_labels", original_custom)

    def test_logs_distinctly_on_refresh_failure(self):
        # Qodo #4: a refresh failure must not be misreported as
        # "labels unchanged". The provider signals refresh failure via
        # LabelRefreshError; _safe_publish_labels catches it specifically
        # and logs a warning rather than treating None as success.
        from pr_agent.git_providers.git_provider import LabelRefreshError

        provider = MagicMock()
        provider.publish_managed_labels.side_effect = LabelRefreshError(
            "refresh failed: gitlab 5xx"
        )

        obj = self._obj_with_provider(provider)
        # Must not raise (failure is isolated to the label step).
        obj._safe_publish_labels(["Bug fix"])

        provider.publish_managed_labels.assert_called_once()

    def test_skips_log_when_no_change(self):
        # publish_managed_labels returns None when nothing changed; the helper
        # must treat that as a no-op rather than an error.
        provider = MagicMock()
        provider.publish_managed_labels.return_value = None

        obj = self._obj_with_provider(provider)
        obj._safe_publish_labels(["Bug fix"])

        provider.publish_managed_labels.assert_called_once()


class TestDescribeManagedLabelNamespace:
    """Lock the ``/describe``-managed label classification in one place.

    ``is_describe_managed_label`` and ``get_user_labels`` must agree: a label
    classified as managed is **never** a user label, and vice versa. The
    classification has to match ``set_custom_labels``' default-injection
    behavior so labels like ``Bug fix with tests`` (Qodo #3) can be removed
    by ``/describe`` once they are no longer recommended.
    """

    def _with_settings(self, enable_custom_labels, custom_labels):
        from pr_agent.config_loader import get_settings
        settings = get_settings()
        snap = (
            settings.config.get("enable_custom_labels", False),
            settings.get("custom_labels", None),
        )
        settings.config.enable_custom_labels = enable_custom_labels
        if custom_labels is not None:
            settings.set("custom_labels", custom_labels)
        return settings, snap

    def _restore(self, settings, snap):
        original_enable, original_custom = snap
        settings.config.enable_custom_labels = original_enable
        if original_custom is not None:
            settings.set("custom_labels", original_custom)

    def test_default_set_classified_as_managed(self):
        from pr_agent.algo.utils import is_describe_managed_label
        settings, snap = self._with_settings(False, None)
        try:
            for label in ("Bug fix", "Tests", "Enhancement", "Documentation", "Other",
                          "bug fix", "TESTS", "enhancement"):
                assert is_describe_managed_label(label) is True
            assert is_describe_managed_label("area/backend") is False
            assert is_describe_managed_label("") is False
            assert is_describe_managed_label(None) is False
        finally:
            self._restore(settings, snap)

    def test_custom_labels_default_includes_bug_fix_with_tests(self):
        # Reproduces Qodo #3: when enable_custom_labels=True and
        # custom_labels is empty, set_custom_labels injects defaults that
        # include "Bug fix with tests". That label must be classified as
        # managed so /describe can remove it once it is no longer
        # recommended.
        from pr_agent.algo.utils import is_describe_managed_label
        settings, snap = self._with_settings(True, [])
        try:
            assert is_describe_managed_label("Bug fix with tests") is True
            assert is_describe_managed_label("bug fix with tests") is True
            # Base set still managed.
            assert is_describe_managed_label("Tests") is True
            # User labels still user.
            assert is_describe_managed_label("area/backend") is False
        finally:
            self._restore(settings, snap)

    def test_get_user_labels_and_is_managed_are_disjoint(self):
        # Any label classified as managed must be filtered out of
        # get_user_labels' output, and vice versa. This is what keeps the
        # /describe-managed namespace in one place.
        from pr_agent.algo.utils import get_user_labels, is_describe_managed_label
        settings, snap = self._with_settings(True, [])
        try:
            current = [
                "Bug fix", "Bug fix with tests", "Tests",
                "area/backend", "team/security",
            ]
            user = get_user_labels(current)
            for label in current:
                if is_describe_managed_label(label):
                    assert label not in user, f"{label} leaked into user labels"
                else:
                    assert label in user, f"{label} dropped from user labels"
        finally:
            self._restore(settings, snap)
