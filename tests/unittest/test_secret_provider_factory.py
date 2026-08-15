from unittest.mock import MagicMock, patch

import pytest

from pr_agent.secret_providers import SUPPORTED_SECRET_PROVIDERS, get_secret_provider, validate_secret_provider_setting


class TestValidateSecretProviderSetting:
    """Startup validation that rejects a typo without constructing a cloud client."""

    def _settings(self, value):
        settings = MagicMock()
        settings.get.return_value = value
        return settings

    @pytest.mark.parametrize("provider_id", SUPPORTED_SECRET_PROVIDERS)
    def test_accepts_supported_providers(self, provider_id):
        with patch('pr_agent.secret_providers.get_settings', return_value=self._settings(provider_id)):
            validate_secret_provider_setting()

    def test_accepts_unset_provider(self):
        with patch('pr_agent.secret_providers.get_settings', return_value=self._settings("")):
            validate_secret_provider_setting()

    def test_rejects_unknown_provider(self):
        with patch('pr_agent.secret_providers.get_settings', return_value=self._settings("gcs")):
            with pytest.raises(ValueError, match="Unknown SECRET_PROVIDER"):
                validate_secret_provider_setting()

    def test_does_not_construct_a_client(self):
        # The whole point: validation must stay free of the cloud SDKs, so it can run at
        # import time in the gunicorn master without creating a client that gets forked.
        with patch('pr_agent.secret_providers.get_settings', return_value=self._settings("aws_secrets_manager")):
            with patch('pr_agent.secret_providers.get_secret_provider') as mock_build:
                validate_secret_provider_setting()
            mock_build.assert_not_called()


class TestSecretProviderFactory:

    def test_get_secret_provider_none_when_not_configured(self):
        with patch('pr_agent.secret_providers.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = None
            mock_get_settings.return_value = settings

            result = get_secret_provider()
            assert result is None

    def test_get_secret_provider_google_cloud_storage(self):
        with patch('pr_agent.secret_providers.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = "google_cloud_storage"
            settings.config.secret_provider = "google_cloud_storage"
            mock_get_settings.return_value = settings

            with patch('pr_agent.secret_providers.google_cloud_storage_secret_provider.GoogleCloudStorageSecretProvider') as MockProvider:
                mock_instance = MagicMock()
                MockProvider.return_value = mock_instance

                result = get_secret_provider()
                assert result is mock_instance
                MockProvider.assert_called_once()

    def test_get_secret_provider_aws_secrets_manager(self):
        with patch('pr_agent.secret_providers.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = "aws_secrets_manager"
            settings.config.secret_provider = "aws_secrets_manager"
            mock_get_settings.return_value = settings

            with patch('pr_agent.secret_providers.aws_secrets_manager_provider.AWSSecretsManagerProvider') as MockProvider:
                mock_instance = MagicMock()
                MockProvider.return_value = mock_instance

                result = get_secret_provider()
                assert result is mock_instance
                MockProvider.assert_called_once()

    def test_get_secret_provider_unknown_provider(self):
        with patch('pr_agent.secret_providers.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = "unknown_provider"
            settings.config.secret_provider = "unknown_provider"
            mock_get_settings.return_value = settings

            with pytest.raises(ValueError, match="Unknown SECRET_PROVIDER"):
                get_secret_provider()

    def test_get_secret_provider_initialization_error(self):
        with patch('pr_agent.secret_providers.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = "aws_secrets_manager"
            settings.config.secret_provider = "aws_secrets_manager"
            mock_get_settings.return_value = settings

            with patch('pr_agent.secret_providers.aws_secrets_manager_provider.AWSSecretsManagerProvider') as MockProvider:
                MockProvider.side_effect = Exception("Initialization failed")

                with pytest.raises(ValueError, match="Failed to initialize aws_secrets_manager secret provider"):
                    get_secret_provider()
