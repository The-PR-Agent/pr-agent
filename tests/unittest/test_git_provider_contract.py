"""Contract tests for the GitProvider interface.

Every provider registered in _GIT_PROVIDERS must fully implement the abstract
interface and answer is_supported with a bool for every known capability.
These tests catch interface drift (a new abstract method not implemented by a
less-used provider) without needing network access or provider credentials.
"""

import inspect
from unittest.mock import patch

import pytest

from pr_agent.git_providers import _GIT_PROVIDERS
from pr_agent.git_providers.git_provider import KNOWN_CAPABILITIES, GitProvider

PROVIDERS = sorted(_GIT_PROVIDERS.items())
PROVIDER_IDS = [name for name, _ in PROVIDERS]


def _bare_instance(cls):
    # bypass __init__, which requires credentials / network for most providers
    return object.__new__(cls)


@pytest.mark.parametrize("name,cls", PROVIDERS, ids=PROVIDER_IDS)
def test_provider_is_git_provider_subclass(name, cls):
    assert issubclass(cls, GitProvider)


@pytest.mark.parametrize("name,cls", PROVIDERS, ids=PROVIDER_IDS)
def test_provider_implements_all_abstract_methods(name, cls):
    missing = sorted(getattr(cls, "__abstractmethods__", ()))
    assert not inspect.isabstract(cls), f"{cls.__name__} does not implement: {missing}"


@pytest.mark.parametrize("capability", sorted(KNOWN_CAPABILITIES))
@pytest.mark.parametrize("name,cls", PROVIDERS, ids=PROVIDER_IDS)
def test_is_supported_returns_bool_for_every_known_capability(name, cls, capability):
    provider = _bare_instance(cls)
    result = provider.is_supported(capability)
    assert isinstance(result, bool), f"{cls.__name__}.is_supported({capability!r}) returned {result!r}"


@pytest.mark.parametrize("name,cls", PROVIDERS, ids=PROVIDER_IDS)
def test_is_supported_warns_on_unknown_capability(name, cls):
    provider = _bare_instance(cls)
    with patch("pr_agent.git_providers.git_provider.get_logger") as mock_logger:
        result = provider.is_supported("definitely_not_a_capability")
        assert result is False
        assert mock_logger.return_value.warning.called


@pytest.mark.parametrize("name,cls", PROVIDERS, ids=PROVIDER_IDS)
def test_is_supported_does_not_warn_on_known_capability(name, cls):
    provider = _bare_instance(cls)
    with patch("pr_agent.git_providers.git_provider.get_logger") as mock_logger:
        for capability in KNOWN_CAPABILITIES:
            provider.is_supported(capability)
        assert not mock_logger.return_value.warning.called
