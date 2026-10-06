"""Startup validation and credential-free SDK call context for MOSAICO."""
import pytest
from starlette.authentication import AuthCredentials, SimpleUser
from starlette.requests import Request

from pr_agent.mosaico.auth import MosaicoAuthenticationBackend, MosaicoCallContextBuilder


@pytest.mark.parametrize("tokens", [
    None, "secret", [], {"": "secret"}, {"a b": "secret"}, {"alice": ""},
    {"alice": "with spaces"}, {"alice": "nonascii-é"}, {"alice": 123},
    {"alice": "same", "bob": "same"},
])
def test_invalid_credentials_fail_startup(tokens):
    with pytest.raises(ValueError, match="mosaico.bearer_tokens"):
        MosaicoAuthenticationBackend(tokens)


def test_call_context_uses_authenticated_user_and_drops_bearer_secret():
    request = Request({
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"authorization", b"Bearer private-secret"), (b"a2a-version", b"1.0")],
        "user": SimpleUser("alice"), "auth": AuthCredentials(["authenticated"]),
    })
    ctx = MosaicoCallContextBuilder().build(request)
    assert ctx.user.user_name == "alice"
    assert ctx.user.is_authenticated
    assert ctx.state["headers"] == {"a2a-version": "1.0"}
    assert "private-secret" not in repr(ctx)
