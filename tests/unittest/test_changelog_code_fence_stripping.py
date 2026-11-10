"""The changelog entry the model wrote must be committed exactly as written.

`str.strip("```")` removes *characters*, not a fence, so an entry ending in an inline code
span loses its closing backtick - and with `push_changelog_changes` on, the corrupted line is
committed to CHANGELOG.md.
"""
from unittest.mock import MagicMock

import pytest

from pr_agent.commands.pr_update_changelog import PRUpdateChangelog, strip_wrapping_code_fence
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import FileContentSnapshot

EXISTING = "# Changelog\n\n## 2026-01-01\n- Initial release\n"


def _prepared(prediction, commit=True):
    command = PRUpdateChangelog.__new__(PRUpdateChangelog)
    command.changelog_path = "CHANGELOG.md"
    command.prediction = prediction
    command.changelog_file = EXISTING
    command.commit_changelog = commit
    return command._prepare_changelog_update()


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
    # A trailing line that merely starts with a fence is content (main keeps it).
    "- a\n```python",
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
def committing_command(monkeypatch):
    """The real push path, with only the provider and the 5s settle sleep replaced."""
    async def no_sleep(seconds):
        assert seconds == 5

    monkeypatch.setattr("pr_agent.commands.pr_update_changelog.asyncio.sleep", no_sleep)
    monkeypatch.setitem(get_settings().config, "git_provider", "local")
    provider = MagicMock()
    provider.get_pr_branch.return_value = "feature/retry"
    command = PRUpdateChangelog.__new__(PRUpdateChangelog)
    command.changelog_path = "CHANGELOG.md"
    command.git_provider = provider
    command.changelog_file = EXISTING
    command.changelog_snapshot = FileContentSnapshot(EXISTING, True, "fixture-revision")
    command.commit_changelog = True
    return command, provider


@pytest.mark.asyncio
async def test_the_committed_file_keeps_the_inline_code_span(committing_command):
    command, provider = committing_command
    command.prediction = "## 2026-09-06\n\n### Fixed\n- Handle `None` in `parse()`"

    new_file_content, answer = command._prepare_changelog_update()
    await command._push_changelog_update(new_file_content, answer)

    committed = provider.create_or_update_pr_file.call_args.kwargs["contents"]
    assert committed.startswith("## 2026-09-06\n\n### Fixed\n- Handle `None` in `parse()`")
    assert committed.count("`") % 2 == 0, "an unbalanced backtick reached CHANGELOG.md"
    assert committed.endswith(EXISTING)


@pytest.mark.asyncio
async def test_the_committed_file_is_written_to_the_pr_branch(committing_command):
    command, provider = committing_command
    command.prediction = "## 2026-09-06\n- Handle `None`"

    new_file_content, answer = command._prepare_changelog_update()
    await command._push_changelog_update(new_file_content, answer)

    kwargs = provider.create_or_update_pr_file.call_args.kwargs
    assert kwargs["file_path"] == "CHANGELOG.md"
    assert kwargs["branch"] == "feature/retry"
    assert kwargs["message"] == "[skip ci] Update CHANGELOG.md"


# --------------------------------------------------------------------------------------
# Four-backtick fences: standard Markdown for embedding ``` inside a block, and the
# long-run linear-time contract (a plain {3,} backtracks quadratically there).
# --------------------------------------------------------------------------------------


def test_a_four_backtick_fence_is_stripped():
    assert strip_wrapping_code_fence("````yaml\n- Handle `None`\n````") == "- Handle `None`"


def test_a_four_backtick_fence_leaves_no_stray_backtick():
    """A four-backtick closer must consume all four backticks, not three."""
    out = strip_wrapping_code_fence("````yaml\n- one\n- two\n````")

    assert out == "- one\n- two"


def test_stripping_twice_is_the_same_as_once_for_a_four_backtick_fence():
    fenced = "````markdown\n- one\n```python\nx = 1\n````"

    once = strip_wrapping_code_fence(fenced)

    assert strip_wrapping_code_fence(once.strip()) == once.strip()


def test_a_long_backtick_run_is_stripped_in_linear_time():
    """A whole-text regex over the fence is quadratic there (4x per doubling);
    the line-structured scan must stay linear (~2x per doubling), and the ratio
    assertion below separates the two growth curves."""
    import time

    # Ratio assertions only run on sizes large enough that the measurement is
    # not noise-dominated (sub-millisecond timings jitter past 2.5x on fast
    # CI machines — the quadratic curve itself is 4x per doubling and lands
    # in whole seconds, so it is still caught unambiguously).
    timings = []
    for size in (20_000, 160_000, 320_000):
        text = "`" * size + "x"
        start = time.perf_counter()
        strip_wrapping_code_fence(text)
        timings.append(time.perf_counter() - start)

    assert timings[0] < 0.5, f"20k backticks must stay fast, took {timings[0]:.3f}s"
    if timings[1] > 0.01 and timings[2] > 0.01:
        ratio = timings[2] / timings[1]
        assert ratio < 3.0, (
            f"timing grows super-linearly: {timings[1]:.3f}s -> {timings[2]:.3f}s "
            f"(ratio {ratio:.2f}; quadratic would be ~4x)"
        )


@pytest.mark.asyncio
async def test_a_fenced_answer_is_committed_without_its_fence(committing_command):
    """The model often wraps the whole entry; the fence must not reach the file."""
    command, provider = committing_command
    command.prediction = "```markdown\n## 2026-09-06\n- Handle `None`\n```"

    new_file_content, answer = command._prepare_changelog_update()
    await command._push_changelog_update(new_file_content, answer)

    committed = provider.create_or_update_pr_file.call_args.kwargs["contents"]
    assert committed.startswith("## 2026-09-06\n- Handle `None`")
    assert "```" not in committed.split(EXISTING)[0]
