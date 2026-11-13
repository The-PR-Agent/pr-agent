"""GITEA.SKIP_SSL_VERIFICATION must parse string booleans.

A non-empty string is truthy, so skip_ssl_verification="false" would
disable TLS verification even though the operator turned the flag off.
"""
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.git_providers.gitea_provider import GiteaProvider


def _configuration_from_skip_ssl(configured):
    with patch("pr_agent.git_providers.gitea_provider.giteapy.ApiClient") as api_client_cls, \
         patch("pr_agent.git_providers.gitea_provider.get_settings") as get_settings:
        get_settings.return_value.get.side_effect = lambda key, default=None: {
            "GITEA.URL": "https://gitea.example.com",
            "GITEA.PERSONAL_ACCESS_TOKEN": "token",
            "GITEA.SKIP_SSL_VERIFICATION": configured,
        }.get(key, default)
        api_client_cls.return_value = MagicMock()
        GiteaProvider("https://gitea.example.com/owner/repo/pulls/1")
    assert api_client_cls.call_args is not None
    args, kwargs = api_client_cls.call_args
    return args[0] if args else kwargs.get("configuration")


@pytest.mark.parametrize(
    ("configured", "expected_verify_ssl"),
    [
        (False, True),
        ("false", True),
        ("FALSE", True),
        ("0", True),
        (True, False),
        ("true", False),
    ],
)
def test_skip_ssl_verification_parses_string_config(configured, expected_verify_ssl):
    configuration = _configuration_from_skip_ssl(configured)
    assert configuration.verify_ssl is expected_verify_ssl
