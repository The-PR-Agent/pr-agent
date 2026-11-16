"""Configured bearer principals for the MOSAICO HTTP and A2A boundaries."""
import re
import secrets
from collections.abc import Mapping

from a2a.server.routes import DefaultServerCallContextBuilder
from starlette.authentication import AuthCredentials, AuthenticationBackend, AuthenticationError, SimpleUser
from starlette.responses import JSONResponse

_BEARER_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")


class MosaicoAuthenticationBackend(AuthenticationBackend):
    def __init__(self, tokens):
        if not isinstance(tokens, Mapping):
            raise ValueError("mosaico.bearer_tokens must map principal names to distinct bearer secrets")
        credentials = []
        seen = set()
        for principal, token in tokens.items():
            if (not isinstance(principal, str) or not principal or any(c.isspace() for c in principal)
                    or not isinstance(token, str) or not _BEARER_TOKEN_RE.fullmatch(token)):
                raise ValueError("mosaico.bearer_tokens contains an invalid principal or bearer secret")
            encoded = token.encode("ascii")
            if encoded in seen:
                raise ValueError("mosaico.bearer_tokens must use a distinct secret for each principal")
            seen.add(encoded)
            credentials.append((principal, encoded))
        self.credentials = tuple(credentials)

    @property
    def enabled(self):
        return bool(self.credentials)

    async def authenticate(self, conn):
        public_card = (
            conn.scope.get("method") in {"GET", "HEAD"}
            and conn.scope["path"] == "/.well-known/agent-card.json"
        )
        if not self.enabled or public_card:
            return None
        headers = conn.headers.getlist("authorization")
        if len(headers) != 1:
            raise AuthenticationError("Unauthorized")
        parts = headers[0].split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not _BEARER_TOKEN_RE.fullmatch(parts[1]):
            raise AuthenticationError("Unauthorized")
        token = parts[1].encode("ascii")
        for principal, expected in self.credentials:
            if secrets.compare_digest(token, expected):
                return AuthCredentials(["authenticated"]), SimpleUser(principal)
        raise AuthenticationError("Unauthorized")


def authentication_error(_conn, _exc):
    return JSONResponse({"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


class MosaicoCallContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request):
        call_context = super().build(request)
        # Keep credentials out of SDK context state and its logs. Identity comes
        # from the authenticated Starlette user, never from message metadata.
        call_context.state["headers"] = {
            key: value for key, value in call_context.state["headers"].items() if key.lower() != "authorization"
        }
        return call_context
