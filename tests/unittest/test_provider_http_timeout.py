import copy
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
def clear_global_settings_cache():
    from pr_agent.git_providers.git_provider import _GLOBAL_SETTINGS_CACHE

    _GLOBAL_SETTINGS_CACHE.clear()
    yield
    _GLOBAL_SETTINGS_CACHE.clear()



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
                                        float("inf"), float("-inf"), 10 ** 400])
def test_an_unusable_timeout_falls_back_to_the_default(monkeypatch, configured):
    """A typo must not leave the clients unbounded, which is the whole point of the setting."""
    assert _configured(configured, monkeypatch) == DEFAULT_HTTP_REQUEST_TIMEOUT


@pytest.mark.parametrize("configured", [MAX_HTTP_REQUEST_TIMEOUT, 601, 3600, 86400])
def test_an_oversized_timeout_is_capped_to_the_ceiling(monkeypatch, configured):
    """Cap an oversized host timeout instead of leaving the request unbounded."""
    assert _configured(configured, monkeypatch) == MAX_HTTP_REQUEST_TIMEOUT


@pytest.mark.parametrize(("configured", "expected"), [("invalid", 60.0), (601, 600.0)])
def test_invalid_host_values_are_reported_per_request(monkeypatch, configured, expected):
    from pr_agent.git_providers import request_timeout

    logger = MagicMock()
    monkeypatch.setattr(request_timeout, "get_logger", lambda: logger)
    assert _configured(configured, monkeypatch) == expected
    assert get_http_request_timeout() == expected
    assert logger.warning.call_count == 2


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


@pytest.mark.parametrize("auth_type", ["oauth_token", "private_token"])
def test_cached_gitlab_client_uses_host_external_timeout(tmp_path, monkeypatch, auth_type):
    import requests

    from pr_agent.git_providers import utils as git_utils
    from pr_agent.git_providers.gitlab_provider import GitLabProvider

    monkeypatch.setitem(global_settings.gitlab, "url", "https://gitlab.example")
    monkeypatch.setitem(global_settings.gitlab, "personal_access_token", "offline-token")
    monkeypatch.setitem(global_settings.gitlab, "auth_type", auth_type)
    monkeypatch.setattr(GitLabProvider, "_set_merge_request", lambda *args: None)
    provider = GitLabProvider("https://gitlab.example/g/p/-/merge_requests/1")
    external = tmp_path / "host.toml"
    external.write_text("[config]\nhttp_request_timeout = 12\n", encoding="utf-8")
    monkeypatch.setitem(global_settings.config, "extra_config_url", str(external))
    monkeypatch.setitem(global_settings.config, "use_repo_settings_file", False)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda url: provider)
    git_utils.apply_repo_settings(provider.pr_url)

    response = requests.Response()
    response.status_code = 200
    response._content = b"[]"
    response.headers["Content-Type"] = "application/json"
    with patch.object(provider.gl.session, "send", return_value=response) as send:
        assert provider.gl.http_get("/projects") == []
        assert send.call_args.kwargs["timeout"] == 12.0
        _configured(17.5, monkeypatch)
        assert provider.gl.http_get("/projects") == []
        assert send.call_args.kwargs["timeout"] == 17.5
        assert provider.gl.http_get("/projects", timeout=3) == []
        assert send.call_args.kwargs["timeout"] == 3


@pytest.mark.asyncio
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


def _attempt(call, *args, **kwargs):
    """Run a stubbed giteapy call while leaving transport verification to the test.

    The stub answers with a MagicMock where giteapy expects a parsed payload, so these calls
    usually fail while decoding. That failure is not what these tests are about: each one asserts on
    the timeout urllib3 was handed, which is decided before the response is read. So the outcome is
    returned rather than raised, and the assertion on ``urlopen`` below is what has to hold.
    """
    try:
        call(*args, **kwargs)
    except Exception:
        return False
    return True


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


def test_the_gitea_client_keeps_a_fractional_configured_timeout(monkeypatch):
    """A sub-second value must not be truncated to 0, which giteapy would send unbounded."""
    _configured(0.5, monkeypatch)
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # exclude the constructor's calls from this request assertion
    _attempt(_gitea_pull_request, client)

    assert urlopen.call_count, "the call never reached urllib3, so the assertion below proves nothing"
    sent = urlopen.call_args.kwargs["timeout"]
    assert (sent.connect_timeout, sent.read_timeout) == (0.5, 0.5)


def test_the_gitea_timeout_is_read_per_call(monkeypatch):
    """The Gitea value is read per call, so a change made after the client was built still counts."""
    client, urlopen = _gitea_client(monkeypatch)
    urlopen.reset_mock()  # the constructor's call would otherwise satisfy what follows
    first = _configured(11.0, monkeypatch)
    _attempt(_gitea_pull_request, client)
    assert urlopen.call_count, "the first call never reached urllib3"
    first_sent = urlopen.call_args.kwargs["timeout"]

    _configured(22.0, monkeypatch)
    _attempt(_gitea_pull_request, client)
    assert urlopen.call_count == 2, "the second call never reached urllib3"
    second_sent = urlopen.call_args.kwargs["timeout"]

    assert first_sent.connect_timeout == first == 11.0
    assert second_sent.connect_timeout == 22.0


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
        settings.return_value.config.global_settings_repo = "operator-settings"
        provider._get_global_repo_settings()

    assert request.call_count == 3
    assert "myws/operator-settings" in request.call_args_list[0].args[1]
    assert all(call.kwargs["timeout"] == DEFAULT_HTTP_REQUEST_TIMEOUT
               for call in request.call_args_list)


@pytest.mark.parametrize("configured", [12, 600, 6000])
def test_repository_settings_cannot_override_the_host_timeout(monkeypatch, configured):
    """Keep the operator's timeout when a repository supplies its own value."""
    import tempfile
    from pathlib import Path

    from pr_agent.git_providers import utils as git_utils

    _configured(7.5, monkeypatch)
    toml = f'[config]\nhttp_request_timeout = {configured}\n'
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "pr_agent.toml"
        path.write_text(toml, encoding="utf-8")
        git_utils._apply_repo_settings_file(str(path))

    assert get_http_request_timeout() == 7.5


def test_a_repository_cannot_change_the_host_timeout_at_a_client(monkeypatch):
    """Preserve the host timeout at transport after merging repository settings."""
    import tempfile
    from pathlib import Path

    from pr_agent.git_providers import utils as git_utils
    from pr_agent.git_providers.bitbucket_provider import BitbucketProvider

    _configured(9.5, monkeypatch)
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

    assert request.call_args.kwargs["timeout"] == 9.5
