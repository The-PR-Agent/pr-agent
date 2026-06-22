"""End-to-end A2A 1.0 round-trip through the real MOSAICO server stack.

Drives an HTTP POST through the whole server (Starlette + DefaultRequestHandler +
executor + RawContextMiddleware + route_and_run + real PRReviewer with publish_output
forced False -> A2A 1.0 response). Tier 0b: load-bearing interop proof.

Only the LLM is stubbed (LiteLLMAIHandler.chat_completion -> canned review YAML);
everything above it runs real and unmocked.

1.0 WIRE CONTRACT:
- method: "SendMessage" (not "message/send")
- header: A2A-Version: 1.0
- params: built from SDK types (SendMessageRequest.MessageToDict) — never hand-rolled
- result shape: {"task": {"status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": [...]}}
- review text is in artifacts[0].parts[0].text (RISK 2: reference agent reads artifacts)
- bad URL → state "TASK_STATE_FAILED" (Fix C)

Non-vacuity: reverting Fix C (making ok=False route to complete()) would flip the
bad-URL assertion to TASK_STATE_COMPLETED — making test_bad_url_roundtrip fail.

Note: import pr_agent.config_loader first to avoid the pr_agent.log <->
custom_merge_loader circular import (mirrors server.py)."""
import os

import pr_agent.config_loader  # noqa: F401  (import-order load; see module docstring)

import httpx
import pytest
from google.protobuf.json_format import MessageToDict
from httpx import ASGITransport

from a2a.types import Message, Part, Role, SendMessageRequest

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

# A URL that is unreachable / SSRF-blocked -> routes to failed (Fix C).
_BAD_URL = "https://github.com/nonexistent-org-xyz/nonexistent-repo-xyz/pull/99999"

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

# A2A 1.0 request header — required so the server does not fall back to 0.3 mode.
_A2A_HEADERS = {"A2A-Version": "1.0"}


def _message_send_body(text: str) -> dict:
    """Build a genuine A2A 1.0 JSON-RPC message/send body from the SDK's own types.

    Using MessageToDict(SendMessageRequest(...)) ensures the payload shape is identical
    to what the reference agent sends — never hand-rolled."""
    msg = Message(
        message_id="rt-msg-1",
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
    )
    req = SendMessageRequest(message=msg)
    return {
        "id": "rt-1",
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": MessageToDict(req),
    }


def _extract_artifact_text(result: dict) -> str:
    """Extract the agent's review text from A2A 1.0 task artifacts.

    A2A 1.0 result shape: {"task": {"artifacts": [{"parts": [{"text": "..."}]}]}}
    The reference agent's pollTask reads task.artifacts (RISK 2); this mirrors that."""
    task = result.get("task", result) if isinstance(result, dict) else {}
    artifacts = task.get("artifacts", [])
    texts = []
    for art in artifacts:
        for part in art.get("parts", []):
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "\n".join(texts)


def _get_task_state(result: dict) -> str:
    """Extract the task state from an A2A 1.0 result dict."""
    task = result.get("task", result) if isinstance(result, dict) else {}
    return task.get("status", {}).get("state", "")


def _build_client(app):
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers=_A2A_HEADERS,
    )


def _live_llm_creds_absent() -> bool:
    """True when the MOSAICO LLM packaging creds (API_BASE + API_KEY) are NOT both set."""
    return not (os.getenv("API_BASE") and os.getenv("API_KEY"))


class TestA2ARoundTripStubbedLLM:
    @pytest.mark.asyncio
    async def test_warmup_health_and_card(self, monkeypatch):
        """Warm-up: /health and the agent card respond over the same transport."""
        import litellm

        # health_check() issues a direct, non-retry litellm.acompletion probe (NOT
        # LiteLLMAIHandler.chat_completion), so stub acompletion here to keep /health
        # healthy offline — no LLM/network in CI.
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

            # A2A 1.0: supported_interfaces replaces top-level url.
            assert "supportedInterfaces" in body, f"A2A 1.0 card missing supportedInterfaces: {body}"
            assert "url" not in body, f"A2A 1.0 card must not have top-level url: {body}"

            # Observability extension still advertised required over the wire.
            exts = body["capabilities"]["extensions"]
            obs_uri = "https://mosaico-project.eu/extensions/mosaico-observability"
            assert any(
                e["uri"] == obs_uri and e["required"] is True for e in exts
            )

    @pytest.mark.asyncio
    async def test_supplied_diff_completed_with_artifact(self, monkeypatch):
        """RISK 2 proof: a supplied-diff /review POST returns the real review in artifacts.

        The reference agent's pollTask reads task.artifacts — not the completion message.
        This test asserts:
          1. state == TASK_STATE_COMPLETED
          2. artifacts contains our canned-review marker text
        A revert of the add_artifact() call would break assertion 2 (non-vacuity)."""
        import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod

        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return _CANNED_REVIEW_YAML, "stop"

        # Stub ONLY the LLM. Everything above it runs real.
        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        from pr_agent.mosaico.server import build_app
        app = build_app()
        async with _build_client(app) as client:
            resp = await client.post("/", json=_message_send_body(_DIFF_TEXT))

        assert resp.status_code == 200
        payload = resp.json()
        assert "error" not in payload, f"JSON-RPC error returned: {payload.get('error')}"
        assert "result" in payload, f"no result in response: {payload}"

        state = _get_task_state(payload["result"])
        assert state == "TASK_STATE_COMPLETED", f"task not completed: {payload['result']}"

        # RISK 2 assertion: review text must be in artifacts, not just status.
        text = _extract_artifact_text(payload["result"])
        assert text, "agent returned empty artifacts — review not retrievable by reference agent"
        assert _MARKER in text, f"canned review marker missing from artifacts: {text[:300]!r}"
        assert "(no output produced)" not in text
        assert not text.startswith("Error:")
        # Confirms PRReviewer actually rendered (not a raw YAML passthrough).
        assert "PR Reviewer Guide" in text

    @pytest.mark.asyncio
    async def test_bad_url_yields_failed(self, monkeypatch):
        """Fix C proof: an unfetchable/SSRF-blocked PR URL must yield TASK_STATE_FAILED.

        Non-vacuity: reverting Fix C (completing instead of failing on ok=False) would
        change this result to TASK_STATE_COMPLETED, flipping this test red."""
        import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod

        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return _CANNED_REVIEW_YAML, "stop"

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        from pr_agent.mosaico.server import build_app
        app = build_app()
        async with _build_client(app) as client:
            resp = await client.post("/", json=_message_send_body(f"review {_BAD_URL}"))

        assert resp.status_code == 200
        payload = resp.json()
        assert "error" not in payload, f"unexpected JSON-RPC error: {payload.get('error')}"
        assert "result" in payload, f"no result in response: {payload}"

        state = _get_task_state(payload["result"])
        assert state == "TASK_STATE_FAILED", (
            f"bad URL must yield TASK_STATE_FAILED (Fix C), got: {state}"
        )


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
        state = _get_task_state(payload["result"])
        assert state == "TASK_STATE_COMPLETED", f"live test expected completed: {payload['result']}"
        text = _extract_artifact_text(payload["result"])
        assert text, "live agent returned empty artifacts"
        assert "(no output produced)" not in text
        assert not text.startswith("Error:")
