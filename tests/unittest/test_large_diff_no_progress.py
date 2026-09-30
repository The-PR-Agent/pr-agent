from types import SimpleNamespace

import pr_agent.algo.pr_processing as pr_processing
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


class LengthTokenHandler:
    prompt_tokens = 0

    def count_tokens(self, text):
        return len(text)


def _file(filename, payload_size):
    return FilePatchInfo(
        base_file="old\n",
        head_file="new\n",
        patch="@@ -1 +1 @@\n-old\n+" + ("x" * payload_size),
        filename=filename,
        edit_type=EDIT_TYPE.MODIFIED,
    )


def _pack(monkeypatch, files, *, soft_token_budget):
    monkeypatch.setattr(
        pr_processing,
        "get_settings",
        lambda: SimpleNamespace(pr_description={"max_ai_calls": 4}),
    )
    original = pr_processing.generate_full_patch
    rounds = 0

    def counted_round(*args, **kwargs):
        nonlocal rounds
        rounds += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pr_processing, "generate_full_patch", counted_round)
    result = pr_processing.pr_generate_compressed_diff(
        [{"language": "Python", "files": files}],
        LengthTokenHandler(),
        soft_token_budget=soft_token_budget,
        hard_token_budget=soft_token_budget + 50,
        convert_hunks_to_line_numbers=False,
        large_pr_handling=True,
    )
    return result, rounds


def test_large_pr_packing_stops_when_first_round_cannot_fit_any_patch(monkeypatch):
    result, rounds = _pack(monkeypatch, [_file("large.py", 200)], soft_token_budget=60)

    patches_list, _, _, remaining_files, _, files_in_patches = result
    assert rounds == 1
    assert patches_list == [[]]
    assert remaining_files == ["large.py"]
    assert files_in_patches == [[]]


def test_large_pr_packing_stops_after_a_later_round_makes_no_progress(monkeypatch):
    result, rounds = _pack(
        monkeypatch,
        [_file("small.py", 5), _file("large.py", 200)],
        soft_token_budget=80,
    )

    patches_list, _, _, remaining_files, _, files_in_patches = result
    assert rounds == 2
    assert len(patches_list) == 1
    assert files_in_patches == [["small.py"]]
    assert remaining_files == ["large.py"]
