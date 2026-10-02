"""The changelog entry the model wrote must be committed exactly as written.

`str.strip("```")` removes *characters*, not a fence, so an entry ending in an inline code
span loses its closing backtick - and with `push_changelog_changes` on, the corrupted line is
committed to CHANGELOG.md.
"""
from unittest.mock import MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import FileContentSnapshot
from pr_agent.tools.pr_update_changelog import PRUpdateChangelog, strip_wrapping_code_fence

EXISTING = "# Changelog\n\n## 2026-01-01\n- Initial release\n"


def _prepared(prediction, commit=True):
    tool = PRUpdateChangelog.__new__(PRUpdateChangelog)
    tool.prediction = prediction
    tool.changelog_file = EXISTING
    tool.commit_changelog = commit
    return tool._prepare_changelog_update()


@pytest.mark.parametrize("entry", [
    "## 2026-09-06\n\n### Fixed\n- Handle `None` in `parse()`",
    "## 2026-09-06\n\n### Added\n- A `--dry-run` flag",
    "## 2026-09-06\n\n### Changed\n- Renamed `a` to `b`",
])
def test_an_entry_ending_in_inline_code_is_committed_verbatim(entry):
    new_file_content, _answer = _prepared(entry)

    assert new_file_content.startswith(entry)


def test_a_plain_entry_is_committed_verbatim():
    """Control: an entry with no backticks was already correct."""
    entry = "## 2026-09-06\n\n### Fixed\n- Handle a missing value in the parser"

    new_file_content, _answer = _prepared(entry)

    assert new_file_content.startswith(entry)


def test_the_existing_changelog_is_kept_below_the_new_entry():
    entry = "## 2026-09-06\n\n### Fixed\n- Handle `None`"

    new_file_content, _answer = _prepared(entry)

    assert new_file_content == f"{entry}\n\n{EXISTING}"


@pytest.mark.parametrize("fenced, expected", [
    ("```\n- Handle `None`\n```", "- Handle `None`"),
    ("```markdown\n- Handle `None`\n```", "- Handle `None`"),
    ("  ```md\n- one\n- two\n```  ", "- one\n- two"),
    # The prompt ends with a dangling open "```markdown", so this is what the model usually
    # sends: a closing fence and no opening one. It is still the wrapper.
    ("- Added foo\n- Fixed `bar()`\n```", "- Added foo\n- Fixed `bar()`"),
    ("## 2026-09-06\n- Handle `None`\n```", "## 2026-09-06\n- Handle `None`"),
])
def test_a_wrapping_fence_is_removed(fenced, expected):
    assert strip_wrapping_code_fence(fenced) == expected


@pytest.mark.parametrize("text", [
    "- Handle `None` in `parse()`",
    "- A line with ``` inside it",
    "```\nunterminated fence",
    "- one\n\n```python\nx = 1\n```\n\n- two",
    # Balanced fences of the entry's own: the trailing fence closes them, it is not a wrapper.
    "- one\n\n```python\nx = 1\n```",
])
def test_text_without_a_wrapping_fence_is_untouched(text):
    assert strip_wrapping_code_fence(text) == text


@pytest.mark.parametrize("fenced, expected", [
    ("```markdown\n- one\n```", "- one"),
    # The dangling-closing-fence answer the prompt usually produces.
    ("- one\n- two\n```", "- one\n- two"),
    # A whole answer wrapping an entry that itself ends in a code block.
    ("```markdown\n- one\n\n```python\nx = 1\n```\n```", "- one\n\n```python\nx = 1\n```"),
])
def test_stripping_twice_is_the_same_as_once(fenced, expected):
    """The answer is stripped on the reply and again before the changelog is built."""
    once = strip_wrapping_code_fence(fenced)

    assert once == expected
    assert strip_wrapping_code_fence(once.strip()) == expected


def test_an_entry_ending_in_a_code_block_is_committed_verbatim():
    """A second strip used to eat the closing fence, unterminating the code block."""
    entry = "## 2026-09-06\n\n### Added\n- New CLI flag\n\n```python\nflags.add('--x')\n```"

    new_file_content, _answer = _prepared(strip_wrapping_code_fence(f"```markdown\n{entry}\n```"))

    assert new_file_content == f"{entry}\n\n{EXISTING}"


# --------------------------------------------------------------------------------------
# A complete internal code block followed by the wrapper close
# --------------------------------------------------------------------------------------
_ENTRY_WITH_CODE_BLOCK = "## 2026-09-06\n\n### Added\n- New CLI flag\n\n```python\nflags.add('--x')\n```"


def test_a_wrapper_close_after_a_complete_code_block_is_removed():
    """No opening wrapper, a closed internal block, then a trailing wrapper close.

    The trailing fence is a wrapper here: the internal block is already balanced. Keeping it
    left a dangling fence that rendered every older entry as code.
    """
    fenced = f"{_ENTRY_WITH_CODE_BLOCK}\n```"

    assert strip_wrapping_code_fence(fenced) == _ENTRY_WITH_CODE_BLOCK
    assert strip_wrapping_code_fence(strip_wrapping_code_fence(fenced).strip()) == _ENTRY_WITH_CODE_BLOCK


def test_a_leftover_wrapper_fence_is_not_committed():
    """The prepared changelog must not put the existing entries inside a code block."""
    new_file_content, _answer = _prepared(strip_wrapping_code_fence(f"{_ENTRY_WITH_CODE_BLOCK}\n```"))

    assert new_file_content == f"{_ENTRY_WITH_CODE_BLOCK}\n\n{EXISTING}"
    # Every fence in the new entry pairs up, so the existing changelog renders as itself.
    assert new_file_content.split(EXISTING)[0].count("\n```") % 2 == 0


def test_the_commit_hint_is_appended_when_not_committing():
    """Control: the non-committing branch still explains how to commit."""
    _new_file_content, answer = _prepared("## 2026-09-06\n- Handle `None`", commit=False)

    assert "push_changelog_changes=true" in answer


# --------------------------------------------------------------------------------------
# End to end: what actually reaches CHANGELOG.md on the branch
# --------------------------------------------------------------------------------------
@pytest.fixture
def committing_tool(monkeypatch):
    """The real push path, with only the provider and the 5s settle sleep replaced."""
    async def no_sleep(seconds):
        assert seconds == 5

    monkeypatch.setattr("pr_agent.tools.pr_update_changelog.asyncio.sleep", no_sleep)
    monkeypatch.setattr(get_settings().config, "git_provider", "local", raising=False)
    provider = MagicMock()
    provider.get_pr_branch.return_value = "feature/retry"
    tool = PRUpdateChangelog.__new__(PRUpdateChangelog)
    tool.git_provider = provider
    tool.changelog_file = EXISTING
    tool.changelog_snapshot = FileContentSnapshot(EXISTING, True, "fixture-revision")
    tool.commit_changelog = True
    return tool, provider


@pytest.mark.asyncio
async def test_the_committed_file_keeps_the_inline_code_span(committing_tool):
    tool, provider = committing_tool
    tool.prediction = "## 2026-09-06\n\n### Fixed\n- Handle `None` in `parse()`"

    new_file_content, answer = tool._prepare_changelog_update()
    await tool._push_changelog_update(new_file_content, answer)

    committed = provider.create_or_update_pr_file.call_args.kwargs["contents"]
    assert committed.startswith("## 2026-09-06\n\n### Fixed\n- Handle `None` in `parse()`")
    assert committed.count("`") % 2 == 0, "an unbalanced backtick reached CHANGELOG.md"
    assert committed.endswith(EXISTING)


@pytest.mark.asyncio
async def test_the_committed_file_is_written_to_the_pr_branch(committing_tool):
    tool, provider = committing_tool
    tool.prediction = "## 2026-09-06\n- Handle `None`"

    new_file_content, answer = tool._prepare_changelog_update()
    await tool._push_changelog_update(new_file_content, answer)

    kwargs = provider.create_or_update_pr_file.call_args.kwargs
    assert kwargs["file_path"] == "CHANGELOG.md"
    assert kwargs["branch"] == "feature/retry"
    assert kwargs["message"] == "[skip ci] Update CHANGELOG.md"


@pytest.mark.asyncio
async def test_a_fenced_answer_is_committed_without_its_fence(committing_tool):
    """The model often wraps the whole entry; the fence must not reach the file."""
    tool, provider = committing_tool
    tool.prediction = "```markdown\n## 2026-09-06\n- Handle `None`\n```"

    new_file_content, answer = tool._prepare_changelog_update()
    await tool._push_changelog_update(new_file_content, answer)

    committed = provider.create_or_update_pr_file.call_args.kwargs["contents"]
    assert committed.startswith("## 2026-09-06\n- Handle `None`")
    assert "```" not in committed.split(EXISTING)[0]
