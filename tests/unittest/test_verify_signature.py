import hashlib
import hmac

import pytest
from fastapi import HTTPException

from pr_agent.servers.utils import verify_signature


def _sign(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256).hexdigest()


class TestVerifySignature:
    secret = "unit-test-signing-value"
    payload = b'{"action":"opened","number":1}'

    def test_valid_signature_does_not_raise(self):
        signature = _sign(self.payload, self.secret)
        # Should return None without raising
        assert verify_signature(self.payload, self.secret, signature) is None

    @pytest.mark.parametrize("missing", [None, ""])
    def test_missing_signature_raises_403(self, missing):
        with pytest.raises(HTTPException) as exc_info:
            verify_signature(self.payload, self.secret, missing)
        assert exc_info.value.status_code == 403
        assert "x-hub-signature-256" in exc_info.value.detail

    def test_invalid_signature_raises_403(self):
        bad_signature = "sha256=" + "0" * 64
        with pytest.raises(HTTPException) as exc_info:
            verify_signature(self.payload, self.secret, bad_signature)
        assert exc_info.value.status_code == 403
        assert "didn't match" in exc_info.value.detail

    def test_signature_for_different_payload_is_rejected(self):
        other_payload = b'{"action":"closed","number":2}'
        signature_for_other = _sign(other_payload, self.secret)
        with pytest.raises(HTTPException) as exc_info:
            verify_signature(self.payload, self.secret, signature_for_other)
        assert exc_info.value.status_code == 403

    def test_signature_with_wrong_secret_is_rejected(self):
        signature_wrong_secret = _sign(self.payload, "other-signing-value")
        with pytest.raises(HTTPException) as exc_info:
            verify_signature(self.payload, self.secret, signature_wrong_secret)
        assert exc_info.value.status_code == 403
