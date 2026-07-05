from pr_agent.config_loader import get_settings
from pr_agent.identity_providers.default_identity_provider import DefaultIdentityProvider

_IDENTITY_PROVIDERS = {
    'default': DefaultIdentityProvider
}


def get_identity_provider():
    identity_provider_id = get_settings().get("CONFIG.IDENTITY_PROVIDER", "default")
    if identity_provider_id not in _IDENTITY_PROVIDERS:
        raise ValueError(f"Unknown identity provider: {identity_provider_id}")
    return _IDENTITY_PROVIDERS[identity_provider_id]()
