import ujson
from google.cloud import storage

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.secret_providers.secret_provider import SecretProvider


class GoogleCloudStorageSecretProvider(SecretProvider):
    def __init__(self):
        try:
            self.client = storage.Client.from_service_account_info(ujson.loads(get_settings().google_cloud_storage.
                                                                               service_account))
            self.bucket_name = get_settings().google_cloud_storage.bucket_name
            self.bucket = self.client.bucket(self.bucket_name)
        except Exception as e:
            get_logger().error(f"Failed to initialize Google Cloud Storage Secret Provider: {type(e).__name__}")
            raise e

    def get_secret(self, secret_name: str) -> str:
        try:
            blob = self.bucket.blob(secret_name)
            return blob.download_as_text()
        except Exception as e:
            # Omit the secret name because the GitLab webhook passes its token here.
            get_logger().warning(f"Failed to get secret from Google Cloud Storage: {type(e).__name__}")
            return ""

    def store_secret(self, secret_name: str, secret_value: str):
        try:
            blob = self.bucket.blob(secret_name)
            blob.upload_from_string(secret_value)
        except Exception as e:
            get_logger().error(f"Failed to store secret in Google Cloud Storage: {type(e).__name__}")
            raise e
