import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from requests import Response
from requests.exceptions import ConnectionError, HTTPError, JSONDecodeError, SSLError, Timeout

from tests.e2e_tests import test_gitea_app as gitea_e2e

REPO_API = "https://gitea.example.test/api/v1/repos/codiumai/pr-agent-tests"
DESCRIPTION = "### **PR Type**\nbug fix\n\n### **Description**\n- Update the CLI."
REVIEW = {"body": "## Team review\n\n<!-- pr-agent:review:full -->\n\nReview results."}
IMPROVE = {"body": "## Team suggestions\n\n<!-- pr-agent:improve:summary -->\n\nSuggested changes."}


def _response(payload, status=200):
    """Use real Requests status and JSON handling without a network connection."""
    response = Response()
    response.status_code = status
    response.url = REPO_API
    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    return response


@pytest.fixture
def case(monkeypatch):
    """Isolate the E2E settings, HTTP calls, logging, and polling delays."""
    settings = SimpleNamespace(
        config=SimpleNamespace(git_provider=None),
        get={"GITEA.URL": "https://gitea.example.test", "GITEA.TOKEN": "test-token"}.get,
    )
    http = MagicMock(spec=["get", "post", "put", "patch", "delete"])
    sleep = MagicMock()
    monkeypatch.setattr(gitea_e2e, "get_settings", lambda: settings)
    monkeypatch.setattr(gitea_e2e, "requests", http)
    monkeypatch.setattr(gitea_e2e, "logger", MagicMock())
    monkeypatch.setattr(gitea_e2e.time, "sleep", sleep)
    monkeypatch.setattr(gitea_e2e, "NUM_MINUTES", 2)

    def get(url, **kwargs):
        if "/contents/" in url:
            return _response({"sha": "file-sha"})
        if url == f"{REPO_API}/pulls/123":
            return _response({"body": DESCRIPTION})
        if url == f"{REPO_API}/issues/123/comments":
            return _response([REVIEW, IMPROVE])
        raise AssertionError(f"Unexpected GET: {url}")

    def post(url, **kwargs):
        if url.endswith("/branches"):
            return _response({}, 201)
        if "/contents/" in url:
            return _response({}, 201)
        if url.endswith("/pulls"):
            return _response({"number": 123}, 201)
        raise AssertionError(f"Unexpected POST: {url}")

    http.get.side_effect = get
    http.post.side_effect = post
    http.put.return_value = _response({})
    http.patch.return_value = _response({})
    http.delete.return_value = _response(None, 204)
    return http, sleep


@pytest.mark.parametrize("file_status", [200, 404])
def test_e2e_updates_existing_files_and_creates_only_missing_files(case, file_status):
    http, sleep = case
    original_get = http.get.side_effect
    http.get.side_effect = lambda url, **kwargs: (
        _response({"sha": "file-sha"}, file_status) if "/contents/" in url else original_get(url, **kwargs)
    )

    gitea_e2e.test_e2e_run_gitea_app()

    writes = [call for call in http.mock_calls if call[0] in {"post", "put"} and "/contents/" in call.args[0]]
    assert len(writes) == 1
    write = writes[0]
    assert write[0] == ("post" if file_status == 404 else "put")
    assert write.args[0] == f"{REPO_API}/contents/{gitea_e2e.FILE_PATH}"
    payload = write.kwargs["json"]
    assert payload["branch"] == http.post.call_args_list[0].kwargs["json"]["new_branch_name"]
    assert base64.b64decode(payload["content"]).decode() == gitea_e2e.NEW_FILE_CONTENT
    assert payload["message"] == ("Add cli_pip.py" if file_status == 404 else "Update cli_pip.py")
    assert ("sha" in payload) == (file_status == 200)
    if file_status == 200:
        assert payload["sha"] == "file-sha"
    assert write.kwargs["headers"]["Authorization"] == "token test-token"
    sleep.assert_called_once_with(60)
    http.patch.assert_called_once()
    http.delete.assert_called_once()


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_file_lookup_http_errors_do_not_trigger_a_write(case, status):
    http, _ = case
    response = _response({}, status)
    http.get.return_value = response
    http.get.side_effect = None

    with pytest.raises(HTTPError) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value.response is response
    http.put.assert_not_called()
    assert http.post.call_count == 1
    http.delete.assert_called_once()


@pytest.mark.parametrize("error", [Timeout("timeout"), ConnectionError("connection"), SSLError("certificate")])
def test_file_lookup_transport_errors_preserve_the_original_exception(case, error):
    http, _ = case
    http.get.side_effect = error

    with pytest.raises(type(error)) as caught:
        gitea_e2e.test_e2e_run_gitea_app()

    assert caught.value is error
    http.put.assert_not_called()
    assert http.post.call_count == 1
    http.delete.assert_called_once()


@pytest.mark.parametrize("malformed_json", [True, False])
def test_invalid_file_metadata_is_not_treated_as_a_missing_file(case, malformed_json):
    http, _ = case
    response = _response({})
    if malformed_json:
        response._content = b"{"
    http.get.side_effect = None
    http.get.return_value = response

    with pytest.raises(JSONDecodeError if malformed_json else KeyError):
        gitea_e2e.test_e2e_run_gitea_app()

    http.put.assert_not_called()
    assert http.post.call_count == 1
    http.delete.assert_called_once()


@pytest.mark.parametrize("missing", ["/describe", "/review", "/improve"])
def test_missing_tool_output_times_out_with_its_name_and_cleans_up(case, missing):
    http, sleep = case
    original_get = http.get.side_effect

    def get(url, **kwargs):
        if url == f"{REPO_API}/pulls/123" and missing == "/describe":
            return _response({"body": "update cli_pip.py"})
        if url == f"{REPO_API}/issues/123/comments":
            comments = [REVIEW, IMPROVE]
            if missing == "/review":
                comments = [IMPROVE]
            elif missing == "/improve":
                comments = [REVIEW]
            return _response(comments)
        return original_get(url, **kwargs)

    http.get.side_effect = get

    with pytest.raises(AssertionError, match=missing):
        gitea_e2e.test_e2e_run_gitea_app()

    assert sleep.call_count == 2
    http.patch.assert_called_once()
    http.delete.assert_called_once()


def test_polling_waits_for_results_instead_of_failing_on_comment_count(case):
    http, sleep = case
    original_get = http.get.side_effect
    poll = {"number": 0}

    def get(url, **kwargs):
        if url == f"{REPO_API}/pulls/123":
            poll["number"] += 1
            body = "update cli_pip.py" if poll["number"] == 1 else DESCRIPTION
            return _response({"body": body})
        if url == f"{REPO_API}/issues/123/comments":
            if poll["number"] == 1:
                comments = [{"body": f"unrelated {i}"} for i in range(8)]
                return _response(comments)
        return original_get(url, **kwargs)

    http.get.side_effect = get

    gitea_e2e.test_e2e_run_gitea_app()

    assert sleep.call_count == 2
    http.patch.assert_called_once()
    http.delete.assert_called_once()


def test_result_detection_accepts_no_suggestions_and_reads_comments_once(case):
    http, _ = case
    no_suggestions = {"body": "## No suggestions\n\n<!-- pr-agent:improve:no-suggestions -->"}

    def get(url, **kwargs):
        if url == f"{REPO_API}/pulls/123":
            return _response({"body": DESCRIPTION})
        if url == f"{REPO_API}/issues/123/comments":
            return _response([REVIEW, no_suggestions])
        raise AssertionError(f"Unexpected GET: {url}")

    http.get.side_effect = get

    assert gitea_e2e._missing_gitea_tool_results(REPO_API, 123, {"Authorization": "token test"}) == []
    pr_call = next(call for call in http.get.call_args_list if call.args[0] == f"{REPO_API}/pulls/123")
    comment_calls = [call for call in http.get.call_args_list if "/comments" in call.args[0]]
    assert pr_call.kwargs["timeout"] == 30
    assert len(comment_calls) == 1
    assert comment_calls[0].kwargs["timeout"] == 30


def test_result_detection_reports_missing_tools_without_comment_order_assumptions(case):
    http, _ = case

    def get(url, **kwargs):
        if url == f"{REPO_API}/pulls/123":
            return _response({"body": "update cli_pip.py"})
        if url == f"{REPO_API}/issues/123/comments":
            comments = [IMPROVE, {"body": "unrelated"}, REVIEW]
            return _response(comments)
        raise AssertionError(f"Unexpected GET: {url}")

    http.get.side_effect = get

    assert gitea_e2e._missing_gitea_tool_results(REPO_API, 123, {}) == ["/describe"]
