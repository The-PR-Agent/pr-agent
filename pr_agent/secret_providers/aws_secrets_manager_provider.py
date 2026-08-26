import json

import boto3
from botocore.exceptions import ClientError

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.secret_providers.secret_provider import SecretProvider


class AWSSecretsManagerProvider(SecretProvider):
    def __init__(self):
        try:
            region_name = get_settings().get("aws_secrets_manager.region_name") or \
                         get_settings().get("aws.AWS_REGION_NAME")
            if region_name:
                self.client = boto3.client('secretsmanager', region_name=region_name)
            else:
                self.client = boto3.client('secretsmanager')

            self.secret_arn = get_settings().get("aws_secrets_manager.secret_arn")
            if not self.secret_arn:
                raise ValueError("AWS Secrets Manager ARN is not configured")
        except Exception as e:
            get_logger().error(f"Failed to initialize AWS Secrets Manager Provider: {e}")
            raise e

    def get_secret(self, secret_name: str) -> str:
        """
        Retrieve individual secret by name (for webhook tokens)
        """
        try:
            response = self.client.get_secret_value(SecretId=secret_name)
            return response['SecretString']
        except Exception as e:
            get_logger().warning(f"Failed to get secret {secret_name} from AWS Secrets Manager: {e}")
            return ""

    def get_all_secrets(self) -> dict:
        """
        Retrieve all secrets for configuration override
        """
        try:
            response = self.client.get_secret_value(SecretId=self.secret_arn)
            return json.loads(response['SecretString'])
        except Exception as e:
            get_logger().error(f"Failed to get secrets from AWS Secrets Manager {self.secret_arn}: {e}")
            return {}

    def store_secret(self, secret_name: str, secret_value: str):
        try:
            self.client.put_secret_value(
                SecretId=secret_name,
                SecretString=secret_value
            )
        except Exception as e:
            get_logger().error(f"Failed to store secret {secret_name} in AWS Secrets Manager: {e}")
            raise e
