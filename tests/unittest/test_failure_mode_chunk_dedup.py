from pr_agent.algo.output_models import parse_failure_modes
from pr_agent.algo.review_merge import merge_review_chunks


def mode(name):
    return {
        "what": name, "where": "app.py", "trigger": "request",
        "detected_by": "test", "covered_in_this_pr": True,
    }


def test_repeated_chunk_failure_modes_do_not_crowd_out_distinct_cases():
    first, second, third = mode("first"), mode("second"), mode("third")
    result = merge_review_chunks([
        {"review": {"failure_modes": [first, second]}},
        {"review": {"failure_modes": [first, third]}},
    ])
    assert result["review"]["failure_modes"] == [first, second, third]


def test_single_chunk_failure_modes_are_unique_before_the_cap():
    first, second = mode("first"), mode("second")
    assert parse_failure_modes([first, first, first, second]) == [first, second]
