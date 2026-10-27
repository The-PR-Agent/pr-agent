from types import SimpleNamespace

import pytest

from pr_agent.algo import pr_processing
from pr_agent.algo.types import EDIT_TYPE


class NoLinearMembership(list):
    def __contains__(self, _value):
        raise AssertionError("Do not linearly scan the included file list")

    def __iter__(self):
        if getattr(self, "forbid_iteration", False):
            raise AssertionError("Do not build the membership set when metadata has no room")
        return super().__iter__()


@pytest.mark.parametrize("available_tokens", [300, 14])
def test_compressed_diff_metadata_reuses_included_file_membership(monkeypatch, available_tokens):
    included_files = NoLinearMembership(["included.py"])
    included_files.forbid_iteration = available_tokens == 14
    file_dict = {
        "included.py": {"edit_type": EDIT_TYPE.MODIFIED},
        "added.py": {"edit_type": EDIT_TYPE.ADDED},
        "renamed.py": {"edit_type": EDIT_TYPE.RENAMED},
        "modified.py": {"edit_type": EDIT_TYPE.MODIFIED},
        "deleted.py": {"edit_type": EDIT_TYPE.DELETED},
    }
    handler = SimpleNamespace(prompt_tokens=0, count_tokens=len)
    budget = SimpleNamespace(
        token_handler=handler,
        context_window=10000,
        available_tokens=lambda *_args, **_kwargs: available_tokens,
    )
    monkeypatch.setattr(pr_processing.AttemptTokenBudget, "for_attempt", lambda *_a, **_k: budget)
    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda *_a: [])
    monkeypatch.setattr(
        pr_processing, "pr_generate_extended_diff",
        lambda *_a, **_k: (["full diff"], 400, []),
    )
    monkeypatch.setattr(
        pr_processing, "pr_generate_compressed_diff",
        lambda *_a, **_k: (
            [["original diff"]], [0], [], [], file_dict, [included_files],
        ),
    )
    provider = SimpleNamespace(
        get_diff_files=lambda: [],
        get_languages=lambda: {},
        get_filtered_diff_file_names=lambda: [],
    )

    result = pr_processing.get_pr_diff(provider, handler, "test-model")

    assert result.startswith("original diff")
    assert included_files == ["included.py"]
    if available_tokens == 300:
        assert result.index("added.py") < result.index("renamed.py") < result.index("deleted.py")
        assert result.index("renamed.py") < result.index("modified.py")
        assert "included.py" not in result
    else:
        assert result == "original diff"
