import pytest

from pr_agent.git_providers.git_provider import GitProvider


def test_language_percentages_count_recognised_files_and_skip_empty_paths():
    languages = GitProvider._language_percentages(["a.py", "b.py", "", None, "c.js", "README"])

    assert languages == {"Python": pytest.approx(200 / 3), "JavaScript": pytest.approx(100 / 3)}


def test_language_percentages_of_nothing_is_empty():
    assert GitProvider._language_percentages([]) == {}
