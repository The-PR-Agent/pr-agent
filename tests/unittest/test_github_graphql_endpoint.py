"""GraphQL requests go to the endpoint PyGithub derives from ``github.base_url``.

GitHub Enterprise Server serves REST under ``/api/v3`` and GraphQL at ``/api/graphql``. These tests
drive a real PyGithub client and stub ``requests.Session`` underneath it, so they check the request
path the provider actually sends. The stub answers GraphQL only at the documented endpoint.
"""
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests
from github import Auth, Github

from pr_agent.git_providers.github_provider import GithubProvider

GITHUB_COM = "https://api.github.com"
ENTERPRISE_SERVER = "https://github.example.com/api/v3"
# base_url -> (REST path prefix, GraphQL path)
ENDPOINTS = {
    GITHUB_COM: ("", "/graphql"),
    ENTERPRISE_SERVER: ("/api/v3", "/api/graphql"),
}
BASE_URLS = pytest.mark.parametrize("base_url", list(ENDPOINTS), ids=["github.com", "enterprise-server"])


class _GitHubStub:
    """Serve a REST route table and GraphQL at one path; answer anything else with a 404."""

    def __init__(self, graphql_path, rest_routes, graphql_responses):
        self.graphql_path = graphql_path
        self.rest_routes = rest_routes
        self.graphql_responses = list(graphql_responses)
        self.sent = []

    def handle(self, method, url):
        path = urlsplit(url).path
        self.sent.append((method, path))
        if method == "POST" and path == self.graphql_path:
            status, body = 200, self.graphql_responses.pop(0)
        elif (method, path) in self.rest_routes:
            status, body = 200, self.rest_routes[(method, path)]
        else:
            status, body = 404, {"message": "Not Found"}
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = "application/json; charset=utf-8"
        response.encoding = "utf-8"
        response._content = json.dumps(body).encode()
        return response


def _provider(monkeypatch, base_url, rest_routes=None, graphql_responses=()):
    stub = _GitHubStub(ENDPOINTS[base_url][1], rest_routes or {}, graphql_responses)
    monkeypatch.setattr(requests.Session, "request", lambda session, method, url, **kwargs: stub.handle(method, url))
    client = Github(auth=Auth.Token("token"), base_url=base_url, seconds_between_requests=0, seconds_between_writes=0)
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.pr_num = 7
    provider.base_url = base_url
    provider.github_client = client
    provider.pr = SimpleNamespace(_requester=client._Github__requester)
    return provider, stub


@BASE_URLS
def test_fetch_sub_issues_queries_the_graphql_endpoint(monkeypatch, base_url):
    sub_issue = "https://github.example.com/owner/repo/issues/2"
    provider, stub = _provider(monkeypatch, base_url, graphql_responses=[
        {"data": {"repository": {"issue": {"id": "I_1", "subIssues": {"nodes": [{"url": sub_issue}]}}}}},
    ])

    assert provider.fetch_sub_issues("https://github.example.com/owner/repo/issues/1") == {sub_issue}
    assert stub.sent == [("POST", ENDPOINTS[base_url][1])]


@BASE_URLS
def test_resolve_comment_thread_sends_the_mutation_to_the_graphql_endpoint(monkeypatch, base_url):
    rest_prefix, graphql_path = ENDPOINTS[base_url]
    comment_path = f"{rest_prefix}/repos/owner/repo/pulls/comments/123"
    thread = {"id": "PRRT_1", "isResolved": False, "comments": {"nodes": [{"id": "PRRC_1"}]}}
    provider, stub = _provider(
        monkeypatch,
        base_url,
        rest_routes={("GET", comment_path): {"node_id": "PRRC_1"}},
        graphql_responses=[
            {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [thread],
            }}}}},
            {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}},
        ],
    )

    assert provider.resolve_comment_thread(123) is True
    # The thread lookup and the resolve mutation both use the GraphQL endpoint.
    assert stub.sent == [("GET", comment_path), ("POST", graphql_path), ("POST", graphql_path)]
