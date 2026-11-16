import pytest

from pr_agent.algo.pr_processing import MAX_EXTRA_LINES, cap_and_log_extra_lines


def test_accept_a_quoted_number():
    """Accept a quoted number, which is what TOML yields for `patch_extra_lines_before = "3"`."""
    assert cap_and_log_extra_lines("3", "before") == 3


def test_cap_a_quoted_number_above_the_maximum():
    """Cap a quoted number using the same limit as a numeric one."""
    assert cap_and_log_extra_lines(str(MAX_EXTRA_LINES + 5), "after") == MAX_EXTRA_LINES


@pytest.mark.parametrize("value", ["abc", None, [3]])
def test_fall_back_to_zero_for_an_unreadable_value(value):
    """Fall back to no extra lines rather than raising when the value cannot be read."""
    assert cap_and_log_extra_lines(value, "before") == 0


def test_numeric_values_are_unchanged():
    """Keep the existing behaviour for genuinely numeric settings."""
    assert cap_and_log_extra_lines(3, "before") == 3
    assert cap_and_log_extra_lines(MAX_EXTRA_LINES + 1, "before") == MAX_EXTRA_LINES


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_fall_back_for_a_non_finite_value(value):
    """Fall back to no extra lines for a non-finite float, which int() cannot convert."""
    assert cap_and_log_extra_lines(value, "before") == 0


@pytest.mark.parametrize("value", [-1, "-3", -100])
@pytest.mark.parametrize("direction", ["before", "after"])
def test_negative_context_is_disabled(value, direction):
    assert cap_and_log_extra_lines(value, direction) == 0


@pytest.mark.parametrize("before,after", [(1, -1), (-1, 1), (-2, -3), (0, 0), (1, 1)])
def test_extended_diff_headers_match_the_rendered_body(before, after, monkeypatch):
    from types import SimpleNamespace

    from pr_agent.algo.git_patch_processing import RE_HUNK_HEADER
    from pr_agent.algo.pr_processing import pr_generate_extended_diff
    from pr_agent.algo.types import FilePatchInfo
    from pr_agent.config_loader import get_settings

    monkeypatch.setitem(get_settings().config, "allow_dynamic_context", False)
    file = FilePatchInfo(
        base_file="one\ntwo\nthree\nfour\nfive\n",
        head_file="one\nchanged\nthree\nfour\nfive\n",
        patch="@@ -2,2 +2,2 @@\n-two\n+changed\n three", filename="app.py",
    )
    patches, _, _ = pr_generate_extended_diff(
        [{"language": "Python", "files": [file]}],
        SimpleNamespace(prompt_tokens=0, count_tokens=len), False,
        patch_extra_lines_before=cap_and_log_extra_lines(before, "before"),
        patch_extra_lines_after=cap_and_log_extra_lines(after, "after"),
    )
    lines = patches[0].splitlines()
    header_index = next(index for index, line in enumerate(lines) if line.startswith("@@"))
    header = RE_HUNK_HEADER.match(lines[header_index])
    body = lines[header_index + 1:]
    assert int(header.group(2)) == sum(line.startswith((" ", "-")) for line in body)
    assert int(header.group(4)) == sum(line.startswith((" ", "+")) for line in body)
