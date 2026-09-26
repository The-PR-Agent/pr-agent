import json

import boto3

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger
from pr_agent.secret_providers.secret_provider import SecretProvider


def _error_kind(error: Exception) -> str:
    # Keep the AWS error code for diagnostics; never log the associated message.
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        error_details = response.get("Error")
        if isinstance(error_details, dict):
            code = error_details.get("Code")
            if isinstance(code, str) and code:
                return code
    return type(error).__name__


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
            # Avoid logging SDK error text, which can include credential-process output.
            get_logger().error(f"Failed to initialize AWS Secrets Manager Provider: {_error_kind(e)}")
            raise e

    def get_secret(self, secret_name: str) -> str:
        """
        Retrieve individual secret by name (for webhook tokens)
        """
        try:
            response = self.client.get_secret_value(SecretId=secret_name)
            return response['SecretString']
        except Exception as e:
            # Omit the secret name because GitLab passes its webhook token here.
            get_logger().warning(f"Failed to get secret from AWS Secrets Manager: {_error_kind(e)}")
            return ""

    def get_all_secrets(self) -> dict:
        """
        Retrieve all secrets for configuration override
        """
        try:
            response = self.client.get_secret_value(SecretId=self.secret_arn)
            return json.loads(response['SecretString'])
        except Exception as e:
            get_logger().error(
                f"Failed to get secrets from AWS Secrets Manager {self.secret_arn}: {_error_kind(e)}"
            )
            return {}

    def store_secret(self, secret_name: str, secret_value: str):
        try:
            self.client.put_secret_value(
                SecretId=secret_name,
                SecretString=secret_value
            )
        except Exception as e:
            get_logger().error(f"Failed to store secret in AWS Secrets Manager: {_error_kind(e)}")
            raise e
