"""GitLabProvider must honor string-valued GITLAB.SSL_VERIFY like the webhook does.

A non-empty string is truthy and, in requests, is treated as a CA bundle path.
So ssl_verify="false" does not disable TLS verification. A custom CA path must
still be passed through unchanged.
"""
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.config_loader import global_settings


def _built_gitlab_client(monkeypatch, auth_type="oauth_token", ssl_verify=True):
    import gitlab

    from pr_agent.git_providers.gitlab_provider import GitLabProvider

    monkeypatch.setitem(global_settings.gitlab, "personal_access_token", "offline-token")
    monkeypatch.setitem(global_settings.gitlab, "url", "https://gitlab.example")
    monkeypatch.setitem(global_settings.gitlab, "auth_type", auth_type)
    monkeypatch.setitem(global_settings.gitlab, "ssl_verify", ssl_verify)
    with patch.object(gitlab, "Gitlab") as client:
        client.return_value = MagicMock()
        monkeypatch.setattr(GitLabProvider, "_set_merge_request", lambda *args: None)
        GitLabProvider("https://gitlab.example/g/p/-/merge_requests/1")

    assert client.call_args is not None
    return client.call_args


@pytest.mark.parametrize("auth_type", ["oauth_token", "private_token"])
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("FALSE", False),
        ("/custom/ca.pem", "/custom/ca.pem"),
    ],
)
def test_gitlab_client_honors_ssl_verify_setting(monkeypatch, auth_type, configured, expected):
    built = _built_gitlab_client(monkeypatch, auth_type, ssl_verify=configured)
    assert built.kwargs["ssl_verify"] == expected


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        (" False ", False),
        ("/custom/ca.pem", "/custom/ca.pem"),
        (None, True),
    ],
)
def test_as_ssl_verify_setting(configured, expected):
    from pr_agent.algo.setting_utils import _as_ssl_verify_setting

    assert _as_ssl_verify_setting(configured) == expected
