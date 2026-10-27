"""Merge rules for a `/review` that was answered one diff chunk at a time.

Every chunk answers the same schema about a different part of the PR, so each field needs a
stated rule. The invariant the whole module protects: a merged review is never less alarming
than its worst chunk.
"""

from unittest.mock import Mock

import pytest

from pr_agent.algo import review_merge
from pr_agent.algo.output_models import PRReview
from pr_agent.algo.review_merge import all_key_issues, chunk_hit_findings_limit, merge_review_chunks
from pr_agent.algo.utils import convert_to_markdown_v2


def _chunk(**review):
    return {"review": review}


def _issue(file="app.py", header="Possible Issue", content="unchecked index", start=3, end=4):
    return {"relevant_file": file, "issue_header": header, "issue_content": content,
            "start_line": start, "end_line": end}


def test_no_chunk_produces_no_review():
    assert merge_review_chunks([]) == {}
    assert merge_review_chunks([{"not_a_review": 1}, {"review": "text"}]) == {}


def test_a_single_chunk_is_passed_through_without_being_aliased():
    review = {"score": "80", "key_issues_to_review": [_issue()], "todo_sections": "Add timeout handling"}
    merged = merge_review_chunks([{"review": review}])

    assert merged == {"review": review}
    assert merged["review"] is not review


def test_chunks_without_a_review_mapping_are_skipped():
    merged = merge_review_chunks([_chunk(score="90"), {"review": None}, _chunk(score="40")])

    assert merged["review"]["score"] == "40"


def test_fields_keep_the_order_they_first_appear_in():
    merged = merge_review_chunks([
        _chunk(score="90", relevant_tests="No"),
        _chunk(security_concerns="No", score="40"),
    ])

    assert list(merged["review"]) == ["score", "relevant_tests", "security_concerns"]


def test_key_issues_from_every_chunk_are_kept_in_chunk_order():
    first, second = _issue(file="a.py"), _issue(file="b.py", content="off by one")
    merged = merge_review_chunks([
        _chunk(key_issues_to_review=[first]),
        _chunk(key_issues_to_review=[second]),
    ])

    assert merged["review"]["key_issues_to_review"] == [first, second]


def test_the_same_finding_reported_by_two_chunks_is_kept_once():
    merged = merge_review_chunks([
        _chunk(key_issues_to_review=[_issue()]),
        # same file, header and text, restated with different whitespace, casing and lines
        _chunk(key_issues_to_review=[_issue(header="possible  issue", content="Unchecked index",
                                            start=30, end=31)]),
    ])

    assert merged["review"]["key_issues_to_review"] == [_issue()]


def test_findings_survive_a_chunk_that_found_nothing():
    merged = merge_review_chunks([
        _chunk(key_issues_to_review=[]),
        _chunk(key_issues_to_review=[_issue()]),
        _chunk(key_issues_to_review="No"),
    ])

    assert merged["review"]["key_issues_to_review"] == [_issue()]


def test_merged_findings_are_cut_to_the_configured_limit():
    # every chunk may report its own full allowance, so the union can exceed the limit
    issues = [_issue(file=f"{name}.py", content=f"problem in {name}") for name in "abcd"]
    merged = merge_review_chunks([
        _chunk(key_issues_to_review=issues[:2]),
        _chunk(key_issues_to_review=issues[2:]),
    ], max_findings=3)

    assert merged["review"]["key_issues_to_review"] == issues[:3]


def test_the_finding_cap_keeps_chunk_order_and_drops_from_the_tail():
    first, second, third = (_issue(file="a.py"),
                            _issue(file="b.py", content="b"),
                            _issue(file="c.py", content="c"))
    merged = merge_review_chunks([_chunk(key_issues_to_review=[first, second, third])],
                                 max_findings=2)

    assert merged["review"]["key_issues_to_review"] == [first, second]


def test_a_review_within_the_limit_is_kept_whole():
    issue = _issue()
    merged = merge_review_chunks([_chunk(key_issues_to_review=[issue]),
                                  _chunk(key_issues_to_review=[])], max_findings=3)

    assert merged["review"]["key_issues_to_review"] == [issue]


def test_no_cap_is_applied_when_the_limit_is_absent():
    issues = [_issue(file=f"{name}.py", content=f"problem in {name}") for name in "abcd"]
    merged = merge_review_chunks([
        _chunk(key_issues_to_review=issues[:2]),
        _chunk(key_issues_to_review=issues[2:]),
    ])

    assert merged["review"]["key_issues_to_review"] == issues


def test_a_security_concern_in_one_chunk_wins_over_the_chunks_that_found_none():
    merged = merge_review_chunks([
        _chunk(security_concerns="No"),
        _chunk(security_concerns="SQL injection: user input reaches the query unescaped"),
    ])

    assert merged["review"]["security_concerns"] == "SQL injection: user input reaches the query unescaped"


def test_security_concerns_are_only_no_when_every_chunk_said_no():
    merged = merge_review_chunks([_chunk(security_concerns="No"), _chunk(security_concerns="no")])

    assert merged["review"]["security_concerns"] == "No"


def test_two_different_security_concerns_are_both_reported_once():
    merged = merge_review_chunks([
        _chunk(security_concerns="XSS: unescaped output"),
        _chunk(security_concerns="XSS: unescaped output"),
        _chunk(security_concerns="Secret exposure: the token is logged"),
    ])

    assert merged["review"]["security_concerns"] == (
        "XSS: unescaped output\n\nSecret exposure: the token is logged")


def test_the_score_of_the_worst_chunk_is_the_score_of_the_pr():
    merged = merge_review_chunks([_chunk(score="90"), _chunk(score=35), _chunk(score="70")])

    # the winning chunk's own value is returned, so its type and formatting survive
    assert merged["review"]["score"] == 35


def test_an_unreadable_score_does_not_hide_the_scores_that_parsed():
    merged = merge_review_chunks([_chunk(score="not a score"), _chunk(score="60")])

    assert merged["review"]["score"] == "60"


def test_review_effort_reports_the_hardest_chunk():
    merged = merge_review_chunks([_chunk(**{"estimated_effort_to_review_[1-5]": "1"}),
                                  _chunk(**{"estimated_effort_to_review_[1-5]": 2})])

    assert merged["review"]["estimated_effort_to_review_[1-5]"] == 2


def test_review_effort_does_not_saturate_across_modest_chunks():
    merged = merge_review_chunks([_chunk(**{"estimated_effort_to_review_[1-5]": "2"}) for _ in range(3)])

    assert merged["review"]["estimated_effort_to_review_[1-5]"] == 2


def test_review_effort_stays_within_the_one_to_five_scale():
    merged = merge_review_chunks([_chunk(**{"estimated_effort_to_review_[1-5]": "4"}),
                                  _chunk(**{"estimated_effort_to_review_[1-5]": "3"}),
                                  _chunk(**{"estimated_effort_to_review_[1-5]": "7"})])

    assert merged["review"]["estimated_effort_to_review_[1-5]"] == 5


def test_unparsable_effort_falls_back_to_the_first_chunk():
    merged = merge_review_chunks([_chunk(**{"estimated_effort_to_review_[1-5]": "unknown"}),
                                  _chunk(**{"estimated_effort_to_review_[1-5]": "also unknown"})])

    assert merged["review"]["estimated_effort_to_review_[1-5]"] == "unknown"


@pytest.mark.parametrize("levels, expected", [
    (["low", "high", "medium"], "high"),
    (["low", "medium"], "medium"),
    (["low", "low"], "low"),
])
def test_the_riskiest_chunk_sets_the_risk_level(levels, expected):
    merged = merge_review_chunks([_chunk(risk_level=level) for level in levels])

    assert merged["review"]["risk_level"] == expected


@pytest.mark.parametrize("recommendations, expected", [
    (["no_concerns_found", "changes_required"], "changes_required"),
    (["no_concerns_found", "needs_review"], "needs_review"),
    (["no_concerns_found", "no_concerns_found"], "no_concerns_found"),
])
def test_the_most_cautious_chunk_sets_the_merge_recommendation(recommendations, expected):
    merged = merge_review_chunks([_chunk(merge_recommendation=value) for value in recommendations])

    assert merged["review"]["merge_recommendation"] == expected


def test_an_unrecognised_risk_level_falls_back_to_the_first_chunk():
    merged = merge_review_chunks([_chunk(risk_level="unknown"), _chunk(risk_level="also unknown")])

    assert merged["review"]["risk_level"] == "unknown"


def test_tests_added_in_any_chunk_count_as_tests_added_by_the_pr():
    merged = merge_review_chunks([_chunk(relevant_tests="No"), _chunk(relevant_tests="Yes")])

    assert merged["review"]["relevant_tests"] == "Yes"


def test_no_tests_when_no_chunk_found_any():
    merged = merge_review_chunks([_chunk(relevant_tests="No"), _chunk(relevant_tests="No")])

    assert merged["review"]["relevant_tests"] == "No"


def test_priority_files_are_unioned_without_repeats():
    merged = merge_review_chunks([
        _chunk(review_priority_files=["src/a.py", "src/b.py"]),
        _chunk(review_priority_files=["src/b.py", "src/c.py"]),
    ])

    assert merged["review"]["review_priority_files"] == ["src/a.py", "src/b.py", "src/c.py"]


def test_todo_sections_are_unioned_and_stay_no_when_no_chunk_found_any():
    todo = {"relevant_file": "a.py", "line_number": 4, "content": "drop the shim"}
    merged = merge_review_chunks([_chunk(todo_sections="No"), _chunk(todo_sections=[todo]),
                                  _chunk(todo_sections=[todo])])

    assert merged["review"]["todo_sections"] == [todo]
    assert merge_review_chunks([_chunk(todo_sections="No"),
                                _chunk(todo_sections="No")])["review"]["todo_sections"] == "No"


@pytest.mark.parametrize("gfm_supported", [True, False])
def test_todo_prose_survives_a_chunk_that_found_none(gfm_supported):
    chunks = [_chunk(key_issues_to_review=[], todo_sections=value)
              for value in ("No", "Add timeout handling")]
    for chunk in chunks:
        PRReview.model_validate(chunk)

    merged = merge_review_chunks(chunks)
    PRReview.model_validate(merged)
    provider = Mock()
    markdown = convert_to_markdown_v2(merged, gfm_supported=gfm_supported, git_provider=provider)

    assert "Add timeout handling" in markdown
    assert "No TODO sections" not in markdown
    provider.get_line_link.assert_not_called()


@pytest.mark.parametrize("gfm_supported", [True, False])
def test_todo_prose_and_located_entries_are_unioned_without_repeats(gfm_supported):
    todo = {"relevant_file": "src/app.py", "line_number": 3, "content": "fix the parser"}
    chunks = [_chunk(key_issues_to_review=[], todo_sections=value)
              for value in ([todo], "Add timeout handling", " add  TIMEOUT handling \n", "Handle cancellation")]
    for chunk in chunks:
        PRReview.model_validate(chunk)

    merged = merge_review_chunks(chunks)
    validated = PRReview.model_validate(merged)
    assert [entry.content for entry in validated.review.todo_sections] == [
        "fix the parser", "Add timeout handling", "Handle cancellation"]
    provider = Mock()
    provider.get_line_link.return_value = "https://example.com/src/app.py#L3"
    markdown = convert_to_markdown_v2(merged, gfm_supported=gfm_supported, git_provider=provider)

    assert "https://example.com/src/app.py#L3" in markdown
    assert (markdown.index("fix the parser") < markdown.index("Add timeout handling")
            < markdown.index("Handle cancellation"))
    assert markdown.count("Add timeout handling") == 1
    provider.get_line_link.assert_called_once_with("src/app.py", 3, 3)


@pytest.mark.parametrize("values", [
    (["Add timeout handling"], " add  TIMEOUT handling \n"),
    ("Add timeout handling", [" add  TIMEOUT handling \n"]),
])
def test_list_form_todo_prose_is_deduplicated_with_scalar_prose(values):
    chunks = [_chunk(key_issues_to_review=[], todo_sections=value) for value in values]

    merged = merge_review_chunks(chunks)
    provider = Mock()
    markdown = convert_to_markdown_v2(merged, git_provider=provider)

    assert len(merged["review"]["todo_sections"]) == 1
    assert "Add timeout handling" in markdown
    provider.get_line_link.assert_not_called()


@pytest.mark.parametrize("value", ["No", "None", "false", None, "", [], "  "], ids=repr)
def test_empty_or_negative_todo_chunks_keep_the_existing_fallback(value):
    chunks = [_chunk(todo_sections=value), _chunk(todo_sections=value)]

    assert merge_review_chunks(chunks)["review"]["todo_sections"] == value


def test_sub_prs_are_unioned_by_their_files_and_stay_within_the_prompt_limit():
    merged = merge_review_chunks([
        _chunk(can_be_split=[{"relevant_files": ["a.py"], "title": "first"},
                             {"relevant_files": ["b.py"], "title": "second"}]),
        # the same split, restated: same files, different title
        _chunk(can_be_split=[{"relevant_files": ["a.py"], "title": "first split"},
                             {"relevant_files": ["c.py"], "title": "third"},
                             {"relevant_files": ["d.py"], "title": "fourth"}]),
    ])

    assert [sub_pr["title"] for sub_pr in merged["review"]["can_be_split"]] == [
        "first", "second", "third"]


def test_ticket_compliance_is_grouped_per_ticket_and_its_bullet_lists_are_unioned():
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[{"ticket_url": "https://tracker/1",
                                         "fully_compliant_requirements": "- adds the endpoint",
                                         "not_compliant_requirements": ""}]),
        _chunk(ticket_compliance_check=[{"ticket_url": "https://tracker/1",
                                         "fully_compliant_requirements": "- adds the endpoint",
                                         "not_compliant_requirements": "- no rate limiting"},
                                        {"ticket_url": "https://tracker/2",
                                         "fully_compliant_requirements": "- renames the flag"}]),
    ])

    first, second = merged["review"]["ticket_compliance_check"]
    assert first["ticket_url"] == "https://tracker/1"
    assert first["fully_compliant_requirements"] == "- adds the endpoint"
    assert first["not_compliant_requirements"] == "- no rate limiting"
    assert second["ticket_url"] == "https://tracker/2"


def test_ticket_compliance_without_urls_does_not_collapse_unrelated_entries():
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[{"fully_compliant_requirements": "- adds the endpoint"}]),
        _chunk(ticket_compliance_check=[{"fully_compliant_requirements": "- updates the docs"}]),
    ])

    assert merged["review"]["ticket_compliance_check"] == [
        {"fully_compliant_requirements": "- adds the endpoint"},
        {"fully_compliant_requirements": "- updates the docs"},
    ]


def _ticket_entry(ticket_url, met, not_met=""):
    return {"ticket_url": ticket_url, "fully_compliant_requirements": met,
            "not_compliant_requirements": not_met, "requires_further_human_verification": ""}


def test_ticket_written_as_url_id_and_shortened_url_merges_into_one_entry():
    related = [{"ticket_id": 12, "ticket_url": "https://github.com/acme/widgets/issues/12",
                "title": "Widgets"}]
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[_ticket_entry("https://github.com/acme/widgets/issues/12",
                                                      "- A", "- B")]),
        _chunk(ticket_compliance_check=[_ticket_entry("#12", "- A\n- B")]),
        _chunk(ticket_compliance_check=[_ticket_entry("https://github.com/.../issues/12", "- B")]),
    ], related_tickets=related)

    entries = merged["review"]["ticket_compliance_check"]
    assert len(entries) == 1
    assert entries[0]["ticket_url"] == "https://github.com/acme/widgets/issues/12"
    assert entries[0]["fully_compliant_requirements"] == "- A\n- B"
    assert entries[0]["not_compliant_requirements"] == "- B"


def test_ticket_variants_stay_separate_without_related_tickets():
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[_ticket_entry("https://github.com/acme/widgets/issues/12",
                                                      "- A")]),
        _chunk(ticket_compliance_check=[_ticket_entry("#12", "- B")]),
    ])

    assert [t["ticket_url"] for t in merged["review"]["ticket_compliance_check"]] == [
        "https://github.com/acme/widgets/issues/12", "#12"]


def test_a_ticket_that_matches_no_related_ticket_keeps_its_own_url():
    related = [{"ticket_id": 12, "ticket_url": "https://github.com/acme/widgets/issues/12",
                "title": "Widgets"}]
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[_ticket_entry("https://tracker/99", "- A")]),
    ], related_tickets=related)

    assert merged["review"]["ticket_compliance_check"][0]["ticket_url"] == "https://tracker/99"


def test_a_shared_trailing_number_does_not_merge_unrelated_tickets():
    related = [
        {"ticket_id": 12, "ticket_url": "https://github.com/acme/widgets/issues/12"},
        {"ticket_id": 12, "ticket_url": "https://github.com/acme/gadgets/issues/12"},
    ]
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[_ticket_entry("#12", "- A")]),
        _chunk(ticket_compliance_check=[_ticket_entry("https://github.com/acme/gadgets/issues/12",
                                                      "- B")]),
    ], related_tickets=related)

    assert [t["ticket_url"] for t in merged["review"]["ticket_compliance_check"]] == [
        "#12", "https://github.com/acme/gadgets/issues/12"]


def test_the_canonical_ticket_url_is_an_exact_url_match_taken_verbatim():
    related = [{"ticket_id": 7, "ticket_url": "https://github.com/acme/widgets/issues/7"}]
    merged = merge_review_chunks([
        _chunk(ticket_compliance_check=[_ticket_entry("HTTPS://GitHub.com/acme/widgets/issues/7",
                                                      "- A")]),
    ], related_tickets=related)

    assert merged["review"]["ticket_compliance_check"][0]["ticket_url"] == (
        "https://github.com/acme/widgets/issues/7")


def test_contribution_time_adds_up_across_the_chunks():
    merged = merge_review_chunks([
        _chunk(contribution_time_cost_estimate={"best_case": "45m", "average_case": "2h",
                                                "worst_case": "5h"}),
        _chunk(contribution_time_cost_estimate={"best_case": "45m", "average_case": "3h",
                                                "worst_case": "10h"}),
    ])

    assert merged["review"]["contribution_time_cost_estimate"] == {
        "best_case": "1.5h", "average_case": "5h", "worst_case": "15h"}


def test_contribution_time_falls_back_when_a_chunk_uses_an_unknown_unit():
    first = {"best_case": "45m", "average_case": "2h", "worst_case": "5h"}
    merged = merge_review_chunks([
        _chunk(contribution_time_cost_estimate=first),
        _chunk(contribution_time_cost_estimate={"best_case": "two days", "average_case": "3h",
                                                "worst_case": "10h"}),
    ])

    assert merged["review"]["contribution_time_cost_estimate"] == first


def test_a_field_the_rules_do_not_know_keeps_the_first_chunk_that_answered_it():
    merged = merge_review_chunks([_chunk(a_future_field=""), _chunk(a_future_field="kept"),
                                  _chunk(a_future_field="dropped")])

    assert merged["review"]["a_future_field"] == "kept"


def test_a_field_that_raises_while_merging_keeps_the_first_chunk_instead_of_failing(monkeypatch):
    def explode(values):
        raise ValueError("unexpected shape")

    monkeypatch.setitem(review_merge._MERGE_RULES, "score", explode)
    merged = merge_review_chunks([_chunk(score="90"), _chunk(score="40")])

    assert merged["review"]["score"] == "90"


def test_case_distinct_paths_remain_distinct_when_merging_chunks():
    def findings(path):
        return {
            "key_issues_to_review": [_issue(file=path)],
            "todo_sections": [{"relevant_file": path, "line_number": 3, "content": "fix this"}],
            "can_be_split": [{"relevant_files": [path], "title": "extract module"}],
            "review_priority_files": [path],
        }

    upper = findings("src/Foo.py")
    lower = findings("src/foo.py")
    merged = merge_review_chunks([
        _chunk(**upper),
        _chunk(**lower),
        _chunk(**upper),
    ], max_findings=3)["review"]

    assert [item["relevant_file"] for item in merged["key_issues_to_review"]] == [
        "src/Foo.py", "src/foo.py"]
    assert [item["relevant_file"] for item in merged["todo_sections"]] == [
        "src/Foo.py", "src/foo.py"]
    assert [item["relevant_files"] for item in merged["can_be_split"]] == [
        ["src/Foo.py"], ["src/foo.py"]]
    assert merged["review_priority_files"] == ["src/Foo.py", "src/foo.py"]


def _chunks_of(*sizes):
    return [_chunk(key_issues_to_review=[_issue(file=f"{index}_{n}.py", content=f"problem {index}_{n}")
                                         for n in range(size)])
            for index, size in enumerate(sizes)]


@pytest.mark.parametrize("sizes, limit, hit_limit", [
    ((2, 1), 3, False),   # reaches the limit, but no chunk was full
    ((1, 1), 3, False),
    ((3, 0), 3, True),    # a chunk that reports the limit on its own may have stopped there
    ((2, 2), 3, False),   # the merge drops one finding, but it is known and so not missing
    ((), 3, False),
])
def test_chunk_hit_findings_limit(sizes, limit, hit_limit):
    assert chunk_hit_findings_limit(_chunks_of(*sizes), limit) is hit_limit


def test_all_key_issues_keeps_what_the_merge_cuts_and_drops_repeats():
    issue = _issue()
    chunks = [_chunk(key_issues_to_review=[issue, _issue(file="b.py", content="b")]),
              _chunk(key_issues_to_review=[issue, _issue(file="c.py", content="c")])]

    assert [i["relevant_file"] for i in all_key_issues(chunks)] == ["app.py", "b.py", "c.py"]
    assert len(merge_review_chunks(chunks, max_findings=2)["review"]["key_issues_to_review"]) == 2


def test_all_key_issues_ignores_chunks_without_a_list():
    assert all_key_issues([{}, _chunk(key_issues_to_review="none"), _chunk()]) == []
