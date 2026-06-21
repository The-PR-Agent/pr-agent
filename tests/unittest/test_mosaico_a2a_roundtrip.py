"""End-to-end A2A round-trip through the real MOSAICO server stack.

Drives an HTTP POST through the whole server (ASGI -> A2AStarletteApplication ->
executor -> RawContextMiddleware -> route_and_run -> real PRReviewer with
publish_output forced False -> A2A response). Regression proof that the
publish_output fix returns real content instead of "(no output produced)".

Only the LLM is stubbed (LiteLLMAIHandler.chat_completion -> canned review YAML);
everything above it runs real and unmocked.

Note: import pr_agent.config_loader first to avoid the pr_agent.log <->
custom_merge_loader circular import (mirrors server.py)."""
import os

import pr_agent.config_loader  # noqa: F401  (import-order load; see module docstring)

import httpx
import pytest
from httpx import ASGITransport

# A small, valid unified diff wrapped in a ```diff fence -> the supplied-diff (path b)
# of the router: no PR URL, no network, parsed by the mosaico_diff provider.
_DIFF_TEXT = (
    "review the following\n"
    "```diff\n"
    "diff --git a/foo.py b/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-x = 1\n"
    "+x = 2\n"
    " y = 3\n"
    "```"
)

_MARKER = "MARKER_CANNED_REVIEW_E2E"

# Schema-valid /review prediction. The issue_header carries our marker so we can prove
# the canned content survives all the way through PRReviewer's rendering into the artifact.
_CANNED_REVIEW_YAML = f"""\
review:
  estimated_effort_to_review_[1-5]: '2'
  score: '85'
  relevant_tests: 'No'
  key_issues_to_review:
    - relevant_file: foo.py
      issue_header: '{_MARKER}'
      issue_content: 'x changed from 1 to 2'
      start_line: 1
      end_line: 1
  security_concerns: 'No'
"""


def _message_send_body(text: str) -> dict:
    """A genuine A2A JSON-RPC message/send body (shape verified against the installed
    a2a SDK: SendMessageRequest.model_dump)."""
    return {
        "id": "rt-1",
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": "rt-msg-1",
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def _extract_text(result: dict) -> str:
    """Pull the agent's text out of an A2A message/send result. The executor completes a
    Task, so the agent reply lands in result.status.message.parts[*].text; tolerate the
    plain-Message shape too."""
    if not isinstance(result, dict):
        return ""
    msg = result.get("status", {}).get("message") if "status" in result else result
    parts = (msg or {}).get("parts", []) if isinstance(msg, dict) else []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _build_client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _live_llm_creds_absent() -> bool:
    """True when the MOSAICO LLM packaging creds (API_BASE + API_KEY) are NOT both set.
    Boolean presence only — never reads or echoes the values."""
    return not (os.getenv("API_BASE") and os.getenv("API_KEY"))


class TestA2ARoundTripStubbedLLM:
    @pytest.mark.asyncio
    async def test_warmup_health_and_card(self, monkeypatch):
        """Warm-up: /health and the agent card respond over the same transport."""
        import litellm

        # health_check() issues a direct, non-retry litellm.acompletion probe (NOT
        # LiteLLMAIHandler.chat_completion), so stub acompletion here to keep /health
        # healthy offline — no LLM/network in CI. health_check ignores the response body.
        async def fake_acompletion(**kwargs):
            return {"choices": [{"message": {"content": "ping"}}]}
        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

        from pr_agent.mosaico.server import build_app
        app = build_app()
        async with _build_client(app) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["is_healthy"] is True

            card = await client.get("/.well-known/agent-card.json")
            assert card.status_code == 200
            body = card.json()
            assert body["name"] == "PR-Agent Solution Agent"
            # observability extension still advertised required over the wire
            exts = body["capabilities"]["extensions"]
            obs_uri = "https://mosaico-project.eu/extensions/mosaico-observability"
            assert any(
                e["uri"] == obs_uri and e["required"] is True for e in exts
            )

    @pytest.mark.asyncio
    async def test_supplied_diff_review_roundtrip(self, monkeypatch):
        """The end-to-end proof of the publish_output fix: a supplied-diff /review POST
        returns the real rendered review (our marker) over the wire — NOT the old
        "(no output produced)" and NOT an "Error:" message."""
        import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod

        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return _CANNED_REVIEW_YAML, "stop"

        # Stub ONLY the LLM. route_and_run / executor / middleware / publish_output stay real.
        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        from pr_agent.mosaico.server import build_app
        app = build_app()
        async with _build_client(app) as client:
            resp = await client.post("/", json=_message_send_body(_DIFF_TEXT))

        assert resp.status_code == 200
        payload = resp.json()
        assert "error" not in payload, f"JSON-RPC error returned: {payload.get('error')}"
        assert "result" in payload, f"no result in response: {payload}"

        text = _extract_text(payload["result"])
        assert text, "agent returned an empty text part"
        # The publish_output fix proof: real content, marker present, no old failure strings.
        assert _MARKER in text, f"canned review marker missing from response: {text[:300]!r}"
        assert "(no output produced)" not in text
        assert not text.startswith("Error:")
        # Confirms PRReviewer actually rendered (not a raw YAML passthrough).
        assert "PR Reviewer Guide" in text


class TestA2ARoundTripLiveLLM:
    @pytest.mark.skipif(
        _live_llm_creds_absent(),
        reason="live LLM creds (API_BASE + API_KEY) absent; deterministic stubbed test covers the wiring",
    )
    @pytest.mark.asyncio
    async def test_supplied_diff_review_roundtrip_live(self):
        """Same round-trip but against the REAL configured LLM (no stub). Auto-skips when
        the MOSAICO packaging creds are absent. Asserts only that real, non-empty,
        non-error content comes back — content is model-dependent so no marker check."""
        from pr_agent.mosaico.server import build_app
        app = build_app()
        async with _build_client(app) as client:
            resp = await client.post("/", json=_message_send_body(_DIFF_TEXT))

        assert resp.status_code == 200
        payload = resp.json()
        assert "result" in payload, f"no result in response: {payload}"
        text = _extract_text(payload["result"])
        assert text, "live agent returned an empty text part"
        assert "(no output produced)" not in text
        assert not text.startswith("Error:")
