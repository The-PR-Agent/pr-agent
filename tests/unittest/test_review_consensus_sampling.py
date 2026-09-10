"""Consensus over independent review samples (`pr_reviewer.num_samples`).

A small model at non-zero temperature reports a different subset of the real defects on every
run. Sampling N times and keeping what recurs turns that variance into recall without giving up
precision. Two things have to hold for the number to mean anything: findings are matched by
*where* they are, not how they are worded, and losing a sample lowers the vote bar instead of
silently emptying the review.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from pr_agent.algo.review_merge import consensus_votes_needed, vote_review_samples
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


def _issue(file="app.py", header="Possible Issue", content="unchecked index", start=10, end=12):
    return {"relevant_file": file, "issue_header": header, "issue_content": content,
            "start_line": start, "end_line": end}


def _sample(*issues, **fields):
    return {"review": {"key_issues_to_review": list(issues), **fields}}


def _voted(samples, min_votes=2, max_findings=0):
    """The voted review dict. `vote_review_samples` also returns what it dropped; see below."""
    return vote_review_samples(samples, min_votes, max_findings=max_findings).review


def _issues(merged):
    return merged["review"]["key_issues_to_review"]


def _files(merged):
    return [(i["relevant_file"], i["start_line"]) for i in _issues(merged)]


# --- voting rules --------------------------------------------------------------------------------

def test_a_finding_seen_in_enough_samples_is_kept_and_a_one_off_is_dropped():
    samples = [
        _sample(_issue(start=10), _issue(file="b.py", start=50)),
        _sample(_issue(start=11)),                 # same finding, placed one line off
        _sample(_issue(start=10)),
    ]
    merged = _voted(samples)
    assert _files(merged) == [("app.py", 10)]


def test_findings_are_matched_by_location_not_wording():
    samples = [
        _sample(_issue(header="Bug", content="index may be out of range")),
        _sample(_issue(header="Possible Issue", content="the loop reads one past the end")),
    ]
    merged = _voted(samples)
    assert len(_issues(merged)) == 1


def test_the_most_informative_wording_represents_the_cluster():
    samples = [_sample(_issue(content="short")), _sample(_issue(content="a much longer explanation"))]
    merged = _voted(samples)
    assert _issues(merged)[0]["issue_content"] == "a much longer explanation"


def test_same_lines_in_different_files_are_different_findings():
    samples = [_sample(_issue(file="a.py")), _sample(_issue(file="b.py"))]
    assert _issues(_voted(samples)) == []


def test_lines_outside_the_tolerance_are_different_findings():
    samples = [_sample(_issue(start=10, end=10)), _sample(_issue(start=20, end=20))]
    assert _issues(_voted(samples)) == []


def test_findings_without_line_numbers_fall_back_to_exact_wording():
    a = {"relevant_file": "app.py", "issue_header": "Bug", "issue_content": "same words"}
    b = dict(a)
    c = {**a, "issue_content": "different words"}
    merged = _voted([_sample(a), _sample(b), _sample(c)])
    assert [i["issue_content"] for i in _issues(merged)] == ["same words"]


def test_findings_are_ordered_by_agreement():
    samples = [
        _sample(_issue(file="weak.py", start=1), _issue(file="strong.py", start=1)),
        _sample(_issue(file="strong.py", start=1)),
        _sample(_issue(file="strong.py", start=1), _issue(file="weak.py", start=1)),
    ]
    merged = _voted(samples)
    assert [i["relevant_file"] for i in _issues(merged)] == ["strong.py", "weak.py"]


def test_the_vote_bar_never_exceeds_the_samples_that_parsed():
    """One usable sample must yield its findings, not an empty review."""
    merged = _voted([_sample(_issue())], min_votes=3)
    assert len(_issues(merged)) == 1
    merged = _voted([_sample(_issue()), _sample(_issue())], min_votes=5)
    assert len(_issues(merged)) == 1


def test_judgement_fields_take_the_samples_central_tendency_not_their_worst_case():
    """The samples all describe the same diff, so the outlier must not decide the verdict.

    Merging them by the chunk rules - min score, worst risk, union of concerns - reproduced the
    noise the vote exists to average away.
    """
    samples = [_sample(score="90", risk_level="low", security_concerns="No"),
               _sample(score="80", risk_level="low", security_concerns="No"),
               _sample(score="20", risk_level="high",
                       security_concerns="SQL injection: string-built query")]
    merged = _voted(samples)["review"]
    assert merged["score"] == "80"                    # median, not the outlier's 20
    assert merged["risk_level"] == "low"              # majority, not worst-of
    assert merged["security_concerns"] == "No"        # 1 of 3 samples is not a consensus


def test_a_sample_that_omitted_a_field_still_counts_against_the_majority():
    """A small model drops schema keys, and an omission is not agreement.

    The reducers used to see only the samples that answered, so a concern one of three samples
    reported was a majority of one and published - the outlier vote the sampling exists to filter.
    """
    samples = [_sample(security_concerns="SQL injection: string-built query"),
               _sample(), _sample()]
    merged = _voted(samples)["review"]
    assert merged["security_concerns"] == "No"

    samples = [_sample(relevant_tests="yes"), _sample(), _sample()]
    assert _voted(samples)["review"]["relevant_tests"] == "No"

    samples = [_sample(review_priority_files=["a.py"]), _sample(), _sample()]
    assert _voted(samples)["review"]["review_priority_files"] == []


def test_a_concern_most_samples_report_is_published():
    samples = [_sample(security_concerns="hardcoded token in config"),
               _sample(security_concerns="a token is hardcoded in the config file"),
               _sample(security_concerns="No")]
    assert "hardcoded" in _voted(samples)["review"]["security_concerns"]


def test_a_tie_between_two_verdicts_resolves_to_the_more_conservative_one():
    samples = [_sample(merge_recommendation="safe_to_merge"),
               _sample(merge_recommendation="changes_required")]
    assert _voted(samples)["review"]["merge_recommendation"] == "changes_required"


def test_contribution_time_is_the_median_of_the_samples_not_their_sum():
    """Summing scaled the published estimate with num_samples instead of the work."""
    estimate = {"best_case": "1h", "average_case": "2h", "worst_case": "3h"}
    samples = [_sample(contribution_time_cost_estimate=dict(estimate)) for _ in range(3)]
    assert _voted(samples)["review"]["contribution_time_cost_estimate"] == estimate


def test_effort_is_the_median_judgement():
    samples = [_sample(**{"estimated_effort_to_review_[1-5]": value}) for value in ("2", "2", "5")]
    assert _voted(samples)["review"]["estimated_effort_to_review_[1-5]"] == 2


def test_a_test_only_one_sample_saw_is_not_reported_as_a_test():
    samples = [_sample(relevant_tests="No"), _sample(relevant_tests="No"),
               _sample(relevant_tests="Yes")]
    assert _voted(samples)["review"]["relevant_tests"] == "No"


def test_a_priority_file_only_one_sample_named_is_dropped():
    samples = [_sample(review_priority_files=["a.py"]), _sample(review_priority_files=["a.py"]),
               _sample(review_priority_files=["a.py", "b.py"])]
    assert _voted(samples)["review"]["review_priority_files"] == ["a.py"]


def test_a_field_no_sample_answered_in_a_known_form_keeps_the_first_value():
    samples = [_sample(risk_level="unknown"), _sample(risk_level="unknown")]
    assert _voted(samples)["review"]["risk_level"] == "unknown"


# --- the vote bar and the cap --------------------------------------------------------------------

def test_min_votes_zero_means_a_majority_of_the_samples_that_parsed():
    assert consensus_votes_needed(0, 2) == 1
    assert consensus_votes_needed(0, 3) == 2
    assert consensus_votes_needed(0, 5) == 3
    assert consensus_votes_needed(0, 0) == 1


def test_an_explicit_min_votes_is_honoured_but_clamped_to_the_samples():
    assert consensus_votes_needed(3, 5) == 3
    assert consensus_votes_needed(9, 3) == 3
    assert consensus_votes_needed("bad", 4) == 2  # unparsable falls back to auto


def test_two_samples_under_auto_keep_a_finding_only_one_of_them_placed_differently():
    """Unanimity from two samples emptied the review whenever they disagreed by a few lines."""
    samples = [_sample(_issue(start=40, end=42)), _sample(_issue(start=45, end=46))]
    assert _issues(_voted(samples, min_votes=2)) == []      # explicit 2 still demands both
    assert len(_issues(_voted(samples, min_votes=0))) == 2   # auto keeps them for the reader


def test_the_kept_findings_are_capped_at_num_max_findings():
    """A sampled review must not exceed the ceiling a single call respects."""
    samples = [_sample(*[_issue(file=f"f{i}.py", start=10) for i in range(5)]) for _ in range(2)]
    result = vote_review_samples(samples, 1, max_findings=3)
    assert len(result.review["review"]["key_issues_to_review"]) == 3
    assert result.dropped == 2


def test_the_result_reports_what_the_vote_discarded():
    samples = [_sample(_issue(start=10), _issue(file="only-here.py", start=1)),
               _sample(_issue(start=10))]
    result = vote_review_samples(samples, 2)
    assert result.dropped == 1
    assert result.candidates == 2
    assert result.needed == 2
    assert result.samples == 2


# --- clustering findings that carry no line numbers ----------------------------------------------

def _wordy(content, file="app.py", header="Missing coverage"):
    return {"relevant_file": file, "issue_header": header, "issue_content": content}


def test_one_defect_worded_differently_clusters_when_neither_sample_gave_lines():
    """File-level findings carry no start_line, and demanding identical text dropped them.

    Small models both omit line numbers most often and are the reason this vote exists.
    """
    samples = [
        _sample(_wordy("the new parser module ships without any unit tests covering it")),
        _sample(_wordy("no unit tests were added covering the new parser module")),
    ]
    assert len(_issues(_voted(samples, min_votes=2))) == 1


def test_two_lineless_findings_that_share_only_boilerplate_stay_separate():
    samples = [
        _sample(_wordy("the retry loop never handles a timeout from the upstream provider")),
        _sample(_wordy("the config parser never handles a missing section header")),
    ]
    assert _issues(_voted(samples, min_votes=2)) == []


def test_a_lineless_finding_too_short_to_compare_falls_back_to_exact_wording():
    samples = [_sample(_wordy("bad code")), _sample(_wordy("bad code")), _sample(_wordy("odd code"))]
    assert [i["issue_content"] for i in _issues(_voted(samples, min_votes=2))] == ["bad code"]


def test_samples_that_are_not_reviews_are_ignored():
    assert vote_review_samples([{}, {"review": "text"}, None], min_votes=1).review == {}


# --- reviewer wiring -----------------------------------------------------------------------------

SAMPLE_A = yaml.safe_dump(_sample(_issue(start=10), _issue(file="only-a.py", start=5)), sort_keys=False)
SAMPLE_B = yaml.safe_dump(_sample(_issue(start=11)), sort_keys=False)
SAMPLE_C = yaml.safe_dump(_sample(_issue(start=10)), sort_keys=False)


def _make_reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.token_handler = MagicMock()
    reviewer.pr_url = "https://example/pr/1"
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.remaining_files_list = []
    reviewer.prediction = None
    return reviewer


@pytest.fixture
def sampling(request):
    num_samples, min_votes = getattr(request, "param", (3, 2))
    settings = get_settings()
    saved = {k: settings.get(k, None) for k in
             ("pr_reviewer.num_samples", "pr_reviewer.min_votes", "config.temperature")}
    settings.set("pr_reviewer.num_samples", num_samples)
    settings.set("pr_reviewer.min_votes", min_votes)
    settings.set("config.temperature", 0.4)
    yield
    for key, value in saved.items():
        settings.set(key, value)


@pytest.mark.asyncio
async def test_the_review_is_sampled_num_samples_times_and_the_consensus_is_kept(sampling):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[SAMPLE_A, SAMPLE_B, SAMPLE_C])

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])):
        await reviewer._prepare_prediction("model")

    assert reviewer._get_prediction.await_count == 3
    # the vote is handed on as a dict, not re-serialised to YAML for the caller to re-parse
    assert [i["relevant_file"] for i in reviewer.prediction_data["review"]["key_issues_to_review"]] == ["app.py"]
    assert reviewer.review_vote_dropped_count == 1  # only-a.py was seen by one sample
    for sample in (SAMPLE_A, SAMPLE_B, SAMPLE_C):   # the raw samples are kept for logging
        assert sample in reviewer.prediction


@pytest.mark.asyncio
async def test_a_failed_or_unparsable_sample_is_dropped_and_the_bar_lowers(sampling):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("timeout"), "not yaml", SAMPLE_C])

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])):
        await reviewer._prepare_prediction("model")

    issues = reviewer.prediction_data["review"]["key_issues_to_review"]
    assert len(issues) == 1  # one sample, bar clamped to 1


@pytest.mark.asyncio
async def test_losing_every_sample_raises_so_the_fallback_chain_gets_a_turn(sampling):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(side_effect=[RuntimeError("boom"), "garbage", ""])

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await reviewer._prepare_prediction("model")


@pytest.mark.asyncio
@pytest.mark.parametrize("sampling", [(1, 2)], indirect=True)
async def test_one_sample_is_the_plain_single_call(sampling):
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=SAMPLE_C)

    with patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])):
        await reviewer._prepare_prediction("model")

    reviewer._get_prediction.assert_awaited_once_with("model")
    assert reviewer.prediction == SAMPLE_C  # untouched, not re-serialised


@pytest.mark.asyncio
async def test_sampling_at_temperature_zero_warns_that_the_vote_is_a_no_op(sampling):
    get_settings().set("config.temperature", 0)
    reviewer = _make_reviewer()
    reviewer._get_prediction = AsyncMock(return_value=SAMPLE_C)

    with (
        patch("pr_agent.tools.pr_reviewer.get_pr_diff", return_value=("diff", [])),
        patch("pr_agent.tools.pr_reviewer.get_logger") as get_logger,
    ):
        await reviewer._prepare_prediction("model")

    warnings = " ".join(str(c) for c in get_logger.return_value.warning.call_args_list)
    assert "temperature = 0" in warnings


def test_sampling_is_off_by_default():
    assert int(get_settings().pr_reviewer.num_samples) == 1


def test_two_distinct_findings_in_one_sample_never_collapse_into_one():
    """A sample cannot vote twice for its own cluster - two findings in one response are distinct
    by construction, even when their line ranges sit within VOTE_LINE_TOLERANCE of each other."""
    samples = [
        _sample(_issue(start=10, end=12), _issue(start=14, end=16, content="a different bug"))
        for _ in range(3)
    ]
    merged = _voted(samples)
    assert _files(merged) == [("app.py", 10), ("app.py", 14)]


def test_the_same_finding_across_samples_still_clusters_when_a_neighbour_exists():
    samples = [
        _sample(_issue(start=10, end=12), _issue(start=14, end=16, content="a different bug")),
        _sample(_issue(start=11, end=13)),
    ]
    merged = _voted(samples)
    files = _files(merged)
    assert len(files) == 1
    assert files[0][0] == "app.py"
    assert files[0][1] in (10, 11)


def test_a_hidden_file_and_its_dotless_twin_are_different_files():
    """lstrip("./") stripped the dot: ".env" and "env" voted as one finding."""
    samples = [_sample(_issue(file=".env")), _sample(_issue(file="env"))]
    assert _issues(_voted(samples)) == []
    samples = [_sample(_issue(file="./app.py")), _sample(_issue(file="app.py"))]
    assert len(_issues(_voted(samples))) == 1
