"""Tests for dashboard log redaction — assert secret absence, not placeholder presence."""
from __future__ import annotations

import base64
from urllib.parse import quote

import pytest

from pr_dashboard import redaction


class TestRedact:
    def test_exact_secret_absent_from_output(self, monkeypatch):
        """A configured secret value is absent from the redacted output"""
        secret = "ghp_ExactSecretValueForTest1234567890"
        monkeypatch.setattr(redaction, "secret_values", lambda: [secret])
        out = redaction.redact(f"token was {secret} in the log")
        assert secret not in out

    def test_url_encoded_secret_absent_from_output(self, monkeypatch):
        """A URL-encoded form of a configured secret is absent from the output"""
        secret = "tok/with spaces&special=1"
        monkeypatch.setattr(redaction, "secret_values", lambda: [secret])
        encoded = quote(secret, safe="")
        out = redaction.redact(f"saw {encoded} in a query")
        assert secret not in out
        assert encoded not in out

    def test_base64_secret_absent_from_output(self, monkeypatch):
        """A Base64 form of a configured secret is absent from the output"""
        secret = "super-secret-token-value-xyz"
        monkeypatch.setattr(redaction, "secret_values", lambda: [secret])
        b64 = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        out = redaction.redact(f"Authorization payload {b64}")
        assert secret not in out
        assert b64 not in out

    def test_authorization_bearer_redacted_with_no_secrets_configured(self, monkeypatch):
        """Authorization: Bearer <blob> is redacted even when the secret set is empty"""
        monkeypatch.setattr(redaction, "secret_values", lambda: [])
        blob = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        line = f"Authorization: Bearer {blob}"
        out = redaction.redact(line)
        assert blob not in out
        assert "Authorization:" in out

    def test_redaction_unavailable_propagates_when_inventory_fails(self, monkeypatch):
        """RedactionUnavailable propagates when the secret inventory cannot be read"""
        def boom():
            raise redaction.RedactionUnavailable("secret inventory unavailable")

        monkeypatch.setattr(redaction, "secret_values", boom)
        with pytest.raises(redaction.RedactionUnavailable):
            redaction.redact("harmless log line")

    def test_ordinary_log_text_unchanged(self, monkeypatch):
        """Ordinary log text with no secrets is unchanged"""
        monkeypatch.setattr(redaction, "secret_values", lambda: [])
        text = "review finished for owner/repo#12 in 3.2s"
        assert redaction.redact(text) == text
