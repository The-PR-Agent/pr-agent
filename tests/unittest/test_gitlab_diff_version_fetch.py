import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import requests

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def test_set_merge_request_fetches_only_latest_diff_version(monkeypatch):
    settings = get_settings()
    monkeypatch.setitem(settings.gitlab, "url", "https://gitlab.example")
    monkeypatch.setitem(settings.gitlab, "personal_access_token", "offline-token")
    version_queries = []

    def send(_session, request, **_kwargs):
        url = urlparse(request.url)
        headers = {"Content-Type": "application/json"}
        if url.path.endswith("/api/v4/projects/group%2Frepo"):
            payload = {"id": 41}
        elif url.path.endswith("/merge_requests/42"):
            payload = {"iid": 42, "title": "A merge request"}
        elif url.path.endswith("/versions"):
            query = parse_qs(url.query)
            version_queries.append(query)
            payload = [{"id": 3 if query.get("page", ["1"]) == ["1"] else 2}]
            if query.get("page", ["1"]) == ["1"]:
                headers["Link"] = (
                    '<https://gitlab.example/api/v4/projects/41/merge_requests/42/versions?page=2>; rel="next"'
                )
        else:
            raise AssertionError(f"Unexpected GitLab request: {request.url}")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.headers.update(headers)
        response.request = request
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    provider = GitLabProvider("https://gitlab.example/group/repo/-/merge_requests/42")

    assert provider.get_title() == "A merge request"
    assert version_queries == [{"page": ["1"], "per_page": ["1"]}]


def test_get_relevant_diff_fetches_only_latest_diff_version():
    provider = GitLabProvider.__new__(GitLabProvider)

    latest_diff = object()
    list_diffs = Mock(return_value=[latest_diff])
    provider.mr = SimpleNamespace(diffs=SimpleNamespace(list=list_diffs))
    provider.last_diff = latest_diff
    provider._get_merge_request_changes = Mock(return_value={
        "changes": [{"new_path": "src/app.py", "diff": "@@\n+hello"}],
        "diff_refs": {"base_sha": "base", "head_sha": "head"},
    })
    provider._expand_submodule_changes = Mock(side_effect=lambda changes, diff_refs=None: changes)

    assert provider.get_relevant_diff("src/app.py", "hello") is latest_diff

    list_diffs.assert_called_once_with(page=1, per_page=1, get_all=False)
