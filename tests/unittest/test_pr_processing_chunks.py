"""`get_pr_multi_diffs_with_files` names which files landed in each model call, and which of
those were clipped to fit - the plumbing the review coverage ledger (`pr_reviewer.py`) reads
to tell a fully-reviewed file from one the token budget only partially covered.
"""
import pr_agent.algo.pr_processing as pr_processing
from pr_agent.algo.pr_processing import ChunkPlan
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings


class FakeTokenHandler:
    def __init__(self, prompt_tokens=100):
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


def test_two_chunks_partition_files_by_call_budget(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "large_patch_policy": settings.config.get("large_patch_policy", "skip"),
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.large_patch_policy = "skip"
    settings.config.verbosity_level = 0

    # a.py sorts first (more tokens) and alone fits the per-call budget; adding b.py on top
    # overflows it, so b.py must start a second call rather than share the first.
    a_hunk = "@@ -1 +1 @@\n-old\n+" + ("alpha " * 40)
    b_hunk = "@@ -1 +1 @@\n-old\n+" + ("beta " * 30)
    files = [_file("a.py", a_hunk), _file("b.py", b_hunk)]
    provider = FakeProvider(files)
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1650)

    try:
        plans, remaining_files = pr_processing.get_pr_multi_diffs_with_files(
            provider, token_handler, "tiny-model", max_calls=2, add_line_numbers=False
        )

        assert [plan.files for plan in plans] == [("a.py",), ("b.py",)]
        assert plans[0].clipped == ()
        assert plans[1].clipped == ()
        assert remaining_files == []
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)


def test_clipped_file_is_named_in_its_chunk_plan(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "large_patch_policy": settings.config.get("large_patch_policy", "skip"),
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.large_patch_policy = "clip"
    settings.config.verbosity_level = 0

    file_info = _file("large.py", "@@ -1 +1 @@\n-old\n+" + ("new " * 200))
    provider = FakeProvider([file_info])
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1700)
    monkeypatch.setattr(pr_processing, "clip_tokens", lambda patch, *args, **kwargs: "clipped patch")

    try:
        plans, remaining_files = pr_processing.get_pr_multi_diffs_with_files(
            provider, token_handler, "tiny-model", max_calls=2, add_line_numbers=False
        )

        assert len(plans) == 1
        assert plans[0].diff == "clipped patch"
        assert plans[0].files == ("large.py",)
        assert plans[0].clipped == ("large.py",)
        assert remaining_files == []
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)


def test_get_pr_multi_diffs_delegates_to_with_files(monkeypatch):
    """The thin wrapper must still return exactly the diff strings, byte for byte."""
    plans = [
        ChunkPlan(diff="diff-one", files=("a.py",), clipped=()),
        ChunkPlan(diff="diff-two", files=("b.py",), clipped=("b.py",)),
    ]
    monkeypatch.setattr(
        pr_processing, "get_pr_multi_diffs_with_files",
        lambda *args, **kwargs: (plans, ["left_out.py"]),
    )

    diffs = pr_processing.get_pr_multi_diffs(None, None, "model")
    assert diffs == ["diff-one", "diff-two"]

    diffs_with_remaining, remaining = pr_processing.get_pr_multi_diffs(
        None, None, "model", return_remaining_files=True
    )
    assert diffs_with_remaining == ["diff-one", "diff-two"]
    assert remaining == ["left_out.py"]


def test_preserve_order_packs_priority_list_without_token_resort(monkeypatch):
    """Ship-scope passes an already-ordered list; preserve_order=True must not put the large
    design file ahead of the smaller lib/ files."""
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "large_patch_policy": settings.config.get("large_patch_policy", "skip"),
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.large_patch_policy = "skip"
    settings.config.verbosity_level = 0

    from pr_agent.algo.ship_scope import DEFAULT_LOW_PRIORITY_GLOBS, order_files_by_priority

    design = _file("design/a.html", "@@ -1 +1 @@\n-old\n+" + ("design " * 50))
    lib_a = _file("lib/a.dart", "@@ -1 +1 @@\n-old\n+" + ("alpha " * 20))
    docs = _file("docs/b.md", "@@ -1 +1 @@\n-old\n+" + ("docs " * 15))
    lib_b = _file("lib/b.dart", "@@ -1 +1 @@\n-old\n+" + ("beta " * 20))
    mixed = [design, lib_a, docs, lib_b]
    ordered = order_files_by_priority(mixed, DEFAULT_LOW_PRIORITY_GLOBS)
    provider = FakeProvider(mixed)
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1700)

    try:
        plans, remaining_files = pr_processing.get_pr_multi_diffs_with_files(
            provider, token_handler, "tiny-model", max_calls=3, add_line_numbers=False,
            diff_files=ordered, preserve_order=True,
        )
        assert len(plans) >= 2
        assert plans[0].files == ("lib/a.dart", "lib/b.dart")
        later = [name for plan in plans[1:] for name in plan.files] + remaining_files
        assert "design/a.html" in later
        assert "docs/b.md" in later
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)


def test_preserve_order_false_still_sorts_by_tokens_descending(monkeypatch):
    """Default packing is unchanged: largest patch first regardless of input order."""
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "large_patch_policy": settings.config.get("large_patch_policy", "skip"),
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.large_patch_policy = "skip"
    settings.config.verbosity_level = 0

    design = _file("design/a.html", "@@ -1 +1 @@\n-old\n+" + ("design " * 50))
    lib_a = _file("lib/a.dart", "@@ -1 +1 @@\n-old\n+" + ("alpha " * 20))
    docs = _file("docs/b.md", "@@ -1 +1 @@\n-old\n+" + ("docs " * 15))
    lib_b = _file("lib/b.dart", "@@ -1 +1 @@\n-old\n+" + ("beta " * 20))
    mixed = [design, lib_a, docs, lib_b]
    provider = FakeProvider(mixed)
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(
        pr_processing, "sort_files_by_main_languages",
        lambda languages, files: [{"files": list(files)}],
    )
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1700)

    try:
        plans, remaining_files = pr_processing.get_pr_multi_diffs_with_files(
            provider, token_handler, "tiny-model", max_calls=3, add_line_numbers=False,
            diff_files=mixed, preserve_order=False,
        )
        assert len(plans) >= 2
        assert plans[0].files[0] == "design/a.html"
        assert remaining_files == []
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)
