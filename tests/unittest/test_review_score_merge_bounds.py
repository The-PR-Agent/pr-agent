import pytest

from pr_agent.algo.review_merge import merge_review_chunks


@pytest.mark.parametrize("invalid", [-10, "-1", 101, "999"])
def test_invalid_chunk_score_cannot_replace_valid_scores(invalid):
    result = merge_review_chunks([
        {"review": {"score": invalid}},
        {"review": {"score": 60}},
        {"review": {"score": 80}},
    ])
    assert result["review"]["score"] == 60


def test_valid_zero_score_remains_the_most_conservative_result():
    result = merge_review_chunks([{"review": {"score": 60}}, {"review": {"score": 0}}])
    assert result["review"]["score"] == 0
