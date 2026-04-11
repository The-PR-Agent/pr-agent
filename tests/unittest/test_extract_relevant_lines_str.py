import textwrap
from unittest.mock import Mock

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import convert_to_markdown_v2, extract_relevant_lines_str


def _make_file(filename: str, head_file: str, language: str = "python", patch: str = "") -> FilePatchInfo:
    return FilePatchInfo(
        base_file=head_file,
        head_file=head_file,
        patch=patch,
        filename=filename,
        language=language,
        edit_type=EDIT_TYPE.MODIFIED,
    )


class TestExtractRelevantLinesStr:
    def test_returns_empty_when_no_files(self):
        result = extract_relevant_lines_str(end_line=5, files=None, relevant_file="src/foo.py", start_line=1)
        assert result == ""

    def test_returns_empty_when_file_not_found(self):
        file = _make_file("src/other.py", "line1\nline2\nline3\n")
        result = extract_relevant_lines_str(end_line=2, files=[file], relevant_file="src/foo.py", start_line=1)
        assert result == ""

    def test_extracts_single_line(self):
        file = _make_file("src/foo.py", "line1\nline2\nline3\n")
        result = extract_relevant_lines_str(end_line=2, files=[file], relevant_file="src/foo.py", start_line=2)
        assert result == "```python\nline2\n```"

    def test_extracts_line_range(self):
        file = _make_file("src/foo.py", "line1\nline2\nline3\nline4\n")
        result = extract_relevant_lines_str(end_line=3, files=[file], relevant_file="src/foo.py", start_line=2)
        assert result == "```python\nline2\nline3\n```"

    def test_language_in_fenced_block(self):
        file = _make_file("src/foo.js", "const x = 1;\nconst y = 2;\n", language="javascript")
        result = extract_relevant_lines_str(end_line=1, files=[file], relevant_file="src/foo.js", start_line=1)
        assert result.startswith("```javascript\n")
        assert result.endswith("\n```")

    def test_dedent_removes_common_indent(self):
        head_file = "    if True:\n        pass\n    return\n"
        file = _make_file("src/foo.py", head_file)
        result = extract_relevant_lines_str(
            end_line=2, files=[file], relevant_file="src/foo.py", start_line=1, dedent=True
        )
        # common indent of 4 spaces should be stripped
        assert "```python\n" + textwrap.dedent("    if True:\n        pass") + "\n```" == result

    def test_no_dedent_by_default(self):
        head_file = "    if True:\n        pass\n"
        file = _make_file("src/foo.py", head_file)
        result = extract_relevant_lines_str(end_line=2, files=[file], relevant_file="src/foo.py", start_line=1)
        assert "    if True:" in result

    def test_fallback_to_patch_when_head_file_is_none(self):
        patch = textwrap.dedent("""\
            @@ -1,3 +1,4 @@
             unchanged
            +new line
             unchanged2
        """)
        file = FilePatchInfo(
            base_file="original content",
            head_file=None,
            patch=patch,
            filename="src/foo.py",
            language="python",
            edit_type=EDIT_TYPE.MODIFIED,
        )
        result = extract_relevant_lines_str(end_line=2, files=[file], relevant_file="src/foo.py", start_line=2)
        assert result.startswith("```python\n")
        assert "new line" in result

    def test_no_triple_backticks_in_content(self):
        # Ensures the generated string never contains triple backticks inside the code block,
        # which would break markdown rendering inside <details> tags on GitHub.
        head_file = "def foo():\n    return 42\n"
        file = _make_file("src/foo.py", head_file)
        result = extract_relevant_lines_str(end_line=2, files=[file], relevant_file="src/foo.py", start_line=1)
        # The only backtick sequences should be the opening and closing fence — not inside the content
        inner_content = result.split("\n", 1)[1].rsplit("\n", 1)[0]  # strip first and last lines
        assert "```" not in inner_content

    def test_filename_with_leading_trailing_spaces(self):
        file = _make_file("  src/foo.py  ", "line1\nline2\n")
        result = extract_relevant_lines_str(end_line=1, files=[file], relevant_file="src/foo.py", start_line=1)
        assert result == "```python\nline1\n```"

    def test_content_with_inner_fenced_block_uses_longer_fence(self):
        # When the extracted lines contain ```, the outer fence must not collide with that run.
        # The new implementation chooses a tilde fence (~~~) because it is shorter than ````.
        readme = "Usage:\n\n```bash\necho hello\n```\n"
        file = _make_file("README.md", readme, language="markdown")
        result = extract_relevant_lines_str(end_line=5, files=[file], relevant_file="README.md", start_line=1)

        first_line = result.splitlines()[0]
        last_line = result.splitlines()[-1]

        # The outer fence must be a tilde fence because it is shorter than a 4-backtick fence.
        assert first_line.startswith("~~~"), f"Expected tilde fence, got: {first_line!r}"
        assert last_line.startswith("~~~"), f"Expected tilde fence, got: {last_line!r}"
        # The inner ``` must be preserved verbatim inside the outer fence.
        assert "```bash" in result
        assert "```\n" in result


class TestDetailsBlockWithCodeFence:
    """Verifies that a <details> block renders correctly when the extracted lines
    from the file themselves contain triple backticks (e.g. a README with a ```bash
    code block inside it).

    Without the fix, extract_relevant_lines_str wrapped any content in ```lang...```
    regardless of whether the content contained its own ```, which caused the parser
    to see two fenced blocks and emit a stray ``` before </details>.

    The fix uses a longer fence (````markdown...````) whenever the content contains ```.
    """

    def test_fenced_block_in_issue_content_uses_longer_fence(self):
        # issue_content as the LLM produces it — no triple backticks (confirmed by real log)
        issue_content = (
            "Lorem ipsum `foo_param` and `bar_param` dolor sit amet. "
            "Consectetur adipiscing elit, sed do `'<placeholder-value>'` eiusmod.\n"
        )

        # The README itself has a ```bash block in the extracted range — this is the real trigger.
        head_file = (
            "## Usage\n"
            "\n"
            "```bash\n"
            "python lorem_script.py \\\n"
            "  --foo-param 'ipsum-dolor-sit-amet' \\\n"
            "  --bar-param 'consectetur-adipiscing'\n"
            "```\n"
        )
        file = _make_file("docs/lorem/README.md", head_file, language="markdown")

        mock_git_provider = Mock()
        mock_git_provider.get_line_link.return_value = (
            "https://example.com/repo/-/blob/main/docs/lorem/README.md#L1-8"
        )

        input_data = {
            "review": {
                "key_issues_to_review": [
                    {
                        "relevant_file": "docs/lorem/README.md",
                        "issue_header": "[Secure] Lorem ipsum placeholder values in README",
                        "issue_content": issue_content,
                        "start_line": 1,
                        "end_line": 8,
                    }
                ]
            }
        }

        result = convert_to_markdown_v2(
            input_data,
            gfm_supported=True,
            git_provider=mock_git_provider,
            files=[file],
        )

        assert "<details>" in result

        details_start = result.index("<details>")
        details_end = result.index("</details>") + len("</details>")
        details_block = result[details_start:details_end]

        # The outer fence must be a tilde fence because it is shorter than a 4-backtick fence
        # when the README content contains ```bash inside it.
        assert "~~~markdown\n" in details_block, (
            f"Expected a tilde outer fence (~~~markdown), got:\n{details_block}"
        )
        # The closing fence must also be tildes.
        assert "\n~~~\n" in details_block, (
            f"Expected a tilde closing fence, got:\n{details_block}"
        )
        # The inner ```bash block from the README must be preserved verbatim inside the outer fence.
        assert "```bash\n" in details_block
        # Must NOT use <pre> blocks — fenced code syntax is required.
        assert "<pre>" not in details_block


class TestConvertToMarkdownV2ThreeIssues:
    """Tests using synthetic input shaped after a three-issue review:

      1. Python file, lines 59-63 — no inner fences → plain ```python fence expected.
      2. Python file, lines 25-38 — no inner fences → plain ```python fence expected.
      3. README.md, lines 41-43 — line 43 IS a closing ``` fence inside the README,
         so the outer fence must use 4 backticks (````markdown).
    """

    # Generic Python block at lines 59-63: no backtick sequences inside.
    _PY_LINES_59_63 = (
        'if response.get("error"):\n'
        '    raise RuntimeError(f"lorem {name!r} error: {response[\"error\"]}"  )\n'
        'if "result" not in response:\n'
        '    raise RuntimeError(f"lorem {name!r}: unexpected response: {response}")\n'
        'return response["result"]'
    )

    # Generic Python block at lines 25-38: no backtick sequences inside.
    _PY_LINES_25_38 = (
        'def ipsum_fetch(base_url: str) -> str:\n'
        '    url = f"{base_url}/dolor"\n'
        '    payload = json.dumps({"sit": "amet"}).encode()\n'
        '    req = urllib.request.Request(\n'
        '        url,\n'
        '        data=payload,\n'
        '        headers={"Content-Type": "application/json"},\n'
        '        method="GET",\n'
        '    )\n'
        '    with urllib.request.urlopen(req) as resp:\n'
        '        data = json.loads(resp.read())\n'
        '    if "consectetur" not in data:\n'
        '        raise RuntimeError(f"ipsum_fetch: unexpected response: {data}")\n'
        '    return data["consectetur"]'
    )

    def _make_py_file(self) -> FilePatchInfo:
        """Build a synthetic .py file with the two blocks at the correct line positions."""
        # 24 filler lines so that line 25 starts _PY_LINES_25_38
        filler_24 = '\n'.join(f'# filler {i}' for i in range(1, 25))
        # filler between end of block 1 (line 38) and start of block 2 (line 59)
        filler_between = '\n'.join(f'# filler {i}' for i in range(39, 59))
        content = (
            filler_24 + '\n' + self._PY_LINES_25_38 + '\n'
            + filler_between + '\n' + self._PY_LINES_59_63 + '\n'
        )
        return _make_file('src/lorem/script.py', content, language='python')

    def _make_readme_file(self) -> FilePatchInfo:
        """Build a synthetic README.md where line 43 is a closing ``` fence.

        Lines 41-43:
          41: lorem ipsum flag value \\
          42: dolor sit amet flag value
          43: ```          ← closing fence of a bash block already open in the README
        """
        filler_40 = '\n'.join(f'lorem ipsum line {i}' for i in range(1, 41))
        readme_lines_41_43 = (
            "  --lorem-param 'adipiscing-elit-sed-do-eiusmod' \\\n"
            "  --ipsum-param 'tempor-incididunt'\n"
            '```'
        )
        content = filler_40 + '\n' + readme_lines_41_43 + '\n'
        return _make_file('docs/ipsum/README.md', content, language='markdown')

    def _make_input_data(self) -> dict:
        return {
            'review': {
                'estimated_effort_to_review_[1-5]': '2\n',
                'relevant_tests': 'No\n',
                'security_concerns': (
                    'Lorem ipsum: docs/ipsum/README.md contains values that look like '
                    'real credentials used as examples.\n'
                ),
                'key_issues_to_review': [
                    {
                        'relevant_file': 'src/lorem/script.py\n',
                        'issue_header': '[Secure] Lorem ipsum error messages expose response body\n',
                        'issue_content': (
                            'The `lorem` function includes the full response body in '
                            'exception messages (`RuntimeError`).\n'
                        ),
                        'start_line': 59,
                        'end_line': 63,
                    },
                    {
                        'relevant_file': 'src/lorem/script.py\n',
                        'issue_header': '[Correct] Lorem ipsum incorrect HTTP method in `ipsum_fetch`\n',
                        'issue_content': (
                            'The `ipsum_fetch` function sets `method="GET"` but also sends '
                            'a JSON `data=payload` body.\n'
                        ),
                        'start_line': 25,
                        'end_line': 38,
                    },
                    {
                        'relevant_file': 'docs/ipsum/README.md\n',
                        'issue_header': '[Secure] Lorem ipsum placeholder values in README\n',
                        'issue_content': (
                            'The README contains values that look like real credentials: '
                            '`\'adipiscing-elit-sed-do-eiusmod\'` (lorem ipsum example).\n'
                        ),
                        'start_line': 41,
                        'end_line': 43,
                    },
                ],
            }
        }

    def test_python_issue_lines_59_63_uses_plain_fence(self):
        """Python code with no inner fences must use a plain ```python fence."""
        result = extract_relevant_lines_str(
            end_line=63,
            files=[self._make_py_file()],
            relevant_file='src/lorem/script.py',
            start_line=59,
            dedent=True,
        )
        assert result.startswith('```python\n'), (
            f'Expected plain ```python fence, got: {result[:40]!r}'
        )
        assert result.endswith('\n```'), f'Expected closing ```, got end: {result[-20:]!r}'
        assert '````' not in result, 'Should not need a 4-backtick fence for plain Python code'

    def test_python_issue_lines_25_38_uses_plain_fence(self):
        """Python code block (lines 25-38) with no inner fences must use a plain ```python fence."""
        result = extract_relevant_lines_str(
            end_line=38,
            files=[self._make_py_file()],
            relevant_file='src/lorem/script.py',
            start_line=25,
            dedent=True,
        )
        assert result.startswith('```python\n'), f'Expected plain ```python fence, got: {result[:40]!r}'
        assert result.endswith('\n```'), f'Expected closing ```, got end: {result[-20:]!r}'
        assert '````' not in result, 'Should not need a 4-backtick fence for plain Python code'

    def test_readme_issue_lines_41_43_uses_tilde_fence(self):
        """README lines 41-43 include a closing ``` fence on line 43.
        The outer fence must not collide with that inner ```. The new implementation
        picks a tilde fence (~~~markdown) because it is shorter than a 4-backtick fence.
        """
        result = extract_relevant_lines_str(
            end_line=43,
            files=[self._make_readme_file()],
            relevant_file='docs/ipsum/README.md',
            start_line=41,
            dedent=True,
        )
        first_line = result.splitlines()[0]
        last_line = result.splitlines()[-1]
        assert first_line == '~~~markdown', f'Expected tilde opening fence, got: {first_line!r}'
        assert last_line == '~~~', f'Expected tilde closing fence, got: {last_line!r}'
        inner_lines = result.splitlines()[1:-1]
        assert any(line.strip() == '```' for line in inner_lines), (
            f'Expected inner ``` preserved as content, got inner lines: {inner_lines}'
        )

    def test_convert_to_markdown_v2_uses_fenced_blocks_not_pre(self):
        """In the gfm_supported <details> path, all issues must use fenced code blocks, not <pre>."""
        mock_git_provider = Mock()
        mock_git_provider.get_line_link.return_value = (
            'https://example.com/repo/-/blob/main/src/lorem/script.py#L59-63'
        )

        result = convert_to_markdown_v2(
            self._make_input_data(),
            gfm_supported=True,
            git_provider=mock_git_provider,
            files=[self._make_py_file(), self._make_readme_file()],
        )

        # Python issues: plain ```python fence inside <details>
        assert '```python\n' in result
        # README issue: tilde fence inside <details> (shorter than a 4-backtick fence)
        assert '~~~markdown\n' in result
        # No <pre> blocks anywhere — fenced syntax only
        assert '<pre>' not in result
