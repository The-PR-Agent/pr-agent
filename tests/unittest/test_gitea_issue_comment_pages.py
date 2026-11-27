"""Exercise the pinned Gitea SDK against a real, paginated local comments API."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import giteapy
import pytest
from giteapy.rest import ApiException

from pr_agent.git_providers.gitea_provider import GiteaProvider, RepoApi


@pytest.fixture
def comment_api():
    state = {"comments": [], "requests": [], "fail_page": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            page = int(query.get("page", ["1"])[0])
            state["requests"].append((url.path, query))
            status = 200
            if url.path.endswith("/comments"):
                # Simulate MAX_RESPONSE_ITEMS=2, below the requested page size.
                payload = state["comments"][(page - 1) * 2:page * 2]
                if page == state["fail_page"]:
                    status, payload = 503, {"message": "temporary failure"}
            elif url.path.endswith("/reviews"):
                payload = []
            else:
                status, payload = 404, {"message": "unknown endpoint"}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    configuration = giteapy.Configuration()
    configuration.host = f"http://127.0.0.1:{server.server_port}/api/v1"
    configuration.api_key["Authorization"] = "token fixture-only"
    client = giteapy.ApiClient(configuration)
    try:
        yield RepoApi(client), state
    finally:
        client.rest_client.pool_manager.clear()
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def comments(count):
    return [
        {"id": index, "body": f"comment {index}", "user": {"login": "review-bot"},
         "created_at": f"2026-01-0{index}T12:00:00Z", "html_url": f"https://git.example/comment/{index}"}
        for index in range(1, count + 1)
    ]


def provider(api, issue=False):
    result = GiteaProvider.__new__(GiteaProvider)
    result.repo_api = api
    result.owner, result.repo = "owner", "repo"
    result.pr_number, result.issue_number = 42, 73
    result.enabled_issue = issue
    return result


@pytest.mark.parametrize("issue", [False, True], ids=["pull-request", "issue"])
def test_all_comment_pages_keep_sdk_models(comment_api, issue):
    api, state = comment_api
    state["comments"] = comments(5)

    result = provider(api, issue).get_issue_comments()

    assert [comment.id for comment in result] == [1, 2, 3, 4, 5]
    assert all(isinstance(comment, giteapy.Comment) for comment in result)
    assert result[-1].body == "comment 5"
    assert result[-1].user.login == "review-bot"
    assert result[-1].created_at.year == 2026
    assert result[-1].html_url == "https://git.example/comment/5"
    index = 73 if issue else 42
    assert {path for path, _ in state["requests"]} == {f"/api/v1/repos/owner/repo/issues/{index}/comments"}
    assert [query["page"] for _, query in state["requests"]] == [["1"], ["2"], ["3"], ["4"]]


def test_later_comment_bodies_reach_persistent_lookup_and_dedup(comment_api):
    api, state = comment_api
    state["comments"] = comments(4)
    git_provider = provider(api)

    assert [comment.id for comment in git_provider.get_issue_comments_newest_first()] == [4, 3, 2, 1]
    assert git_provider.get_persistent_comment_bodies() == ["comment 1", "comment 2", "comment 3", "comment 4"]


@pytest.mark.parametrize("count", [0, 1], ids=["empty", "single-page"])
def test_empty_and_single_comment_pages(comment_api, count):
    api, state = comment_api
    state["comments"] = comments(count)

    result = provider(api).get_issue_comments()

    assert [comment.id for comment in result] == list(range(1, count + 1))
    assert [comment.body for comment in result] == [comment["body"] for comment in state["comments"]]


def test_later_page_failure_does_not_return_partial_comments(comment_api):
    api, state = comment_api
    state["comments"] = comments(4)
    state["fail_page"] = 2

    with pytest.raises(ApiException) as error:
        provider(api).get_issue_comments()

    assert error.value.status == 503
    assert [query["page"] for _, query in state["requests"]] == [["1"], ["2"]]
