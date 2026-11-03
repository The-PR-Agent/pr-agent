"""GitLab list calls must request the maximum page size when reading everything."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pr_agent.git_providers.gitlab_provider as gitlab_provider_module
from pr_agent.git_providers.gitlab_provider import GitLabProvider


class _PaginatedList:
    """Fake python-gitlab manager that counts requests for get_all pagination."""

    def __init__(self, items):
        self.items = items
        self.requests = 0
        self.calls = []

    def _collect(self, kwargs):
        self.calls.append(kwargs)
        per_page = kwargs.get("per_page") or 20
        if not kwargs.get("get_all", False):
            self.requests += 1
            return self.items[:per_page]
        page = 0
        result = []
        while True:
            self.requests += 1
            chunk = self.items[page * per_page:(page + 1) * per_page]
            result.extend(chunk)
            if len(chunk) < per_page:
                return result
            page += 1

    def list(self, **kwargs):
        return self._collect(kwargs)

    def http_list(self, _path, **kwargs):
        return self._collect(kwargs)


def _provider_with_lists(count=150):
    discussions = _PaginatedList([
        SimpleNamespace(attributes={"notes": [{"body": f"discussion-{index}"}]})
        for index in range(count)
    ])
    notes = _PaginatedList([SimpleNamespace(body=f"note-{index}") for index in range(count)])
    draft_notes = _PaginatedList([SimpleNamespace(note=f"draft-{index}") for index in range(count)])
    provider = object.__new__(GitLabProvider)
    provider.mr = SimpleNamespace(discussions=discussions, notes=notes, draft_notes=draft_notes)
    return provider, discussions, notes, draft_notes


def test_persistent_comment_listing_requests_max_pages():
    provider, discussions, notes, draft_notes = _provider_with_lists()

    bodies = provider.get_persistent_comment_bodies()

    assert len(bodies) == 450
    for listing in (discussions, notes, draft_notes):
        assert listing.requests == 2
        assert listing.calls == [{"get_all": True, "per_page": 100}]


def test_issue_comment_listing_requests_max_pages():
    provider, _, notes, _ = _provider_with_lists()

    comments = provider.get_issue_comments()

    assert len(comments) == 150
    assert notes.requests == 2
    assert notes.calls == [{"get_all": True, "per_page": 100}]


def test_diff_listing_requests_max_pages():
    changes = [{"new_path": f"file-{index}.py"} for index in range(150)]
    diffs = _PaginatedList(changes)
    provider = object.__new__(GitLabProvider)
    provider.gl = SimpleNamespace(
        http_get=lambda _path: {
            "sha": "head",
            "diff_refs": {"base_sha": "base", "head_sha": "head"},
            "changes_count": "150",
        },
        http_list=diffs.http_list,
    )

    result = provider._collect_merge_request_changes("/projects/1/merge_requests/1")

    assert len(result["changes"]) == 150
    assert diffs.requests == 2
    assert diffs.calls == [{"get_all": True, "per_page": 100}]


def test_every_get_all_call_sets_per_page():
    source = Path(gitlab_provider_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    get_all_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        get_all = keywords.get("get_all")
        if isinstance(get_all, ast.Constant) and get_all.value is True:
            get_all_calls.append(keywords)

    assert get_all_calls
    for keywords in get_all_calls:
        per_page = keywords.get("per_page")
        assert isinstance(per_page, ast.Constant) and per_page.value == 100
