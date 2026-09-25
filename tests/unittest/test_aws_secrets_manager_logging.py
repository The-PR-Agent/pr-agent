"""Keep secret lookup tokens and SDK error details out of provider logs."""

from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError, CredentialRetrievalError
from botocore.stub import Stubber

import pr_agent.secret_providers.aws_secrets_manager_provider as aws_provider
from pr_agent.log import get_logger

ERROR_SECRET = "aws-error-secret-sentinel"
WEBHOOK_TOKEN = "webhook-token-secret-sentinel"
STORE_SECRET_NAME = "aws-store-secret-name-sentinel"
SECRET_NAME = "test-secret"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret"


@pytest.fixture
def provider_settings(monkeypatch):
    settings = {
        "aws_secrets_manager.region_name": "us-east-1",
        "aws_secrets_manager.secret_arn": SECRET_ARN,
    }
    monkeypatch.setattr(aws_provider, "get_settings", lambda: settings)


@pytest.fixture(params=[False, True], ids=["console", "json"])
def provider_logs(request):
    messages = []
    sink = get_logger().add(
        messages.append,
        filter=lambda record: record["name"] == aws_provider.__name__,
        level="DEBUG",
        format="{message}",
        serialize=request.param,
        backtrace=True,
        diagnose=True,
    )
    try:
        yield messages
    finally:
        get_logger().remove(sink)


def assert_safe_log(messages, expected_message, level):
    assert len(messages) == 1
    message = messages[0]
    assert ERROR_SECRET not in str(message)
    assert WEBHOOK_TOKEN not in str(message)
    assert STORE_SECRET_NAME not in str(message)
    assert "Traceback (most recent call last)" not in str(message)
    assert message.record["message"] == expected_message
    assert message.record["level"].name == level
    assert message.record["exception"] is None
    assert not message.record["extra"]


@pytest.mark.parametrize("method", ["__init__", "get_secret", "get_all_secrets", "store_secret"])
def test_sdk_error_details_are_not_logged(monkeypatch, provider_settings, provider_logs, method):
    # Model the SDK error that includes stderr from a failed credential_process.
    error = CredentialRetrievalError(provider="custom-process", error_msg=ERROR_SECRET)
    client = MagicMock()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(aws_provider.boto3, "client", client_factory)

    if method == "__init__":
        client_factory.side_effect = error
        with pytest.raises(CredentialRetrievalError) as caught:
            aws_provider.AWSSecretsManagerProvider()
        assert caught.value is error
        expected = "Failed to initialize AWS Secrets Manager Provider: CredentialRetrievalError"
    else:
        provider = aws_provider.AWSSecretsManagerProvider()
        if method == "store_secret":
            client.put_secret_value.side_effect = error
            with pytest.raises(CredentialRetrievalError) as caught:
                provider.store_secret(STORE_SECRET_NAME, "test-value")
            assert caught.value is error
            expected = "Failed to store secret in AWS Secrets Manager: CredentialRetrievalError"
        else:
            client.get_secret_value.side_effect = error
            if method == "get_secret":
                assert provider.get_secret(SECRET_NAME) == ""
                expected = "Failed to get secret from AWS Secrets Manager: CredentialRetrievalError"
            else:
                assert provider.get_all_secrets() == {}
                expected = f"Failed to get secrets from AWS Secrets Manager {SECRET_ARN}: CredentialRetrievalError"

    assert ERROR_SECRET in str(error)  # Do not mutate the exception returned to callers.
    assert_safe_log(provider_logs, expected, "WARNING" if method == "get_secret" else "ERROR")


def test_unmodeled_aws_error_code_is_logged_without_message(
    monkeypatch, provider_settings, provider_logs
):
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": ERROR_SECRET}},
        "GetSecretValue",
    )
    client = MagicMock()
    monkeypatch.setattr(aws_provider.boto3, "client", MagicMock(return_value=client))
    provider = aws_provider.AWSSecretsManagerProvider()
    client.get_secret_value.side_effect = error

    assert provider.get_secret(SECRET_NAME) == ""
    assert ERROR_SECRET in str(error)
    assert_safe_log(
        provider_logs,
        "Failed to get secret from AWS Secrets Manager: AccessDeniedException",
        "WARNING",
    )


def test_webhook_token_is_not_logged_on_service_error(monkeypatch, provider_settings, provider_logs):
    client = boto3.session.Session().client(
        "secretsmanager",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    try:
        monkeypatch.setattr(aws_provider.boto3, "client", lambda *args, **kwargs: client)
        provider = aws_provider.AWSSecretsManagerProvider()
        with Stubber(client) as stubber:
            # Keep the service error free of secrets to isolate the token-bearing SecretId.
            stubber.add_client_error(
                "get_secret_value",
                service_error_code="InternalServiceError",
                service_message="Temporary service failure",
                http_status_code=500,
                expected_params={"SecretId": WEBHOOK_TOKEN},
            )
            assert provider.get_secret(WEBHOOK_TOKEN) == ""
            stubber.assert_no_pending_responses()
    finally:
        client.close()

    assert_safe_log(provider_logs, "Failed to get secret from AWS Secrets Manager: InternalServiceError", "WARNING")


def test_store_secret_logs_error_code_without_secret_name(
    monkeypatch, provider_settings, provider_logs
):
    error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": ERROR_SECRET}},
        "PutSecretValue",
    )
    client = MagicMock()
    monkeypatch.setattr(aws_provider.boto3, "client", MagicMock(return_value=client))
    provider = aws_provider.AWSSecretsManagerProvider()
    client.put_secret_value.side_effect = error

    with pytest.raises(ClientError) as caught:
        provider.store_secret(STORE_SECRET_NAME, "test-value")
    assert caught.value is error
    assert ERROR_SECRET in str(error)
    assert_safe_log(
        provider_logs,
        "Failed to store secret in AWS Secrets Manager: AccessDeniedException",
        "ERROR",
    )
