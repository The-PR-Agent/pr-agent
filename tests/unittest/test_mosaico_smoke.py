"""Full-path smoke test (A2A 1.0).

Drive message/send requests through the SDK via an in-process ASGI HTTP transport against
build_app() (REAL RawContextMiddleware mounted), proving the full path:
  HTTP POST / -> RawContextMiddleware -> DefaultRequestHandler -> PRAgentExecutor.execute
  -> request-scoped settings deepcopy -> route_and_run -> DiffInputProvider -> render.

The LLM is mocked (LiteLLMAIHandler.chat_completion -> canned review YAML); no real
LLM/Langfuse/host. A second case drives an empty diff and asserts the empty-fallback
text comes back with NO exception escaping.

This test passing is the end-to-end proof that the middleware->executor->dispatch->
provider->render chain works through the wire."""
import uuid

import httpx
import pytest
from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict

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
    """Build a valid A2A 1.0 message/send JSON-RPC body from SDK types."""
    msg = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
    )
    req = SendMessageRequest(message=msg)
    return {
        "id": "1",
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": MessageToDict(req),
    }


# A2A 1.0 requires the version header so the handler does not fall back to 0.3.
_A2A_HEADERS = {"A2A-Version": "1.0"}


def _extract_text(result: dict) -> str:
    """Pull all text parts out of a message/send JSON-RPC result.

    In A2A 1.0 the result is wrapped: {"task": {...}} with artifacts.
    Harvest all text values recursively from the result envelope."""
    chunks = []

    def harvest(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get("text"), str):
                chunks.append(obj["text"])
            for v in obj.values():
                harvest(v)
        elif isinstance(obj, list):
            for v in obj:
                harvest(v)

    harvest(result)
    return "\n".join(chunks)


async def _post_message(text: str):
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/", json=_send_message_payload(text), headers=_A2A_HEADERS)


class TestSmokeFullPath:
    @pytest.mark.asyncio
    async def test_review_diff_round_trip(self, monkeypatch):
        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return CANNED_REVIEW_YAML, "stop"

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        resp = await _post_message(f"review this\n{REVIEW_DIFF}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body, body
        result = body["result"]

        # A2A 1.0: result is {"task": {...}}
        task = result.get("task", result)
        status = task.get("status", {})
        state = status.get("state", "")
        assert state == "TASK_STATE_COMPLETED", f"task not completed: {task}"

        # Review text should be in artifacts.
        artifacts = task.get("artifacts", [])
        assert artifacts, f"no artifacts in completed task: {task}"
        text = _extract_text({"artifacts": artifacts})
        assert text.strip(), f"no text content in artifacts: {task}"
        # Must NOT be the executor's exception placeholder.
        assert not text.startswith("Error:"), text

    @pytest.mark.asyncio
    async def test_empty_diff_yields_fallback_no_exception(self, monkeypatch):
        # No LLM call should be needed (parse yields nothing -> empty fallback), but mock
        # anyway so any accidental call is harmless.
        async def fake_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            return CANNED_REVIEW_YAML, "stop"

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", fake_chat_completion)

        empty = "```diff\nnot actually a diff\n```"
        resp = await _post_message(f"review\n{empty}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body, body
        result = body["result"]

        # A2A 1.0: result is {"task": {...}}
        task = result.get("task", result)
        status = task.get("status", {})
        state = status.get("state", "")
        assert state == "TASK_STATE_COMPLETED", f"task not completed: {task}"

        # The defensive empty-fallback text is in the artifact.
        text = _extract_text(result)
        assert "no output produced" in text, text
        assert not text.startswith("Error:"), text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verb", ["review", "improve", "describe", "ask"])
    async def test_llm_outage_yields_failed_not_completed(self, monkeypatch, verb):
        """A total model outage must reach the caller as TASK_STATE_FAILED. review/improve/
        describe used to swallow it and report success with 'no output produced'; 'ask' was
        already correct and is pinned here so the fix does not regress it."""
        async def failing_chat_completion(self, model, system, user, temperature=0.2, **kwargs):
            raise RuntimeError("provider unavailable")

        monkeypatch.setattr(litellm_mod.LiteLLMAIHandler, "chat_completion", failing_chat_completion)

        resp = await _post_message(f"{verb} this\n{REVIEW_DIFF}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body, body
        result = body["result"]

        task = result.get("task", result)
        state = task.get("status", {}).get("state", "")
        assert state == "TASK_STATE_FAILED", (
            f"{verb}: an LLM outage must fail the task, got {state}: {task}"
        )
        text = _extract_text(result)
        assert "no output produced" not in text, (
            f"{verb}: outage must not be reported as an empty result: {text}"
        )
