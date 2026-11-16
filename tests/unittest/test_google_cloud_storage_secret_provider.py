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


def test_get_secret_returns_empty_on_not_found():
    from google.api_core.exceptions import NotFound

    bucket = MagicMock()
    blob = MagicMock()
    bucket.blob.return_value = blob
    blob.download_as_text.side_effect = NotFound("not found")

    provider = object.__new__(GoogleCloudStorageSecretProvider)
    provider.bucket = bucket

    assert provider.get_secret("missing-secret") == ""


def test_get_secret_raises_on_other_errors():
    import pytest

    bucket = MagicMock()
    blob = MagicMock()
    bucket.blob.return_value = blob
    blob.download_as_text.side_effect = RuntimeError("other error")

    provider = object.__new__(GoogleCloudStorageSecretProvider)
    provider.bucket = bucket

    with pytest.raises(RuntimeError):
        provider.get_secret("error-secret")
