"""Bound the git provider clients this repository drives itself.

PyGithub, boto3 and the Bitbucket SDK set a timeout themselves, and the Azure DevOps client takes
none at all; both are left as they are and documented as uncovered in configuration.toml.
python-gitlab defaults to none, giteapy's generated methods forward one as None unless a caller
supplied a value, and the calls in the Bitbucket and Gerrit providers had none, so a stalled
connection to a self-hosted host would hold a worker - and the event loop it runs on - open
indefinitely. These tests pin the wiring for each of the ones this repository bounds.
"""

import copy
from contextlib import suppress
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_loader import global_settings
from pr_agent.git_providers.git_provider import FileContentSnapshot
from pr_agent.git_providers.request_timeout import (
    DEFAULT_HTTP_REQUEST_TIMEOUT,
    MAX_HTTP_REQUEST_TIMEOUT,
    get_http_request_timeout,
)


@pytest.fixture(autouse=True)
def fresh_global_settings():
    """Restore global_settings, since the repository-settings test below merges into it."""
    snapshot = copy.deepcopy(global_settings.as_dict())
    yield
    for section in set(global_settings.as_dict().keys()) - set(snapshot.keys()):
        global_settings.unset(section)
    for section, contents in snapshot.items():
        global_settings.unset(section)
        global_settings.set(section, copy.deepcopy(contents), merge=False)


@pytest.fixture(autouse=True)
def clear_global_settings_cache():
    """Keep the provider settings cache out of these tests, as the other provider tests do."""
    from pr_agent.git_providers import git_provider as _gp
    from pr_agent.git_providers import request_timeout
    _gp._GLOBAL_SETTINGS_CACHE.clear()
    request_timeout._reported_timeouts.clear()
    yield
    _gp._GLOBAL_SETTINGS_CACHE.clear()
    request_timeout._reported_timeouts.clear()


def _capture_logs(call):
    import io

    from pr_agent.log import get_logger

    buffer = io.StringIO()
    handler_id = get_logger().add(buffer, level='DEBUG', format='{message}', colorize=False)
    try:
        call()
    finally:
        get_logger().remove(handler_id)
    return buffer.getvalue()


def _configured(value, monkeypatch):
    monkeypatch.setattr(global_settings.config, "http_request_timeout", value, raising=False)
    return get_http_request_timeout()


def test_the_shipped_default_matches_the_fallback(monkeypatch):
    """The value in configuration.toml is what an operator gets, and the fallback agrees with it."""
    import tomllib
    from pathlib import Path

    shipped = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pr_agent/settings/configuration.toml").read_text()
    )["config"]["http_request_timeout"]

    assert float(shipped) == DEFAULT_HTTP_REQUEST_TIMEOUT
    assert _configured(shipped, monkeypatch) == DEFAULT_HTTP_REQUEST_TIMEOUT


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(30, 30.0), (2.5, 2.5), ("45", 45.0), (0.5, 0.5)],
)
def test_a_configured_timeout_is_returned_as_seconds(monkeypatch, configured, expected):
    assert _configured(configured, monkeypatch) == expected


@pytest.mark.parametrize("configured", [0, -1, -0.5, True, False, None, "", "abc", float("nan"),
                                        float("inf")])
def test_an_unusable_timeout_falls_back_to_the_default(monkeypatch, configured):
    """A typo must not leave the clients unbounded, which is the whole point of the setting."""
    assert _configured(configured, monkeypatch) == DEFAULT_HTTP_REQUEST_TIMEOUT


@pytest.mark.parametrize("configured", [MAX_HTTP_REQUEST_TIMEOUT, 601, 3600, 86400])
def test_an_oversized_timeout_is_capped_to_the_ceiling(monkeypatch, configured):
    """A repository raising the timeout in its .pr_agent.toml must not undo the bound."""
    assert _configured(configured, monkeypatch) == MAX_HTTP_REQUEST_TIMEOUT


def test_capping_a_timeout_is_reported(monkeypatch):
    from pr_agent.git_providers import request_timeout

    monkeypatch.setattr(request_timeout, "_reported_timeouts", set())
    monkeypatch.setattr(global_settings.config, "http_request_timeout", 86400, raising=False)

    reported = _capture_logs(get_http_request_timeout)

    assert "ceiling" in reported
    assert _capture_logs(get_http_request_timeout) == "", "the same value should be reported once"


def _built_gitlab_client(monkeypatch, auth_type="oauth_token"):
    import gitlab

    from pr_agent.git_providers.gitlab_provider import GitLabProvider

    # Both auth types read the same token key; only the kwarg handed to the client differs.
    monkeypatch.setitem(global_settings.gitlab, "personal_access_token", "offline-token")
    monkeypatch.setitem(global_settings.gitlab, "url", "https://gitlab.example")
    monkeypatch.setitem(global_settings.gitlab, "auth_type", auth_type)
    with patch.object(gitlab, "Gitlab") as client:
        client.return_value = MagicMock()
        try:
            GitLabProvider("https://gitlab.example/g/p/-/merge_requests/1")
        except Exception:
            pass  # the provider reads the MR afterwards; the client construction is what matters

    assert client.call_args is not None
    return client.call_args


@pytest.mark.parametrize("auth_type", ["oauth_token", "private_token"])
def test_the_gitlab_client_is_built_with_a_timeout(monkeypatch, auth_type):
    built = _built_gitlab_client(monkeypatch, auth_type)

    assert built.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_gitlab_client_uses_a_configured_timeout(monkeypatch):
    _configured(7.5, monkeypatch)

    assert _built_gitlab_client(monkeypatch).kwargs["timeout"] == 7.5


def _bitbucket_failure(kind):
    """The exception a Bitbucket call raises for the two failure kinds a host produces."""
    import requests

    if kind == "timeout":
        return requests.Timeout("read timed out")
    assert kind == "connection", kind
    return requests.ConnectionError("connection refused")


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_a_failed_default_branch_read_is_reported(failure):
    """A bounded request can now fail, and the fallback branch would otherwise hide that."""

    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(destination_branch="main")

    def read():
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=_bitbucket_failure(failure)):
            return provider.get_repo_default_branch()

    assert read() == "main", "the fallback still has to happen"
    assert "Failed to read the default branch" in _capture_logs(read)


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_a_failed_file_read_is_reported(failure):
    """An empty string is the same answer a missing file gives, so the log has to say which."""

    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.headers = {"Authorization": "Bearer token"}
    link = "https://bitbucket.org/workspace/repository/raw/file.py"

    def read():
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=_bitbucket_failure(failure)):
            return provider._get_pr_file_content(link)

    assert read() == ""
    assert f"Failed to read {link}" in _capture_logs(read)


def test_a_non_request_failure_of_the_default_branch_read_stays_unreported():
    """The other two fallbacks existed before this change, so only a request failure is new here."""
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(destination_branch="main")

    def read():
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=ValueError("not the call under test")):
            return provider.get_repo_default_branch()

    assert read() == "main", "the fallback still has to happen"
    assert _capture_logs(read) == "", "an unrelated failure is not this change's to report"


def test_a_malformed_pull_request_still_falls_back_when_errors_are_not_propagated():
    """The branch lookup sits inside the try, so a pr.data without a commit hash is not new.

    It raised before this change moved the URL build, and the only caller passes
    propagate_errors=True, but the fallback is what the flag promises.
    """
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature", destination_branch="main",
                            data={"source": {}, "destination": {}})

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request") as request:
        assert provider.get_pr_file_content("CHANGELOG.md", "main", propagate_errors=False) == ""

    assert not request.called, "the hash lookup failed, so no request should have gone out"


def test_a_request_failure_before_the_url_is_built_still_names_the_file():
    """The warning logs the URL, which is not built yet when pr.data itself raises.

    The handler falls back to the file path, so the diagnostic survives an unbound local.
    """
    import requests

    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    class _PrWithUnreadableData:
        source_branch = "feature"
        destination_branch = "main"

        @property
        def data(self):
            raise requests.Timeout("the PR could not be read")

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = _PrWithUnreadableData()

    assert provider.get_pr_file_content("CHANGELOG.md", "main", propagate_errors=False) == ""
    assert "Failed to read CHANGELOG.md" in _capture_logs(
        lambda: provider.get_pr_file_content("CHANGELOG.md", "main", propagate_errors=False))


def test_a_non_request_failure_stays_unreported():
    """A KeyError is not what the bounded request introduces, so it keeps its old quiet fallback."""
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature",
                            data={"source": {"commit": {"hash": "a1b2c3d4e5f6"}}})

    def read():
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=ValueError("not the call under test")):
            return provider.get_pr_file_content("CHANGELOG.md", "feature", propagate_errors=False)

    assert read() == ""
    assert _capture_logs(read) == "", "an unrelated failure is not this change's to report"


@pytest.mark.parametrize("failure", ["timeout", "connection"])
def test_a_failed_repo_file_read_is_reported_when_errors_are_not_propagated(failure):
    """The empty string is what a missing file returns too, so the log has to say which it was."""

    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature", destination_branch="main",
                            data={"source": {"commit": {"hash": "a1b2c3d4e5f6"}},
                                  "destination": {"commit": {"hash": "b1b2c3d4e5f6"}}})

    def read():
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=_bitbucket_failure(failure)):
            return provider.get_pr_file_content("CHANGELOG.md", "main", propagate_errors=False)

    assert read() == ""
    logs = _capture_logs(read)
    assert "Failed to read" in logs and "empty file" in logs


def test_a_failed_read_that_raises_is_not_logged_twice():
    """The caller gets the exception, so a warning would only repeat what it already has."""
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature",
                            data={"source": {"commit": {"hash": "a1b2c3d4e5f6"}}})

    def read():
        with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
                   side_effect=_bitbucket_failure("timeout")):
            with suppress(Exception):
                provider.get_pr_file_content("CHANGELOG.md", "feature")

    read()
    assert _capture_logs(read) == "", "the exception itself is the report in this mode"


@pytest.mark.parametrize("auth_type", ["oauth_token", "private_token"])
async def test_the_gitlab_webhook_bot_lookup_is_bounded(monkeypatch, auth_type):
    """The webhook resolves the bot's user id on its own client, so it needs a timeout too."""
    import gitlab

    from pr_agent.servers import gitlab_webhook

    monkeypatch.setitem(global_settings.gitlab, "personal_access_token", "offline-token")
    monkeypatch.setitem(global_settings.gitlab, "auth_type", auth_type)
    monkeypatch.setattr(gitlab_webhook, "_bot_user_id_cache", {})
    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda *a, **k: global_settings)

    with patch.object(gitlab, "Gitlab") as client:
        client.return_value.auth.return_value = None
        client.return_value.user.id = 42
        assert await gitlab_webhook._get_bot_user_id() == 42

    assert client.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def _gitea_client(monkeypatch):
    """Build a GiteaProvider and return its client, with the pool's urlopen already stubbed.

    The stub has to be installed from the ApiClient spy, which is the first point where the pool
    manager exists: the provider constructor reads the pull request over the same client.
    """
    import giteapy

    from pr_agent.git_providers.gitea_provider import GiteaProvider

    monkeypatch.setitem(global_settings.gitea, "personal_access_token", "offline-token")
    monkeypatch.setitem(global_settings.gitea, "url", "https://gitea.example")
    captured = {}
    real_api_client = giteapy.ApiClient

    def spy(*args, **kwargs):
        client = real_api_client(*args, **kwargs)
        stub = MagicMock(status=200, data=bytearray(b"{}"), reason="OK", headers={})
        monkeypatch.setattr(client.rest_client.pool_manager, "urlopen",
                            MagicMock(return_value=stub))
        captured["client"] = client
        captured["urlopen"] = client.rest_client.pool_manager.urlopen
        return client

    monkeypatch.setattr(giteapy, "ApiClient", spy)
    try:
        GiteaProvider("https://gitea.example/g/p/pulls/1")
    except Exception:
        pass  # the stub body is empty, so the constructor gives up; the client is built either way

    assert "client" in captured, "the provider never built a giteapy client"
    return captured["client"], captured["urlopen"]


def _gitea_pull_request(client, index=1, **kwargs):
    """Call a giteapy generated method, the way the provider itself reaches the API.

    Generated methods always forward ``_request_timeout`` with whatever the caller passed, which
    is None here, so this is the path that has to pick up the default.
    """
    from giteapy import RepositoryApi

    return RepositoryApi(client).repo_get_pull_request("owner", "repo", index, **kwargs)


def test_the_gitea_client_sends_a_real_timeout(monkeypatch):
    """The timeout has to reach urllib3; an attribute giteapy never reads would do nothing."""
    import urllib3

    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    try:
        _gitea_pull_request(client)
    except Exception:
        pass  # decoding the stub body is out of scope; the outgoing timeout is not

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert isinstance(sent, urllib3.Timeout)
    assert sent.connect_timeout == DEFAULT_HTTP_REQUEST_TIMEOUT
    assert sent.read_timeout == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_repeated_gitea_calls_are_bounded_too(monkeypatch):
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise be counted as one of the two
    for index in (1, 2):
        try:
            _gitea_pull_request(client, index=index)
        except Exception:
            pass

    assert urlopen.call_count == 2, "a call never reached urllib3, so the rest proves nothing"
    assert all(call.kwargs["timeout"].connect_timeout == DEFAULT_HTTP_REQUEST_TIMEOUT
               for call in urlopen.call_args_list)


def test_the_gitea_client_keeps_a_timeout_the_caller_asked_for(monkeypatch):
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    try:
        _gitea_pull_request(client, _request_timeout=(3, 4))
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertion below proves nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (3, 4)


def test_the_gitea_client_keeps_a_plain_integer_caller_timeout(monkeypatch):
    """giteapy turns an int into Timeout(total=...), a different shape from the pair it gets here."""
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    try:
        client.call_api("/repos", "GET", _request_timeout=3)
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert sent.total == 3, "giteapy's own int handling has to survive the wrapper"


def test_the_gitea_client_keeps_a_fractional_configured_timeout(monkeypatch):
    """A sub-second value must not be truncated to 0, which giteapy would send unbounded."""
    _configured(0.5, monkeypatch)
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call was the default, not this 0.5
    try:
        _gitea_pull_request(client)
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertion below proves nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (0.5, 0.5)


def test_the_gerrit_patch_upload_is_bounded(monkeypatch):
    from pr_agent.git_providers.gerrit_provider import upload_patch

    monkeypatch.setattr(global_settings.gerrit, "patch_server_endpoint",
                        "https://gerrit.example/patch", raising=False)
    monkeypatch.setattr(global_settings.gerrit, "patch_server_token", "offline-token", raising=False)

    with patch("pr_agent.git_providers.gerrit_provider.requests.post") as post:
        post.return_value = MagicMock(status_code=200)
        assert upload_patch("patch body", "42") == "https://gerrit.example/patch/42"

    assert post.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_provider_calls_carry_a_timeout():
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.headers = {"Authorization": "Bearer token"}
    response = MagicMock(status_code=200, text="file body")

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=response) as request:
        assert provider._get_pr_file_content("https://example.com/branch") == "file body"

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_public_file_read_carries_a_timeout():
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature", destination_branch="main")

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=MagicMock(status_code=404)) as request:
        assert provider.get_pr_file_content("src/example.py", "main") == ""

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_source_writes_carry_a_timeout():
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    response = MagicMock(status_code=200, text="")

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=response) as request:
        provider.create_or_update_pr_file("CHANGELOG.md", "feature", "new content", "Update changelog",
                                          expected_snapshot=FileContentSnapshot(
                                              contents="old content", exists=True, revision="a1b2c3d4e5f6"))

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_snapshot_read_carries_a_timeout():
    """Pin the timeout on the source-read that captures a snapshot before a guarded write."""
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature",
                            data={"source": {"commit": {"hash": "a1b2c3d4e5f6"}}})
    response = MagicMock(status_code=200, text="body")

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=response) as request:
        provider.get_pr_file_content_snapshot("CHANGELOG.md", "feature")

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_description_update_carries_a_timeout():
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.bitbucket_pull_request_api_url = "https://api.bitbucket.org/pullrequests/1"
    provider.headers = {"Authorization": "Bearer token"}

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=MagicMock(status_code=200)) as request:
        provider.publish_description("A title", "A description")

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_default_branch_lookup_carries_a_timeout():
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    response = MagicMock(status_code=200)
    response.json.return_value = {"mainbranch": {"name": "main"}}

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=response) as request:
        assert provider.get_repo_default_branch() == "main"

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_bitbucket_local_settings_fetch_carries_a_timeout():
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock()
    provider.pr.data = {"destination": {"commit": {"hash": "abc123"}}}

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=MagicMock(status_code=200, text="")) as request, \
         patch.object(BitbucketProvider, "_get_global_repo_settings", MagicMock(return_value="")):
        provider.get_repo_settings()

    assert request.call_args.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_each_unusable_timeout_is_reported_once(monkeypatch):
    """The value is read per request, so a bad one must not flood the log, nor go unreported."""
    from pr_agent.git_providers import request_timeout

    monkeypatch.setattr(request_timeout, "_reported_timeouts", set())

    def warnings_for(value):
        # the setting is put in place first: _configured() would log before the capture starts
        monkeypatch.setattr(global_settings.config, "http_request_timeout", value, raising=False)
        return _capture_logs(get_http_request_timeout)

    first = warnings_for("abc")
    again = warnings_for("abc")
    other = warnings_for("def")

    assert "config.http_request_timeout" in first
    assert again == "", "the same unusable value should be reported once"
    assert "config.http_request_timeout" in other, "a different unusable value still needs reporting"


def test_two_values_sharing_a_prefix_are_reported_separately(monkeypatch):
    """A repository can set an arbitrarily long string here, so the report key cannot be truncated.

    Two values agreeing on their first 64 characters are still two values the operator may have
    meant differently, and the second one would otherwise be swallowed silently.
    """
    from pr_agent.git_providers import request_timeout

    monkeypatch.setattr(request_timeout, "_reported_timeouts", set())
    shared = "a" * 80

    def warnings_for(value):
        monkeypatch.setattr(global_settings.config, "http_request_timeout", value, raising=False)
        return _capture_logs(get_http_request_timeout)

    first = warnings_for(shared + "one")
    second = warnings_for(shared + "two")

    assert "config.http_request_timeout" in first
    assert "config.http_request_timeout" in second, "a value differing past the prefix is still its own"


def test_a_long_unusable_value_is_not_logged_in_full(monkeypatch):
    """The value comes from a repository setting, so the log line is bounded like the key is."""
    from pr_agent.git_providers import request_timeout

    monkeypatch.setattr(request_timeout, "_reported_timeouts", set())
    monkeypatch.setattr(global_settings.config, "http_request_timeout", "z" * 5000, raising=False)

    logs = _capture_logs(get_http_request_timeout)

    assert "config.http_request_timeout" in logs
    assert "z" * 100 in logs and "..." in logs, "the rendering has to be cut short"
    assert "z" * 121 not in logs, "the whole value reached the log line"
    assert len(logs) < 400, f"a 5000-character value produced a {len(logs)}-character log line"
    assert get_http_request_timeout() == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_set_of_reported_values_does_not_grow_forever(monkeypatch):
    """The key comes from a repository setting, which a long-lived server reads once per revision.

    Without a ceiling every distinct value ever seen is remembered for the life of the process.
    """
    from pr_agent.git_providers import request_timeout

    monkeypatch.setattr(request_timeout, "_reported_timeouts", set())

    for index in range(request_timeout._MAX_REPORTED_TIMEOUTS + 10):
        monkeypatch.setattr(global_settings.config, "http_request_timeout", f"bogus{index}",
                            raising=False)
        _capture_logs(get_http_request_timeout)

    assert len(request_timeout._reported_timeouts) <= request_timeout._MAX_REPORTED_TIMEOUTS


def test_the_bitbucket_calls_re_read_the_configured_timeout(monkeypatch):
    """Bitbucket re-reads the setting per request, so a change applies without a restart."""
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.headers = {"Authorization": "Bearer token"}
    response = MagicMock(status_code=200, text="file body")

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=response) as request:
        _configured(9.5, monkeypatch)
        provider._get_pr_file_content("https://example.com/branch")
        first = request.call_args.kwargs["timeout"]
        _configured(21.5, monkeypatch)
        provider._get_pr_file_content("https://example.com/branch")

    assert first == 9.5
    assert request.call_args.kwargs["timeout"] == 21.5, "the second call has to see the new value"


def test_the_bitbucket_settings_fetches_carry_a_timeout():
    """The repo-settings lookups run on every command, so they are bounded too."""
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "myws"
    provider.headers = {"Authorization": "Bearer x"}
    repo = MagicMock(status_code=200)
    repo.json.return_value = {"mainbranch": {"name": "main"}}
    ref = MagicMock(status_code=200)
    ref.json.return_value = {"target": {"hash": "settings-sha"}}
    config_file = MagicMock(status_code=200)
    config_file.text = ""

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               side_effect=[repo, ref, config_file]) as request, \
         patch("pr_agent.git_providers.git_provider.get_settings") as settings:
        settings.return_value.config.use_global_settings_file = True
        provider._get_global_repo_settings()

    assert request.call_count == 3
    assert all(call.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT
               for call in request.call_args_list)


def test_the_gitea_timeout_is_read_per_call(monkeypatch):
    """The Gitea value is read per call, so a change made after the client was built still counts."""
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    first = _configured(11.0, monkeypatch)
    try:
        _gitea_pull_request(client)
    except Exception:
        pass
    assert urlopen.call_count, "the first call never reached urllib3"
    first_sent = urlopen.call_args.kwargs["timeout"]

    _configured(22.0, monkeypatch)
    try:
        _gitea_pull_request(client)
    except Exception:
        pass
    assert urlopen.call_count == 2, "the second call never reached urllib3"
    second_sent = urlopen.call_args.kwargs["timeout"]

    assert first_sent.connect_timeout == first == 11.0
    assert second_sent.connect_timeout == 22.0


def test_a_merged_repository_timeout_reaches_the_accessor(monkeypatch):
    """Pin that a repository's own .pr_agent.toml is what the timeout accessor then reads.

    Whether a given client re-reads it per call is a separate question, pinned per client above.
    """
    import tempfile
    from pathlib import Path

    from pr_agent.git_providers import utils as git_utils

    toml = '[config]\nhttp_request_timeout = 12\n'
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "pr_agent.toml"
        path.write_text(toml, encoding="utf-8")
        git_utils._apply_repo_settings_file(str(path))

    assert get_http_request_timeout() == 12.0


def test_a_repository_cannot_lift_the_ceiling_at_a_client(monkeypatch):
    """The threat the ceiling exists for, end to end: a repo's own .pr_agent.toml, then a call.

    The accessor test above stops at the accessor and the capped-value tests set the setting
    directly, so nothing joined the two ends of the path this is meant to close.
    """
    import tempfile
    from pathlib import Path

    from pr_agent.git_providers import utils as git_utils
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / ".pr_agent.toml"
        path.write_text(f"[config]\nhttp_request_timeout = {MAX_HTTP_REQUEST_TIMEOUT * 10}\n",
                        encoding="utf-8")
        git_utils._apply_repo_settings_file(str(path))

    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.workspace_slug = "workspace"
    provider.repo_slug = "repository"
    provider.headers = {"Authorization": "Bearer token"}
    provider.pr = MagicMock(source_branch="feature",
                            data={"source": {"commit": {"hash": "a1b2c3d4e5f6"}}})
    response = MagicMock(status_code=200, text="file body")

    with patch("pr_agent.git_providers.bitbucket_provider.requests.request",
               return_value=response) as request:
        provider.get_pr_file_content_snapshot("CHANGELOG.md", "feature")

    assert request.call_args.kwargs["timeout"] == MAX_HTTP_REQUEST_TIMEOUT


def test_the_gitea_client_replaces_a_falsy_caller_timeout(monkeypatch):
    """giteapy sends a falsy _request_timeout unbounded, so 0 must not disable the bound."""
    client, urlopen = _gitea_client(monkeypatch)
    # The provider constructor already called urlopen through this same stub, so its `call_args`
    # would satisfy the assertions below even if this call never reached the pool manager.
    urlopen.reset_mock()
    try:
        client.call_api("/repos", "GET", _request_timeout=0)
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert sent is not None
    assert sent.connect_timeout == DEFAULT_HTTP_REQUEST_TIMEOUT


def test_the_gitea_client_refuses_a_pair_of_nones(monkeypatch):
    """giteapy passes a (None, None) pair to urllib3, which resolves it to no timeout at all."""
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    try:
        client.call_api("/repos", "GET", _request_timeout=(None, None))
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (DEFAULT_HTTP_REQUEST_TIMEOUT,) * 2


@pytest.mark.parametrize("last", [None, 0.5])
def test_the_gitea_client_also_fixes_a_timeout_passed_by_position(monkeypatch, last):
    """giteapy's call_api takes _request_timeout as its 15th parameter, so it can arrive positionally.

    Setting the keyword as well would make the call raise "got multiple values for argument", and
    ignoring the position would leave it unbounded, which is the case this wrapper exists to close.
    """
    import inspect

    import giteapy

    # padded to the real index rather than a written-down one, so an index that drifts from
    # giteapy's signature puts the call in the wrong slot and the assertions below fail
    position = list(inspect.signature(giteapy.ApiClient().call_api).parameters).index("_request_timeout")

    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    padding = [None] * (position - 2)  # resource_path and method are passed for real
    try:
        client.call_api("/repos", "GET", *padding, last)
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (DEFAULT_HTTP_REQUEST_TIMEOUT,) * 2


def test_the_wrapper_sets_the_keyword_when_there_is_no_positional_slot():
    """A stand-in for giteapy's call_api may have no _request_timeout parameter at all.

    The wrapper is applied to whatever the client exposes, and a test double or a future generated
    client can differ, so the keyword is still what has to carry the bound.
    """
    from pr_agent.git_providers.gitea_provider import _with_default_request_timeout

    stand_in = MagicMock(side_effect=RuntimeError("stop"))
    wrapped = _with_default_request_timeout(stand_in)

    with pytest.raises(RuntimeError):
        wrapped("/repos", "GET")

    assert stand_in.call_args.kwargs["_request_timeout"] == (DEFAULT_HTTP_REQUEST_TIMEOUT,) * 2


@pytest.mark.parametrize("given", [-1, True, 0, -0.5, float("inf"), (float("inf"), 5.0), (0.0, 5.0)])
def test_the_gitea_client_refuses_a_value_urllib3_would_reject(monkeypatch, given):
    """urllib3 raises ValueError on a non-positive timeout, and a bool is an int to giteapy.

    Forwarding one of these would turn a missing bound into a failed call instead of the default.
    """
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    try:
        client.call_api("/repos", "GET", _request_timeout=given)
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (DEFAULT_HTTP_REQUEST_TIMEOUT,) * 2


def test_the_gitea_client_replaces_a_bare_float_caller_timeout(monkeypatch):
    """giteapy only honours an int and a pair, so a float reaches urllib3 as no timeout at all."""
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    try:
        client.call_api("/repos", "GET", _request_timeout=0.5)
    except Exception:
        pass

    assert urlopen.call_count, "the call never reached urllib3, so the assertions below prove nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (DEFAULT_HTTP_REQUEST_TIMEOUT,) * 2
