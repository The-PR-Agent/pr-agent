"""Keep Google Cloud Storage secret-provider failures out of logs."""

from unittest.mock import MagicMock

import pytest

import pr_agent.secret_providers.google_cloud_storage_secret_provider as gcs_provider
from pr_agent.log import get_logger

ERROR_SECRET = "gcs-sdk-error-secret-sentinel"
STORE_SECRET_NAME = "gcs-store-secret-name-sentinel"


@pytest.fixture
def provider_settings(monkeypatch):
    settings = MagicMock()
    settings.google_cloud_storage.service_account = "{}"
    settings.google_cloud_storage.bucket_name = "test-bucket"
    monkeypatch.setattr(gcs_provider, "get_settings", lambda: settings)


@pytest.fixture
def provider_logs():
    messages = []
    sink = get_logger().add(
        messages.append,
        filter=lambda record: record["name"] == gcs_provider.__name__,
        level="DEBUG",
        format="{message}",
        backtrace=True,
        diagnose=True,
    )
    try:
        yield messages
    finally:
        get_logger().remove(sink)


def assert_safe_log(messages, expected_message, level="ERROR"):
    assert len(messages) == 1
    message = messages[0]
    assert ERROR_SECRET not in str(message)
    assert STORE_SECRET_NAME not in str(message)
    assert "Traceback (most recent call last)" not in str(message)
    assert message.record["message"] == expected_message
    assert message.record["level"].name == level
    assert message.record["exception"] is None
    assert not message.record["extra"]


def test_init_does_not_log_raw_client_error(monkeypatch, provider_settings, provider_logs):
    error = RuntimeError(ERROR_SECRET)
    monkeypatch.setattr(
        gcs_provider.storage.Client,
        "from_service_account_info",
        MagicMock(side_effect=error),
    )

    with pytest.raises(RuntimeError) as caught:
        gcs_provider.GoogleCloudStorageSecretProvider()
    assert caught.value is error
    assert ERROR_SECRET in str(error)
    assert_safe_log(
        provider_logs,
        "Failed to initialize Google Cloud Storage Secret Provider: RuntimeError",
    )


def test_store_does_not_log_secret_name_or_raw_sdk_error(provider_logs):
    error = RuntimeError(ERROR_SECRET)
    blob = MagicMock()
    blob.upload_from_string.side_effect = error
    bucket = MagicMock()
    bucket.blob.return_value = blob

    provider = object.__new__(gcs_provider.GoogleCloudStorageSecretProvider)
    provider.bucket = bucket

    with pytest.raises(RuntimeError) as caught:
        provider.store_secret(STORE_SECRET_NAME, "test-value")
    assert caught.value is error
    bucket.blob.assert_called_once_with(STORE_SECRET_NAME)
    blob.upload_from_string.assert_called_once_with("test-value")
    assert_safe_log(
        provider_logs,
        "Failed to store secret in Google Cloud Storage: RuntimeError",
    )


def test_get_secret_does_not_log_secret_name_or_raw_sdk_error(provider_logs):
    error = RuntimeError(ERROR_SECRET)
    blob = MagicMock()
    blob.download_as_text.side_effect = error
    bucket = MagicMock()
    bucket.blob.return_value = blob

    provider = object.__new__(gcs_provider.GoogleCloudStorageSecretProvider)
    provider.bucket = bucket

    assert provider.get_secret(STORE_SECRET_NAME) == ""
    bucket.blob.assert_called_once_with(STORE_SECRET_NAME)
    blob.download_as_text.assert_called_once_with()
    assert_safe_log(
        provider_logs,
        "Failed to get secret from Google Cloud Storage: RuntimeError",
        level="WARNING",
    )
