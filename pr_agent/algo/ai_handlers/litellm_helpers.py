import json

from pr_agent.algo.ai_handlers.base_ai_handler import make_api_error
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


async def _handle_streaming_response(response):
    """
    Handle streaming response from acompletion and collect the full response.

    Args:
        response: The streaming response object from acompletion

    Returns:
        tuple: (full_response_content, finish_reason)
    """
    full_response = ""
    finish_reason = None

    try:
        async for chunk in response:
            if chunk.choices and len(chunk.choices) > 0:
                choice = chunk.choices[0]
                delta = choice.delta
                content = getattr(delta, 'content', None)
                if content:
                    full_response += content
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
    except Exception as e:
        get_logger().error(f"Error handling streaming response: {e}")
        raise

    if not full_response and finish_reason is None:
        get_logger().warning("Streaming response resulted in empty content with no finish reason")
        raise make_api_error("Empty streaming response received without proper completion")
    elif not full_response and finish_reason:
        get_logger().debug(f"Streaming response resulted in empty content but completed with finish_reason: {finish_reason}")
        raise make_api_error(f"Streaming response completed with finish_reason '{finish_reason}' but no content received")
    return full_response, finish_reason


class MockResponse:
    """Mock response object for streaming models to enable consistent logging."""

    def __init__(self, resp, finish_reason):
        self._data = {
            "choices": [
                {
                    "message": {"content": resp},
                    "finish_reason": finish_reason
                }
            ]
        }

    def dict(self):
        return self._data


def _get_azure_ad_token():
    """
    Generates an access token using Azure AD credentials from settings.
    Returns:
        str: The access token
    """
    from azure.identity import ClientSecretCredential
    try:
        credential = ClientSecretCredential(
            tenant_id=get_settings().azure_ad.tenant_id,
            client_id=get_settings().azure_ad.client_id,
            client_secret=get_settings().azure_ad.client_secret
        )
        # Get token for Azure OpenAI service
        token = credential.get_token("https://cognitiveservices.azure.com/.default")
        return token.token
    except Exception as e:
        get_logger().error(f"Failed to get Azure AD token: {e}")
        raise


def _process_litellm_extra_body(kwargs: dict) -> dict:
    """
    Process LITELLM.EXTRA_BODY configuration and update kwargs accordingly.

    Args:
        kwargs: The current kwargs dictionary to update

    Returns:
        Updated kwargs dictionary

    Raises:
        ValueError: If extra_body contains invalid JSON, unsupported keys, or colliding keys
    """
    allowed_extra_body_keys = {"processing_mode", "service_tier"}
    extra_body = getattr(getattr(get_settings(), "litellm", None), "extra_body", None)
    if extra_body:
        try:
            litellm_extra_body = json.loads(extra_body)
            if not isinstance(litellm_extra_body, dict):
                raise ValueError("LITELLM.EXTRA_BODY must be a JSON object")
            unsupported_keys = set(litellm_extra_body.keys()) - allowed_extra_body_keys
            if unsupported_keys:
                raise ValueError(f"LITELLM.EXTRA_BODY contains unsupported keys: {', '.join(unsupported_keys)}. Allowed keys: {', '.join(allowed_extra_body_keys)}")
            colliding_keys = kwargs.keys() & litellm_extra_body.keys()
            if colliding_keys:
                raise ValueError(f"LITELLM.EXTRA_BODY cannot override existing parameters: {', '.join(colliding_keys)}")
            kwargs.update(litellm_extra_body)
        except json.JSONDecodeError as e:
            raise ValueError(f"LITELLM.EXTRA_BODY contains invalid JSON: {str(e)}")
    return kwargs