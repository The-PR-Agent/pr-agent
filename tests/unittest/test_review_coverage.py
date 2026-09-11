from pr_agent.algo.review_coverage import (
    CoverageLedger,
    FileCoverage,
    changed_lines_from_patch,
    patch_line_counts,
)


def _ledger():
    ledger = CoverageLedger()
    ledger.add(FileCoverage("a.py", changed_lines=100, status="reviewed"))
    ledger.add(FileCoverage("b.py", changed_lines=100, status="clipped"))
    ledger.add(FileCoverage("c.py", changed_lines=200, status="chunk_failed"))
    ledger.add(FileCoverage("d.py", changed_lines=0, status="deletion_only"))
    return ledger


def test_clipped_counts_half_and_failed_counts_zero():
    ledger = _ledger()
    # reviewed 100 + clipped 50 (half credit) + failed 0 over 400 changed lines
    assert ledger.reviewed_ratio == 0.375


def test_not_fully_reviewed_lists_partial_and_failed_only():
    assert _ledger().not_fully_reviewed() == ["b.py", "c.py"]


def test_footer_names_percentage_and_count():
    footer = _ledger().render_footer()
    assert "Reviewed 38% of changed lines" in footer
    assert "2 file(s) not fully reviewed" in footer


def test_mark_overrides_status():
    ledger = _ledger()
    ledger.mark("a.py", "chunk_failed")
    assert ledger.files["a.py"].status == "chunk_failed"


def test_changed_lines_from_patch_counts_plus_and_minus_excluding_headers():
    patch = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " unchanged\n"
        "-old line one\n"
        "-old line two\n"
        "+new line one\n"
        "\\ No newline at end of file\n"
    )
    assert changed_lines_from_patch(patch) == 3


def test_changed_lines_from_patch_handles_empty_patch():
    assert changed_lines_from_patch("") == 0
    assert changed_lines_from_patch(None) == 0


def test_changed_lines_from_patch_does_not_mistake_sql_comment_lines_for_headers():
    """A removed line whose text begins with `--` (an SQL comment) renders as a diff line
    starting with `--- `, and an added counterpart can start with `+++`; both must still count
    as real changes. Header exclusion must be positional (before the first `@@`), not by
    matching the `+++`/`---` prefix, or these get silently dropped."""
    patch = "--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,2 @@\n--- old comment\n+++counter\n"
    assert changed_lines_from_patch(patch) == 2


def test_changed_lines_from_patch_ignores_no_newline_marker():
    patch = "@@ -1,1 +1,1 @@\n-old\n+new\n\\ No newline at end of file\n"
    assert changed_lines_from_patch(patch) == 2


def test_patch_line_counts_returns_plus_and_minus_separately():
    patch = "--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,2 @@\n--- old comment\n+++counter\n"
    assert patch_line_counts(patch) == (1, 1)


def test_patch_line_counts_empty_patch_is_zero_zero():
    assert patch_line_counts("") == (0, 0)
    assert patch_line_counts(None) == (0, 0)
