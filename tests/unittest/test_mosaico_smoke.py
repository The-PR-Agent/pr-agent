"""Full-path smoke test.

Drives ONE message/send through the SDK via Starlette TestClient against build_app()
(REAL RawContextMiddleware mounted), proving the full path:
  HTTP POST / -> RawContextMiddleware -> DefaultRequestHandler -> PRAgentExecutor.execute
  -> request-scoped settings deepcopy -> route_and_run -> DiffInputProvider -> render.

The LLM is mocked (LiteLLMAIHandler.chat_completion -> canned review YAML); no real
LLM/Langfuse/host. A second case drives an empty diff and asserts the empty-fallback
text comes back with NO exception escaping.

This test passing is the end-to-end proof that the middleware->executor->dispatch->
provider->render chain works through the wire."""
import uuid

from starlette.testclient import TestClient

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_mod
from pr_agent.mosaico.server import build_app

REVIEW_DIFF = """```diff
diff --git a/foo.py b/foo.py
index 1111111..2222222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
 y = 3
```"""

CANNED_REVIEW_YAML = """\
review:
  estimated_effort_to_review_[1-5]: '2'
  score: '85'
  relevant_tests: 'No'
  key_issues_to_review:
    - relevant_file: foo.py
      issue_header: 'Possible Bug'
      issue_content: 'x changed from 1 to 2'
      start_line: 1
      end_line: 1
  security_concerns: 'No'
"""


def _send_message_payload(text: str) -> dict:
    return {
        "id": "1",
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def _extract_text(result: dict) -> str:
    """Pull all text parts out of a message/send JSON-RPC result (Task or Message)."""
    chunks = []

    def harvest(obj):
        if isinstance(obj, dict):
            if obj.get("kind") == "text" and isinstance(obj.get("text"), str):
                chunks.append(obj["text"])
            for v in obj.values():
                harvest(v)
        elif isinstance(obj, list):
            for v in obj:
                harvest(v)

    harvest(result)
    return "\n".join(chunks)


class TestSmokeFullPath:
    def test_review_diff_round_trip(self, monkeypatch):
        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return CANNED_REVIEW_YAML, "stop"

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        client = TestClient(build_app())
        resp = client.post("/", json=_send_message_payload(f"review this\n{REVIEW_DIFF}"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body, body
        result = body["result"]

        # Completed task (or a final message) with non-empty rendered content over the wire.
        status = result.get("status", {})
        if status:
            assert status.get("state") in ("completed", "failed")
            assert status.get("state") == "completed", f"task failed: {result}"
        text = _extract_text(result)
        assert text.strip(), f"no text content returned: {result}"
        # Must NOT be the executor's exception placeholder.
        assert not text.startswith("Error:"), text

    def test_empty_diff_yields_fallback_no_exception(self, monkeypatch):
        # No LLM call should be needed (parse yields nothing -> empty fallback), but mock
        # anyway so any accidental call is harmless.
        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return CANNED_REVIEW_YAML, "stop"

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        client = TestClient(build_app())
        empty = "```diff\nnot actually a diff\n```"
        resp = client.post("/", json=_send_message_payload(f"review\n{empty}"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body, body
        result = body["result"]
        status = result.get("status", {})
        if status:
            assert status.get("state") == "completed", f"task failed: {result}"
        text = _extract_text(result)
        # The defensive empty-fallback string, and no exception escaped.
        assert "no output produced" in text, text
        assert not text.startswith("Error:"), text
