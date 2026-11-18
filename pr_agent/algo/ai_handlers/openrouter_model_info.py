"""Fetch exact model-level context limits from OpenRouter's public API."""

import asyncio
import json
import os
import re
from urllib.parse import quote

import httpx

from pr_agent.log import get_logger

MODEL_CONTEXT_TIMEOUT = 5.0
_MAX_RESPONSE_BYTES = 1024 * 1024
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:-]*")


def _unavailable_context(reason: str, *, transient: bool = False) -> None:
    """Log a controlled reason without upstream response or credential details."""
    get_logger().info(f"OpenRouter model context unavailable: {reason}")
    if transient:
        from pr_agent.algo.token_budget import ModelContextPreparationError

        raise ModelContextPreparationError(
            f"OpenRouter model context temporarily unavailable: {reason}."
            " Set config.custom_model_max_tokens to a verified positive input-token limit to skip this lookup."
        ) from None


async def fetch_model_context(model_id: str) -> int | None:
    """Return an exact ID's positive context_length without metadata inference.

    Accept unprefixed author/slug IDs up to 512 characters, retaining variant
    suffixes. Bound each uncached request to at most five seconds and one MiB.
    Definitive misses return None; transient failures permit model fallback.
    """
    from pr_agent.algo.token_budget import ModelContextPreparationError

    if os.getenv("LITELLM_OFFLINE_MODE", "").strip().lower() == "true":
        _unavailable_context("offline mode")
        return None
    if not isinstance(model_id, str) or len(model_id) > 512 or not _MODEL_ID.fullmatch(model_id):
        _unavailable_context("unsupported model ID")
        return None
    author, slug = model_id.split("/")
    url = f"https://openrouter.ai/api/v1/model/{quote(author, safe='')}/{quote(slug, safe='')}"
    try:
        async with asyncio.timeout(MODEL_CONTEXT_TIMEOUT):
            try:
                client = httpx.AsyncClient(timeout=MODEL_CONTEXT_TIMEOUT, follow_redirects=False)
            except ImportError:
                return _unavailable_context("HTTP client dependency unavailable", transient=True)
            async with client:
                async with client.stream(
                    "GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code in (408, 429) or response.status_code >= 500:
                        return _unavailable_context(f"HTTP {response.status_code}", transient=True)
                    if response.status_code != 200:
                        return _unavailable_context(f"HTTP {response.status_code}")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        return _unavailable_context("unexpected response encoding")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            return _unavailable_context("response exceeds size limit")
                        body.extend(chunk)
                    payload = json.loads(body)
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if not isinstance(data, dict) or data.get("id") != model_id:
                        return _unavailable_context("response ID does not match requested model")
                    context = data.get("context_length")
                    if type(context) is int and context > 0:
                        return context
                    return _unavailable_context("response has no positive context_length")
    except ModelContextPreparationError:
        raise
    except (httpx.TransportError, OSError, TimeoutError) as error:
        return _unavailable_context(type(error).__name__, transient=True)
    except (httpx.HTTPError, ValueError, RecursionError) as error:
        return _unavailable_context(type(error).__name__)
