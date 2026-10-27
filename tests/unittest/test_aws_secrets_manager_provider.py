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


            settings = {
                "aws_secrets_manager.secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",
                "aws_secrets_manager.region_name": "us-east-1",
                "aws.AWS_REGION_NAME": "us-east-1"
            }
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
        error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}}, "GetSecretValue")
        mock_client.get_secret_value.side_effect = error

        result = provider.get_secret('nonexistent-secret')
        assert result == ""  # Confirm empty string is returned for missing secret

    def test_get_secret_raises_on_non_not_found_error(self):
        provider, mock_client = self._provider()
        error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "GetSecretValue")
        mock_client.get_secret_value.side_effect = error

        with pytest.raises(ClientError) as caught:
            provider.get_secret('some-secret')
        assert caught.value is error

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

    def test_store_secret_create_missing(self):
        provider, mock_client = self._provider()
        error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "AWS error"}},
                            "PutSecretValue")
        mock_client.put_secret_value.side_effect = error

        provider.store_secret('test-secret', 'test-value')

        mock_client.create_secret.assert_called_once_with(
            Name='test-secret',
            SecretString='test-value'
        )

    @pytest.mark.parametrize(("stored", "raises"), [("test-value", False), ("other-value", True)])
    def test_store_secret_concurrent_create(self, stored, raises):
        provider, mock_client = self._provider()
        mock_client.put_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "AWS error"}}, "PutSecretValue")
        mock_client.create_secret.side_effect = ClientError(
            {"Error": {"Code": "ResourceExistsException", "Message": "AWS error"}}, "CreateSecret")
        mock_client.get_secret_value.return_value = {"SecretString": stored}

        if raises:
            with pytest.raises(ClientError):
                provider.store_secret('test-secret', 'test-value')
        else:
            provider.store_secret('test-secret', 'test-value')

    def test_init_failure_invalid_config(self):
        with patch("pr_agent.secret_providers.aws_secrets_manager_provider.get_settings") as mock_get_settings, \
             patch("pr_agent.secret_providers.aws_secrets_manager_provider.boto3.client"):

            settings = {
                "aws_secrets_manager.region_name": "us-east-1",
                "aws.AWS_REGION_NAME": "us-east-1",
                "aws_secrets_manager.secret_arn": None
            }
            mock_get_settings.return_value = settings

            with pytest.raises(ValueError, match="AWS Secrets Manager ARN is not configured"):
                AWSSecretsManagerProvider()

    def test_store_secret_failure(self):
        provider, mock_client = self._provider()
        error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "AWS error"}},
                            "PutSecretValue")
        mock_client.put_secret_value.side_effect = error

        with pytest.raises(ClientError) as caught:
            provider.store_secret('test-secret', 'test-value')

        assert caught.value is error
