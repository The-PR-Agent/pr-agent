import ast
from pathlib import Path

import pytest

import pr_agent.algo.pr_processing as pr_processing
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from pr_agent.servers.utils import RateLimitExceeded


class FakeTokenHandler:
    def __init__(self, prompt_tokens=100):
        self.prompt_tokens = prompt_tokens
        self.count_calls = 0

    def count_tokens(self, patch):
        self.count_calls += 1
        return len(patch.split())


class FakeProvider:
    def __init__(self, files):
        self.files = files
        self.diff_calls = 0
        self.language_calls = 0

    def get_diff_files(self):
        self.diff_calls += 1
        return self.files

    def get_languages(self):
        self.language_calls += 1
        return {"Python": 100}


def _make_budget_files(tokens_per_file=2_800):
    return [
        FilePatchInfo(
            base_file="old\n",
            head_file="new\n",
            patch="@@ -1 +1 @@\n-old\n+" + ("token " * tokens_per_file),
            filename=f"file_{index}.py",
            edit_type=EDIT_TYPE.MODIFIED,
        )
        for index in range(2)
    ]


@pytest.mark.parametrize("resolved", [None, 0, -1, True, "5000"])
def test_output_token_reserve_rejects_unusable_values(resolved):
    assert pr_processing._resolve_output_token_reserve(lambda model, default: resolved, "model", 1_500) == 1_500


def test_output_token_reserve_falls_back_independently_when_one_resolution_fails():
    calls = []

    def resolve(model, default):
        calls.append((model, default))
        if default == pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD:
            return 5_000
        raise RuntimeError("hard reserve unavailable")

    assert pr_processing._resolve_output_token_reserve(resolve, "model", 1_500) == 5_000
    assert pr_processing._resolve_output_token_reserve(resolve, "model", 1_000) == 1_000
    assert calls == [("model", 1_500), ("model", 1_000)]


def test_get_pr_multi_diffs_reserves_the_active_completion_allowance(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "large_patch_policy": settings.config.get("large_patch_policy", "skip"),
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.large_patch_policy = "skip"
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)

    try:
        default_chunks = pr_processing.get_pr_multi_diffs(
            FakeProvider(_make_budget_files()),
            FakeTokenHandler(prompt_tokens=100),
            "model",
            add_line_numbers=False,
        )
        token_handler = FakeTokenHandler(prompt_tokens=100)
        reserved_chunks = pr_processing.get_pr_multi_diffs(
            FakeProvider(_make_budget_files()),
            token_handler,
            "model",
            add_line_numbers=False,
            output_token_reserve=lambda model, default: 5_000,
        )
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)

    assert len(default_chunks) == 1
    assert len(reserved_chunks) == 2
    assert all(token_handler.prompt_tokens + token_handler.count_tokens(chunk) <= 5_000
               for chunk in reserved_chunks)


def test_get_pr_diff_reserves_output_in_compressed_diff_and_metadata(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.verbosity_level = 0
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)
    token_handler = FakeTokenHandler(prompt_tokens=100)

    try:
        diff, remaining_files = pr_processing.get_pr_diff(
            FakeProvider(_make_budget_files()),
            token_handler,
            "model",
            return_remaining_files=True,
            output_token_reserve=lambda model, default: 5_000,
        )
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)

    assert "file_0.py" in diff
    assert "file_1.py" in diff
    assert remaining_files == ["file_1.py"]
    assert token_handler.prompt_tokens + token_handler.count_tokens(diff) <= 5_000


def test_get_pr_diff_uses_the_dynamic_hard_reserve_for_metadata(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.verbosity_level = 0
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)
    reserve_calls = []

    def resolve(model, default):
        reserve_calls.append((model, default))
        if default == pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD:
            return 5_000
        return 7_500

    token_handler = FakeTokenHandler(prompt_tokens=100)

    try:
        diff, remaining_files = pr_processing.get_pr_diff(
            FakeProvider(_make_budget_files()),
            token_handler,
            "model",
            return_remaining_files=True,
            output_token_reserve=resolve,
        )
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)

    assert "file_0.py" in diff
    assert "file_1.py" not in diff
    assert remaining_files == ["file_1.py"]
    assert token_handler.prompt_tokens + token_handler.count_tokens(diff) > 10_000 - 7_500
    assert reserve_calls == [("model", 1_500), ("model", 1_000)]


def test_get_pr_diff_multiple_patchs_resolves_the_output_allowance(monkeypatch):
    settings = get_settings()
    original = {
        "max_ai_calls": settings.pr_description.max_ai_calls,
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.pr_description.max_ai_calls = 4
    settings.config.verbosity_level = 0
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)

    try:
        default_result = pr_processing.get_pr_diff_multiple_patchs(
            FakeProvider(_make_budget_files()), FakeTokenHandler(prompt_tokens=100), "model"
        )
        reserved_result = pr_processing.get_pr_diff_multiple_patchs(
            FakeProvider(_make_budget_files()),
            FakeTokenHandler(prompt_tokens=100),
            "model",
            output_token_reserve=lambda model, default: 5_000,
        )
    finally:
        settings.pr_description.max_ai_calls = original["max_ai_calls"]
        settings.config.verbosity_level = original["verbosity_level"]

    assert len(default_result[0]) == 1
    assert len(reserved_result[0]) == 2


def test_prepared_multi_diffs_apply_the_current_attempt_reserve(monkeypatch):
    token_handler = FakeTokenHandler(prompt_tokens=100)
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)
    file_dict = {
        f"file_{index}.py": {
            "patch": "token " * 2_800,
            "tokens": 2_800,
            "edit_type": EDIT_TYPE.MODIFIED,
        }
        for index in range(2)
    }
    prepared = pr_processing.PreparedPRDiff(
        diff="prepared",
        remaining_files_list=[],
        file_dict=file_dict,
        files_by_name={},
        model="model",
        add_line_numbers_to_hunks=True,
        token_handler=token_handler,
    )

    chunks = pr_processing.get_pr_multi_diffs(
        FakeProvider([]),
        token_handler,
        "model",
        prepared_diff=prepared,
        output_token_reserve=lambda model, default: 5_000,
    )

    assert len(chunks) == 2


def test_direct_compressed_helpers_keep_legacy_defaults(monkeypatch):
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)
    file_dict = {
        "file.py": {"patch": "+ change", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }
    token_handler = FakeTokenHandler(prompt_tokens=100)

    default_patch = pr_processing.generate_full_patch(
        False, file_dict, 10_000, ["file.py"], token_handler
    )
    explicit_patch = pr_processing.generate_full_patch(
        False,
        file_dict,
        10_000,
        ["file.py"],
        token_handler,
        pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
        pr_processing.OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
    )

    def compressed(explicit):
        args = ([{"files": _make_budget_files(tokens_per_file=10)[:1]}], FakeTokenHandler(100), "model", False, False)
        if explicit:
            return pr_processing.pr_generate_compressed_diff(
                *args,
                soft_output_token_reserve=pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD,
                hard_output_token_reserve=pr_processing.OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
            )
        return pr_processing.pr_generate_compressed_diff(*args)

    assert default_patch == explicit_patch
    assert compressed(False) == compressed(True)


def test_generate_full_patch_preserves_soft_boundary_equality():
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "file.py": {"patch": "+ exact boundary", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }
    rendered_tokens = token_handler.count_tokens("\n\n## File: 'file.py'\n\n+ exact boundary\n")
    reserve = 5_000

    _, _, remaining_files, files_in_patch = pr_processing.generate_full_patch(
        False,
        file_dict,
        token_handler.prompt_tokens + rendered_tokens + reserve,
        ["file.py"],
        token_handler,
        soft_output_token_reserve=reserve,
        hard_output_token_reserve=reserve,
    )

    assert files_in_patch == ["file.py"]
    assert remaining_files == []


def test_generate_full_patch_preserves_hard_boundary_equality():
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "file.py": {"patch": "+ exact boundary", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }

    _, _, remaining_files, files_in_patch = pr_processing.generate_full_patch(
        False,
        file_dict,
        5_100,
        ["file.py"],
        token_handler,
        soft_output_token_reserve=1_000,
        hard_output_token_reserve=5_000,
    )

    assert files_in_patch == ["file.py"]
    assert remaining_files == []


def test_pack_pr_multi_diffs_preserves_soft_boundary_equality(monkeypatch):
    token_handler = FakeTokenHandler(prompt_tokens=100)
    reserve = 5_000
    file_dict = {
        "file.py": {"patch": "exact boundary", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }
    monkeypatch.setattr(
        pr_processing,
        "get_max_tokens",
        lambda model: token_handler.prompt_tokens + file_dict["file.py"]["tokens"] + reserve,
    )

    chunks = pr_processing._pack_pr_multi_diffs(
        file_dict,
        token_handler,
        "model",
        max_calls=1,
        return_remaining_files=False,
        soft_output_token_reserve=reserve,
    )

    assert chunks == ["exact boundary"]


@pytest.mark.parametrize(("extra_capacity", "uses_full_diff"), [(0, False), (1, True)])
def test_get_pr_diff_preserves_strict_full_diff_boundary(monkeypatch, extra_capacity, uses_full_diff):
    settings = get_settings()
    original_before = settings.config.patch_extra_lines_before
    original_after = settings.config.patch_extra_lines_after
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    max_tokens = {}
    full_diff = {}
    original_generate_extended_diff = pr_processing.pr_generate_extended_diff

    def generate_extended_diff(*args, **kwargs):
        result = original_generate_extended_diff(*args, **kwargs)
        max_tokens["value"] = result[1] + 5_000 + extra_capacity
        full_diff["value"] = "\n".join(result[0])
        return result

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "pr_generate_extended_diff", generate_extended_diff)
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: max_tokens["value"])
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_compressed_diff",
        lambda *args, **kwargs: ([['compressed']], [101], [], [], {}, [[]]),
    )

    try:
        diff = pr_processing.get_pr_diff(
            FakeProvider(_make_budget_files(tokens_per_file=1)[:1]),
            FakeTokenHandler(prompt_tokens=100),
            "model",
            output_token_reserve=lambda model, default: 5_000,
        )
    finally:
        settings.config.patch_extra_lines_before = original_before
        settings.config.patch_extra_lines_after = original_after

    assert diff == (full_diff["value"] if uses_full_diff else "compressed")


@pytest.mark.parametrize(("extra_capacity", "uses_full_diff"), [(0, False), (1, True)])
def test_get_pr_multi_diffs_preserves_strict_full_diff_boundary(monkeypatch, extra_capacity, uses_full_diff):
    settings = get_settings()
    original_before = settings.config.patch_extra_lines_before
    original_after = settings.config.patch_extra_lines_after
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    max_tokens = {}
    full_diff = {}
    packed_reserves = []
    original_generate_extended_diff = pr_processing.pr_generate_extended_diff

    def generate_extended_diff(*args, **kwargs):
        result = original_generate_extended_diff(*args, **kwargs)
        max_tokens["value"] = result[1] + 5_000 + extra_capacity
        full_diff["value"] = "\n".join(result[0])
        return result

    def pack_diffs(*args):
        packed_reserves.append(args[-1])
        return ["compressed"]

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "pr_generate_extended_diff", generate_extended_diff)
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: max_tokens["value"])
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_compressed_diff",
        lambda *args, **kwargs: ([], [], [], [], {}, []),
    )
    monkeypatch.setattr(pr_processing, "_pack_pr_multi_diffs", pack_diffs)

    try:
        chunks = pr_processing.get_pr_multi_diffs(
            FakeProvider(_make_budget_files(tokens_per_file=1)[:1]),
            FakeTokenHandler(prompt_tokens=100),
            "model",
            output_token_reserve=lambda model, default: 5_000,
        )
    finally:
        settings.config.patch_extra_lines_before = original_before
        settings.config.patch_extra_lines_after = original_after

    assert chunks == ([full_diff["value"]] if uses_full_diff else ["compressed"])
    assert packed_reserves == ([] if uses_full_diff else [5_000])


def test_prepared_pr_diff_reuses_compressed_files_without_changing_chunks(monkeypatch):
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

    hunk_sizes = (20, 40, 60, 80)
    hunks = [
        "@@ -1 +1 @@\n-old\n+" + ("alpha " * size)
        for size in hunk_sizes
    ]
    files = [
        FilePatchInfo("old\n", "new\n", hunks[index], f"file_{index}.py", edit_type=EDIT_TYPE.MODIFIED)
        for index in range(4)
    ]
    provider = FakeProvider(files)
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1_700)

    try:
        prepared = pr_processing.get_pr_diff(
            provider,
            token_handler,
            "tiny-model",
            add_line_numbers_to_hunks=True,
            return_remaining_files=True,
            return_prepared=True,
        )

        assert isinstance(prepared, pr_processing.PreparedPRDiff)
        assert prepared.file_dict
        expected_order = [f"file_{index}.py" for index in range(3, -1, -1)]
        assert list(prepared.file_dict) == expected_order
        calls_after_prepare = token_handler.count_calls
        prepared_chunks = pr_processing.get_pr_multi_diffs(
            provider,
            token_handler,
            "tiny-model",
            max_calls=3,
            add_line_numbers=True,
            return_remaining_files=True,
            prepared_diff=prepared,
        )

        fresh_provider = FakeProvider([
            FilePatchInfo("old\n", "new\n", hunks[index], f"file_{index}.py", edit_type=EDIT_TYPE.MODIFIED)
            for index in range(4)
        ])
        fresh_chunks = pr_processing.get_pr_multi_diffs(
            fresh_provider,
            FakeTokenHandler(prompt_tokens=100),
            "tiny-model",
            max_calls=3,
            add_line_numbers=True,
            return_remaining_files=True,
        )

        assert prepared_chunks == fresh_chunks
        prepared_diff_list, _ = prepared_chunks
        combined_chunks = "\n".join(prepared_diff_list)
        assert [combined_chunks.index(filename) for filename in expected_order] == sorted(
            combined_chunks.index(filename) for filename in expected_order
        )
        assert token_handler.count_calls == calls_after_prepare
        assert (provider.diff_calls, provider.language_calls) == (1, 1)
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)


@pytest.mark.parametrize(
    ("prepared_model", "requested_model", "prepared_line_numbers", "requested_line_numbers"),
    [
        ("tiny-model", "other-model", True, True),
        ("tiny-model", "tiny-model", False, False),
    ],
)
def test_prepared_pr_diff_is_not_reused_across_model_or_patch_format(
    monkeypatch, prepared_model, requested_model, prepared_line_numbers, requested_line_numbers
):
    settings = get_settings()
    original_verbosity_level = settings.config.verbosity_level
    settings.config.verbosity_level = 0
    hunk = "@@ -1 +1 @@\n-old\n+" + ("alpha " * 60)
    files = [
        FilePatchInfo("old\n", "new\n", hunk, f"file_{index}.py", edit_type=EDIT_TYPE.MODIFIED)
        for index in range(4)
    ]
    provider = FakeProvider(files)
    token_handler = FakeTokenHandler(prompt_tokens=100)
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1_700)

    try:
        prepared = pr_processing.get_pr_diff(
            provider,
            token_handler,
            prepared_model,
            add_line_numbers_to_hunks=prepared_line_numbers,
            return_remaining_files=True,
            return_prepared=True,
        )
        assert isinstance(prepared, pr_processing.PreparedPRDiff)

        pr_processing.get_pr_multi_diffs(
            provider,
            token_handler,
            requested_model,
            max_calls=3,
            add_line_numbers=requested_line_numbers,
            return_remaining_files=True,
            prepared_diff=prepared,
        )

        assert (provider.diff_calls, provider.language_calls) == (2, 2)
    finally:
        settings.config.verbosity_level = original_verbosity_level


@pytest.mark.parametrize(
    "call_diff",
    [
        lambda provider, token_handler: pr_processing.get_pr_diff(provider, token_handler, "model"),
        lambda provider, token_handler: pr_processing.get_pr_diff_multiple_patchs(provider, token_handler, "model"),
        lambda provider, token_handler: pr_processing.get_pr_multi_diffs(provider, token_handler, "model"),
    ],
)
def test_shared_diff_paths_propagate_project_rate_limit(call_diff):
    class RateLimitedProvider(FakeProvider):
        def get_diff_files(self):
            raise RateLimitExceeded("rate limit exceeded")

    with pytest.raises(RateLimitExceeded, match="rate limit exceeded"):
        call_diff(RateLimitedProvider([]), FakeTokenHandler())


def test_shared_diff_processing_does_not_import_pygithub_rate_limit_exception():
    tree = ast.parse(Path(pr_processing.__file__).read_text())

    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "github"
        and any(alias.name == "RateLimitExceededException" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_generate_full_patch_keeps_remaining_files_when_patch_exceeds_soft_budget():
    settings = get_settings()
    original_verbosity_level = settings.config.verbosity_level
    settings.config.verbosity_level = 0
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "small.py": {"patch": "+ small change", "tokens": 10, "edit_type": EDIT_TYPE.MODIFIED},
        "large.py": {"patch": "+ " + "large " * 80, "tokens": 250, "edit_type": EDIT_TYPE.MODIFIED},
        "second_small.py": {"patch": "+ second change", "tokens": 10, "edit_type": EDIT_TYPE.MODIFIED},
    }
    included_tokens = sum(
        token_handler.count_tokens(f"\n\n## File: '{filename}'\n\n{file_dict[filename]['patch'].strip()}\n")
        for filename in ("small.py", "second_small.py")
    )
    max_tokens_model = (
        pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD + token_handler.prompt_tokens + included_tokens
    )

    try:
        total_tokens, patches, remaining_files, files_in_patch = pr_processing.generate_full_patch(
            convert_hunks_to_line_numbers=False,
            file_dict=file_dict,
            max_tokens_model=max_tokens_model,
            remaining_files_list_prev=list(file_dict),
            token_handler=token_handler,
        )

        assert total_tokens > token_handler.prompt_tokens
        assert "## File: 'small.py'" in patches[0]
        assert "## File: 'second_small.py'" in patches[1]
        assert remaining_files == ["large.py"]
        assert files_in_patch == ["small.py", "second_small.py"]
    finally:
        settings.config.verbosity_level = original_verbosity_level


def test_generate_full_patch_records_files_after_hard_token_stop():
    class HardStopTokenHandler(FakeTokenHandler):
        def count_tokens(self, patch):
            raise AssertionError("hard-stopped patches must not be counted")

    token_handler = HardStopTokenHandler(prompt_tokens=2_001)
    file_dict = {
        "first.py": {"patch": "+ first change", "tokens": 1, "edit_type": EDIT_TYPE.MODIFIED},
        "hard_stop.py": {"patch": "+ hard stop change", "tokens": 1, "edit_type": EDIT_TYPE.MODIFIED},
        "after_stop.py": {"patch": "+ after stop change", "tokens": 1, "edit_type": EDIT_TYPE.MODIFIED},
    }

    total_tokens, patches, remaining_files, files_in_patch = pr_processing.generate_full_patch(
        convert_hunks_to_line_numbers=False,
        file_dict=file_dict,
        max_tokens_model=3_000,
        remaining_files_list_prev=list(file_dict),
        token_handler=token_handler,
    )

    assert total_tokens > 3_000 - pr_processing.OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD
    assert files_in_patch == []
    assert remaining_files == list(file_dict)
    assert patches == []


def test_generate_full_patch_records_too_large_patch_files():
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "included.py": {"patch": "+ included change", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
        "too_large.py": {"patch": "+ " + "large " * 5_000, "tokens": 5_000, "edit_type": EDIT_TYPE.MODIFIED},
        "after_large.py": {"patch": "+ after large change", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
    }

    total_tokens, patches, remaining_files, files_in_patch = pr_processing.generate_full_patch(
        convert_hunks_to_line_numbers=False,
        file_dict=file_dict,
        max_tokens_model=4_000,
        remaining_files_list_prev=list(file_dict),
        token_handler=token_handler,
    )

    assert total_tokens > token_handler.prompt_tokens
    assert files_in_patch == ["included.py", "after_large.py"]
    assert remaining_files == ["too_large.py"]
    assert len(patches) == 2


def test_get_all_models_uses_requested_model_type_and_string_fallbacks():
    settings = get_settings()
    original = {
        "model": settings.config.model,
        "model_weak": settings.get("config.model_weak", None),
        "model_reasoning": settings.get("config.model_reasoning", None),
        "fallback_models": settings.get("config.fallback_models", []),
    }
    try:
        settings.config.model = "regular-model"
        settings.config.model_weak = "weak-model"
        settings.config.model_reasoning = "reasoning-model"
        settings.config.fallback_models = "fallback-a, fallback-b"

        assert pr_processing._get_all_models(ModelType.REGULAR) == ["regular-model", "fallback-a", "fallback-b"]
        assert pr_processing._get_all_models(ModelType.WEAK) == ["weak-model", "fallback-a", "fallback-b"]
        assert pr_processing._get_all_models(ModelType.REASONING) == ["reasoning-model", "fallback-a", "fallback-b"]
    finally:
        settings.config.model = original["model"]
        settings.config.model_weak = original["model_weak"]
        settings.config.model_reasoning = original["model_reasoning"]
        settings.config.fallback_models = original["fallback_models"]


def test_get_all_deployments_rejects_short_fallback_deployment_list():
    settings = get_settings()
    original_deployment_id = settings.get("openai.deployment_id", None)
    original_fallback_deployments = settings.get("openai.fallback_deployments", [])
    try:
        settings.set("openai.deployment_id", "primary")
        settings.set("openai.fallback_deployments", ["fallback-a"])

        with pytest.raises(ValueError, match="less than the number of models"):
            pr_processing._get_all_deployments(["model-a", "model-b", "model-c"])
    finally:
        settings.set("openai.deployment_id", original_deployment_id)
        settings.set("openai.fallback_deployments", original_fallback_deployments)


@pytest.mark.parametrize(("context_limit", "reserve"), [(1_700, None), (5_200, 5_000)])
def test_get_pr_multi_diffs_clips_large_patch_with_legacy_and_dynamic_reserve(
    monkeypatch, context_limit, reserve
):
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

    file_info = FilePatchInfo(
        base_file="old\n",
        head_file="new\n",
        patch="@@ -1 +1 @@\n-old\n+" + ("new " * 200),
        filename="large.py",
        edit_type=EDIT_TYPE.MODIFIED,
    )
    provider = FakeProvider([file_info])
    token_handler = FakeTokenHandler(prompt_tokens=100)
    clip_budgets = []

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: context_limit)

    def clip_patch(patch, token_budget, **kwargs):
        clip_budgets.append(token_budget)
        return "clipped patch"

    monkeypatch.setattr(pr_processing, "clip_tokens", clip_patch)

    try:
        reserve_kwargs = (
            {"output_token_reserve": lambda model, default: reserve} if reserve is not None else {}
        )
        diffs = pr_processing.get_pr_multi_diffs(
            provider,
            token_handler,
            "tiny-model",
            max_calls=2,
            add_line_numbers=False,
            **reserve_kwargs,
        )

        assert diffs == ["clipped patch"]
        expected_reserve = reserve if reserve is not None else pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
        assert clip_budgets == [context_limit - expected_reserve - token_handler.prompt_tokens]
    finally:
        settings.config.patch_extra_lines_before = original["patch_extra_lines_before"]
        settings.config.patch_extra_lines_after = original["patch_extra_lines_after"]
        settings.config.large_patch_policy = original["large_patch_policy"]
        settings.config.verbosity_level = original["verbosity_level"]


def test_get_pr_multi_diffs_reports_the_files_the_token_budget_left_out(monkeypatch):
    # /review needs the same coverage list get_pr_diff returns, so the review footer can name
    # the files that were dropped even when the diff was reviewed in chunks.
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

    def _file(filename, patch):
        return FilePatchInfo(base_file="old\n", head_file="new\n", patch=patch,
                             filename=filename, edit_type=EDIT_TYPE.MODIFIED)

    hunk = "@@ -1 +1 @@\n-old\n+" + ("alpha " * 60)
    deleted = FilePatchInfo(base_file="old\n", head_file="", patch="@@ -1 +0,0 @@\n-old",
                            filename="deleted.py", edit_type=EDIT_TYPE.DELETED)
    files = [_file("first.py", hunk), _file("second.py", hunk), _file("no_patch.py", ""), deleted]
    provider = FakeProvider(files)
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1700)

    try:
        diffs, remaining_files = pr_processing.get_pr_multi_diffs(
            provider, token_handler, "tiny-model", max_calls=1, add_line_numbers=False,
            return_remaining_files=True
        )

        assert len(diffs) == 1
        assert "first.py" in diffs[0]
        # second.py did not fit within max_calls; no_patch.py and deleted.py have nothing to
        # review, so they are not something the token budget left out
        assert remaining_files == ["second.py"]
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)


def test_get_pr_multi_diffs_reports_no_remaining_files_when_the_whole_diff_fits(monkeypatch):
    settings = get_settings()
    original_before = settings.config.patch_extra_lines_before
    original_after = settings.config.patch_extra_lines_after
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0

    file_info = FilePatchInfo(base_file="old\n", head_file="new\n", patch="@@ -1 +1 @@\n-old\n+new",
                              filename="small.py", edit_type=EDIT_TYPE.MODIFIED)
    provider = FakeProvider([file_info])
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 100000)

    try:
        diffs, remaining_files = pr_processing.get_pr_multi_diffs(
            provider, token_handler, "big-model", add_line_numbers=False, return_remaining_files=True
        )

        assert len(diffs) == 1
        assert remaining_files == []
    finally:
        settings.config.patch_extra_lines_before = original_before
        settings.config.patch_extra_lines_after = original_after


def test_pr_description_reads_fall_back_when_keys_missing():
    # Regression for "'DynaBox' object has no attribute 'enable_large_pr_handling'":
    # custom_merge_loader replaces a section instead of merging it, so a custom
    # .pr_agent.toml that defines [pr_description] without the large-PR keys drops
    # their defaults. /describe must still work via .get(..., default) instead of crashing.
    from dynaconf.utils.boxing import DynaBox

    # A [pr_description] section overridden without the large-PR keys
    pr_description = DynaBox({"publish_labels": False})

    # Bare attribute access is what used to raise and abort the run
    with pytest.raises(AttributeError):
        _ = pr_description.enable_large_pr_handling

    # Guarded reads (matching the call sites) resolve to the documented defaults
    assert pr_description.get("enable_large_pr_handling", True) is True
    assert pr_description.get("async_ai_calls", True) is True
    assert pr_description.get("max_ai_calls", 4) == 4
