import json
from pathlib import Path
from types import SimpleNamespace

from pr_agent.algo.utils import (
    _ALL_COMMENT_IDENTITIES,
    PRCodeSuggestionsIdentity,
    PRReviewIdentity,
    add_pr_review_identity,
    convert_to_markdown_v2,
)
from pr_agent.config_loader import get_settings
from pr_dashboard import comments

FIXTURES = Path("tests/unittest/fixtures/pr_dashboard")


def _render_review(review: dict, *, gfm_supported: bool, layout: str) -> str:
    settings = get_settings()
    key = "pr_reviewer.findings_layout"
    was_present = key in settings
    previous = settings.get(key, "details") if was_present else None
    try:
        settings.set(key, layout)
        rendered = convert_to_markdown_v2({"review": review}, gfm_supported=gfm_supported)
    finally:
        if was_present:
            settings.set(key, previous)
        else:
            settings.unset(key)
    return add_pr_review_identity(rendered, PRReviewIdentity.REGULAR.value)


def _provider_from_links(line_links: dict | None):
    if not line_links:
        return None

    class _Provider:
        def get_line_link(self, path, start, end):
            # Fixtures record the exact link the mock provider returned for that file's
            # start/end at generation time; ignore the call's start/end and return it.
            return line_links[path]

    return _Provider()


def _render_from_inputs(payload: dict) -> str:
    settings = get_settings()
    layout_key = "pr_reviewer.findings_layout"
    intro_key = "pr_reviewer.enable_intro_text"
    layout_present = layout_key in settings
    intro_present = intro_key in settings
    previous_layout = settings.get(layout_key, "details") if layout_present else None
    previous_intro = settings.get(intro_key, False) if intro_present else None
    files = None
    if payload.get("files"):
        files = [
            SimpleNamespace(filename=item["filename"], head_file=item["head_file"],
                            patch="", language=item["language"])
            for item in payload["files"]
        ]
    try:
        settings.set(layout_key, payload["findings_layout"])
        settings.set(intro_key, payload["enable_intro_text"])
        rendered = convert_to_markdown_v2(
            {"review": payload["review"]},
            gfm_supported=payload["gfm_supported"],
            git_provider=_provider_from_links(payload.get("line_links")),
            files=files,
        )
    finally:
        if layout_present:
            settings.set(layout_key, previous_layout)
        else:
            settings.unset(layout_key)
        if intro_present:
            settings.set(intro_key, previous_intro)
        else:
            settings.unset(intro_key)
    return add_pr_review_identity(rendered, payload["identity"])



class TestUpstreamContract:
    def test_identity_markers_still_exist(self):
        """Fail loudly if upstream renames the private identity tuple the dashboard imports"""
        assert PRReviewIdentity.REGULAR.value == "<!-- pr-agent:review:full -->"
        assert PRReviewIdentity.INCREMENTAL.value == "<!-- pr-agent:review:incremental -->"
        assert PRCodeSuggestionsIdentity.SUMMARY.value == "<!-- pr-agent:improve:summary -->"
        assert set(comments.IDENTITY_MARKERS) == set(_ALL_COMMENT_IDENTITIES)


class TestClassify:
    def test_full_review(self):
        """A full review is recognised from its identity marker"""
        body = f"{PRReviewIdentity.REGULAR.value}\n## Anything At All\n"
        assert comments.classify(body) is comments.CommentKind.REVIEW

    def test_incremental_review(self):
        """An incremental review is distinguished from a full one"""
        body = f"{PRReviewIdentity.INCREMENTAL.value}\n## x\n"
        assert comments.classify(body) is comments.CommentKind.INCREMENTAL_REVIEW

    def test_suggestions(self):
        """An improve summary is recognised"""
        body = f"{PRCodeSuggestionsIdentity.SUMMARY.value}\n## x\n"
        assert comments.classify(body) is comments.CommentKind.SUGGESTIONS

    def test_renamed_heading_is_still_recognised(self):
        """A repo that overrode pr_reviewer.review_heading is still matched"""
        body = f"{PRReviewIdentity.REGULAR.value}\n## Our Custom Review Title\n"
        assert comments.classify(body) is comments.CommentKind.REVIEW

    def test_suggestions_summary_fixture_is_recognised(self):
        """The only /improve fixture in the suite is classified against a real body, not an inline string"""
        body = (FIXTURES / "suggestions_summary.md").read_text(encoding="utf-8")
        assert comments.classify(body) is comments.CommentKind.SUGGESTIONS

    def test_human_comment_is_not_ours(self):
        """An ordinary human comment is not attributed to PR-Agent"""
        assert comments.classify("looks good to me") is comments.CommentKind.OTHER
        assert comments.is_pr_agent_comment("looks good to me") is False

    def test_legacy_comment_without_marker_falls_back_to_heading(self):
        """Comments posted before identity markers existed are matched on the default heading"""
        assert comments.classify("## PR Reviewer Guide\n\nsome review") is comments.CommentKind.REVIEW


class TestParseFindings:
    def test_details_layout(self):
        """Findings are extracted from the collapsed details layout with their exact titles"""
        findings = comments.parse_findings((FIXTURES / "review_details.md").read_text(encoding="utf-8"))
        titles = [f.title for f in findings]
        assert titles == ["Race condition on shared queue state", "Missing null check before dereference"]

    def test_expanded_layout_exposes_file_and_lines(self):
        """The expanded layout yields the exact file path and line range as text, not only links"""
        findings = comments.parse_findings((FIXTURES / "review_expanded.md").read_text(encoding="utf-8"))
        by_title = {f.title: f for f in findings}
        finding = by_title["Race condition on shared queue state"]
        assert finding.relevant_file == "src/worker/queue.py"
        assert finding.line_range == (42, 58)

    def test_details_layout_non_gfm(self):
        """A non-gfm_supported provider's (e.g. Bitbucket) collapsed layout still yields exact titles"""
        body = (FIXTURES / "review_details_bitbucket.md").read_text(encoding="utf-8")
        titles = [f.title for f in comments.parse_findings(body)]
        assert titles == ["Race condition on shared queue state", "Missing null check before dereference"]

    def test_expanded_layout_non_gfm_exposes_file_and_lines(self):
        """A non-gfm_supported provider puts the location on the line after the title, not inline"""
        body = (FIXTURES / "review_expanded_bitbucket.md").read_text(encoding="utf-8")
        by_title = {f.title: f for f in comments.parse_findings(body)}
        finding = by_title["Missing null check before dereference"]
        assert finding.relevant_file == "src/auth/session.py"
        assert finding.line_range == (10, 12)

    def test_title_excludes_the_location_suffix(self):
        """The title is the bold run only; the file and line range never leak into it"""
        body = (
            f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n\n"
            "Recommended focus areas for review\n\n"
            "- **Race on profile write** `lib/profile.dart` [120-134]\n"
        )
        finding = comments.parse_findings(body)[0]
        assert finding.title == "Race on profile write"
        assert finding.relevant_file == "lib/profile.dart"
        assert finding.line_range == (120, 134)

    def test_backtick_in_title_does_not_steal_the_file_span(self):
        """A backtick-quoted dotted token inside the title itself is not read as the location"""
        body = (
            f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n\n"
            "Recommended focus areas for review\n\n"
            "- **Handle `config.yml` parsing failure** `src/config_loader.py` [5-9]\n"
        )
        finding = comments.parse_findings(body)[0]
        assert finding.title == "Handle `config.yml` parsing failure"
        assert finding.relevant_file == "src/config_loader.py"
        assert finding.line_range == (5, 9)

    def test_locationless_finding_does_not_absorb_the_next_finding_s_location(self):
        """A finding with no location must not steal one from a later finding's title text"""
        body = (
            f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n\n"
            "Recommended focus areas for review\n\n"
            "**Race condition on shared queue state**\n\n"
            "**Update `config.py` to version 2 handling**\n"
        )
        first, second = comments.parse_findings(body)
        assert first.title == "Race condition on shared queue state"
        assert first.relevant_file is None
        assert first.line_range is None
        assert second.title == "Update `config.py` to version 2 handling"

    def test_single_line_finding_has_equal_start_and_end(self):
        """render_focus_area_issue omits the dash for a single-line finding; line_range still resolves"""
        body = (
            f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide\n\n"
            "Recommended focus areas for review\n\n"
            "<strong>Off-by-one when slicing the buffer</strong><br><code>src/buffer.py</code> L77\n"
        )
        finding = comments.parse_findings(body)[0]
        assert finding.relevant_file == "src/buffer.py"
        assert finding.line_range == (77, 77)

    def test_no_findings_returns_empty(self):
        """A "no major issues" review yields no findings while classify still recognises the review"""
        body = (
            f"{PRReviewIdentity.REGULAR.value}\n## PR Reviewer Guide 🔍\n\n"
            "<table>\n<tr><td>⚡&nbsp;<strong>No major issues detected</strong></td></tr>\n</table>\n"
        )
        assert comments.parse_findings(body) == []
        assert comments.classify(body) is comments.CommentKind.REVIEW

    def test_model_injected_table_close_does_not_drop_later_findings(self):
        """A finding whose content contains </td></tr> must not truncate later findings"""
        body = _render_review(
            {
                "key_issues_to_review": [
                    {
                        "relevant_file": "a.py",
                        "issue_header": "Poisoned finding",
                        "issue_content": "Text with </td></tr> inside it.",
                        "start_line": 1,
                        "end_line": 2,
                    },
                    {
                        "relevant_file": "b.py",
                        "issue_header": "Later finding",
                        "issue_content": "Should still be visible.",
                        "start_line": 3,
                        "end_line": 4,
                    },
                ],
            },
            gfm_supported=True,
            layout="details",
        )
        titles = [f.title for f in comments.parse_findings(body)]
        assert titles == ["Poisoned finding", "Later finding"]

    def test_model_injected_row_open_does_not_drop_later_findings(self):
        """A finding body with literal </td></tr><tr><td> must not truncate later findings"""
        body = _render_review(
            {
                "key_issues_to_review": [
                    {
                        "relevant_file": "a.py",
                        "issue_header": "Poisoned finding",
                        "issue_content": "Text with </td></tr><tr><td> forged boundary.",
                        "start_line": 1,
                        "end_line": 2,
                    },
                    {
                        "relevant_file": "b.py",
                        "issue_header": "Later finding",
                        "issue_content": "Should still be visible.",
                        "start_line": 3,
                        "end_line": 4,
                    },
                ],
            },
            gfm_supported=True,
            layout="details",
        )
        assert "</td></tr><tr><td>" in body
        titles = [f.title for f in comments.parse_findings(body)]
        assert titles == ["Poisoned finding", "Later finding"]

    def test_focus_area_findings_are_kept_and_other_sections_are_not(self):
        """Against one renderer body, focus-area titles are returned and section noise is not"""
        body = _render_review(
            {
                "security_concerns": (
                    "**Sensitive information exposure:**\n user tokens are written to application "
                    "logs on startup."
                ),
                "todo_sections": (
                    "Add regression coverage for the new cache eviction path before merging."
                ),
                "key_issues_to_review": [
                    {
                        "relevant_file": "src/worker/queue.py",
                        "issue_header": "Race condition on shared queue state",
                        "issue_content": "Concurrent writers can corrupt the queue.",
                        "start_line": 42,
                        "end_line": 58,
                    },
                    {
                        "relevant_file": "src/auth/session.py",
                        "issue_header": "Missing null check before dereference",
                        "issue_content": "user may be None.",
                        "start_line": 10,
                        "end_line": 12,
                    },
                ],
            },
            gfm_supported=True,
            layout="details",
        )
        titles = [f.title for f in comments.parse_findings(body)]
        assert titles == [
            "Race condition on shared queue state",
            "Missing null check before dereference",
        ]
        assert "Sensitive information exposure" not in titles
        assert not any("regression coverage" in t.lower() for t in titles)

    def test_focus_area_findings_are_kept_and_other_sections_are_not_non_gfm(self):
        """Against one non-gfm renderer body, focus-area titles are returned and section noise is not"""
        body = _render_review(
            {
                "security_concerns": (
                    "**Sensitive information exposure:**\n user tokens are written to application "
                    "logs on startup."
                ),
                "todo_sections": (
                    "Add regression coverage for the new cache eviction path before merging."
                ),
                "key_issues_to_review": [
                    {
                        "relevant_file": "src/worker/queue.py",
                        "issue_header": "Race condition on shared queue state",
                        "issue_content": "Concurrent writers can corrupt the queue.",
                        "start_line": 42,
                        "end_line": 58,
                    },
                    {
                        "relevant_file": "src/auth/session.py",
                        "issue_header": "Missing null check before dereference",
                        "issue_content": "user may be None.",
                        "start_line": 10,
                        "end_line": 12,
                    },
                ],
            },
            gfm_supported=False,
            layout="details",
        )
        titles = [f.title for f in comments.parse_findings(body)]
        assert titles == [
            "Race condition on shared queue state",
            "Missing null check before dereference",
        ]
        assert "Sensitive information exposure" not in titles
        assert not any("regression coverage" in t.lower() for t in titles)


class TestMirroredSectionLabels:
    def test_mirrored_labels_match_convert_to_markdown_v2_emojis_keys(self):
        """_GFM_SECTION_LABELS must stay identical to convert_to_markdown_v2's emojis keys"""
        import ast
        import inspect
        import textwrap

        source = textwrap.dedent(inspect.getsource(convert_to_markdown_v2))
        tree = ast.parse(source)
        emojis_keys = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "emojis":
                    emojis_keys = set(ast.literal_eval(node.value).keys())
        assert emojis_keys is not None, "emojis dict not found in convert_to_markdown_v2"
        assert set(comments._GFM_SECTION_LABELS) == emojis_keys


class TestFixtureProvenance:
    def test_each_review_fixture_matches_convert_to_markdown_v2(self):
        """Every review fixture equals convert_to_markdown_v2 for its recorded inputs"""
        # suggestions_summary.md is the /improve fixture, not a review body from
        # convert_to_markdown_v2, so it is excluded from the sidecar requirement below.
        review_fixtures = sorted(
            path for path in FIXTURES.glob("*.md")
            if path.name != "suggestions_summary.md"
        )
        assert review_fixtures, "expected review fixture .md files"
        sidecar_names = {path.name.removesuffix(".inputs.json") for path in FIXTURES.glob("*.md.inputs.json")}
        fixture_names = {path.name for path in review_fixtures}
        assert sidecar_names == fixture_names, (
            f"review fixture/sidecar mismatch: missing sidecars={sorted(fixture_names - sidecar_names)} "
            f"extra sidecars={sorted(sidecar_names - fixture_names)}"
        )
        for fixture_name in sorted(fixture_names):
            inputs_path = FIXTURES / f"{fixture_name}.inputs.json"
            payload = json.loads(inputs_path.read_text(encoding="utf-8"))
            assert payload["renderer"] == "convert_to_markdown_v2"
            expected = (FIXTURES / fixture_name).read_text(encoding="utf-8")
            assert _render_from_inputs(payload) == expected, fixture_name
