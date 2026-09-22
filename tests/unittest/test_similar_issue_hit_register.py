"""The similar-issue hit register keeps the three result lists aligned per issue."""
from pr_agent.tools.pr_similar_issue import PRSimilarIssue


def _drive(records):
    issues, comments, scores = [], [], []
    for issue_number, comment_number, score in records:
        PRSimilarIssue._record_similar_hit(
            issues, comments, scores, issue_number, comment_number, score
        )
    return issues, comments, scores


def test_first_and_best_scored_hit_per_issue_wins():
    issues, comments, scores = _drive([(7, -1, 0.79), (7, 0, 0.72), (8, -1, 0.66)])

    assert issues == [7, 8]
    assert comments == [-1, -1]
    assert scores == ["0.79", "0.66"]


def test_comment_hit_before_body_hit_is_kept():
    issues, comments, scores = _drive([(7, 0, 0.90), (7, -1, 0.80)])

    assert issues == [7]
    assert comments == [0]
    assert scores == ["0.90"]


def test_lists_stay_aligned_across_interleaved_issues():
    issues, comments, scores = _drive(
        [(7, -1, 0.5), (8, 2, 0.4), (7, 1, 0.3), (9, -1, 0.2), (8, -1, 0.1)]
    )

    assert issues == [7, 8, 9]
    assert comments == [-1, 2, -1]
    assert scores == ["0.50", "0.40", "0.20"]
    assert len(issues) == len(comments) == len(scores)
