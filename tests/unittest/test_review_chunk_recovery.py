"""Bounded chunk-failure recovery: retry, then split the failed chunk in half by file, then try
the first fallback model, and only then mark the chunk's files chunk_failed (R-7).

`split_chunk_plan`'s own file-partitioning is covered directly against `get_pr_multi_diffs_with_files`
(the real chunker); the three `_review_chunk_plans` tests above it stub `split_chunk_plan` out, since
what they exercise is the recovery *staging*, not the chunker.
"""
import pytest

import pr_agent.algo.pr_processing as pr_processing
from pr_agent.algo.pr_processing import ChunkPlan
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.tools import pr_reviewer as mod


class _Reviewer(mod.PRReviewer):
    """Bypass __init__; exercise only the chunk loop."""
    def __init__(self, plans, failures):
        self.chunk_plans = plans
        self._failures = failures  # set of diff strings that fail on the primary model
        self.calls = []
        self.coverage = mod.CoverageLedger()
        for p in plans:
            for f in p.files:
                self.coverage.add(mod.FileCoverage(f, 10, "reviewed"))

    async def _get_review_data(self, model, patches_diff=None, chunk_index=None, files=None):
        self.calls.append((model, patches_diff))
        if patches_diff in self._failures and model == "primary":
            raise RuntimeError("boom")
        return ("raw", {"review": {"key_issues_to_review": []}}, 0)


@pytest.mark.asyncio
async def test_failed_chunk_is_split_then_reviewed(monkeypatch):
    plans = [ChunkPlan("AB", ("a.py", "b.py"), ()), ChunkPlan("C", ("c.py",), ())]
    monkeypatch.setattr(mod, "split_chunk_plan", lambda plan, *a: [ChunkPlan("A", ("a.py",), ()), ChunkPlan("B", ("b.py",), ())])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer(plans, failures={"AB"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert r.coverage.reviewed_ratio == 1.0
    assert ("primary", "A") in r.calls and ("primary", "B") in r.calls


@pytest.mark.asyncio
async def test_unsplittable_chunk_falls_back_to_secondary_model(monkeypatch):
    plans = [ChunkPlan("A", ("a.py",), ())]
    monkeypatch.setattr(mod, "split_chunk_plan", lambda plan, *a: [plan])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer(plans, failures={"A"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok and ("secondary", "A") in r.calls
    assert r.coverage.reviewed_ratio == 1.0


@pytest.mark.asyncio
async def test_exhausted_chunk_marks_files_failed(monkeypatch):
    """Chunk A must fail on every model tried - primary *and* the fallback - to prove stage 3
    (the fallback-model attempt) actually ran and also failed, rather than never being reached."""
    plans = [ChunkPlan("A", ("a.py",), ()), ChunkPlan("B", ("b.py",), ())]
    monkeypatch.setattr(mod, "split_chunk_plan", lambda plan, *a: [plan])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer(plans, failures={"A"})

    async def fail_a_on_every_model(model, patches_diff=None, chunk_index=None, files=None):
        r.calls.append((model, patches_diff))
        if patches_diff == "A":
            raise RuntimeError("boom")
        return ("raw", {"review": {"key_issues_to_review": []}}, 0)
    r._get_review_data = fail_a_on_every_model
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert ("secondary", "A") in r.calls  # proves stage 3 actually attempted the fallback model
    assert r.coverage.files["a.py"].status == "chunk_failed"
    assert r.review_failed_chunk_count == 1


@pytest.mark.asyncio
async def test_a_split_halfs_clipped_files_are_marked_clipped(monkeypatch):
    plan = ChunkPlan("AB", ("a.py", "b.py"), ())
    # a.py's regenerated half still had to clip a.py itself to fit
    half_a = ChunkPlan("A-clipped", ("a.py",), ("a.py",))
    half_b = ChunkPlan("B", ("b.py",), ())
    monkeypatch.setattr(mod, "split_chunk_plan", lambda p, *a: [half_a, half_b])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer([plan], failures={"AB"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert r.coverage.files["a.py"].status == "clipped"
    assert r.coverage.files["b.py"].status == "reviewed"


@pytest.mark.asyncio
async def test_a_file_dropped_by_the_split_is_not_credited_as_reviewed(monkeypatch):
    """The two halves' combined files are a strict subset of the parent plan's files (c.py did
    not survive into either regenerated half); c.py must not be silently left as `reviewed`."""
    plan = ChunkPlan("ABC", ("a.py", "b.py", "c.py"), ())
    half_a = ChunkPlan("A", ("a.py",), ())
    half_b = ChunkPlan("B", ("b.py",), ())
    monkeypatch.setattr(mod, "split_chunk_plan", lambda p, *a: [half_a, half_b])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer([plan], failures={"ABC"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert r.coverage.files["c.py"].status != "reviewed"
    assert r.coverage.files["c.py"].status == "skipped_budget"


@pytest.mark.asyncio
async def test_a_single_surviving_half_is_used_not_discarded_as_unsplittable(monkeypatch):
    """`split_chunk_plan` can legitimately return just one half - the other regenerated to
    nothing (e.g. its files turned out delete-only in isolation). The caller must not mistake
    that single-element result for "unsplittable" (which returns `[plan]` unchanged) and must
    still mark the dropped file so it is not silently credited as reviewed."""
    plan = ChunkPlan("AB", ("a.py", "b.py"), ())
    half_a = ChunkPlan("A", ("a.py",), ())  # b.py's half regenerated to nothing
    monkeypatch.setattr(mod, "split_chunk_plan", lambda p, *a: [half_a])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer([plan], failures={"AB"})
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert ("primary", "A") in r.calls  # the surviving half was actually attempted, not discarded
    assert r.coverage.files["a.py"].status == "reviewed"
    assert r.coverage.files["b.py"].status == "skipped_budget"


@pytest.mark.asyncio
async def test_a_dropped_file_already_deletion_only_keeps_that_status(monkeypatch):
    """A file the split dropped is only downgraded to skipped_budget when it was not already
    known to be deletion_only - a deletion-only file legitimately has nothing to review."""
    plan = ChunkPlan("AB", ("a.py", "b.py"), ())
    half_a = ChunkPlan("A", ("a.py",), ())
    monkeypatch.setattr(mod, "split_chunk_plan", lambda p, *a: [half_a])
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    r = _Reviewer([plan], failures={"AB"})
    r.coverage.mark("b.py", "deletion_only")
    ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    assert ok
    assert r.coverage.files["b.py"].status == "deletion_only"


@pytest.mark.asyncio
async def test_disabling_both_recovery_stages_restores_retry_then_drop(monkeypatch):
    """With both new flags off, a chunk that fails every `model` attempt goes straight to
    chunk_failed - no split, no fallback-model call - matching the pre-task-7 behavior."""
    settings = get_settings()
    original = (settings.pr_reviewer.get("chunk_split_on_failure", True),
                settings.pr_reviewer.get("chunk_fallback_model_on_failure", True))
    settings.pr_reviewer.chunk_split_on_failure = False
    settings.pr_reviewer.chunk_fallback_model_on_failure = False

    def fail_if_called(plan, *a):
        raise AssertionError("split_chunk_plan must not be called when chunk_split_on_failure is False")
    monkeypatch.setattr(mod, "split_chunk_plan", fail_if_called)
    monkeypatch.setattr(mod, "CHUNK_REVIEW_ATTEMPTS", 1)
    try:
        plans = [ChunkPlan("AB", ("a.py", "b.py"), ()), ChunkPlan("C", ("c.py",), ())]
        r = _Reviewer(plans, failures={"AB"})
        ok = await r._review_chunk_plans("primary", fallback_models=["secondary"])
    finally:
        settings.pr_reviewer.chunk_split_on_failure, settings.pr_reviewer.chunk_fallback_model_on_failure = original

    assert ok
    assert r.coverage.files["a.py"].status == "chunk_failed"
    assert r.coverage.files["b.py"].status == "chunk_failed"
    assert ("secondary", "AB") not in r.calls
    assert r.review_failed_chunk_count == 1


class FakeTokenHandler:
    def __init__(self, prompt_tokens=10):
        self.prompt_tokens = prompt_tokens

    def count_tokens(self, patch):
        return len(patch.split())


class FakeProvider:
    def __init__(self, files):
        self.files = files

    def get_diff_files(self):
        return self.files

    def get_languages(self):
        return {"Python": 100}


def _file(filename, patch):
    return FilePatchInfo(base_file="old\n", head_file="new\n", patch=patch,
                         filename=filename, edit_type=EDIT_TYPE.MODIFIED)


def test_split_chunk_plan_splits_two_files_into_two_one_file_plans(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.verbosity_level = 0

    file_a = _file("a.py", "@@ -1 +1 @@\n-old\n+new-a\n")
    file_b = _file("b.py", "@@ -1 +1 @@\n-old\n+new-b\n")
    provider = FakeProvider([file_a, file_b])
    token_handler = FakeTokenHandler(prompt_tokens=10)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)

    plan = ChunkPlan(diff="original-diff", files=("a.py", "b.py"), clipped=())
    try:
        halves = mod.split_chunk_plan(plan, provider, token_handler, "model")
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)

    assert [half.files for half in halves] == [("a.py",), ("b.py",)]


def test_split_chunk_plan_returns_the_plan_unchanged_for_a_single_file():
    plan = ChunkPlan(diff="d", files=("a.py",), clipped=())
    assert mod.split_chunk_plan(plan, object(), object(), "model") == [plan]


def test_split_chunk_plan_returns_the_surviving_half_when_the_other_regenerates_to_nothing(monkeypatch):
    """b.py has no patch content once isolated (as a delete-only file would), so its half
    regenerates to nothing; split_chunk_plan must still return a.py's half rather than falling
    back to `[plan]` (which would mean "could not split at all")."""
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.verbosity_level = 0

    file_a = _file("a.py", "@@ -1 +1 @@\n-old\n+new-a\n")
    file_b = _file("b.py", "")  # no patch content in isolation
    provider = FakeProvider([file_a, file_b])
    token_handler = FakeTokenHandler(prompt_tokens=10)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)

    plan = ChunkPlan(diff="original-diff", files=("a.py", "b.py"), clipped=())
    try:
        halves = mod.split_chunk_plan(plan, provider, token_handler, "model")
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)

    assert halves != [plan]
    assert [half.files for half in halves] == [("a.py",)]
