from pr_agent.algo.review_coverage import CoverageLedger, FileCoverage


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
