"""Size every file before pr_generate_compressed_diff packs them largest-first."""
from pr_agent.algo.pr_processing import pr_generate_compressed_diff
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo


class FakeTokenHandler:
    prompt_tokens = 10

    def count_tokens(self, patch, force_accurate=False):
        return len(patch)


def _file(name, body_lines):
    patch = "@@ -1,1 +1,%d @@\n" % body_lines + "\n".join(f"+l{i}" for i in range(body_lines))
    return FilePatchInfo(base_file="", head_file="x", patch=patch, filename=name,
                         edit_type=EDIT_TYPE.MODIFIED)


def test_token_counts_are_filled_in_before_sorting():
    """Populate FilePatchInfo.tokens, which defaults to -1, on the entry point that skips
    pr_generate_extended_diff."""
    small, large = _file("small.py", 1), _file("large.py", 40)
    assert small.tokens == -1 and large.tokens == -1

    pr_generate_compressed_diff([{"language": "Python", "files": [small, large]}],
                                FakeTokenHandler(), "gpt-4", False, False)

    assert small.tokens > 0
    assert large.tokens > small.tokens


def test_files_are_ordered_largest_first():
    """Order the packing by patch size rather than by provider order."""
    small, large = _file("small.py", 1), _file("large.py", 40)

    _, _, _, _, file_dict, _ = pr_generate_compressed_diff(
        [{"language": "Python", "files": [small, large]}], FakeTokenHandler(), "gpt-4", False, False)

    assert list(file_dict) == ["large.py", "small.py"]


def test_precomputed_token_counts_are_left_alone():
    """Leave a file already sized by pr_generate_extended_diff untouched."""
    f = _file("a.py", 5)
    f.tokens = 12345

    pr_generate_compressed_diff([{"language": "Python", "files": [f]}],
                                FakeTokenHandler(), "gpt-4", False, False)

    assert f.tokens == 12345
