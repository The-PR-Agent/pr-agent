"""Keep unreadable-file notices visible when multi-diff packing exceeds its budget."""

from unittest.mock import MagicMock

import pytest

from pr_agent.algo import pr_processing, token_budget
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings


class _CharacterTokenHandler:
    prompt_tokens = 0

    def count_tokens(self, value):
        return len(value)


class _Provider:
    def __init__(self, files):
        self.files = files

    def get_diff_files(self):
        return self.files

    def get_languages(self):
        return {"Python": 1}


def _unreadable(name="unreadable.py"):
    return FilePatchInfo(
        "old\n", "", "", name,
        edit_type=EDIT_TYPE.MODIFIED, content_fetch_failed=True,
    )


def _modified(name="healthy.py", size=450):
    new_line = "x" * size
    return FilePatchInfo(
        "old\n", f"{new_line}\n",
        f"@@ -1 +1 @@\n-old\n+{new_line}\n", name,
        edit_type=EDIT_TYPE.MODIFIED,
    )


def _multi_diff(monkeypatch, files, context_window, *, add_line_numbers=True, max_calls=5,
                deleted_files=None, large_patch_policy="skip"):
    monkeypatch.setattr(
        token_budget, "get_max_tokens",
        lambda model, ignore_max_model_tokens=False: context_window,
    )
    monkeypatch.setattr(
        pr_processing, "sort_files_by_main_languages",
        lambda languages, files: [{"language": "Python", "files": files}],
    )
    monkeypatch.setitem(get_settings().config, "large_patch_policy", large_patch_policy)
    return pr_processing.get_pr_multi_diffs(
        _Provider(files), _CharacterTokenHandler(), "test-model",
        add_line_numbers=add_line_numbers, max_calls=max_calls,
        return_coverage=True, include_filtered_file_names=False,
        deleted_files=deleted_files,
    )


@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_unreadable_file_in_full_diff(monkeypatch, add_line_numbers):
    result = _multi_diff(
        monkeypatch, [_unreadable(), _modified(size=20)], 10_000,
        add_line_numbers=add_line_numbers,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].count("## File: 'unreadable.py'") == 1
    assert "could not be read" in result.chunks[0]
    assert "healthy.py" in result.chunks[0]
    assert result.remaining_files_list == []
    assert result.partial_files_list == []


@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_unreadable_file_survives_large_pr_chunking(monkeypatch, add_line_numbers):
    result = _multi_diff(
        monkeypatch, [_modified(), _unreadable()], 2_100,
        add_line_numbers=add_line_numbers,
    )

    combined = "\n".join(result.chunks)
    assert len(result.chunks) >= 2
    assert "healthy.py" in combined
    assert "could not be read" in combined
    assert combined.count("## File: 'unreadable.py'") == 1
    assert result.remaining_files_list == []
    assert result.partial_files_list == []
    assert all(len(chunk) <= 600 for chunk in result.chunks)


@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_only_unreadable_files_are_chunked(monkeypatch, add_line_numbers):
    files = [_unreadable(f"unreadable_{i}.py") for i in range(4)]
    result = _multi_diff(
        monkeypatch, files, 2_100, add_line_numbers=add_line_numbers,
    )
    combined = "\n".join(result.chunks)

    assert len(result.chunks) >= 2
    for file in files:
        assert combined.count(f"## File: '{file.filename}'") == 1
    assert combined.count("could not be read") == len(files)
    assert result.remaining_files_list == []
    assert result.partial_files_list == []


@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_unfittable_notice_is_reported_as_remaining(monkeypatch, add_line_numbers):
    result = _multi_diff(
        monkeypatch, [_unreadable()], 1_600,
        add_line_numbers=add_line_numbers,
    )

    assert not any("could not be read" in chunk for chunk in result.chunks)
    assert result.remaining_files_list == ["unreadable.py"]
    assert result.partial_files_list == []


@pytest.mark.parametrize("add_line_numbers", [False, True])
def test_oversized_unreadable_notice_is_never_clipped(monkeypatch, add_line_numbers):
    clip = MagicMock(return_value="## File: 'unreadable.py'\n")
    monkeypatch.setattr(pr_processing, "clip_tokens", clip)
    result = _multi_diff(
        monkeypatch, [_unreadable()], 1_600,
        add_line_numbers=add_line_numbers, large_patch_policy="clip",
    )

    assert result.chunks == []
    assert result.remaining_files_list == ["unreadable.py"]
    assert result.partial_files_list == []
    clip.assert_not_called()


def test_prepared_unreadable_notice_is_never_clipped(monkeypatch):
    handler = _CharacterTokenHandler()
    file = _unreadable()
    _, _, _, _, file_dict, _ = pr_processing.pr_generate_compressed_diff(
        [{"language": "Python", "files": [file]}], handler,
        soft_token_budget=100, hard_token_budget=600,
        convert_hunks_to_line_numbers=True, large_pr_handling=False,
    )
    prepared = pr_processing.PreparedPRDiff(
        "", [], file_dict, {file.filename: file}, model="test-model",
        add_line_numbers_to_hunks=True, token_handler=handler,
    )
    clip = MagicMock(return_value="## File: 'unreadable.py'\n")
    monkeypatch.setattr(pr_processing, "clip_tokens", clip)
    monkeypatch.setattr(
        token_budget, "get_max_tokens",
        lambda model, ignore_max_model_tokens=False: 1_600,
    )
    monkeypatch.setitem(get_settings().config, "large_patch_policy", "clip")

    result = pr_processing.get_pr_multi_diffs(
        _Provider([file]), handler, "test-model", prepared_diff=prepared,
        return_coverage=True, include_filtered_file_names=False,
    )
    assert result.chunks == []
    assert result.remaining_files_list == [file.filename]
    assert result.partial_files_list == []
    clip.assert_not_called()


def test_unreadable_does_not_turn_normal_empty_patches_into_notices(monkeypatch):
    renamed = FilePatchInfo(
        "", "", "", "renamed.py", edit_type=EDIT_TYPE.RENAMED,
    )
    deleted = FilePatchInfo(
        "old\n", "", "@@ -1 +0,0 @@\n-old\n", "deleted.py",
        edit_type=EDIT_TYPE.DELETED,
    )
    result = _multi_diff(
        monkeypatch, [renamed, deleted, _unreadable()], 10_000, deleted_files=[],
    )
    combined = "\n".join(result.chunks)

    assert "unreadable.py" in combined
    assert "renamed.py" not in combined
    assert "deleted.py" not in combined
    assert combined.count("could not be read") == 1
    assert result.remaining_files_list == []
    assert result.partial_files_list == []
