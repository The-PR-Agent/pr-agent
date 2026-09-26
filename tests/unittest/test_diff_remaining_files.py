from types import SimpleNamespace

import pytest

from pr_agent.algo.pr_processing import generate_full_patch


@pytest.mark.parametrize("line_numbers", [False, True])
@pytest.mark.parametrize(
    ("remaining_files", "expected_files"),
    [
        ([], []),
        (["missing.py"], []),
        (["c.py", "a.py", "c.py", "empty.py", "missing.py"], ["a.py", "c.py"]),
    ],
)
def test_remaining_files_preserve_file_order_and_input(line_numbers, remaining_files, expected_files):
    files = {name: {"patch": "+x"} for name in ("a.py", "b.py", "c.py")}
    files["empty.py"] = {"patch": ""}
    original_remaining = remaining_files.copy()
    counter = SimpleNamespace(prompt_tokens=7, count_tokens=len)

    total, patches, remaining, included = generate_full_patch(
        line_numbers, files, 10000, remaining_files, counter, hard_token_budget=10000
    )

    expected_patches = [
        "\n\n+x" if line_numbers else f"\n\n## File: '{name}'\n\n+x\n"
        for name in expected_files
    ]
    assert (total, patches, remaining, included) == (
        7 + len("\n".join(expected_patches)), expected_patches, [], expected_files
    )
    assert remaining_files == original_remaining


@pytest.mark.parametrize("line_numbers", [False, True])
def test_remaining_files_can_be_packed_in_successive_rounds(line_numbers):
    files = {name: {"patch": "+x"} for name in ("a.py", "b.py", "c.py")}
    remaining = ["c.py", "a.py", "b.py", "a.py"]
    counter = SimpleNamespace(prompt_tokens=7, count_tokens=len)
    budget = len("\n\n+x" if line_numbers else "\n\n## File: 'a.py'\n\n+x\n")

    for index, name in enumerate(files):
        previous_remaining = remaining.copy()
        total, patches, next_remaining, included = generate_full_patch(
            line_numbers, files, budget, remaining, counter, hard_token_budget=budget
        )
        expected_patch = "\n\n+x" if line_numbers else f"\n\n## File: '{name}'\n\n+x\n"
        assert (total, patches, included) == (7 + budget, [expected_patch], [name])
        assert next_remaining == list(files)[index + 1:]
        assert remaining == previous_remaining
        remaining = next_remaining
