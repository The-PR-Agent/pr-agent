"""Per-sample diff ordering for consensus review sampling.

Consensus sampling (`pr_reviewer.num_samples`) sends the same prepared diff to the model N times
and keeps the findings that recur. With `config.temperature = 0` - the setting evals run at - the
N samples are near-identical, so the vote has nothing to work with.

Reordering the per-file blocks between samples is a second source of diversity that does not need
temperature: a defect the model skipped when its file sat 150k tokens into the prompt may be
reported when the same file leads. Cursor's BugBot attributes a 52% -> 70% improvement in its
resolution-rate metric partly to running passes over differently ordered diffs and combining them
(<https://cursor.com/blog/building-bugbot>).

Sample 0 always keeps the original order, so enabling this never changes what a single-sample run
sends.
"""

import random
import re

FILE_BLOCK_HEADER = re.compile(r"^## [Ff]ile: ", re.MULTILINE)


def split_patch_by_file(patches_diff: str) -> tuple[str, list[str]]:
    """Split a prepared diff into its leading text and one string per ``## File:`` block.

    Returns ``(preamble, blocks)``. Concatenating the preamble and the blocks in order reproduces
    the input exactly, so a caller that reorders blocks changes only their order.
    """
    if not patches_diff:
        return "", []
    starts = [m.start() for m in FILE_BLOCK_HEADER.finditer(patches_diff)]
    if not starts:
        return patches_diff, []
    preamble = patches_diff[:starts[0]]
    bounds = starts + [len(patches_diff)]
    blocks = [patches_diff[bounds[i]:bounds[i + 1]] for i in range(len(starts))]
    return preamble, blocks


def permute_patch_for_sample(patches_diff: str, sample_index: int, *, seed: int = 0) -> str:
    """Return the diff with its file blocks reordered for ``sample_index``.

    Sample 0 is returned unchanged. Later samples get a deterministic shuffle, so a run is
    reproducible from ``seed`` alone and two rows of an eval see the same orderings.
    """
    if sample_index <= 0:
        return patches_diff
    preamble, blocks = split_patch_by_file(patches_diff)
    if len(blocks) < 2:
        return patches_diff
    order = list(range(len(blocks)))
    random.Random(seed * 1000 + sample_index).shuffle(order)
    if order == list(range(len(blocks))):  # a shuffle that changed nothing is a wasted sample
        order.reverse()
    return preamble + "".join(blocks[i] for i in order)
