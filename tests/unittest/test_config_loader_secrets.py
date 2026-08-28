from unittest.mock import MagicMock, patch

from pr_agent.config_loader import apply_secrets_manager_config, apply_secrets_to_config


class TestConfigLoaderSecrets:

    def test_apply_secrets_manager_config_success(self):
        with patch('pr_agent.secret_providers.get_secret_provider') as mock_get_provider, \
             patch('pr_agent.config_loader.apply_secrets_to_config') as mock_apply_secrets, \
             patch('pr_agent.config_loader.get_settings') as mock_get_settings:

            # Mock secret provider
            mock_provider = MagicMock()
            mock_provider.get_all_secrets.return_value = {'openai.key': 'sk-test'}
            mock_get_provider.return_value = mock_provider

            # Mock settings
            settings = MagicMock()
            settings.get.return_value = "aws_secrets_manager"
            mock_get_settings.return_value = settings

            apply_secrets_manager_config()

            mock_apply_secrets.assert_called_once_with({'openai.key': 'sk-test'})

    def test_apply_secrets_manager_config_no_provider(self):
        with patch('pr_agent.secret_providers.get_secret_provider') as mock_get_provider:
            mock_get_provider.return_value = None

            # Confirm no exception is raised
            apply_secrets_manager_config()

    def test_apply_secrets_manager_config_not_aws(self):
        with patch('pr_agent.secret_providers.get_secret_provider') as mock_get_provider, \
             patch('pr_agent.config_loader.get_settings') as mock_get_settings:

            # Mock Google Cloud Storage provider
            mock_provider = MagicMock()
            mock_get_provider.return_value = mock_provider

            # Mock settings (Google Cloud Storage)
            settings = MagicMock()
            settings.get.return_value = "google_cloud_storage"
            mock_get_settings.return_value = settings

            # Confirm execution is skipped for non-AWS Secrets Manager
            apply_secrets_manager_config()
            
            # Confirm get_all_secrets is not called
            assert not hasattr(mock_provider, 'get_all_secrets') or \
                   not mock_provider.get_all_secrets.called

    def test_apply_secrets_to_config_nested_keys(self):
        with patch('pr_agent.config_loader.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = None  # No existing value
            settings.set = MagicMock()
            mock_get_settings.return_value = settings

            secrets = {
                'openai.key': 'sk-test',
                'github.webhook_secret': 'webhook-secret'
            }

            apply_secrets_to_config(secrets)

            # Confirm settings are applied correctly
            settings.set.assert_any_call('OPENAI.KEY', 'sk-test')
            settings.set.assert_any_call('GITHUB.WEBHOOK_SECRET', 'webhook-secret')

    def test_apply_secrets_to_config_existing_value_preserved(self):
        with patch('pr_agent.config_loader.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = "existing-value"  # Existing value present
            settings.set = MagicMock()
            mock_get_settings.return_value = settings

            secrets = {'openai.key': 'sk-test'}

            apply_secrets_to_config(secrets)

            # Confirm settings are not overridden when existing value present
            settings.set.assert_not_called()

    def test_apply_secrets_to_config_single_key(self):
        with patch('pr_agent.config_loader.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = None
            settings.set = MagicMock()
            mock_get_settings.return_value = settings

            secrets = {'simple_key': 'simple_value'}

            apply_secrets_to_config(secrets)

            # Confirm non-dot notation keys are ignored
            settings.set.assert_not_called()

    def test_apply_secrets_to_config_multiple_dots(self):
        with patch('pr_agent.config_loader.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.get.return_value = None
            settings.set = MagicMock()
            mock_get_settings.return_value = settings

            secrets = {'section.subsection.key': 'value'}

            apply_secrets_to_config(secrets)

            # Confirm keys with multiple dots are ignored
            settings.set.assert_not_called()

    def test_apply_secrets_manager_config_exception_handling(self):
        with patch('pr_agent.secret_providers.get_secret_provider') as mock_get_provider:
            mock_get_provider.side_effect = Exception("Provider error")

            # Confirm processing continues even when exception occurs
            apply_secrets_manager_config()  # Confirm no exception is raised 
