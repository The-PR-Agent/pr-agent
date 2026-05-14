import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from pr_agent.secret_providers.aws_secrets_manager_provider import AWSSecretsManagerProvider


class TestAWSSecretsManagerProvider:

    def _provider(self):
        """Create provider following existing pattern"""
        with patch('pr_agent.secret_providers.aws_secrets_manager_provider.get_settings') as mock_get_settings, \
             patch('pr_agent.secret_providers.aws_secrets_manager_provider.boto3.client') as mock_boto3_client:

            settings = MagicMock()
            settings.get.side_effect = lambda k, d=None: {
                'aws_secrets_manager.secret_arn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret',
                'aws_secrets_manager.region_name': 'us-east-1',
                'aws.AWS_REGION_NAME': 'us-east-1'
            }.get(k, d)
            settings.aws_secrets_manager.secret_arn = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret'
            mock_get_settings.return_value = settings

            # Mock boto3 client
            mock_client = MagicMock()
            mock_boto3_client.return_value = mock_client

            provider = AWSSecretsManagerProvider()
            provider.client = mock_client  # Set client directly for testing
            return provider, mock_client

    # Positive test cases
    def test_get_secret_success(self):
        provider, mock_client = self._provider()
        mock_client.get_secret_value.return_value = {'SecretString': 'test-secret-value'}

        result = provider.get_secret('test-secret-name')
        assert result == 'test-secret-value'
        mock_client.get_secret_value.assert_called_once_with(SecretId='test-secret-name')

    def test_get_all_secrets_success(self):
        provider, mock_client = self._provider()
        secret_data = {'openai.key': 'sk-test', 'github.webhook_secret': 'webhook-secret'}
        mock_client.get_secret_value.return_value = {'SecretString': json.dumps(secret_data)}

        result = provider.get_all_secrets()
        assert result == secret_data

    # Negative test cases (following Google Cloud Storage pattern)
    def test_get_secret_failure(self):
        provider, mock_client = self._provider()
        mock_client.get_secret_value.side_effect = Exception("AWS error")

        result = provider.get_secret('nonexistent-secret')
        assert result == ""  # Confirm empty string is returned

    def test_get_all_secrets_failure(self):
        provider, mock_client = self._provider()
        mock_client.get_secret_value.side_effect = Exception("AWS error")

        result = provider.get_all_secrets()
        assert result == {}  # Confirm empty dictionary is returned

    def test_store_secret_update_existing(self):
        provider, mock_client = self._provider()
        mock_client.update_secret.return_value = {}

        provider.store_secret('test-secret', 'test-value')
        mock_client.put_secret_value.assert_called_once_with(
            SecretId='test-secret',
            SecretString='test-value'
        )

    def test_init_failure_invalid_config(self):
        with patch('pr_agent.secret_providers.aws_secrets_manager_provider.get_settings') as mock_get_settings:
            settings = MagicMock()
            settings.aws_secrets_manager.secret_arn = None  # Configuration error
            mock_get_settings.return_value = settings

            with pytest.raises(Exception):
                AWSSecretsManagerProvider()

    def test_store_secret_failure(self):
        provider, mock_client = self._provider()
        mock_client.put_secret_value.side_effect = Exception("AWS error")

        with pytest.raises(Exception):
            provider.store_secret('test-secret', 'test-value') 
