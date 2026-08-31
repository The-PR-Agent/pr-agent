"""Read config.verbosity_level whatever numeric form the settings file uses."""
import pytest

from pr_agent.algo.pr_processing import get_pr_diff
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings, get_verbosity_level


class BigPRProvider:
    def get_diff_files(self):
        patch = "@@ -0,0 +1,4000 @@\n" + "\n".join(f"+line {i} " + "x" * 200 for i in range(4000))
        return [FilePatchInfo(base_file="", head_file="", patch=patch, filename="big.py",
                              edit_type=EDIT_TYPE.ADDED)]

    def get_languages(self):
        return {"Python": 100}

    def get_files(self):
        return ["big.py"]

    def is_supported(self, capability):
        return True


@pytest.fixture
def restore_verbosity():
    settings = get_settings(use_context=False)
    original = settings.get("config.verbosity_level", 0)
    yield settings
    settings.set("config.verbosity_level", original)


@pytest.mark.parametrize("value, expected", [(2, 2), ("2", 2), (0, 0), ("0", 0), (" 3 ", 3)])
def test_read_a_numeric_verbosity_level(restore_verbosity, value, expected):
    """A quoted number is what TOML yields for verbosity_level = "2"."""
    restore_verbosity.set("config.verbosity_level", value)

    assert get_verbosity_level() == expected


@pytest.mark.parametrize("value", ["chatty", None, [2]])
def test_fall_back_to_silent_for_an_unusable_verbosity_level(restore_verbosity, value):
    """Fall back to the quietest level rather than raising mid-command."""
    restore_verbosity.set("config.verbosity_level", value)

    assert get_verbosity_level() == 0


def test_build_a_large_pr_diff_with_a_quoted_verbosity_level(restore_verbosity):
    """The diff pipeline compares verbosity_level, so a quoted value used to break /review."""
    restore_verbosity.set("config.verbosity_level", "2")
    token_handler = TokenHandler(type("PR", (), {"title": "t"})(), {}, "system", "user")

    assert get_pr_diff(BigPRProvider(), token_handler, "gpt-4o") is not None
