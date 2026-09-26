import pytest

from pr_agent.algo.repo_context import (
    INSTRUCTION_FILES_INTRO,
    TRUNCATION_MARKER,
    _get_markdown_fence,
    render_instruction_files,
    render_instruction_files_with_line_budget,
)


@pytest.mark.parametrize(
    ("content", "fence_length"),
    [
        ("", 5),
        ("Use `type hints`.\n```python\nx = 1\n```", 5),
        ("````\n````", 5),
        ("`````", 6),
        ("``````\n`````", 7),
        ("中文```````說明`````", 8),
        (" ".join(["`"] * 2000) + "\n`````", 6),
        ("prefix\n" + "`" * 50000 + "\nsuffix", 50001),
    ],
)
def test_markdown_fence_exceeds_longest_backtick_run(content, fence_length):
    assert _get_markdown_fence(content) == "`" * fence_length


def test_markdown_fence_scan_follows_configured_minimum(monkeypatch):
    monkeypatch.setattr("pr_agent.algo.repo_context.MARKDOWN_FENCE", "````")
    assert _get_markdown_fence("````") == "`````"


@pytest.mark.parametrize("max_lines", [None, 10])
def test_instruction_renderer_preserves_fences_when_truncating(max_lines):
    content = "Before\n`````````\nAfter\n`````"
    files = {"AGENTS.md": content}
    if max_lines is None:
        rendered = render_instruction_files(files)
        expected_content = content
    else:
        rendered = render_instruction_files_with_line_budget(files, max_lines)
        expected_content = f"Before\n{TRUNCATION_MARKER}"
        assert len(rendered.splitlines()) == max_lines

    assert rendered == (
        f"{INSTRUCTION_FILES_INTRO}\n<instruction_files>\n"
        '<file path="AGENTS.md" scope="repo-root">\n'
        f"``````````markdown\n{expected_content}\n``````````\n"
        "</file>\n\n</instruction_files>"
    )
