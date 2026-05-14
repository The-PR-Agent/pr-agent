from unittest.mock import MagicMock, patch

import pytest

from pr_agent.secret_providers import get_secret_provider


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
