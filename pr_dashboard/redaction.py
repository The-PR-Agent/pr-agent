"""Redact secrets from dashboard-rendered run logs.

Redaction is on read, not on write, so rotating a leaked credential also protects
historical logs. Exact-value substitution alone is not enough — tokens appear
URL-encoded, Base64'd, or truncated — so three passes run. Fail closed: if the secret
inventory cannot be read, raise rather than render the log unredacted.
"""
from __future__ import annotations

import base64
import re
from urllib.parse import quote

from pr_agent.config_loader import get_settings

# Known secret setting paths. An empty inventory is fine (structural passes still run);
# an *unreadable* inventory is not — that raises RedactionUnavailable.
_SECRET_SETTING_KEYS = (
    "openai.key",
    "anthropic.key",
    "cohere.key",
    "replicate.key",
    "groq.key",
    "sambanova.key",
    "xai.key",
    "huggingface.key",
    "google_ai_studio.gemini_api_key",
    "github.user_token",
    "github.webhook_secret",
    "gitlab.personal_access_token",
    "gitlab.shared_secret",
    "gitea.personal_access_token",
    "gitea.webhook_secret",
    "bitbucket.bearer_token",
    "bitbucket.basic_token",
    "bitbucket_server.bearer_token",
    "bitbucket_server.webhook_secret",
    "azure_devops.org.pat",
    "openai.api_key",
)

_PLACEHOLDER = "***"

_AUTH_HEADER = re.compile(
    r"(?im)^(Authorization:\s*)(\S+)(\s+)(\S+)\s*$",
)
_TOKEN_PREFIX = re.compile(
    r"\b(?:ghp_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9\-]+)\b",
)
# Long high-entropy hex (e.g. SHA-like tokens) and Base64 runs.
_LONG_HEX = re.compile(r"\b[A-Fa-f0-9]{40,}\b")
_LONG_B64 = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


class RedactionUnavailable(RuntimeError):
    """Raised when the secret inventory cannot be read; callers must not render the log."""


def secret_values() -> list[str]:
    """Return configured secret strings. Raises RedactionUnavailable if inventory is unreadable."""
    try:
        settings = get_settings()
        values: list[str] = []
        for key in _SECRET_SETTING_KEYS:
            raw = settings.get(key)
            if not isinstance(raw, str):
                continue
            text = raw.strip()
            if not text:
                continue
            # Skip template placeholders from .secrets_template.toml.
            if text in {"...", '""'} or text.startswith("<") or "YOUR_" in text:
                continue
            # Multi-line PEMs: keep whole block for exact match; skip tiny stubs.
            if (
                "BEGIN " in text
                and "PRIVATE KEY" in text
                and ("REPLACE" in text or "<GITHUB" in text)
            ):
                continue
            values.append(text)
        return values
    except RedactionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any inventory failure must fail closed
        raise RedactionUnavailable("secret inventory unavailable") from exc


def redact(text: str) -> str:
    """Return `text` with secrets and credential-shaped strings replaced by placeholders."""
    # Fail closed: an unreadable inventory means do not render, never "nothing to redact".
    secrets = secret_values()
    result = text

    # Pass 1+2: exact configured values, then their URL-encoded and Base64 forms.
    # Longest first so a longer secret is not partially left behind by a substring match.
    for secret in sorted(set(secrets), key=len, reverse=True):
        result = result.replace(secret, _PLACEHOLDER)
        encoded = quote(secret, safe="")
        if encoded != secret:
            result = result.replace(encoded, _PLACEHOLDER)
        result = result.replace(base64.b64encode(secret.encode("utf-8")).decode("ascii"), _PLACEHOLDER)

    # Pass 3: structural patterns that look like credentials regardless of source.
    result = _AUTH_HEADER.sub(rf"\1\2\3{_PLACEHOLDER}", result)
    result = _TOKEN_PREFIX.sub(_PLACEHOLDER, result)
    result = _LONG_HEX.sub(_PLACEHOLDER, result)
    result = _LONG_B64.sub(_PLACEHOLDER, result)
    return result
