"""Cover the eval harness itself, offline.

The harness under tests/eval/ needs a live model, so it is outside pyproject's testpaths and
never runs in CI. Its *logic* still has to be right: a scorer that silently miscounts would make
every A/B number in ACCURACY_PLAN.md meaningless, and it would fail quietly. These tests pin the
scoring rules and drive the whole plumbing once with a stubbed model.
"""

import json
import os
from functools import partial

import pytest

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.config_loader import get_settings
from tests.eval.corpus import ALL_DEFECTS, MUTANTS, REVERTED_FIXES, SeededDefect
from tests.eval.scoring import Outcome, score_defect, summarize

DEFECT = SeededDefect(
    id="d1", defect_class="boundary", summary="s",
    files=("svc/auth/token.py",), signals=("expiry", "flipped"), source="mutant",
)


def _review(*findings):
    return {"review": {"key_issues_to_review": list(findings)}}


def _finding(file="svc/auth/token.py", header="Flipped expiry comparison", content=""):
    return {"relevant_file": file, "issue_header": header, "issue_content": content}


def test_a_finding_on_the_right_file_naming_the_defect_is_a_hit():
    result = score_defect(DEFECT, _review(_finding()))
    assert result.outcome is Outcome.HIT


def test_a_finding_on_the_right_file_that_misses_the_defect_is_not_a_hit():
    result = score_defect(DEFECT, _review(_finding(header="Consider renaming this variable")))
    assert result.outcome is Outcome.FILE_ONLY


def test_a_correctly_worded_finding_on_the_wrong_file_is_a_miss():
    result = score_defect(DEFECT, _review(_finding(file="svc/other.py")))
    assert result.outcome is Outcome.MISS


def test_a_basename_or_partial_path_still_matches():
    for reported in ("token.py", "./svc/auth/token.py", "repo/svc/auth/token.py"):
        assert score_defect(DEFECT, _review(_finding(file=reported))).outcome is Outcome.HIT


@pytest.mark.parametrize("review", [None, {}, {"review": {}}, {"review": "text"}])
def test_a_review_that_never_parsed_is_a_parse_failure_not_a_miss(review):
    """The distinction is the point: an unparsable review writes no JSON at all, and folding
    that into recall would hide the failure mode small local models hit most."""
    assert score_defect(DEFECT, review).outcome is Outcome.PARSE_FAIL


def test_signals_match_case_insensitively_and_inside_the_issue_body():
    finding = _finding(header="Possible problem", content="The EXPIRY check looks inverted")
    assert score_defect(DEFECT, _review(finding)).outcome is Outcome.HIT


def test_recall_is_computed_over_reviews_that_parsed():
    results = [
        score_defect(DEFECT, _review(_finding())),                 # hit
        score_defect(DEFECT, _review(_finding(header="style"))),   # file_only
        score_defect(DEFECT, None),                                # parse fail
    ]
    summary = summarize(results)
    assert summary["parse_fail_rate"] == round(1 / 3, 3)
    assert summary["recall"] == 0.5  # 1 hit out of the 2 that parsed
    assert summary["by_class"]["boundary"] == {"n": 2, "hits": 1, "recall": 0.5}


def test_rationale_leaking_defects_are_reported_separately():
    leaky = SeededDefect(**{**DEFECT.__dict__, "id": "d2", "leaks_rationale": True})
    results = [
        score_defect(leaky, _review(_finding())),                    # hit, but leaked
        score_defect(DEFECT, _review(_finding(header="style"))),     # honest miss
    ]
    summary = summarize(results)
    assert summary["recall"] == 0.5
    assert summary["recall_no_rationale_leak"] == 0.0


def test_findings_that_match_no_seeded_defect_are_counted_but_not_called_wrong():
    review = _review(_finding(), _finding(file="svc/unrelated.py", header="Something else"))
    result = score_defect(DEFECT, review)
    assert result.outcome is Outcome.HIT
    assert result.unmatched_findings == 1


def test_every_corpus_diff_parses_as_a_unified_diff():
    from pr_agent.git_providers.diff_parsing import parse_unified_diff

    for defect in MUTANTS:
        files = parse_unified_diff(defect.diff_text)
        assert [f.filename for f in files] == list(defect.files), defect.id


def test_mutant_paths_do_not_exist_in_this_checkout():
    """Working-tree enrichment would replace a mutant with the real file and void the score."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for defect in MUTANTS:
        for path in defect.files:
            assert not os.path.exists(os.path.join(root, path)), f"{defect.id}: {path} exists"


def test_reverted_fixes_reference_commits_that_are_reachable():
    """Catches a typo in a SHA - but only where history is available to check against.

    CI checks out with the default depth of 1 and the Docker test image has no .git at all, so
    this skips there rather than failing on the environment.
    """
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)

    if git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout")
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        pytest.skip("shallow clone: reverted-fix commits are not fetched")
    for defect in REVERTED_FIXES:
        sha = defect.source.split(":", 1)[1]
        assert git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0, defect.id


def test_defect_ids_are_unique():
    ids = [d.id for d in ALL_DEFECTS]
    assert len(ids) == len(set(ids))


class _StubHandler(BaseAiHandler):
    def __init__(self, response):
        self.response = response

    @property
    def deployment_id(self):
        return None

    async def chat_completion(self, model, system, user, temperature=0.2, img_path=None, **_kwargs):
        return self.response, "stop"


@pytest.mark.asyncio
async def test_the_runner_scores_a_stubbed_review_end_to_end(tmp_path, monkeypatch):
    """Drives corpus -> plain_diff provider -> PRReviewer -> structured JSON -> scorer.

    Without this the harness is unverified plumbing: every piece could be individually right and
    the run still produce nothing scoreable.
    """
    from tests.eval.run_eval import run_one

    defect = next(d for d in MUTANTS if d.id == "mutant-flipped-comparison")
    response = (
        "review:\n"
        "  key_issues_to_review:\n"
        "  - relevant_file: svc/auth/token.py\n"
        "    issue_header: 'Flipped expiry comparison'\n"
        "    issue_content: 'Expired tokens are accepted because the comparison is inverted.'\n"
        "    start_line: 9\n"
        "    end_line: 12\n"
    )

    settings = get_settings()
    saved = {k: settings.get(k, None) for k in
             ("config.git_provider", "config.publish_output", "config.fallback_models",
              "plain_diff.content", "plain_diff.output_path", "plain_diff.json_output_path")}
    try:
        settings.set("config.fallback_models", [])
        result, review = await run_one(defect, os.getcwd(),
                                       ai_handler=partial(_StubHandler, response))
    finally:
        for key, value in saved.items():
            settings.set(key, value)

    assert result.outcome is Outcome.HIT, review
    assert json.dumps(review)  # the structured review round-tripped through the JSON file


@pytest.mark.asyncio
async def test_a_reverted_fix_run_from_inside_the_checkout_is_refused(tmp_path):
    """The guard against scoring a poisoned corpus.

    Reverted-fix items use real repository paths, so run from inside the checkout the provider
    enriches them with the *fixed* file and the model reviews content that already contains the
    fix. That produces a plausible-looking score for something other than the seeded defect,
    which is worse than not running at all.
    """
    from tests.eval.run_eval import EnrichedCorpusError, run_one

    defect = next(d for d in REVERTED_FIXES if d.id == "revert-a6484241")
    settings = get_settings()
    saved = {k: settings.get(k, None) for k in
             ("config.git_provider", "config.publish_output", "plain_diff.content",
              "plain_diff.output_path", "plain_diff.json_output_path")}
    try:
        with pytest.raises(EnrichedCorpusError, match="enrichment replaced"):
            await run_one(defect, os.getcwd(), ai_handler=partial(_StubHandler, "review: {}"))
    finally:
        for key, value in saved.items():
            settings.set(key, value)


# --- line-based matching -------------------------------------------------------------------------

LINE_DEFECT = SeededDefect(
    id="d-lines", defect_class="boundary", summary="s", files=("svc/auth/token.py",),
    signals=("some-word-the-model-will-never-say",), source="mutant",
)
LINE_DIFF = next(d for d in MUTANTS if d.id == "mutant-flipped-comparison").diff_text


def test_defect_lines_are_derived_from_the_diff_not_hand_written():
    from tests.eval.scoring import defect_line_ranges
    ranges = defect_line_ranges(LINE_DIFF)
    assert ranges == {"svc/auth/token.py": [(9, 11)]}


def test_a_finding_on_the_seeded_lines_is_a_hit_regardless_of_wording():
    finding = _finding(header="Hmm", content="something looks odd here")
    finding.update(start_line=11, end_line=11)
    result = score_defect(LINE_DEFECT, _review(finding), LINE_DIFF)
    assert result.outcome is Outcome.HIT
    assert result.hit_by == "lines"


def test_a_finding_far_from_the_seeded_lines_with_the_right_words_is_still_a_signal_hit():
    """Wording stays as secondary evidence - but the summary says which kind earned the hit."""
    finding = _finding(header="Flipped expiry comparison")
    finding.update(start_line=200, end_line=200)
    result = score_defect(DEFECT, _review(finding), LINE_DIFF)
    assert result.outcome is Outcome.HIT
    assert result.hit_by == "signal"


def test_a_finding_on_the_right_file_but_wrong_lines_and_wrong_words_is_file_only():
    finding = _finding(header="Style nit", content="rename this")
    finding.update(start_line=200, end_line=200)
    assert score_defect(LINE_DEFECT, _review(finding), LINE_DIFF).outcome is Outcome.FILE_ONLY


def test_the_summary_reports_how_hits_were_earned():
    lines_hit = _finding(header="Hmm")
    lines_hit.update(start_line=10, end_line=10)
    results = [
        score_defect(LINE_DEFECT, _review(lines_hit), LINE_DIFF),
        score_defect(DEFECT, _review(_finding()), None),   # signal only, no diff given
    ]
    assert summarize(results)["hits_by"] == {"lines": 1, "signal": 1, "both": 0}


# --- mutation engine -----------------------------------------------------------------------------

SOURCE = '''
async def handler(store, user_id, limit):
    profile = store.get(user_id)
    if profile is None:
        return None
    if not profile.active:
        return None
    for i in range(limit):
        if i < limit:
            await store.touch(profile, i)
    try:
        return await store.save(profile)
    except KeyError:
        return None
'''


def _ops(mutations):
    return sorted({m.operator for m in mutations})


def test_every_operator_finds_its_target_in_a_file_that_has_one():
    from tests.eval.mutate import OPERATORS, mutations_for_source
    found = _ops(mutations_for_source(SOURCE))
    assert found == sorted(OPERATORS), f"operators with no candidate: {set(OPERATORS) - set(found)}"


def test_every_mutant_still_parses_and_differs_from_the_original():
    import ast

    from tests.eval.mutate import mutations_for_source
    for m in mutations_for_source(SOURCE):
        assert m.source != SOURCE, m.operator
        ast.parse(m.source)  # a syntax error is a CI failure, not a review finding


def test_each_operator_changes_exactly_what_it_claims():
    from tests.eval.mutate import mutations_for_source
    by_op = {m.operator: m for m in mutations_for_source(SOURCE)}
    assert "isinstance" not in SOURCE  # keep the fixture honest about what is being checked
    assert "range(limit)" in SOURCE and "if i <= limit:" in by_op["boundary"].source
    assert ("if i >= limit:" in by_op["invert-comparison"].source
            or "is not None" in by_op["invert-comparison"].source)
    assert "if profile is None:" not in by_op["drop-none-guard"].source
    assert (
        "    store.touch(profile, i)" in by_op["drop-await"].source
        or "return store.save(profile)" in by_op["drop-await"].source
    )
    assert "except Exception:" in by_op["widen-except"].source
    assert "if profile.active:" in by_op["drop-not"].source
    assert "store.touch(i, profile)" in by_op["swap-args"].source or "store.get(" in by_op["swap-args"].source


def test_invert_comparison_produces_the_logical_negation():
    """_FLIP_INVERT must negate the condition, not merely swap its direction."""
    from tests.eval.mutate import mutations_for_source
    mutants = [m for m in mutations_for_source("def f(a, b):\n    return a < b\n")
               if m.operator == "invert-comparison"]
    assert any("a >= b" in m.source for m in mutants)
    mutants = [m for m in mutations_for_source("def f(a, b):\n    return a <= b\n")
               if m.operator == "invert-comparison"]
    assert any("a > b" in m.source for m in mutants)


def test_the_edit_is_confined_to_one_line_except_for_a_dropped_guard():
    from tests.eval.mutate import mutations_for_source
    original = SOURCE.splitlines()
    for m in mutations_for_source(SOURCE):
        mutated = m.source.splitlines()
        if m.operator == "drop-none-guard":
            assert len(mutated) == len(original) - 2
            continue
        assert len(mutated) == len(original), m.operator
        changed = [i for i, (a, b) in enumerate(zip(original, mutated, strict=True)) if a != b]
        assert changed == [m.start_line - 1], (m.operator, changed)


def test_a_reraising_except_is_not_widened():
    """`except X: raise` widened still re-raises; treating it as a defect would be a false label."""
    from tests.eval.mutate import mutations_for_source
    src = "def f():\n    try:\n        g()\n    except KeyError:\n        raise\n"
    assert not [m for m in mutations_for_source(src) if m.operator == "widen-except"]


def test_mutant_diffs_parse_and_carry_the_real_path():
    from pr_agent.git_providers.diff_parsing import parse_unified_diff
    from tests.eval.mutate import mutations_for_source, to_defect
    m = mutations_for_source(SOURCE)[0]
    defect = to_defect("svc/x/handler.py", SOURCE, m)
    files = parse_unified_diff(defect.diff_text)
    assert [f.filename for f in files] == ["svc/x/handler.py"]
    assert defect.files == ("svc/x/handler.py",)


def test_the_line_label_of_a_mutant_lands_on_its_edit():
    """The whole point of generating labels: the scorer's line ranges must cover the edit."""
    from tests.eval.mutate import mutations_for_source, to_defect
    from tests.eval.scoring import defect_line_ranges
    for m in mutations_for_source(SOURCE):
        defect = to_defect("svc/x/handler.py", SOURCE, m)
        ranges = defect_line_ranges(defect.diff_text)["svc/x/handler.py"]
        assert any(start <= m.start_line <= end + 2 for start, end in ranges), m.operator


def test_generation_is_reproducible_and_balanced_across_operators(tmp_path):
    from collections import Counter

    from tests.eval.mutate import generate_mutants

    # a small synthetic tree: parsing the whole repository here would make this a 30s test
    (tmp_path / "pkg").mkdir()
    for name in ("a", "b", "c", "d"):
        (tmp_path / "pkg" / f"{name}.py").write_text(SOURCE.replace("handler", f"handler_{name}"))
    globs = ("pkg/*.py",)

    a = generate_mutants(str(tmp_path), 14, seed=3, globs=globs)
    b = generate_mutants(str(tmp_path), 14, seed=3, globs=globs)
    assert [d.id for d in a] == [d.id for d in b]
    assert len({d.id for d in a}) == 14
    assert max(Counter(d.source for d in a).values()) == 2  # 7 operators, 14 items, 2 each
    assert all(d.files[0].startswith("pkg/") for d in a)
    assert [d.id for d in generate_mutants(str(tmp_path), 14, seed=4, globs=globs)] != [d.id for d in a]


# --- regressions found in self-review ------------------------------------------------------------

def test_hidden_paths_keep_their_leading_dot_when_normalised():
    """lstrip("./") strips characters, not a prefix: ".github/x" became "github/x"."""
    from pr_agent.algo.review_merge import normalize_finding_path
    assert normalize_finding_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"
    assert normalize_finding_path("./src/.env") == "src/.env"
    assert normalize_finding_path("/abs/x.py") == "abs/x.py"


def test_the_harness_matches_findings_the_way_the_reviewer_clusters_them():
    """The harness measures the production matching rule, so it must be the same code.

    A private copy scored against the old rule while reporting recall for the new one.
    """
    from pr_agent.algo import review_merge
    from tests.eval import scoring
    assert scoring.normalize_finding_path is review_merge.normalize_finding_path
    assert scoring.finding_line_range is review_merge.finding_line_range
    assert scoring.line_ranges_overlap is review_merge.line_ranges_overlap
    assert scoring.LINE_TOLERANCE == review_merge.VOTE_LINE_TOLERANCE


def test_a_path_suffix_only_matches_on_a_segment_boundary():
    defect = SeededDefect(id="d", defect_class="c", summary="s", files=("pr_agent/x.py",),
                          signals=("bug",), source="mutant")
    assert score_defect(defect, _review(_finding(file="repo/pr_agent/x.py", header="bug"))).outcome is Outcome.HIT
    assert score_defect(defect, _review(_finding(file="x.py", header="bug"))).outcome is Outcome.HIT
    assert score_defect(defect, _review(_finding(file="_agent/x.py", header="bug"))).outcome is Outcome.MISS
    assert score_defect(defect, _review(_finding(file="py", header="bug"))).outcome is Outcome.MISS


def test_only_a_guard_that_fires_on_none_is_a_null_dereference_candidate():
    from tests.eval.mutate import mutations_for_source
    src = "def f(x):\n    if x is not None:\n        return x\n    if x == None:\n        return 0\n    return 1\n"
    guards = [m for m in mutations_for_source(src) if m.operator == "drop-none-guard"]
    assert [m.start_line for m in guards] == [4]
