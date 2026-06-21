"""Tests for the diff / hunk pipeline.

Covers:
- pr_agent.algo.git_patch_processing.decouple_and_convert_to_hunks_with_lines_numbers
- pr_agent.algo.git_patch_processing.extract_hunk_lines_from_patch
- pr_agent.algo.pr_processing.generate_full_patch
- pr_agent.algo.pr_processing.pr_generate_compressed_diff

The tests document current behavior and assert on the key structural
markers (hunk headers, line numbers, selected lines, returned lists)
rather than on full golden strings, so they remain robust to minor
formatting tweaks.
"""

import pr_agent.algo.pr_processing as pr_processing
from pr_agent.algo.git_patch_processing import (
    decouple_and_convert_to_hunks_with_lines_numbers,
    extract_hunk_lines_from_patch,
)
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTokenHandler:
    """Deterministic token handler: 1 token per whitespace-split word."""

    def __init__(self, prompt_tokens: int = 100):
        self.prompt_tokens = prompt_tokens

    def count_tokens(self, patch: str) -> int:
        return len(patch.split())


MULTI_HUNK_PATCH = (
    "@@ -1,3 +1,4 @@\n"
    " line1\n"
    "-line2\n"
    "+line2_new\n"
    "+line2b\n"
    " line3\n"
    "@@ -10,3 +11,3 @@\n"
    " ctx_a\n"
    "-removed\n"
    "+added\n"
    " ctx_b\n"
)


def _make_file(filename="src/sample.py", patch=MULTI_HUNK_PATCH,
               edit_type=EDIT_TYPE.MODIFIED, tokens=10,
               base="orig", head="new"):
    return FilePatchInfo(
        base_file=base,
        head_file=head,
        patch=patch,
        filename=filename,
        tokens=tokens,
        edit_type=edit_type,
    )


# ---------------------------------------------------------------------------
# decouple_and_convert_to_hunks_with_lines_numbers
# ---------------------------------------------------------------------------


class TestDecoupleAndConvertToHunks:
    def test_multi_hunk_emits_both_hunks_with_new_and_old_sections(self):
        file = _make_file()
        out = decouple_and_convert_to_hunks_with_lines_numbers(MULTI_HUNK_PATCH, file)

        # File header is present.
        assert "## File: 'src/sample.py'" in out
        # Both hunk headers are preserved.
        assert "@@ -1,3 +1,4 @@" in out
        assert "@@ -10,3 +11,3 @@" in out
        # Each hunk produces a __new hunk__ section; modified hunks produce __old hunk__ too.
        assert out.count("__new hunk__") == 2
        assert out.count("__old hunk__") == 2

    def test_new_hunk_lines_are_numbered_starting_at_start2(self):
        file = _make_file()
        out = decouple_and_convert_to_hunks_with_lines_numbers(MULTI_HUNK_PATCH, file)

        # First hunk starts at +1 in the new file; context line " line1" -> "1  line1".
        assert "1  line1" in out
        # The inserted lines get the next numbers (2, 3) in the new file.
        assert "2 +line2_new" in out
        assert "3 +line2b" in out
        assert "4  line3" in out

        # Second hunk starts at +11 in the new file (we set start2=11).
        # Layout per implementation: context, then '+added' (replacing '-removed'),
        # then trailing context. The '-' line is not numbered in __new hunk__.
        assert "11  ctx_a" in out
        assert "12 +added" in out

    def test_old_hunk_contains_removed_and_context_lines_unnumbered(self):
        file = _make_file()
        out = decouple_and_convert_to_hunks_with_lines_numbers(MULTI_HUNK_PATCH, file)

        old_section = out.split("__old hunk__", 1)[1]
        # The first __old hunk__ contains the removed line and context.
        assert "-line2" in old_section
        assert " line1" in old_section
        # Old hunk lines do NOT have numeric prefixes — assert no "1 -line2" style line.
        for line in old_section.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("-", "+", " ")) and stripped == line:
                # current implementation does not prefix old-hunk lines with numbers
                assert not line[:1].isdigit()

    def test_deleted_file_short_circuits_with_message(self):
        file = _make_file(edit_type=EDIT_TYPE.DELETED)
        out = decouple_and_convert_to_hunks_with_lines_numbers(MULTI_HUNK_PATCH, file)
        assert "was deleted" in out
        assert "src/sample.py" in out
        # No hunk content should be emitted for deleted files.
        assert "__new hunk__" not in out
        assert "__old hunk__" not in out

    def test_pure_addition_hunk_emits_only_new_hunk_section(self):
        patch = (
            "@@ -0,0 +1,2 @@\n"
            "+brand new line 1\n"
            "+brand new line 2\n"
        )
        file = _make_file(patch=patch, edit_type=EDIT_TYPE.ADDED)
        out = decouple_and_convert_to_hunks_with_lines_numbers(patch, file)

        assert "__new hunk__" in out
        assert "__old hunk__" not in out
        # Numbering: implementation falls back to start2=0 for "@@ -0,0 +1 @@"-style
        # headers, but here header has explicit "+1,2" so start2=1.
        assert "1 +brand new line 1" in out
        assert "2 +brand new line 2" in out

    def test_no_file_arg_omits_file_header(self):
        out = decouple_and_convert_to_hunks_with_lines_numbers(MULTI_HUNK_PATCH, file=None)
        assert "## File:" not in out
        assert "@@ -1,3 +1,4 @@" in out
        assert "__new hunk__" in out


# ---------------------------------------------------------------------------
# extract_hunk_lines_from_patch
# ---------------------------------------------------------------------------


class TestExtractHunkLinesFromPatch:
    def test_right_side_single_line_selection_in_first_hunk(self):
        # In MULTI_HUNK_PATCH new-file numbering:
        #   1 " line1"
        #   2 "+line2_new"
        #   3 "+line2b"
        #   4 " line3"
        full, selected = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", line_start=2, line_end=2, side="right"
        )
        assert "## File: 'src/sample.py'" in full
        assert "@@ -1,3 +1,4 @@" in full
        # The second hunk's header should NOT be in `full` since its range
        # does not contain line_start=2.
        assert "@@ -10,3 +11,3 @@" not in full
        # Current production behavior includes the paired removed line before
        # the targeted inserted line; assert exactly so this test cannot pass
        # while silently selecting additional wrong-side/context lines.
        assert selected == "-line2\n+line2_new"

    def test_right_side_range_selection_across_consecutive_lines(self):
        full, selected = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", line_start=2, line_end=3, side="right"
        )
        assert selected == "-line2\n+line2_new\n+line2b"

    def test_left_side_selects_from_old_line_numbers(self):
        # Old file numbering in first hunk starts at 1; "-line2" is old-line 2.
        full, selected = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", line_start=2, line_end=2, side="left"
        )
        assert "@@ -1,3 +1,4 @@" in full
        # Current production behavior includes adjacent context/paired new
        # lines around the deleted line; assert exactly so this test documents
        # the full selected payload rather than only a partial match.
        assert selected == " line1\n-line2\n+line2_new"

    def test_targets_second_hunk_when_line_in_its_range(self):
        # Second hunk new-file range: start2=11, size2=3 -> lines 11..14.
        full, selected = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", line_start=12, line_end=12, side="right"
        )
        assert "@@ -10,3 +11,3 @@" in full
        assert "@@ -1,3 +1,4 @@" not in full
        assert selected == "-removed\n+added"

    def test_out_of_range_returns_only_header_and_empty_selection(self):
        full, selected = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", line_start=999, line_end=1000, side="right"
        )
        # Neither hunk matched, so no hunk headers are emitted.
        assert "@@" not in full
        assert "## File: 'src/sample.py'" in full
        assert selected == ""

    def test_malformed_patch_returns_empty_tuple(self):
        # An '@@' line that does not match RE_HUNK_HEADER causes the
        # implementation to raise inside extract_hunk_headers; the function
        # catches it and returns ("", "").
        bad_patch = "@@ not a real header @@\n+something\n"
        full, selected = extract_hunk_lines_from_patch(
            bad_patch, "src/sample.py", line_start=1, line_end=1, side="right"
        )
        assert full == ""
        assert selected == ""

    def test_remove_trailing_chars_false_preserves_trailing_newlines(self):
        full_stripped, sel_stripped = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", 2, 2, "right", remove_trailing_chars=True
        )
        full_raw, sel_raw = extract_hunk_lines_from_patch(
            MULTI_HUNK_PATCH, "src/sample.py", 2, 2, "right", remove_trailing_chars=False
        )
        assert full_raw.endswith("\n")
        assert sel_raw.endswith("\n")
        # Trimmed variants are strict suffixes (no trailing whitespace).
        assert full_stripped == full_raw.rstrip()
        assert sel_stripped == sel_raw.rstrip()


# ---------------------------------------------------------------------------
# generate_full_patch
# ---------------------------------------------------------------------------


class TestGenerateFullPatch:
    def test_files_within_budget_are_all_included(self, monkeypatch):
        monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)
        token_handler = FakeTokenHandler(prompt_tokens=10)
        file_dict = {
            "a.py": {"patch": "+ change a", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
            "b.py": {"patch": "+ change b", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
        }
        total, patches, remaining, files_in = pr_processing.generate_full_patch(
            convert_hunks_to_line_numbers=False,
            file_dict=file_dict,
            max_tokens_model=10_000,
            remaining_files_list_prev=list(file_dict),
            token_handler=token_handler,
        )
        assert files_in == ["a.py", "b.py"]
        assert remaining == []
        assert len(patches) == 2
        # File header format for non-line-numbered patches:
        assert any("## File: 'a.py'" in p for p in patches)
        assert any("## File: 'b.py'" in p for p in patches)
        assert total > token_handler.prompt_tokens

    def test_oversized_patch_is_deferred_to_remaining_list(self):
        token_handler = FakeTokenHandler(prompt_tokens=10)
        big_tokens = 5000  # exceeds (max_tokens - SOFT=1500) when added on top of prompt
        file_dict = {
            "small.py": {"patch": "+ small", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
            "huge.py":  {"patch": "+ huge",  "tokens": big_tokens, "edit_type": EDIT_TYPE.MODIFIED},
        }
        total, patches, remaining, files_in = pr_processing.generate_full_patch(
            convert_hunks_to_line_numbers=False,
            file_dict=file_dict,
            max_tokens_model=4_000,  # SOFT=1500, HARD=1000
            remaining_files_list_prev=list(file_dict),
            token_handler=token_handler,
        )
        assert "small.py" in files_in
        assert "huge.py" not in files_in
        assert remaining == ["huge.py"]

    def test_remaining_files_list_prev_filters_input(self):
        token_handler = FakeTokenHandler(prompt_tokens=10)
        file_dict = {
            "a.py": {"patch": "+ change a", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
            "b.py": {"patch": "+ change b", "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
        }
        total, patches, remaining, files_in = pr_processing.generate_full_patch(
            convert_hunks_to_line_numbers=False,
            file_dict=file_dict,
            max_tokens_model=10_000,
            remaining_files_list_prev=["b.py"],  # only b.py is eligible this round
            token_handler=token_handler,
        )
        assert files_in == ["b.py"]
        assert remaining == []

    def test_line_numbered_mode_omits_extra_file_header(self):
        token_handler = FakeTokenHandler(prompt_tokens=10)
        prebuilt = "## File: 'a.py'\n@@ -1 +1 @@\n+x"
        file_dict = {
            "a.py": {"patch": prebuilt, "tokens": 5, "edit_type": EDIT_TYPE.MODIFIED},
        }
        _, patches, _, _ = pr_processing.generate_full_patch(
            convert_hunks_to_line_numbers=True,
            file_dict=file_dict,
            max_tokens_model=10_000,
            remaining_files_list_prev=["a.py"],
            token_handler=token_handler,
        )
        # In line-numbered mode, the function does not wrap with another header.
        assert patches[0].count("## File: 'a.py'") == 1


# ---------------------------------------------------------------------------
# pr_generate_compressed_diff
# ---------------------------------------------------------------------------


class TestPrGenerateCompressedDiff:
    def _settings(self):
        from pr_agent.config_loader import get_settings
        return get_settings()

    def test_deleted_files_collected_and_excluded_from_patches(self, monkeypatch):
        monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)

        deleted = FilePatchInfo(
            base_file="old content",
            head_file="",
            patch="@@ -1,2 +0,0 @@\n-old1\n-old2\n",
            filename="gone.py",
            tokens=5,
            edit_type=EDIT_TYPE.DELETED,
        )
        kept = _make_file(filename="kept.py", tokens=5)
        top_langs = [{"files": [deleted, kept]}]

        (patches_list, total_tokens_list, deleted_files_list,
         remaining_files_list, file_dict, files_in_patches_list) = \
            pr_processing.pr_generate_compressed_diff(
                top_langs=top_langs,
                token_handler=FakeTokenHandler(prompt_tokens=10),
                model="some-model",
                convert_hunks_to_line_numbers=False,
                large_pr_handling=False,
            )

        assert "gone.py" in deleted_files_list
        assert "gone.py" not in file_dict
        assert "kept.py" in file_dict
        # First (and only) iteration carries kept.py and no remaining files.
        assert files_in_patches_list[0] == ["kept.py"]
        assert remaining_files_list == []
        assert len(patches_list) == 1
        assert len(total_tokens_list) == 1

    def test_large_pr_handling_paginates_across_iterations(self, monkeypatch):
        # Build patches large enough that exactly one fits per iteration. The
        # per-iteration budget in generate_full_patch is
        #   max_tokens_model - OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD - prompt_tokens
        # so we derive max_tokens from the actual token count of a patch (since
        # pr_generate_compressed_diff recomputes tokens from patch content via
        # token_handler.count_tokens, ignoring FilePatchInfo.tokens).
        prompt_tokens = 100
        token_handler = FakeTokenHandler(prompt_tokens=prompt_tokens)
        patch_str = "@@ -1,1 +1,1 @@\n+" + " ".join(["tok"] * 100) + "\n"
        patch_tokens = token_handler.count_tokens(patch_str)
        soft_threshold = pr_processing.OUTPUT_BUFFER_TOKENS_SOFT_THRESHOLD
        # Budget allows exactly one patch per iteration (prompt + patch fits,
        # but prompt + 2*patch does not):
        #   prompt + patch_tokens     <= max - SOFT
        #   prompt + 2 * patch_tokens >  max - SOFT
        max_tokens = soft_threshold + prompt_tokens + patch_tokens + 1
        monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: max_tokens)

        settings = self._settings()
        original_max_ai_calls = settings.pr_description.max_ai_calls
        # NUMBER_OF_ALLOWED_ITERATIONS = max_ai_calls - 1; loop runs range(that - 1).
        # We want 3 total iterations (1 mandatory + 2 in the loop) -> max_ai_calls = 4.
        settings.pr_description.max_ai_calls = 4

        try:
            files = [
                _make_file(filename=f"f{i}.py", tokens=5, patch=patch_str)
                for i in range(3)
            ]

            top_langs = [{"files": files}]
            (patches_list, total_tokens_list, deleted_files_list,
             remaining_files_list, file_dict, files_in_patches_list) = \
                pr_processing.pr_generate_compressed_diff(
                    top_langs=top_langs,
                    token_handler=token_handler,
                    model="some-model",
                    convert_hunks_to_line_numbers=False,
                    large_pr_handling=True,
                )

            # Pagination actually fired: 3 batches, one file each, nothing left over.
            assert len(patches_list) == 3
            assert len(patches_list) == len(total_tokens_list) == len(files_in_patches_list)
            assert files_in_patches_list == [["f0.py"], ["f1.py"], ["f2.py"]]
            assert remaining_files_list == []
            assert deleted_files_list == []
        finally:
            settings.pr_description.max_ai_calls = original_max_ai_calls

    def test_files_with_empty_patch_are_skipped(self, monkeypatch):
        monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)

        empty = _make_file(filename="empty.py", patch="", tokens=0)
        kept = _make_file(filename="kept.py", tokens=5)
        top_langs = [{"files": [empty, kept]}]

        (patches_list, _, deleted_files_list, remaining_files_list,
         file_dict, files_in_patches_list) = \
            pr_processing.pr_generate_compressed_diff(
                top_langs=top_langs,
                token_handler=FakeTokenHandler(prompt_tokens=10),
                model="some-model",
                convert_hunks_to_line_numbers=False,
                large_pr_handling=False,
            )

        assert "empty.py" not in file_dict
        assert "empty.py" not in deleted_files_list
        assert "kept.py" in file_dict
        assert files_in_patches_list[0] == ["kept.py"]

    def test_convert_hunks_to_line_numbers_runs_decouple_per_file(self, monkeypatch):
        monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 10_000)
        kept = _make_file(filename="kept.py", tokens=5)
        top_langs = [{"files": [kept]}]

        (patches_list, _, _, _, file_dict, files_in_patches_list) = \
            pr_processing.pr_generate_compressed_diff(
                top_langs=top_langs,
                token_handler=FakeTokenHandler(prompt_tokens=10),
                model="some-model",
                convert_hunks_to_line_numbers=True,
                large_pr_handling=False,
            )
        # Decoupled output marker should be present in the stored patch.
        assert "__new hunk__" in file_dict["kept.py"]["patch"]
        assert files_in_patches_list[0] == ["kept.py"]

    def test_max_ai_calls_boundary_caps_iterations(self, monkeypatch):
        # Force every patch to be too big to fit so each iteration defers
        # everything to the next round; this isolates the iteration cap.
        monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1_000)
        settings = self._settings()
        original_max_ai_calls = settings.pr_description.max_ai_calls
        settings.pr_description.max_ai_calls = 2  # allow 1 extra loop iteration (range(0))

        try:
            files = [_make_file(filename=f"f{i}.py", tokens=5,
                                patch=f"@@ -1 +1 @@\n+x_{i}\n")
                     for i in range(3)]
            top_langs = [{"files": files}]

            (patches_list, _, _, remaining_files_list, _, files_in_patches_list) = \
                pr_processing.pr_generate_compressed_diff(
                    top_langs=top_langs,
                    token_handler=FakeTokenHandler(prompt_tokens=10_000),
                    model="some-model",
                    convert_hunks_to_line_numbers=False,
                    large_pr_handling=True,
                )

            # The first (mandatory) iteration always appends one batch, even if empty.
            assert len(patches_list) >= 1
            # With max_ai_calls=2, NUMBER_OF_ALLOWED_ITERATIONS=1 and the loop body
            # executes range(0) -> zero times. So only the first iteration runs.
            assert len(patches_list) == 1
            assert len(files_in_patches_list) == 1
        finally:
            settings.pr_description.max_ai_calls = original_max_ai_calls
