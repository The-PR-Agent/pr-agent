"""Pin the exception contract of the GitHub provider: handle the expected, surface the rest.

Read each test as one method's contract. While these handlers caught bare `Exception`, a
`TypeError` from our own code was indistinguishable from a GitHub outage: it was logged as an
API failure and the run continued with a wrong result. Keep the API or transport error
swallowed the way callers rely on, and let a programming error propagate.
"""

import binascii
from types import SimpleNamespace

import pytest
from github import GithubException
from requests.exceptions import RequestException

from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.log import get_logger


class _Requester:
    """Raise the configured error for every request, or return the canned response."""

    def __init__(self, error=None, response=None):
        self.error = error
        self.response = response or ({}, {"id": 1})

    def requestJsonAndCheck(self, method, url, **kwargs):
        if self.error is not None:
            raise self.error
        return self.response


def _make_provider(requester=None, pr_extra=None):
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo = "owner/repo"
    provider.base_url = "https://api.github.com"
    provider.pr = SimpleNamespace(
        _requester=requester or _Requester(),
        issue_url="https://api.github.com/repos/owner/repo/issues/1",
        **(pr_extra or {}),
    )
    provider.last_commit_id = SimpleNamespace(sha="deadbeef")
    provider._check_run_ids = {}
    provider._check_runs_in_progress = set()
    provider.github_user_id = ""
    return provider


API_ERRORS = [
    pytest.param(GithubException(500, {"message": "boom"}, {}), id="github-api-error"),
    pytest.param(RequestException("connection reset"), id="transport-error"),
]

# A bug in our own code, not a failure of the remote side.
UNEXPECTED_ERRORS = [
    pytest.param(TypeError("unhashable type"), id="TypeError"),
    pytest.param(AttributeError("'NoneType' object has no attribute 'sha'"), id="AttributeError"),
]


@pytest.mark.parametrize("error", API_ERRORS)
def test_find_existing_check_run_returns_none_on_api_failure(error):
    provider = _make_provider(_Requester(error=error))
    assert provider._find_existing_check_run("PR Agent - Review", "deadbeef") is None


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_find_existing_check_run_propagates_unexpected_errors(error):
    provider = _make_provider(_Requester(error=error))
    with pytest.raises(type(error)):
        provider._find_existing_check_run("PR Agent - Review", "deadbeef")


@pytest.mark.parametrize("error", API_ERRORS)
def test_add_reaction_returns_none_on_api_failure(error):
    provider = _make_provider(_Requester(error=error))
    assert provider.add_reaction(123, "eyes") is None


@pytest.mark.parametrize("error", UNEXPECTED_ERRORS)
def test_add_reaction_propagates_unexpected_errors(error):
    provider = _make_provider(_Requester(error=error))
    with pytest.raises(type(error)):
        provider.add_reaction(123, "eyes")


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_pr_labels_returns_empty_list_on_api_failure(error):
    provider = _make_provider(_Requester(error=error))
    assert provider.get_pr_labels(update=True) == []


def test_get_pr_labels_propagates_unexpected_errors():
    """Keep TypeError expected here: the labels payload is indexed as ``label["name"]``."""
    provider = _make_provider(_Requester(error=AttributeError("no issue_url")))
    with pytest.raises(AttributeError):
        provider.get_pr_labels(update=True)


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_user_id_falls_back_to_empty_on_api_failure(error):
    provider = _make_provider()
    provider.github_client = SimpleNamespace(get_user=lambda: (_ for _ in ()).throw(error))
    assert provider.get_user_id() == ""


def test_get_user_id_propagates_unexpected_errors():
    provider = _make_provider()
    provider.github_client = SimpleNamespace(
        get_user=lambda: (_ for _ in ()).throw(TypeError("bad client"))
    )
    with pytest.raises(TypeError):
        provider.get_user_id()


def test_get_user_id_still_tolerates_a_login_less_payload():
    """Treat a login-less payload as an unresolved user: the key is read straight from the API."""
    provider = _make_provider()
    provider.github_client = SimpleNamespace(get_user=lambda: SimpleNamespace(raw_data={}))
    assert provider.get_user_id() == ""


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_commit_messages_returns_empty_string_on_api_failure(error):
    provider = _make_provider(pr_extra={"get_commits": lambda: (_ for _ in ()).throw(error)})
    assert provider.get_commit_messages() == ""


def test_get_commit_messages_propagates_unexpected_errors():
    provider = _make_provider(pr_extra={"get_commits": lambda: (_ for _ in ()).throw(TypeError("boom"))})
    with pytest.raises(TypeError):
        provider.get_commit_messages()


@pytest.mark.parametrize(
    "payload",
    [b"not json at all", b'["a list, not an object"]'],
    ids=["malformed-json", "json-that-is-not-an-object"],
)
def test_fetch_sub_issues_falls_back_to_an_empty_set_on_a_bad_graphql_payload(payload):
    """Return no sub-issues rather than escaping into the compliance caller.

    The GraphQL body is json.loads-ed and then walked with .get(), so a malformed payload
    raises ValueError and a valid non-object payload raises AttributeError.
    """
    provider = _make_provider()
    requester = SimpleNamespace(requestJson=lambda method, url, input=None: (200, {}, payload))
    provider.github_client = SimpleNamespace(_Github__requester=requester)

    assert provider.fetch_sub_issues("https://github.com/owner/repo/issues/1") == set()


def _provider_with_corrupt_file_content():
    """Serve a ContentFile whose base64 payload cannot be decoded.

    PyGithub decodes the body inside `decoded_content`, so the failure surfaces at attribute
    access rather than at the request.
    """
    provider = _make_provider()

    class _CorruptContent:
        @property
        def decoded_content(self):
            raise binascii.Error("Invalid base64-encoded string")

    provider._get_repo = lambda: SimpleNamespace(
        get_contents=lambda path, ref=None: _CorruptContent()
    )
    return provider


def test_get_pr_file_content_returns_empty_string_on_a_corrupt_payload():
    """Do not let a corrupt body escape: the diff builder re-raises anything that does as
    RateLimitExceeded, which retries the review as though GitHub had throttled it."""
    provider = _provider_with_corrupt_file_content()

    assert provider.get_pr_file_content("a.py", "main") == ""


def test_get_pr_file_content_propagates_a_corrupt_payload_when_asked():
    provider = _provider_with_corrupt_file_content()

    with pytest.raises(binascii.Error):
        provider.get_pr_file_content("a.py", "main", propagate_errors=True)


def test_get_pr_file_content_returns_empty_for_a_submodule_entry():
    """Keep AssertionError expected here: PyGithub asserts on an entry with no base64 content."""
    from github.ContentFile import ContentFile

    requester = SimpleNamespace(is_not_lazy=False)
    entry = ContentFile(requester, {}, {"type": "submodule", "path": "vendor/lib", "size": 0}, completed=True)
    provider = _make_provider()
    provider._get_repo = lambda: SimpleNamespace(get_contents=lambda path, ref=None: entry)

    assert provider.get_pr_file_content("vendor/lib", "head-sha") == ""


def _capture_logs(call):
    """Run `call` with a loguru sink attached and return (result, captured lines)."""
    captured = []
    sink_id = get_logger().add(lambda message: captured.append(str(message)), format="{message}")
    try:
        result = call()
    finally:
        get_logger().remove(sink_id)
    return result, captured


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_user_id_records_why_the_login_is_unresolved(error):
    """Carry the reason into the log: an empty login is otherwise indistinguishable from a
    deployment that has none, and `_resolve_user_login` only ever sees the empty string."""
    provider = _make_provider()
    provider.github_client = SimpleNamespace(get_user=lambda: (_ for _ in ()).throw(error))

    login, captured = _capture_logs(provider.get_user_id)

    assert login == ""
    assert any("Could not resolve the GitHub user id" in line for line in captured)


@pytest.mark.parametrize("error", API_ERRORS)
def test_get_commit_messages_records_why_the_result_is_empty(error):
    """An empty commit-message string otherwise reads the same as a PR with no commits."""
    provider = _make_provider(pr_extra={"get_commits": lambda: (_ for _ in ()).throw(error)})

    messages, captured = _capture_logs(provider.get_commit_messages)

    assert messages == ""
    assert any("Failed to get commit messages" in line for line in captured)


def test_get_user_id_tolerates_a_payload_without_raw_data():
    """Read the payload under its own handler: a body that is not an object is a shape problem,
    not the programming error that `get_user` raising the same type would be."""
    provider = _make_provider()
    provider.github_client = SimpleNamespace(get_user=lambda: SimpleNamespace(raw_data=None))

    assert provider.get_user_id() == ""


def test_get_pr_labels_tolerates_a_payload_of_non_objects():
    """`label["name"]` on a string raises TypeError; the fetch above still propagates it."""
    provider = _make_provider(_Requester(response=({}, ["not-an-object"])))

    assert provider.get_pr_labels(update=True) == []


def test_get_pr_labels_tolerates_labels_without_a_name():
    """The cached path reads `label.name`, so an entry without one is a shape problem too."""
    provider = _make_provider(pr_extra={"labels": [SimpleNamespace()]})

    assert provider.get_pr_labels() == []


def test_upsert_check_run_returns_false_on_an_empty_response_body(monkeypatch):
    """An empty body decodes to None, which is a body problem and not a failed request.

    PyGithub's ``__structuredFromJson`` returns None for a zero-length body, so reading the
    created check run's id raises TypeError rather than KeyError.
    """
    provider = _make_provider(_Requester(response=({}, None)))
    monkeypatch.setattr(provider, "_find_existing_check_run", lambda name, sha: None)

    assert provider._upsert_check_run("Review", {"output": {}}) is False


def test_verify_code_comment_reports_failure_on_an_empty_response_body():
    provider = _make_provider(
        _Requester(response=({}, None)),
        pr_extra={"url": "https://api.github.com/repos/owner/repo/pulls/1"},
    )

    is_verified, error = provider._verify_code_comment({"path": "a.py", "body": "x", "line": 1})

    assert is_verified is False
    assert isinstance(error, TypeError)


def test_validate_comments_inside_hunks_survives_a_regex_error(monkeypatch):
    """Keep ``re.error`` handled: it subclasses Exception directly, so the other four types
    never cover it, and main's ``except Exception`` did.

    #3577 passes the replacement as a function, which removed the trigger that reached this
    handler through a backslash in the proposed code. Pin the handler itself instead, by making
    the substitution fail inside the loop.
    """
    import re as real_re

    class _ReWithFailingSub:
        error = real_re.error
        DOTALL = real_re.DOTALL
        compile = staticmethod(real_re.compile)

        @staticmethod
        def sub(*args, **kwargs):
            raise real_re.error("bad pattern")

    diff_file = SimpleNamespace(filename="a.py", patch="@@ -1,3 +1,3 @@\n-old\n+new\n")
    provider = _make_provider()
    monkeypatch.setattr(provider, "get_diff_files", lambda: [diff_file])
    monkeypatch.setattr(
        "pr_agent.git_providers.github_provider.set_file_languages", lambda files: files)
    monkeypatch.setattr("pr_agent.git_providers.github_provider.re", _ReWithFailingSub)

    suggestions = [{
        "relevant_file": "a.py",
        "relevant_lines_start": 5,
        "relevant_lines_end": 6,
        "original_suggestion": {"existing_code": "old", "improved_code": "new"},
        "body": "```suggestion\nnew\n```",
    }]

    result = provider.validate_comments_inside_hunks(suggestions)

    assert len(result) == 1
    assert result[0]["body"] == suggestions[0]["body"]
