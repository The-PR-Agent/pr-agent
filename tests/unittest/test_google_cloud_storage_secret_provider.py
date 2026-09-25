from unittest.mock import MagicMock

from pr_agent.secret_providers.google_cloud_storage_secret_provider import GoogleCloudStorageSecretProvider


def test_get_secret_returns_text():
    bucket = MagicMock()
    blob = MagicMock()
    bucket.blob.return_value = blob
    blob.download_as_text.return_value = "secret-value"

    provider = object.__new__(GoogleCloudStorageSecretProvider)
    provider.bucket = bucket

    result = provider.get_secret("test-secret")

    assert result == "secret-value"
    assert isinstance(result, str)
    bucket.blob.assert_called_once_with("test-secret")
    blob.download_as_text.assert_called_once_with()
