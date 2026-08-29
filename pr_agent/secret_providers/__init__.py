from pr_agent.config_loader import get_settings

# Keep in step with the branches of get_secret_provider() below.
SUPPORTED_SECRET_PROVIDERS = ("google_cloud_storage", "aws_secrets_manager")


def validate_secret_provider_setting():
    """Check CONFIG.SECRET_PROVIDER names a known provider, without building its client.

    Lets a server reject a typo at startup while leaving the cloud client itself to be
    constructed per process. That split matters under gunicorn's `preload_app`: a client
    built during import would be created in the master and inherited by every worker.
    """
    provider_id = get_settings().get("CONFIG.SECRET_PROVIDER")
    if provider_id and provider_id not in SUPPORTED_SECRET_PROVIDERS:
        raise ValueError("Unknown SECRET_PROVIDER")


def get_secret_provider():
    if not get_settings().get("CONFIG.SECRET_PROVIDER"):
        return None

    provider_id = get_settings().config.secret_provider
    if provider_id == 'google_cloud_storage':
        try:
            from pr_agent.secret_providers.google_cloud_storage_secret_provider import GoogleCloudStorageSecretProvider
            return GoogleCloudStorageSecretProvider()
        except Exception as e:
            raise ValueError(f"Failed to initialize google_cloud_storage secret provider {provider_id}") from e
    elif provider_id == 'aws_secrets_manager':
        try:
            from pr_agent.secret_providers.aws_secrets_manager_provider import AWSSecretsManagerProvider
            return AWSSecretsManagerProvider()
        except Exception as e:
            raise ValueError(f"Failed to initialize aws_secrets_manager secret provider {provider_id}") from e
    else:
        raise ValueError("Unknown SECRET_PROVIDER")
