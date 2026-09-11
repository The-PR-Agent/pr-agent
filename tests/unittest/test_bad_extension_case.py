"""Match bad extensions case-insensitively, so 'logo.SVG' is dropped like 'logo.svg'."""
import pytest

from pr_agent.algo.language_handler import filter_bad_extensions, is_valid_file


class _File:
    def __init__(self, filename):
        self.filename = filename


@pytest.mark.parametrize("filename", ["logo.SVG", "logo.Svg", "report.CSV", "photo.JPG", "bundle.ZIP"])
def test_uppercase_bad_extension_is_dropped(filename):
    """An asset named with an uppercase extension is the same asset."""
    assert is_valid_file(filename) is False


@pytest.mark.parametrize("filename", ["logo.svg", "report.csv", "photo.jpg", "bundle.zip"])
def test_lowercase_bad_extension_is_still_dropped(filename):
    """Keep the existing behaviour for the canonical spelling."""
    assert is_valid_file(filename) is False


@pytest.mark.parametrize("filename", ["main.py", "Makefile", "app.SVGZ"])
def test_reviewable_files_are_kept(filename):
    """Do not widen the filter: only the extension itself is compared."""
    assert is_valid_file(filename) is True


@pytest.mark.parametrize("filename,configured", [("archive.TAR", ["tar"]), ("archive.tar", ["TAR"])])
def test_configured_extension_matches_either_case(filename, configured):
    """A user-supplied bad_extensions entry matches regardless of how it is spelled."""
    assert is_valid_file(filename, bad_extensions=configured) is False


def test_filter_bad_extensions_drops_uppercase_assets():
    """The filter used by sort_files_by_main_languages drops them too."""
    files = [_File("logo.SVG"), _File("main.py")]

    assert [f.filename for f in filter_bad_extensions(files)] == ["main.py"]
