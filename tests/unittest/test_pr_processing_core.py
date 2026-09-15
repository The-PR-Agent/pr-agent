import ast
from pathlib import Path

import pytest

import pr_agent.algo.pr_processing as pr_processing
import pr_agent.algo.token_budget as token_budget
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


class CharacterTokenHandler(FakeTokenHandler):
    def count_tokens(self, patch):
        self.count_calls += 1
        return len(patch)


class NonMonotoneTokenHandler(FakeTokenHandler):
    def count_tokens(self, patch):
        self.count_calls += 1
        letters = "".join(
            line for line in patch.splitlines() if line in {"A", "B", "C", "D", "E"}
        )
        prefix_counts = {
            "A": 1,
            "AB": 6,
            "ABC": 4,
            "ABCD": 4,
            "ABCDE": 6,
            "BCD": 3,
            "BCDE": 6,
        }
        return prefix_counts.get(letters, len(letters))


def _rendered_budget_files():
    lines = [
        'F6[SZCg 3utmp{/(o8HXGWIwSROm2l(ULv"2d":{',
        "E'57U/iCFe V9\\'2t:_JBD=d4W{il2T'zdAMWM)]",
    ]
    return [
        FilePatchInfo("old\n", lines[i % 2] + "\n",
                      "@@ -1 +1 @@\n-old\n+" + lines[i % 2] + "\n",
                      f"file_{i}.py", edit_type=EDIT_TYPE.MODIFIED)
        for i in range(4)
    ]


@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_extended_diff_total_counts_the_rendered_join(add_line_numbers):
    handler = CharacterTokenHandler(prompt_tokens=11)
    patches, total, per_patch = pr_processing.pr_generate_extended_diff(
        [{"files": _rendered_budget_files()}], handler, add_line_numbers,
    )

    assert per_patch == [handler.count_tokens(patch) for patch in patches]
    assert total == handler.prompt_tokens + handler.count_tokens("\n".join(patches))


@pytest.mark.parametrize("packing_path", ["single", "multi"])
@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_fast_path_rejects_a_join_that_exceeds_the_reserved_limit(
    monkeypatch, packing_path, add_line_numbers,
):
    handler = CharacterTokenHandler(prompt_tokens=11)
    patches, _, individual_counts = pr_processing.pr_generate_extended_diff(
        [{"files": _rendered_budget_files()}], handler, add_line_numbers,
    )
    reserve = 5_000
    limit = handler.prompt_tokens + sum(individual_counts) + reserve + 1
    assert handler.prompt_tokens + handler.count_tokens("\n".join(patches)) + reserve > limit
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda langs, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "extend_patch", lambda original, patch, *args, **kwargs: patch)
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: limit)
    monkeypatch.setattr(
        pr_processing, "pr_generate_compressed_diff",
        lambda *args, **kwargs: ([["compressed"]], [12], [], [], {}, [[]]),
    )
    monkeypatch.setattr(pr_processing, "_pack_pr_multi_diffs", lambda *args: ["compressed"])

    if packing_path == "single":
        result = pr_processing.get_pr_diff(
            FakeProvider(_rendered_budget_files()), handler, "model",
            add_line_numbers_to_hunks=add_line_numbers,
            output_token_reserve=lambda model, default: reserve,
        )
        assert result == "compressed"
    else:
        result = pr_processing.get_pr_multi_diffs(
            FakeProvider(_rendered_budget_files()), handler, "model",
            add_line_numbers=add_line_numbers,
            output_token_reserve=lambda model, default: reserve,
        )
        assert result == ["compressed"]


def test_real_encoder_extended_diff_total_includes_non_additive_join(monkeypatch):
    import tiktoken

    encoder = tiktoken.get_encoding("o200k_base")

    class EncoderHandler(FakeTokenHandler):
        def count_tokens(self, patch):
            return len(encoder.encode(patch))

    handler = EncoderHandler(prompt_tokens=11)
    patches, total, per_patch = pr_processing.pr_generate_extended_diff(
        [{"files": _rendered_budget_files()}], handler, False,
    )
    rendered_count = handler.count_tokens("\n".join(patches))
    assert rendered_count > sum(per_patch)
    assert total == handler.prompt_tokens + rendered_count

    reserve = 1_500
    limit = handler.prompt_tokens + sum(per_patch) + reserve
    file_dict = {
        file.filename: {"patch": file.patch, "tokens": handler.count_tokens(file.patch),
                        "edit_type": file.edit_type}
        for file in _rendered_budget_files()
    }
    _, compressed, remaining, _ = pr_processing.generate_full_patch(
        False,
        file_dict,
        limit - reserve - handler.prompt_tokens,
        list(file_dict),
        handler,
        hard_token_budget=limit - 1_000 - handler.prompt_tokens,
    )
    assert remaining
    assert handler.prompt_tokens + handler.count_tokens("\n".join(compressed)) + reserve <= limit

    transformed = {
        name: {"patch": patch, "tokens": tokens}
        for name, patch, tokens in zip(file_dict, patches, per_patch, strict=True)
    }
    chunks = pr_processing._pack_pr_multi_diffs(
        transformed, handler, 5, False, limit - reserve - handler.prompt_tokens,
    )
    assert len(chunks) > 1
    assert all(handler.prompt_tokens + handler.count_tokens(chunk) + reserve <= limit for chunk in chunks)


@pytest.mark.parametrize("convert_line_numbers", [False, True])
@pytest.mark.parametrize("overflow", [0, 1])
def test_compressed_packing_counts_join_before_admitting_file(convert_line_numbers, overflow):
    handler = CharacterTokenHandler(prompt_tokens=11)
    file_dict = {
        name: {"patch": "+ alpha", "tokens": 7, "edit_type": EDIT_TYPE.MODIFIED}
        for name in ["a.py", "b.py"]
    }
    rendered = [
        "\n\n+ alpha" if convert_line_numbers else f"\n\n## File: '{name}'\n\n+ alpha\n"
        for name in file_dict
    ]
    reserve = 1_500
    limit = handler.prompt_tokens + handler.count_tokens("\n".join(rendered)) + reserve - overflow

    total, patches, remaining, included = pr_processing.generate_full_patch(
        convert_line_numbers,
        file_dict,
        limit - reserve - handler.prompt_tokens,
        list(file_dict),
        handler,
        hard_token_budget=limit - 1_000 - handler.prompt_tokens,
    )

    assert total == handler.prompt_tokens + handler.count_tokens("\n".join(patches))
    assert total + reserve <= limit
    assert included == (["a.py", "b.py"] if overflow == 0 else ["a.py"])
    assert remaining == ([] if overflow == 0 else ["b.py"])


@pytest.mark.parametrize("overflow", [0, 1])
@pytest.mark.parametrize("max_calls", [1, 2])
def test_multi_packing_recounts_non_additive_join_and_preserves_remaining(
    monkeypatch, overflow, max_calls,
):
    class NonAdditiveHandler(CharacterTokenHandler):
        def count_tokens(self, patch):
            return len(patch) + (3 if "\n" in patch else 0)

    handler = NonAdditiveHandler(prompt_tokens=11)
    reserve = 1_500
    limit = handler.prompt_tokens + handler.count_tokens("A\nB") + reserve - overflow
    file_dict = {name: {"patch": patch, "tokens": 1} for name, patch in [("a.py", "A"), ("b.py", "B")]}

    chunks, remaining = pr_processing._pack_pr_multi_diffs(
        file_dict, handler, max_calls, True, limit - reserve - handler.prompt_tokens,
    )

    assert all(handler.prompt_tokens + handler.count_tokens(chunk) + reserve <= limit for chunk in chunks)
    assert chunks == (["A\nB"] if overflow == 0 else ["A", "B"][:max_calls])
    assert remaining == (["b.py"] if overflow and max_calls == 1 else [])


@pytest.mark.parametrize(
    ("max_calls", "expected_chunks", "expected_remaining"),
    [
        (2, ["A", "B\nC\nD"], ["e.py"]),
        (3, ["A", "B\nC\nD", "E"], []),
    ],
)
def test_multi_packing_repairs_non_monotone_prefixes_in_order(
    max_calls, expected_chunks, expected_remaining,
):
    handler = NonMonotoneTokenHandler()
    file_dict = {
        f"{patch.lower()}.py": {"patch": patch, "tokens": 1}
        for patch in "ABCDE"
    }

    chunks, remaining = pr_processing._pack_pr_multi_diffs(
        file_dict, handler, max_calls, True, token_budget=5,
    )

    assert chunks == expected_chunks
    assert remaining == expected_remaining
    assert all(handler.count_tokens(chunk) <= 5 for chunk in chunks)


def test_compressed_packing_repairs_non_monotone_prefixes_in_order():
    handler = NonMonotoneTokenHandler(prompt_tokens=11)
    file_dict = {
        f"{patch.lower()}.py": {
            "patch": patch,
            "tokens": 1,
            "edit_type": EDIT_TYPE.MODIFIED,
        }
        for patch in "ABCDE"
    }

    total, patches, remaining, included = pr_processing.generate_full_patch(
        True,
        file_dict,
        soft_token_budget=5,
        remaining_files_list_prev=list(file_dict),
        token_handler=handler,
        hard_token_budget=5,
    )

    assert total == handler.prompt_tokens + 1
    assert patches == ["\n\nA"]
    assert included == ["a.py"]
    assert remaining == ["b.py", "c.py", "d.py", "e.py"]


def test_extended_diff_counts_trimmed_rendering():
    class StripSensitiveHandler(CharacterTokenHandler):
        def count_tokens(self, patch):
            return 100 if patch and patch == patch.strip() else super().count_tokens(patch)

    handler = StripSensitiveHandler(prompt_tokens=11)
    _, total, _ = pr_processing.pr_generate_extended_diff(
        [{"files": [_rendered_budget_files()[0]]}], handler, False,
    )

    assert total == handler.prompt_tokens + 100


def test_compressed_packing_rejects_trimmed_overflow():
    class StripSensitiveHandler(CharacterTokenHandler):
        def count_tokens(self, patch):
            return 100 if patch == "AB" else super().count_tokens(patch)

    handler = StripSensitiveHandler(prompt_tokens=0)
    total, patches, remaining, included = pr_processing.generate_full_patch(
        True,
        {"a.py": {"patch": "AB", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED}},
        soft_token_budget=4,
        remaining_files_list_prev=["a.py"],
        token_handler=handler,
        hard_token_budget=4,
    )

    assert total == 0
    assert patches == []
    assert remaining == ["a.py"]
    assert included == []


@pytest.mark.parametrize("policy", ["skip", "clip"])
def test_multi_packing_does_not_assume_stripping_reduces_tokens(monkeypatch, policy):
    class StripSensitiveHandler(CharacterTokenHandler):
        def count_tokens(self, patch):
            return 100 if patch == "AB" else len(patch)

    handler = StripSensitiveHandler(prompt_tokens=0)
    settings = get_settings()
    original_policy = settings.config.get("large_patch_policy", "skip")
    settings.config.large_patch_policy = policy
    monkeypatch.setattr(pr_processing, "clip_tokens", lambda patch, *args, **kwargs: patch)
    try:
        chunks, remaining = pr_processing._pack_pr_multi_diffs(
            {"a.py": {"patch": " AB ", "tokens": 4}}, handler, 2, True, 4,
        )
    finally:
        settings.config.large_patch_policy = original_policy

    assert chunks == []
    assert remaining == ["a.py"]


def test_multi_packing_clips_with_exact_single_patch_count(monkeypatch):
    class StripSensitiveHandler(CharacterTokenHandler):
        def count_tokens(self, patch):
            return 100 if patch == "AB" else len(patch)

    observed_counts = []

    def clip_with_reported_count(patch, max_tokens, *, num_input_tokens, **kwargs):
        observed_counts.append(num_input_tokens)
        return patch if num_input_tokens <= max_tokens else "A"

    handler = StripSensitiveHandler(prompt_tokens=0)
    settings = get_settings()
    original_policy = settings.config.get("large_patch_policy", "skip")
    settings.config.large_patch_policy = "clip"
    monkeypatch.setattr(pr_processing, "clip_tokens", clip_with_reported_count)
    try:
        chunks, remaining = pr_processing._pack_pr_multi_diffs(
            {"a.py": {"patch": " AB ", "tokens": 4}}, handler, 2, True, 4,
        )
    finally:
        settings.config.large_patch_policy = original_policy

    assert chunks == ["A"]
    assert remaining == []
    assert observed_counts == [100]


def test_fresh_and_prepared_multi_diffs_fit_the_same_rendered_boundary(monkeypatch):
    handler = CharacterTokenHandler(prompt_tokens=11)
    files = _rendered_budget_files()
    patches, _, _ = pr_processing.pr_generate_extended_diff([{"files": files}], handler, True)
    reserve = 5_000
    limit = handler.prompt_tokens + handler.count_tokens("\n".join(patches[:2])) + reserve - 1
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: limit)
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda langs, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "extend_patch", lambda original, patch, *args, **kwargs: patch)
    provider = FakeProvider(files)
    prepared = pr_processing.get_pr_diff(
        provider, handler, "model", add_line_numbers_to_hunks=True, return_prepared=True,
        output_token_reserve=lambda model, default: reserve,
    )
    assert prepared.file_dict

    prepared_chunks = pr_processing.get_pr_multi_diffs(
        provider, handler, "model", prepared_diff=prepared, return_remaining_files=True,
        output_token_reserve=lambda model, default: reserve,
    )
    fresh_chunks = pr_processing.get_pr_multi_diffs(
        FakeProvider(_rendered_budget_files()), handler, "model", return_remaining_files=True,
        output_token_reserve=lambda model, default: reserve,
    )

    assert prepared_chunks == fresh_chunks
    chunks, remaining = prepared_chunks
    assert not remaining
    assert len(chunks) > 1
    assert all(handler.prompt_tokens + handler.count_tokens(chunk) + reserve <= limit for chunk in chunks)
    assert provider.diff_calls == 1


@pytest.mark.parametrize(
    ("max_tokens", "expected_diff", "expected_tokens"),
    [
        (9, "base", 7),
        (10, "base\n\nx", 10),
    ],
)
def test_append_metadata_section_reserves_exact_separator_capacity(
    max_tokens, expected_diff, expected_tokens
):
    token_handler = CharacterTokenHandler(prompt_tokens=3)

    final_diff, curr_token, _ = pr_processing._append_metadata_section(
        "base", 7, "x", max_tokens, token_handler
    )

    assert final_diff == expected_diff
    assert curr_token == expected_tokens


def test_append_metadata_section_omits_non_additive_clipped_candidate(monkeypatch):
    class NonAdditiveTokenHandler(CharacterTokenHandler):
        candidate_counts = 0

        def count_tokens(self, patch):
            if patch.startswith("A\n\n"):
                self.candidate_counts += 1
            tokens = super().count_tokens(patch)
            return tokens + 2 if patch.startswith("A\n\n") else tokens

    clip_calls = 0

    def clip_with_marker(text, max_tokens, **kwargs):
        nonlocal clip_calls
        clip_calls += 1
        if len(text) <= max_tokens:
            return text
        return text[:max(0, max_tokens - 1)] + "…"

    monkeypatch.setattr(pr_processing, "clip_tokens", clip_with_marker)
    token_handler = NonAdditiveTokenHandler(prompt_tokens=0)

    final_diff, curr_token, clipped = pr_processing._append_metadata_section(
        "A", 1, "BBBB", 6, token_handler
    )

    assert clipped == ""
    assert final_diff == "A"
    assert curr_token == 1
    assert clip_calls == 1
    assert token_handler.candidate_counts == 1


def test_append_metadata_section_omits_trimmed_overflow():
    class StripSensitiveHandler(CharacterTokenHandler):
        def count_tokens(self, patch):
            return 100 if patch == "A\n\nB" else super().count_tokens(patch)

    token_handler = StripSensitiveHandler(prompt_tokens=0)
    final_diff, curr_token, clipped = pr_processing._append_metadata_section(
        " A", 2, "B", 8, token_handler,
    )

    assert clipped == ""
    assert final_diff == " A"
    assert curr_token == 2


def test_append_metadata_sections_keep_complete_diff_within_budget():
    token_handler = CharacterTokenHandler(prompt_tokens=3)
    max_tokens = token_handler.prompt_tokens + len("base\n\naa\n\nbb")
    final_diff = "base"
    curr_token = token_handler.prompt_tokens + token_handler.count_tokens(final_diff)

    for section in ("aa", "bb"):
        final_diff, curr_token, _ = pr_processing._append_metadata_section(
            final_diff, curr_token, section, max_tokens, token_handler
        )

    assert final_diff == "base\n\naa\n\nbb"
    assert curr_token == token_handler.prompt_tokens + token_handler.count_tokens(final_diff)
    assert curr_token == max_tokens


@pytest.mark.parametrize(
    ("edit_type", "heading"),
    [
        (EDIT_TYPE.ADDED, pr_processing.ADDED_FILES_.strip()),
        (EDIT_TYPE.MODIFIED, pr_processing.MORE_MODIFIED_FILES_.strip()),
        (EDIT_TYPE.RENAMED, pr_processing.MORE_MODIFIED_FILES_.strip()),
        (EDIT_TYPE.DELETED, pr_processing.DELETED_FILES_.strip()),
    ],
)
def test_get_pr_diff_routes_each_metadata_type_through_bounded_append(
    monkeypatch, edit_type, heading
):
    token_handler = CharacterTokenHandler(prompt_tokens=0)
    file_dict = {"metadata.py": {"edit_type": edit_type}}
    appended_sections = []
    original_append_metadata_section = pr_processing._append_metadata_section

    def append_metadata_section(*args, **kwargs):
        appended_sections.append(args[2])
        return original_append_metadata_section(*args, **kwargs)

    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 1_500)
    monkeypatch.setattr(pr_processing, "_append_metadata_section", append_metadata_section)
    monkeypatch.setattr(
        pr_processing,
        "sort_files_by_main_languages",
        lambda languages, files: [{"files": files}],
    )
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_extended_diff",
        lambda *args, **kwargs: (["full diff"], 1_500, []),
    )
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_compressed_diff",
        lambda *args, **kwargs: ([["base"]], [1_498], [], [], file_dict, [[]]),
    )

    diff = pr_processing.get_pr_diff(
        FakeProvider([]),
        token_handler,
        "model",
        output_token_reserve=lambda model, default: 1,
    )

    assert heading in diff
    assert "metadata.py" in diff
    assert [section for section in appended_sections if section] == [f"{heading}\n\nmetadata.py"]
    assert token_handler.prompt_tokens + token_handler.count_tokens(diff) <= 500


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
    budget = token_budget.AttemptTokenBudget(
        "model", object(), object(), 10_000, output_token_reserve=lambda model, default: resolved,
    )

    assert budget.resolve_output_reserve(1_500, preserve_minimum=True) == 1_500


@pytest.mark.parametrize("default", [1_000, 1_500])
@pytest.mark.parametrize("resolved", [1, 100, 999, 1_000, 1_200, 1_499, 1_500, 5_000])
def test_output_token_reserve_keeps_the_legacy_minimum(default, resolved):
    budget = token_budget.AttemptTokenBudget(
        "model", object(), object(), 10_000, output_token_reserve=lambda model, fallback: resolved,
    )

    assert budget.resolve_output_reserve(default, preserve_minimum=True) == max(default, resolved)


@pytest.mark.parametrize("packing_path", ["single", "multi", "multiple_patchs", "prepared"])
def test_small_output_reserve_preserves_legacy_packing(monkeypatch, packing_path):
    monkeypatch.setattr(get_settings().config, "patch_extra_lines_before", 0)
    monkeypatch.setattr(get_settings().config, "patch_extra_lines_after", 0)
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 6_500)

    def pack(**kwargs):
        provider = FakeProvider(_make_budget_files())
        token_handler = FakeTokenHandler(prompt_tokens=100)
        if packing_path == "single":
            return pr_processing.get_pr_diff(provider, token_handler, "model", **kwargs)
        if packing_path == "multiple_patchs":
            return pr_processing.get_pr_diff_multiple_patchs(provider, token_handler, "model", **kwargs)
        if packing_path == "prepared":
            prepared = pr_processing.get_pr_diff(
                provider, token_handler, "model", add_line_numbers_to_hunks=True,
                return_prepared=True, **kwargs,
            )
            return pr_processing.get_pr_multi_diffs(
                provider, token_handler, "model", prepared_diff=prepared, **kwargs
            )
        return pr_processing.get_pr_multi_diffs(provider, token_handler, "model", **kwargs)

    assert pack(output_token_reserve=lambda model, default: 100) == pack()


@pytest.mark.parametrize("packing_path", ["get_pr_diff", "get_pr_diff_multiple_patchs"])
def test_compressed_paths_keep_distinct_soft_and_hard_floors(monkeypatch, packing_path):
    monkeypatch.setattr(get_settings().config, "patch_extra_lines_before", 0)
    monkeypatch.setattr(get_settings().config, "patch_extra_lines_after", 0)
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 6_500)
    original_compressed_diff = pr_processing.pr_generate_compressed_diff
    reserves = []

    def compressed_diff(*args, **kwargs):
        reserves.append((args[2], args[3]))
        return original_compressed_diff(*args, **kwargs)

    monkeypatch.setattr(pr_processing, "pr_generate_compressed_diff", compressed_diff)
    getattr(pr_processing, packing_path)(
        FakeProvider(_make_budget_files()), FakeTokenHandler(prompt_tokens=100), "model",
        output_token_reserve=lambda model, default: 1_200,
    )

    assert reserves == [(4_900, 5_200)]


def test_output_token_reserve_falls_back_independently_when_one_resolution_fails():
    calls = []

    def resolve(model, default):
        calls.append((model, default))
        if default == pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD:
            return 5_000
        raise RuntimeError("hard reserve unavailable")

    budget = token_budget.AttemptTokenBudget("model", object(), object(), 10_000, output_token_reserve=resolve)

    assert budget.resolve_output_reserve(1_500, preserve_minimum=True) == 5_000
    assert budget.resolve_output_reserve(1_000, preserve_minimum=True) == 1_000
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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 10_000)

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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 10_000)
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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 10_000)
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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 10_000)

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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 10_000)
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


def test_direct_compressed_helpers_accept_explicit_numeric_budgets(monkeypatch):
    context_window = 10_000
    prompt_tokens = 100
    soft_token_budget = context_window - pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD - prompt_tokens
    hard_token_budget = context_window - pr_processing.OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD - prompt_tokens
    monkeypatch.setattr(
        token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: context_window
    )
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    file_dict = {
        "file.py": {"patch": "+ change", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }
    token_handler = FakeTokenHandler(prompt_tokens=prompt_tokens)

    total, patches, remaining_files, files_in_patch = pr_processing.generate_full_patch(
        False,
        file_dict,
        soft_token_budget,
        ["file.py"],
        token_handler,
        hard_token_budget=hard_token_budget,
    )

    expected_compressed = pr_processing.pr_generate_compressed_diff(
        [{"files": _make_budget_files(tokens_per_file=10)[:1]}],
        FakeTokenHandler(prompt_tokens),
        soft_token_budget,
        hard_token_budget,
        False,
        True,
    )
    actual_compressed = pr_processing.get_pr_diff_multiple_patchs(
        FakeProvider(_make_budget_files(tokens_per_file=10)[:1]),
        FakeTokenHandler(prompt_tokens),
        "model",
    )

    assert total == token_handler.prompt_tokens + token_handler.count_tokens("\n".join(patches))
    assert remaining_files == []
    assert files_in_patch == ["file.py"]
    assert actual_compressed == expected_compressed


def test_generate_full_patch_preserves_soft_boundary_equality():
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "file.py": {"patch": "+ exact boundary", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }
    rendered_tokens = token_handler.count_tokens("\n\n## File: 'file.py'\n\n+ exact boundary\n")
    _, _, remaining_files, files_in_patch = pr_processing.generate_full_patch(
        False,
        file_dict,
        rendered_tokens,
        ["file.py"],
        token_handler,
        hard_token_budget=rendered_tokens,
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
        4_000,
        ["file.py"],
        token_handler,
        hard_token_budget=0,
    )

    assert files_in_patch == ["file.py"]
    assert remaining_files == []


def test_pack_pr_multi_diffs_preserves_soft_boundary_equality():
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "file.py": {"patch": "exact boundary", "tokens": 2, "edit_type": EDIT_TYPE.MODIFIED},
    }
    chunks = pr_processing._pack_pr_multi_diffs(
        file_dict,
        token_handler,
        1,
        False,
        file_dict["file.py"]["tokens"],
    )

    assert chunks == ["exact boundary"]


@pytest.mark.parametrize(("extra_capacity", "uses_full_diff"), [(0, False), (1, True)])
@pytest.mark.parametrize("reserve", [100, 1_000, 1_200, 1_500, 5_000])
def test_get_pr_diff_preserves_strict_full_diff_boundary(monkeypatch, extra_capacity, uses_full_diff, reserve):
    settings = get_settings()
    original_before = settings.config.patch_extra_lines_before
    original_after = settings.config.patch_extra_lines_after
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    original_generate_extended_diff = pr_processing.pr_generate_extended_diff
    probe_handler = FakeTokenHandler(prompt_tokens=100)
    probe_files = _make_budget_files(tokens_per_file=1)[:1]
    probe_patches, probe_total, _ = original_generate_extended_diff(
        [{"files": probe_files}], probe_handler, False, patch_extra_lines_before=0, patch_extra_lines_after=0
    )
    full_diff = "\n".join(probe_patches)
    context_window = probe_total + max(reserve, 1_500) + extra_capacity

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(
        token_budget,
        "get_max_tokens",
        lambda model, ignore_max_model_tokens=False: context_window,
    )
    monkeypatch.setattr(
        pr_processing,
        "pr_generate_compressed_diff",
        lambda *args, **kwargs: ([["compressed"]], [101], [], [], {}, [[]]),
    )

    try:
        diff = pr_processing.get_pr_diff(
            FakeProvider(_make_budget_files(tokens_per_file=1)[:1]),
            FakeTokenHandler(prompt_tokens=100),
            "model",
            output_token_reserve=lambda model, default: reserve,
        )
    finally:
        settings.config.patch_extra_lines_before = original_before
        settings.config.patch_extra_lines_after = original_after

    assert diff == (full_diff if uses_full_diff else "compressed")


@pytest.mark.parametrize(("extra_capacity", "uses_full_diff"), [(0, False), (1, True)])
@pytest.mark.parametrize("reserve", [100, 1_000, 1_200, 1_500, 5_000])
def test_get_pr_multi_diffs_preserves_strict_full_diff_boundary(
    monkeypatch, extra_capacity, uses_full_diff, reserve,
):
    settings = get_settings()
    original_before = settings.config.patch_extra_lines_before
    original_after = settings.config.patch_extra_lines_after
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    packed_budgets = []
    original_generate_extended_diff = pr_processing.pr_generate_extended_diff
    probe_handler = FakeTokenHandler(prompt_tokens=100)
    probe_files = _make_budget_files(tokens_per_file=1)[:1]
    probe_patches, probe_total, _ = original_generate_extended_diff(
        [{"files": probe_files}], probe_handler, True, patch_extra_lines_before=0, patch_extra_lines_after=0
    )
    full_diff = "\n".join(probe_patches)
    context_window = probe_total + max(reserve, 1_500) + extra_capacity

    def pack_diffs(*args):
        packed_budgets.append(args[-1])
        return ["compressed"]

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(
        token_budget,
        "get_max_tokens",
        lambda model, ignore_max_model_tokens=False: context_window,
    )
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
            output_token_reserve=lambda model, default: reserve,
        )
    finally:
        settings.config.patch_extra_lines_before = original_before
        settings.config.patch_extra_lines_after = original_after

    assert chunks == ([full_diff] if uses_full_diff else ["compressed"])
    assert packed_budgets == ([] if uses_full_diff else [probe_total - probe_handler.prompt_tokens])


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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 1_700)

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

        def unexpected_preparation(*args, **kwargs):
            pytest.fail("Prepared patches must not be transformed again")

        with monkeypatch.context() as packing_patch:
            packing_patch.setattr(pr_processing, "extend_patch", unexpected_preparation)
            packing_patch.setattr(pr_processing, "handle_patch_deletions", unexpected_preparation)
            packing_patch.setattr(
                pr_processing, "decouple_and_convert_to_hunks_with_lines_numbers", unexpected_preparation,
            )
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
        # Bound exact candidate checks linearly while keeping preparation cached.
        assert 0 < token_handler.count_calls - calls_after_prepare <= 6 * len(prepared.file_dict)
        assert (provider.diff_calls, provider.language_calls) == (1, 1)
    finally:
        for key, value in original.items():
            setattr(settings.config, key, value)


def test_exact_packing_checks_stay_within_linear_bound_for_many_files():
    file_count = 400
    file_dict = {
        f"file_{index}.py": {
            "patch": f"patch {index}",
            "tokens": 2,
            "edit_type": EDIT_TYPE.MODIFIED,
        }
        for index in range(file_count)
    }

    chunk_handler = FakeTokenHandler(prompt_tokens=100)
    chunks = pr_processing._pack_pr_multi_diffs(
        file_dict,
        chunk_handler,
        max_calls=5,
        return_remaining_files=False,
        token_budget=180,
    )

    assert len(chunks) == 5
    assert all(chunk_handler.count_tokens(chunk) <= 180 for chunk in chunks)
    assert chunk_handler.count_calls < 3 * file_count

    compressed_handler = FakeTokenHandler(prompt_tokens=100)
    _, patches, remaining, included = pr_processing.generate_full_patch(
        False,
        file_dict,
        soft_token_budget=10_000,
        remaining_files_list_prev=list(file_dict),
        token_handler=compressed_handler,
        hard_token_budget=10_000,
    )

    assert len(patches) == file_count
    assert remaining == []
    assert len(included) == file_count
    assert compressed_handler.count_calls < 3 * file_count


def test_overflow_repair_bounds_encoded_volume_for_many_files():
    class RepairProbeTokenHandler(FakeTokenHandler):
        def __init__(self, prompt_tokens=100):
            super().__init__(prompt_tokens)
            self.encoded_characters = 0

        def count_tokens(self, patch):
            self.count_calls += 1
            self.encoded_characters += len(patch)
            non_additive_drift = 200 if patch.count("patch-") > 1 else 0
            return len(patch) + non_additive_drift

    file_count = 400
    patch_length = 40
    file_dict = {
        f"file_{index}.py": {
            "patch": f"patch-{index:03d}".ljust(patch_length, "x"),
            "tokens": patch_length,
            "edit_type": EDIT_TYPE.MODIFIED,
        }
        for index in range(file_count)
    }
    joined_length = file_count * patch_length + file_count - 1

    chunk_handler = RepairProbeTokenHandler()
    chunks = pr_processing._pack_pr_multi_diffs(
        file_dict,
        chunk_handler,
        max_calls=2,
        return_remaining_files=False,
        token_budget=joined_length,
    )

    assert len(chunks) == 2
    assert all(chunk_handler.count_tokens(chunk) <= joined_length for chunk in chunks)
    assert chunk_handler.encoded_characters < 12 * joined_length

    compressed_handler = RepairProbeTokenHandler()
    total, patches, remaining, included = pr_processing.generate_full_patch(
        True,
        file_dict,
        soft_token_budget=joined_length + 2 * file_count,
        remaining_files_list_prev=list(file_dict),
        token_handler=compressed_handler,
        hard_token_budget=joined_length + 2 * file_count,
    )
    rendered = "\n".join(patches)

    assert total == compressed_handler.prompt_tokens + compressed_handler.count_tokens(rendered)
    assert compressed_handler.count_tokens(rendered) <= joined_length + 2 * file_count
    assert remaining
    assert included
    # Exact repair checks both the raw and Jinja-trimmed serializations without recounting per admission.
    assert compressed_handler.encoded_characters < 24 * len(
        "\n".join("\n\n" + data["patch"] for data in file_dict.values())
    )


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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 1_700)

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
def test_shared_diff_paths_propagate_project_rate_limit(monkeypatch, call_diff):
    class RateLimitedProvider(FakeProvider):
        def get_diff_files(self):
            raise RateLimitExceeded("rate limit exceeded")

    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 10_000)

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
            soft_token_budget=(
                max_tokens_model
                - pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
                - token_handler.prompt_tokens
            ),
            remaining_files_list_prev=list(file_dict),
            token_handler=token_handler,
            hard_token_budget=(
                max_tokens_model
                - pr_processing.OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD
                - token_handler.prompt_tokens
            ),
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
        soft_token_budget=3_000 - 1_500 - token_handler.prompt_tokens,
        remaining_files_list_prev=list(file_dict),
        token_handler=token_handler,
        hard_token_budget=3_000 - 1_000 - token_handler.prompt_tokens,
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
        soft_token_budget=4_000 - 1_500 - token_handler.prompt_tokens,
        remaining_files_list_prev=list(file_dict),
        token_handler=token_handler,
        hard_token_budget=4_000 - 1_000 - token_handler.prompt_tokens,
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
    monkeypatch.setattr(
        token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: context_limit
    )

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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 1700)

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
    monkeypatch.setattr(token_budget, "get_max_tokens", lambda model, ignore_max_model_tokens=False: 100000)

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
