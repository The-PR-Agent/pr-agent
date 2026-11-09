from pr_agent.algo.output_models import MAX_ARTIFACT_FINDINGS, parse_artifact_findings
from pr_agent.algo.utils import convert_to_markdown_v2

ARTIFACT = {"content": "  # aws_s3_bucket.exports will be destroyed\nPlan: 0 to add, 0 to change, 1 to destroy."}


def _finding(**overrides):
    finding = {"kind": "destroy", "fact": "Plan destroys the exports bucket.",
               "evidence": "aws_s3_bucket.exports will be destroyed"}
    return {**finding, **overrides}


def test_quoted_evidence_is_marked_found():
    [finding] = parse_artifact_findings([_finding()], ARTIFACT)
    assert finding["quoted"] is True


def test_evidence_match_ignores_whitespace_and_quotes():
    [finding] = parse_artifact_findings([_finding(evidence='"aws_s3_bucket.exports   will be\n destroyed"')], ARTIFACT)
    assert finding["quoted"] is True


def test_quote_inside_evidence_is_kept():
    context = {"content": "FAILED test_scope - KeyError: 'scope'"}
    [finding] = parse_artifact_findings([_finding(evidence="KeyError: 'scope'")], context)
    assert finding["evidence"] == "KeyError: 'scope'"
    assert finding["quoted"] is True


def test_invented_evidence_is_marked_not_found():
    [finding] = parse_artifact_findings([_finding(evidence="aws_rds_cluster.main will be destroyed")], ARTIFACT)
    assert finding["quoted"] is False


def test_multiple_artifacts_are_searched():
    context = {"artifacts": [{"content": "nothing here"}, ARTIFACT]}
    [finding] = parse_artifact_findings([_finding()], context)
    assert finding["quoted"] is True


def test_caps_count_and_fact_length_and_drops_invalid_entries():
    long_fact = " ".join(f"w{i}" for i in range(40))
    distinct = [_finding(fact=long_fact, evidence=f"line {i}") for i in range(8)]
    value = [{"kind": "explosion", "fact": "x", "evidence": "y"}, "not a dict", *distinct]
    findings = parse_artifact_findings(value, ARTIFACT)
    assert len(findings) == MAX_ARTIFACT_FINDINGS
    assert findings[0]["fact"].endswith(" …")
    assert len(findings[0]["fact"].split()) == 21


def test_kind_is_normalised():
    [finding] = parse_artifact_findings([_finding(kind=" Test Failure ")], ARTIFACT)
    assert finding["kind"] == "test_failure"


def _render(findings, gfm=True):
    data = {"review": {"artifact_findings": findings, "key_issues_to_review": []}}
    return convert_to_markdown_v2(data, gfm_supported=gfm)


def test_renders_findings_and_escapes_html():
    [finding] = parse_artifact_findings([_finding(fact="<img src=x> destroys bucket")], ARTIFACT)
    text = _render([finding])
    assert "CI artifact findings" in text
    assert "&lt;img src=x&gt;" in text
    assert "<img" not in text
    assert "quote not found" not in text


def test_renders_unquoted_marker_and_empty_list():
    [finding] = parse_artifact_findings([_finding(evidence="made up")], ARTIFACT)
    assert "quote not found in the artifact" in _render([finding])
    assert "CI artifact findings</strong>: none" in _render([])
    assert "### 📋 CI artifact findings: none" in _render([], gfm=False)


def test_plain_markdown_keeps_apostrophes_and_raw_evidence():
    context = {"content": "FAILED test_x - KeyError: 'scope' & <b>"}
    [finding] = parse_artifact_findings(
        [_finding(kind="test_failure", fact="the job's step fails", evidence="KeyError: 'scope' & <b>")], context)
    text = _render([finding], gfm=False)
    assert "the job&#x27;s step fails" in text
    assert "`KeyError: 'scope' & <b>`" in text


def test_evidence_longer_than_the_cap_is_checked_in_full():
    invented_tail = "aws_s3_bucket.exports will be destroyed " + "x" * 200
    [finding] = parse_artifact_findings([_finding(evidence=invented_tail)], ARTIFACT)
    assert finding["quoted"] is False
    assert finding["evidence"].endswith(" …")


def test_hyphenated_kind_is_normalised():
    [finding] = parse_artifact_findings([_finding(kind="test-failure")], ARTIFACT)
    assert finding["kind"] == "test_failure"


def test_repeated_findings_are_listed_once():
    findings = parse_artifact_findings([_finding(), _finding(evidence='"aws_s3_bucket.exports will be destroyed"')],
                                       ARTIFACT)
    assert len(findings) == 1


def test_short_generic_quote_is_not_counted_as_found():
    context = {"content": "FAILED tests/test_a.py::test_save\nerror: build failed"}
    findings = parse_artifact_findings([_finding(kind="test_failure", fact="test_load fails.", evidence="FAILED"),
                                        _finding(kind="failure", fact="The build fails.", evidence="error")], context)
    assert [finding["quoted"] for finding in findings] == [False, False]


def test_short_quote_of_a_whole_line_is_found():
    context = {"content": "pytest summary\n  Tests: 3 failed  \nexit 1"}
    [finding] = parse_artifact_findings([_finding(kind="test_failure", evidence="Tests: 3 failed")], context)
    assert finding["quoted"] is True


def test_comment_cannot_turn_on_artifact_findings():
    from pr_agent.algo.cli_args import CliArgs

    assert not CliArgs.validate_user_args(["--pr_reviewer.require_artifact_findings=true"])[0]


def test_plain_markdown_evidence_cannot_open_an_html_comment():
    marker = "<!-- pr-agent-review-state:v1"
    [finding] = parse_artifact_findings([_finding(kind="warning", evidence=marker)], {"content": marker})
    assert "<!--" not in _render([finding], gfm=False)


def test_credentials_are_redacted_after_the_quote_check_and_before_the_cut():
    token = "ghp_" + "A1b2C3d4" * 5
    line = f"export GITHUB_TOKEN={token}"
    long_line = "x" * 150 + " " + token
    findings = parse_artifact_findings(
        [_finding(kind="warning", fact=f"The job prints {token}.", evidence=line),
         _finding(kind="failure", fact="A long line.", evidence=long_line)],
        {"content": f"{line}\n{long_line}"})
    assert [finding["quoted"] for finding in findings] == [True, True]
    for finding in findings:
        assert "ghp_" not in finding["fact"] and "ghp_" not in finding["evidence"]
        assert "A1b2C3d4" not in finding["evidence"]
