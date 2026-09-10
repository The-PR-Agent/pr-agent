from pathlib import Path

from pr_agent.algo.utils import (
    _ALL_COMMENT_IDENTITIES,
    PRCodeSuggestionsIdentity,
    PRReviewIdentity,
)
from pr_dashboard import comments

FIXTURES = Path("tests/unittest/fixtures/pr_dashboard")


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
