"""Per-sample diff ordering must reorder blocks and change nothing else.

The point of the permutation is that the model sees the same files in a different position; if it
also dropped, duplicated or edited a block, a recall delta measured against it would be measuring
a corrupted diff instead.
"""

import pytest

from pr_agent.algo.diff_ordering import permute_patch_for_sample, split_patch_by_file

PATCH = (
    "preamble line\n"
    "\n\n## File: 'lib/a.dart'\n\n@@ -1,2 +1,2 @@\n-old a\n+new a\n"
    "\n\n## File: 'lib/b.dart'\n\n@@ -5,2 +5,2 @@\n-old b\n+new b\n"
    "\n\n## File: 'test/c_test.dart'\n\n@@ -9,2 +9,2 @@\n-old c\n+new c\n"
)


def test_split_round_trips_exactly():
    preamble, blocks = split_patch_by_file(PATCH)
    assert len(blocks) == 3
    assert preamble + "".join(blocks) == PATCH


def test_sample_zero_is_the_original_order():
    assert permute_patch_for_sample(PATCH, 0) == PATCH


@pytest.mark.parametrize("sample_index", [1, 2, 3])
def test_later_samples_reorder_without_changing_content(sample_index):
    permuted = permute_patch_for_sample(PATCH, sample_index, seed=1)
    assert permuted != PATCH, "a sample that is byte-identical to sample 0 is a wasted call"

    preamble, blocks = split_patch_by_file(PATCH)
    p_preamble, p_blocks = split_patch_by_file(permuted)
    assert p_preamble == preamble
    assert sorted(p_blocks) == sorted(blocks), "permutation must not drop, duplicate or edit a block"


def test_permutation_is_deterministic():
    first = permute_patch_for_sample(PATCH, 2, seed=7)
    assert first == permute_patch_for_sample(PATCH, 2, seed=7)


def test_different_chunks_get_different_orders():
    """Seed is derived from the chunk index, so two chunks do not share one ordering."""
    assert permute_patch_for_sample(PATCH, 1, seed=1) != permute_patch_for_sample(PATCH, 1, seed=2)


@pytest.mark.parametrize("patch", ["", "no file headers here", "\n\n## File: 'only.dart'\n\n+one\n"])
def test_nothing_to_permute_is_returned_unchanged(patch):
    assert permute_patch_for_sample(patch, 3) == patch
