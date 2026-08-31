"""Report an ignore pattern that cannot compile, instead of dropping it silently."""
import copy
import io

import pytest

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


class _File:
    def __init__(self, filename):
        self.filename = filename


@pytest.fixture
def restore_ignore():
    settings = get_settings(use_context=False)
    original = copy.deepcopy(settings.get("IGNORE", None))
    yield settings
    if original is None:
        settings.set("IGNORE", {})
    else:
        settings.set("IGNORE", original)


def _capture(call):
    buffer = io.StringIO()
    handler_id = get_logger().add(buffer, level="DEBUG", format="{message}", colorize=False)
    try:
        call()
    finally:
        get_logger().remove(handler_id)
    return buffer.getvalue()


def test_warn_about_an_invalid_ignore_regex(restore_ignore):
    """Warn so a typo does not silently stop excluding files."""
    restore_ignore.set("ignore.regex", ["(((|["])
    files = [_File("a.py")]

    logged = _capture(lambda: filter_ignored(files))

    assert "invalid ignore pattern" in logged.lower()


def test_a_valid_pattern_still_filters(restore_ignore):
    """A usable pattern keeps working exactly as before."""
    restore_ignore.set("ignore.regex", [r"^vendor/.*"])

    kept = filter_ignored([_File("vendor/lib.py"), _File("app.py")])

    assert [f.filename for f in kept] == ["app.py"]


def test_an_invalid_pattern_does_not_disable_the_valid_ones(restore_ignore):
    """One bad pattern must not stop the remaining patterns from applying."""
    restore_ignore.set("ignore.regex", ["(((|[", r"^vendor/.*"])

    kept = filter_ignored([_File("vendor/lib.py"), _File("app.py")])

    assert [f.filename for f in kept] == ["app.py"]
