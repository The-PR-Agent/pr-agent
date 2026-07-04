import hashlib
import hmac
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# pr_agent.servers.gitea_app imports PRAgent, which pulls in the litellm/openai
# AI-handler stack at import time. Those heavy deps are irrelevant to the
# webhook-routing logic under test here. Install lightweight stand-ins ONLY if
# the real modules are unavailable (e.g. a minimal test env), so on a full
# install the real modules are used untouched.
# ---------------------------------------------------------------------------
def _ensure_import_deps():
    try:
        import litellm  # noqa: F401
        import openai  # noqa: F401
        return
    except Exception:
        pass

    if 'litellm' not in sys.modules:
        litellm_stub = types.ModuleType('litellm')
        litellm_stub.acompletion = lambda *a, **k: None
        sys.modules['litellm'] = litellm_stub

    if 'openai' not in sys.modules:
        openai_stub = types.ModuleType('openai')

        class APIError(Exception):
            pass

        class RateLimitError(Exception):
            pass

        openai_stub.APIError = APIError
        openai_stub.RateLimitError = RateLimitError
        sys.modules['openai'] = openai_stub


_ensure_import_deps()

from pr_agent.servers import gitea_app  # noqa: E402


class _FakeRequest:
    """Minimal stand-in for a Starlette Request for get_body tests."""

    def __init__(self, raw: bytes, headers=None, json_raises=False):
        self._raw = raw
        self.headers = headers or {}
        self._json_raises = json_raises

    async def body(self):
        return self._raw

    async def json(self):
        if self._json_raises:
            raise ValueError("stream already consumed")
        return json.loads(self._raw)


class TestGiteaGetBody:
    """get_body must read the raw bytes first, verify the signature over those
    exact bytes, and only then parse JSON (item (f) body-read ordering fix)."""

    @pytest.mark.asyncio
    async def test_parses_body_without_secret(self):
        payload = {"action": "opened"}
        req = _FakeRequest(json.dumps(payload).encode())

        settings = MagicMock()
        settings.gitea = types.SimpleNamespace()  # no webhook_secret attr
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            body = await gitea_app.get_body(req)

        assert body == payload

    @pytest.mark.asyncio
    async def test_verifies_signature_over_raw_bytes(self):
        payload = {"action": "opened", "number": 3}
        raw = json.dumps(payload).encode()
        secret = "s3cr3t"
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        req = _FakeRequest(raw, headers={'x-gitea-signature': digest})

        settings = MagicMock()
        settings.gitea = types.SimpleNamespace(webhook_secret=secret)
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            body = await gitea_app.get_body(req)

        assert body == payload

    @pytest.mark.asyncio
    async def test_rejects_bad_signature(self):
        from fastapi import HTTPException

        raw = json.dumps({"action": "opened"}).encode()
        req = _FakeRequest(raw, headers={'x-gitea-signature': 'deadbeef'})

        settings = MagicMock()
        settings.gitea = types.SimpleNamespace(webhook_secret='s3cr3t')
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            with pytest.raises(HTTPException) as ei:
                await gitea_app.get_body(req)
        assert ei.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_signature_header_rejected(self):
        from fastapi import HTTPException

        raw = json.dumps({"action": "opened"}).encode()
        req = _FakeRequest(raw, headers={})

        settings = MagicMock()
        settings.gitea = types.SimpleNamespace(webhook_secret='s3cr3t')
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            with pytest.raises(HTTPException) as ei:
                await gitea_app.get_body(req)
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_does_not_rely_on_request_json(self):
        """The ordering fix means we must NOT depend on request.json() (which can
        fail once the stream is consumed). A request whose .json() raises but
        whose raw .body() is intact must still parse."""
        payload = {"action": "reopened"}
        req = _FakeRequest(json.dumps(payload).encode(), json_raises=True)

        settings = MagicMock()
        settings.gitea = types.SimpleNamespace()
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            body = await gitea_app.get_body(req)

        assert body == payload


class TestGiteaBotUserFilter:
    """The agent must never act on an event raised by its own bot account."""

    def test_is_self_comment_true_for_bot_sender(self):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {'GITEA.BOT_USER': 'pr-agent'}.get(k, d)
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            assert gitea_app.is_self_comment({'sender': {'login': 'PR-Agent'}}) is True

    def test_is_self_comment_false_for_other_sender(self):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {'GITEA.BOT_USER': 'pr-agent'}.get(k, d)
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings):
            assert gitea_app.is_self_comment({'sender': {'login': 'alice'}}) is False

    @pytest.mark.asyncio
    async def test_handle_request_ignores_bot_self_event(self):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {'GITEA.BOT_USER': 'pr-agent'}.get(k, d)
        body = {'action': 'created', 'sender': {'login': 'pr-agent'}}

        with patch('pr_agent.servers.gitea_app.get_settings', return_value=settings), \
             patch('pr_agent.servers.gitea_app.PRAgent') as MockAgent, \
             patch('pr_agent.servers.gitea_app.handle_comment_event', new=AsyncMock()) as mock_comment:
            await gitea_app.handle_request(body, event='issue_comment')

        # The bot's own comment must not spin up an agent or a comment handler.
        mock_comment.assert_not_called()


class TestGiteaReviewRequested:
    """review_requested must trigger /review (pr_commands) when the bot is the
    requested reviewer (item (f) review_requested branch)."""

    def _settings(self):
        settings = MagicMock()
        settings.get.side_effect = lambda k, d=None: {'GITEA.BOT_USER': 'pr-agent'}.get(k, d)
        return settings

    def test_review_requested_for_bot_singular_field(self):
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=self._settings()):
            assert gitea_app.review_requested_for_bot(
                {'requested_reviewer': {'login': 'PR-Agent'}}
            ) is True

    def test_review_requested_for_bot_reviewers_list(self):
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=self._settings()):
            assert gitea_app.review_requested_for_bot(
                {'pull_request': {'requested_reviewers': [{'login': 'someone'}, {'login': 'pr-agent'}]}}
            ) is True

    def test_review_requested_for_other_reviewer_is_false(self):
        with patch('pr_agent.servers.gitea_app.get_settings', return_value=self._settings()):
            assert gitea_app.review_requested_for_bot(
                {'requested_reviewer': {'login': 'human-reviewer'}}
            ) is False

    @pytest.mark.asyncio
    async def test_handle_review_requested_runs_pr_commands(self):
        body = {
            'action': 'review_requested',
            'sender': {'login': 'alice'},
            'requested_reviewer': {'login': 'pr-agent'},
            'pull_request': {'url': 'https://gitea.example.com/api/v1/repos/o/r/pulls/1'},
        }
        agent = MagicMock()

        with patch('pr_agent.servers.gitea_app.get_settings', return_value=self._settings()), \
             patch('pr_agent.servers.gitea_app.should_process_pr_logic', return_value=True), \
             patch('pr_agent.servers.gitea_app._perform_commands_gitea', new=AsyncMock()) as mock_perform:
            await gitea_app.handle_review_requested_event(body, 'pull_request', 'review_requested', agent)

        mock_perform.assert_awaited_once()
        args, _ = mock_perform.call_args
        assert args[0] == 'pr_commands'
        assert args[3] == 'https://gitea.example.com/api/v1/repos/o/r/pulls/1'

    @pytest.mark.asyncio
    async def test_handle_review_requested_skips_when_not_for_bot(self):
        body = {
            'action': 'review_requested',
            'requested_reviewer': {'login': 'human-reviewer'},
            'pull_request': {'url': 'https://gitea.example.com/api/v1/repos/o/r/pulls/1'},
        }
        agent = MagicMock()

        with patch('pr_agent.servers.gitea_app.get_settings', return_value=self._settings()), \
             patch('pr_agent.servers.gitea_app._perform_commands_gitea', new=AsyncMock()) as mock_perform:
            await gitea_app.handle_review_requested_event(body, 'pull_request', 'review_requested', agent)

        mock_perform.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_request_routes_review_requested(self):
        """The pull_request/review_requested action must route to the new
        handler (upstream let it fall through as a no-op)."""
        body = {
            'action': 'review_requested',
            'sender': {'login': 'alice'},
            'requested_reviewer': {'login': 'pr-agent'},
            'pull_request': {'url': 'https://gitea.example.com/api/v1/repos/o/r/pulls/1'},
        }

        with patch('pr_agent.servers.gitea_app.get_settings', return_value=self._settings()), \
             patch('pr_agent.servers.gitea_app.PRAgent'), \
             patch('pr_agent.servers.gitea_app.should_process_pr_logic', return_value=True), \
             patch('pr_agent.servers.gitea_app.handle_review_requested_event', new=AsyncMock()) as mock_handler:
            await gitea_app.handle_request(body, event='pull_request')

        mock_handler.assert_awaited_once()
