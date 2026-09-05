"""get_version() must not trust a pyproject.toml that belongs to a different project."""
import pytest

from pr_agent.algo import utils
from pr_agent.algo.utils import get_version

INSTALLED_VERSION = "9.44.0-from-package-metadata"


@pytest.fixture
def installed_version(monkeypatch):
    """Make the package-metadata fallback return a recognizable sentinel."""
    monkeypatch.setattr(utils, "version", lambda name: INSTALLED_VERSION)
    return INSTALLED_VERSION


def write_pyproject(directory, content, encoding="utf-8"):
    (directory / "pyproject.toml").write_text(content, encoding=encoding)


def test_no_pyproject_in_cwd_uses_the_installed_version(tmp_path, monkeypatch, installed_version):
    monkeypatch.chdir(tmp_path)

    assert get_version() == INSTALLED_VERSION


def test_an_unrelated_pyproject_does_not_dictate_our_version(tmp_path, monkeypatch, installed_version):
    """Running from any project directory must not report that project's version."""
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[project]\nname = "totally-unrelated"\nversion = "9.9.9"\n')

    assert get_version() == INSTALLED_VERSION


def test_a_uv_init_scaffold_does_not_dictate_our_version(tmp_path, monkeypatch, installed_version):
    """`uv init` scaffolds version = "0.1.0", the value seen in the wild."""
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[project]\nname = "my-app"\nversion = "0.1.0"\n')

    assert get_version() == INSTALLED_VERSION


def test_the_pr_agent_pyproject_supplies_the_repo_version(tmp_path, monkeypatch, installed_version):
    """Running out of the pr-agent repository still reports the checkout's version."""
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[project]\nname = "pr-agent"\nversion = "1.2.3"\n')

    assert get_version() == "1.2.3"


def test_an_unparseable_pyproject_falls_back_instead_of_raising(tmp_path, monkeypatch, installed_version):
    """A malformed file in the cwd must not take down every pr-agent command."""
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, "this is not valid toml at all\n")

    assert get_version() == INSTALLED_VERSION


def test_a_pyproject_with_a_utf8_bom_falls_back_instead_of_raising(tmp_path, monkeypatch, installed_version):
    """tomllib rejects a BOM, which Windows editors and PowerShell write by default."""
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[project]\nname = "pr-agent"\nversion = "1.2.3"\n', encoding="utf-8-sig")

    assert get_version() == INSTALLED_VERSION


def test_a_non_utf8_pyproject_falls_back_instead_of_raising(tmp_path, monkeypatch, installed_version):
    """tomllib decodes the bytes itself, so a cp1252 file raises UnicodeDecodeError before any parsing.

    UnicodeDecodeError is a sibling of TOMLDecodeError under ValueError, not a subclass.
    """
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[project]\nname = "café-app"\nversion = "7.7.7"\n', encoding="cp1252")

    assert get_version() == INSTALLED_VERSION


def test_a_pyproject_without_a_project_table_falls_back(tmp_path, monkeypatch, installed_version):
    """Poetry-style files have no [project] table."""
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[tool.poetry]\nname = "legacy"\nversion = "2.2.2"\n')

    assert get_version() == INSTALLED_VERSION


def test_our_pyproject_without_a_version_falls_back(tmp_path, monkeypatch, installed_version):
    monkeypatch.chdir(tmp_path)
    write_pyproject(tmp_path, '[project]\nname = "pr-agent"\n')

    assert get_version() == INSTALLED_VERSION
