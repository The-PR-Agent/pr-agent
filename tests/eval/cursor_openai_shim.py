#!/usr/bin/env python3
"""Minimal OpenAI-compatible /v1/chat/completions endpoint backed by the Cursor CLI.

The eval harness needs a live model and the Google AI Studio free tier is spent
(tests/eval/BASELINE.md). Cursor's CLI is an agent, not a completions API, so this shim
adapts it: system+user are concatenated and piped to `cursor-agent -p` on stdin, and the
agent's text result is returned as an assistant message.

Two things make the result usable as eval data rather than an agent transcript:

* `--mode ask --sandbox enabled` blocks shell and network, so the agent cannot fetch the
  PR under review (samer2373/block_rush is not checked out on this machine either). It can
  still read local files; nothing it can reach contains the corpus.
* The agent narrates before answering ("Running both commands now."). The YAML the review
  prompt asks for is extracted from a fenced block when there is one, so the parser sees
  the same shape it would get from a chat model. Content is never edited.

Prompt delivery is via stdin: argv cannot hold 800KB and stdin was verified to carry a
313k-token prompt intact on gemini-3.7-flash-high (a 1M-context model - a smaller-context
model silently truncates, so do not point this at one).

Usage: CURSOR_API_KEY=... python3 cursor_openai_shim.py --port 8899 --model gemini-3.7-flash-high
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None
EMPTY_WORKSPACE = None
LOG_LOCK = threading.Lock()

FENCE = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)```", re.DOTALL)


def _extract_payload(text: str) -> str:
    """Return the fenced block if the agent wrapped its answer in one, else the raw text."""
    blocks = FENCE.findall(text)
    if not blocks:
        return text
    # The review prompt asks for one document; if the agent emitted several blocks the
    # longest is the answer and the rest are narration.
    return max(blocks, key=len).strip()


def _log(record: dict) -> None:
    if not ARGS.log:
        return
    with LOG_LOCK:
        with open(ARGS.log, "a") as fh:
            fh.write(json.dumps(record) + "\n")


def _call_cursor(prompt: str) -> tuple[str, dict]:
    cmd = [
        ARGS.cursor_bin, "-p",
        "--output-format", "json",
        "--mode", "ask",
        "--sandbox", "enabled",
        "--model", ARGS.model,
        "--workspace", EMPTY_WORKSPACE,
        "--trust",
    ]
    started = time.time()
    # The raw model text is the only evidence of a parse-side problem: a scored row that comes
    # back empty cannot be told apart from a model that found nothing without it.
    proc = subprocess.run(cmd, input=prompt.encode(), capture_output=True, timeout=ARGS.timeout)
    raw = proc.stdout.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"cursor-agent exited {proc.returncode}: {proc.stderr.decode()[:600]}")
    # stream-json is off, so the last JSON object on stdout is the result envelope.
    envelope = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
    if envelope is None:
        raise RuntimeError(f"no JSON envelope in cursor-agent output: {raw[:600]}")
    if envelope.get("is_error"):
        raise RuntimeError(f"cursor-agent reported an error: {raw[:600]}")
    usage = envelope.get("usage", {}) or {}
    if ARGS.raw_dir:
        os.makedirs(ARGS.raw_dir, exist_ok=True)
        stem = f"{int(started)}-{envelope.get('session_id', 'nosession')}"
        with open(os.path.join(ARGS.raw_dir, f"{stem}.prompt.txt"), "w") as fh:
            fh.write(prompt)
        with open(os.path.join(ARGS.raw_dir, f"{stem}.result.txt"), "w") as fh:
            fh.write(envelope.get("result", "") or "")
    _log({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": ARGS.model,
        "prompt_chars": len(prompt),
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "wall_s": round(time.time() - started, 1),
        "session_id": envelope.get("session_id"),
    })
    return envelope.get("result", "") or "", usage


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # keep the harness output readable
        pass

    def _send(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": ARGS.model, "object": "model"}]})
        else:
            self._send(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"no route {self.path}"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"error": {"message": f"bad JSON: {exc}"}})
            return
        parts = []
        for message in body.get("messages", []):
            content = message.get("content")
            if isinstance(content, list):  # multimodal shape; keep the text parts
                content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            if content:
                parts.append(str(content))
        prompt = "\n\n".join(parts)
        if not prompt.strip():
            self._send(400, {"error": {"message": "empty prompt"}})
            return
        try:
            text, usage = _call_cursor(prompt)
        except Exception as exc:  # surface as a 502 so retry_with_fallback_models sees a failure
            _log({"error": str(exc)[:800], "prompt_chars": len(prompt)})
            self._send(502, {"error": {"message": str(exc)[:800], "type": "cursor_cli_error"}})
            return
        self._send(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or ARGS.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": _extract_payload(text)},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": int(usage.get("inputTokens") or 0),
                "completion_tokens": int(usage.get("outputTokens") or 0),
                "total_tokens": int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0),
            },
        })


def main() -> int:
    global ARGS, EMPTY_WORKSPACE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--model", default="gemini-3.7-flash-high",
                        help="Cursor model id; must have a 1M context or long prompts truncate")
    parser.add_argument("--cursor-bin", default="cursor-agent")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--log", default="")
    parser.add_argument("--raw-dir", default="", help="dump each prompt and raw result here")
    ARGS = parser.parse_args()
    if not os.environ.get("CURSOR_API_KEY"):
        print("CURSOR_API_KEY is not set", file=sys.stderr)
        return 2
    EMPTY_WORKSPACE = tempfile.mkdtemp(prefix="cursor-shim-empty-")
    server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
    print(f"cursor shim on http://127.0.0.1:{ARGS.port}/v1 model={ARGS.model} workspace={EMPTY_WORKSPACE}",
          flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
