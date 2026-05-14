import pytest

from pr_agent.algo.git_patch_processing import extend_patch
from pr_agent.algo.pr_processing import pr_generate_extended_diff
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import load_large_diff
from pr_agent.config_loader import get_settings

get_settings(use_context=False).set("CONFIG.CLI_MODE", True)
get_settings(use_context=False).config.allow_dynamic_context = False


class TestExtendPatch:
    # Tests that the function works correctly with valid input
    def test_happy_path(self):
        original_file_str = 'line1\nline2\nline3\nline4\nline5'
        patch_str = '@@ -2,2 +2,2 @@ init()\n-line2\n+new_line2\n line3'
        num_lines = 1
        expected_output = '\n@@ -1,4 +1,4 @@ init()\n line1\n-line2\n+new_line2\n line3\n line4'
        actual_output = extend_patch(original_file_str, patch_str,
                                     patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines)
        assert actual_output == expected_output

    # Tests that the function returns an empty string when patch_str is empty
    def test_empty_patch(self):
        original_file_str = 'line1\nline2\nline3\nline4\nline5'
        patch_str = ''
        num_lines = 1
        expected_output = ''
        assert extend_patch(original_file_str, patch_str,
                            patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines) == expected_output

    # Tests that the function returns the original patch when num_lines is 0
    def test_zero_num_lines(self):
        original_file_str = 'line1\nline2\nline3\nline4\nline5'
        patch_str = '@@ -2,2 +2,2 @@ init()\n-line2\n+new_line2\nline3'
        num_lines = 0
        assert extend_patch(original_file_str, patch_str,
                            patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines) == patch_str

    # Tests that the function returns the original patch when patch_str contains no hunks
    def test_no_hunks(self):
        original_file_str = 'line1\nline2\nline3\nline4\nline5'
        patch_str = 'no hunks here'
        num_lines = 1
        expected_output = 'no hunks here'
        assert extend_patch(original_file_str, patch_str, num_lines) == expected_output

    # Tests that the function extends a patch with a single hunk correctly
    def test_single_hunk(self):
        original_file_str = 'line1\nline2\nline3\nline4\nline5'
        patch_str = '@@ -2,3 +2,3 @@ init()\n-line2\n+new_line2\n line3\n line4'

        for num_lines in [1, 2, 3]: # check that even if we are over the number of lines in the file, the function still works
            expected_output = '\n@@ -1,5 +1,5 @@ init()\n line1\n-line2\n+new_line2\n line3\n line4\n line5'
            actual_output = extend_patch(original_file_str, patch_str,
                                         patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines)
            assert actual_output == expected_output

    # Tests the functionality of extending a patch with multiple hunks.
    def test_multiple_hunks(self):
        original_file_str = 'line1\nline2\nline3\nline4\nline5\nline6'
        patch_str = '@@ -2,3 +2,3 @@ init()\n-line2\n+new_line2\n line3\n line4\n@@ -4,1 +4,1 @@ init2()\n-line4\n+new_line4'  # noqa: E501
        num_lines = 1
        original_allow_dynamic_context = get_settings(use_context=False).config.allow_dynamic_context

        get_settings(use_context=False).config.allow_dynamic_context = False
        expected_output = '\n@@ -1,5 +1,5 @@ init()\n line1\n-line2\n+new_line2\n line3\n line4\n line5\n\n@@ -3,3 +3,3 @@ init2()\n line3\n-line4\n+new_line4\n line5' # noqa: E501
        actual_output = extend_patch(original_file_str, patch_str,
                                     patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines)
        assert actual_output == expected_output

        get_settings(use_context=False).config.allow_dynamic_context = True
        expected_output = '\n@@ -1,5 +1,5 @@ init()\n line1\n-line2\n+new_line2\n line3\n line4\n line5\n\n@@ -3,3 +3,3 @@ init2()\n line3\n-line4\n+new_line4\n line5' # noqa: E501
        actual_output = extend_patch(original_file_str, patch_str,
                                     patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines)
        assert actual_output == expected_output
        get_settings(use_context=False).config.allow_dynamic_context = original_allow_dynamic_context


    def test_dynamic_context(self):
        get_settings(use_context=False).config.max_extra_lines_before_dynamic_context = 10
        original_file_str = "def foo():"
        for i in range(9):
            original_file_str += f"\n    line({i})"
        patch_str ="@@ -10,1 +10,1 @@ def foo():\n-    line(8)\n+    new_line(8)"
        new_file_str = "\n".join(original_file_str.splitlines()[:-1] + ["    new_line(8)"])
        num_lines=1

        get_settings(use_context=False).config.allow_dynamic_context = True
        actual_output = extend_patch(original_file_str, patch_str,
                                     patch_extra_lines_before=num_lines, patch_extra_lines_after=num_lines, new_file_str=new_file_str)
        expected_output='\n@@ -1,10 +1,10 @@ \n def foo():\n     line(0)\n     line(1)\n     line(2)\n     line(3)\n     line(4)\n     line(5)\n     line(6)\n     line(7)\n-    line(8)\n+    new_line(8)'
        assert actual_output == expected_output

        get_settings(use_context=False).config.allow_dynamic_context = False
        actual_output2 = extend_patch(original_file_str, patch_str,
                                     patch_extra_lines_before=1, patch_extra_lines_after=1)
        expected_output_no_dynamic_context = '\n@@ -9,2 +9,2 @@ def foo():\n     line(7)\n-    line(8)\n+    new_line(8)'
        assert actual_output2 == expected_output_no_dynamic_context

        get_settings(use_context=False).config.allow_dynamic_context = False
        actual_output3 = extend_patch(original_file_str, patch_str,
                                     patch_extra_lines_before=3, patch_extra_lines_after=3)
        expected_output_no_dynamic_context = '\n@@ -7,4 +7,4 @@ def foo():\n     line(5)\n     line(6)\n     line(7)\n-    line(8)\n+    new_line(8)'
        assert actual_output3 == expected_output_no_dynamic_context





class TestExtendedPatchMoreLines:
    class File:
        def __init__(self, base_file, patch, head_file, filename, ai_file_summary=None):
            self.base_file = base_file
            self.patch = patch
            self.head_file = head_file
            self.filename = filename
            self.ai_file_summary = ai_file_summary

    @pytest.fixture
    def token_handler(self):
        # Create a TokenHandler instance with dummy data
        th = TokenHandler(system="System prompt", user="User prompt")
        th.prompt_tokens = 100
        return th

    @pytest.fixture
    def pr_languages(self):
        # Create a list of languages with files containing base_file and patch data
        return [
            {
                'files': [
                    self.File(base_file="line000\nline00\nline0\nline1\noriginal content\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10",
                              patch="@@ -5,5 +5,5 @@\n-original content\n+modified content\n line2\n line3\n line4\n line5",
                              head_file="line000\nline00\nline0\nline1\nmodified content\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10",
                              filename="file1"),
                    self.File(base_file="original content\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nline9\nline10",
                              patch="@@ -6,5 +6,5 @@\nline6\nline7\nline8\n-line9\n+modified line9\nline10",
                              head_file="original content\nline2\nline3\nline4\nline5\nline6\nline7\nline8\nmodified line9\nline10",
                              filename="file2")
                ]
            }
        ]

    def test_extend_patches_with_extra_lines(self, token_handler, pr_languages):
        patches_extended_no_extra_lines, total_tokens, patches_extended_tokens = pr_generate_extended_diff(
            pr_languages, token_handler, add_line_numbers_to_hunks=False,
            patch_extra_lines_before=0,
            patch_extra_lines_after=0
        )

        # Check that with no extra lines, the patches are the same as the original patches
        p0 = patches_extended_no_extra_lines[0].strip()
        p1 = patches_extended_no_extra_lines[1].strip()
        assert p0 == "## File: 'file1'\n\n" + pr_languages[0]['files'][0].patch.strip()
        assert p1 == "## File: 'file2'\n\n" + pr_languages[0]['files'][1].patch.strip()

        patches_extended_with_extra_lines, total_tokens, patches_extended_tokens = pr_generate_extended_diff(
            pr_languages, token_handler, add_line_numbers_to_hunks=False,
            patch_extra_lines_before=2,
            patch_extra_lines_after=1
        )

        p0_extended = patches_extended_with_extra_lines[0].strip()
        assert p0_extended == "## File: 'file1'\n\n@@ -3,8 +3,8 @@ \n line0\n line1\n-original content\n+modified content\n line2\n line3\n line4\n line5\n line6"

class TestLoadLargeDiff:
    def test_no_newline(self):
        patch = load_large_diff("test.py",
                                """\
                                old content 1
                                some new content
                                another line
                                """,
                                """
                                old content 1
                                old content 2""")

        patch_expected="""\
--- 
+++ 
@@ -1,3 +1,3 @@
-
                                 old content 1
-                                old content 2
+                                some new content
+                                another line
"""
        assert patch == patch_expected

    def test_empty_inputs(self):
        assert load_large_diff("test.py", "", "") == ""
        assert load_large_diff("test.py", None, None) == ""
        assert (load_large_diff("test.py", "content\n", "") ==
                '--- \n+++ \n@@ -1 +1 @@\n-\n+content\n')