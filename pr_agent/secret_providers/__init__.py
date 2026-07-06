from pr_agent.config_loader import get_settings


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
