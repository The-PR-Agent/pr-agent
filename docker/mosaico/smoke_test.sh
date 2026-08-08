#!/usr/bin/env bash
# Pull and test-run the MOSAICO A2A solution-agent image published by the
# The-PR-Agent/pr-agent release workflow (Docker target `mosaico_agent`).
#
#   Smoke (always):       boot the container and validate the A2A agent card.
#   Full round-trip:      when LLM creds are present, also exercise GET /health
#                         (live LLM probe) and an A2A SendMessage PR-review on
#                         an inline diff.
#
# LLM creds are read from a .env file beside this script (gitignored), NOT the
# environment — they must survive across separate shell invocations
# (e.g. an unattended poll loop). Copy pr-agent.env.example to .env and fill it in.
# Format: plain KEY=VALUE lines, e.g.
#   API_BASE=https://openrouter.ai/api/v1
#   API_KEY=sk-...
#   MODEL_NAME=openrouter/mistralai/devstral-small
set -uo pipefail

IMAGE="${IMAGE:-pragent/pr-agent:0.41.0-mosaico_agent}"
PORT="${PORT:-9000}"
CONTAINER="pr-agent-mosaico-test"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"
BASE="http://localhost:${PORT}"

# 0700 by construction, so the /health body (which embeds the raw provider exception
# when unhealthy) is neither world-readable nor writable at a predictable path.
TMPDIR_RUN="$(mktemp -d)" || { echo "FAIL: mktemp -d failed" >&2; exit 1; }
# Only tear down a container this run actually started: the name is fixed, so an early
# exit (a failed pull, or a `docker run` that lost a name race) must not reap someone
# else's container - including the one a concurrent run is still testing against.
started=0
cleanup() {
  if [[ $started == 1 ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMPDIR_RUN"
}
trap cleanup EXIT

# Bound every request: the header documents unattended poll-loop use, where a stalled
# container would otherwise hang the loop forever. Limits are generous because /health
# and SendMessage both wait on a live LLM.
CURL_CONNECT=(--connect-timeout 5)

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "==> Pulling $IMAGE"
docker pull "$IMAGE" || fail "docker pull failed"

# AGENT_CARD_* must match the published host port, otherwise the card advertises
# http://localhost:9000/ regardless of -p and the URL assertion below would be
# meaningless. This is the misconfiguration the README calls out as the easiest to get
# wrong, so the smoke test exercises it rather than sidestepping it.
run_args=(-d --rm --name "$CONTAINER" -p "${PORT}:9000"
          -e AGENT_CARD_HOST=localhost -e "AGENT_CARD_PORT=${PORT}")
have_creds=0
if [[ -f "$ENV_FILE" ]]; then
  run_args+=(--env-file "$ENV_FILE")
  # Full round-trip requires all three LLM keys to be present and non-empty.
  if grep -qE '^API_BASE=.+' "$ENV_FILE" \
     && grep -qE '^API_KEY=.+' "$ENV_FILE" \
     && grep -qE '^MODEL_NAME=.+' "$ENV_FILE"; then
    have_creds=1
  fi
fi

mode=$([[ $have_creds == 1 ]] && echo "full round-trip" || echo "smoke only")
echo "==> Starting container ($mode)"
docker run "${run_args[@]}" "$IMAGE" || fail "docker run failed"
started=1

# --- SMOKE: fetch the card (with retry; needs no LLM) and validate it ---
echo "==> [smoke] fetching + validating agent card"
CARD=""
for _ in $(seq 1 30); do
  CARD=$(curl -fsSL "${CURL_CONNECT[@]}" --max-time 30 "$BASE/.well-known/agent-card.json" 2>/dev/null)
  [[ -n "$CARD" ]] && break
  sleep 2
done
[[ -n "$CARD" ]] || { docker logs "$CONTAINER" 2>&1 | tail -40; fail "card endpoint never served a body"; }

CARD="$CARD" EXPECT_URL="$BASE/" python3 - <<'PY' || fail "agent card invalid"
import json, os
c = json.loads(os.environ["CARD"])
assert c["name"] == "PR-Agent Solution Agent", c.get("name")
assert c["capabilities"]["streaming"] is False, "streaming must be False"
exts = c["capabilities"]["extensions"]
assert any(e.get("required") and "observability" in e["uri"] for e in exts), "observability ext missing/required"
ids = sorted(s["id"] for s in c["skills"])
assert ids == ["ask", "describe", "improve", "review"], ids
# The advertised URL is what MOSAICO stores and what clients dereference; if it does
# not match the address we actually reached, the deployment is wrong.
url = c["supportedInterfaces"][0]["url"]
assert url == os.environ["EXPECT_URL"], f'advertised {url!r}, expected {os.environ["EXPECT_URL"]!r}'
print("    card OK: name, streaming=False, observability required, skills", ids)
print("    advertised URL OK:", url)
PY

if [[ $have_creds == 0 ]]; then
  echo "==> SMOKE PASSED (no LLM creds in $ENV_FILE -> skipped /health + SendMessage)."
  echo "    Add API_BASE/API_KEY/MODEL_NAME to $ENV_FILE for the full round-trip."
  exit 0
fi

# --- FULL: /health (live LLM ping -> 200/503) ---
echo "==> [full] GET /health (live LLM probe)"
code=$(curl -s "${CURL_CONNECT[@]}" --max-time 120 -o "$TMPDIR_RUN/health.json" -w '%{http_code}' "$BASE/health")
# On 503 the body is a raw provider exception (it can name the endpoint), which is
# exactly the diagnostic you want here - just don't paste it into a public issue.
cat "$TMPDIR_RUN/health.json"; echo
[[ "$code" == "200" ]] || fail "/health returned $code (expected 200) — check LLM creds"

# --- FULL: SendMessage review on an inline diff (no PR URL / GitHub token needed) ---
# A2A 1.0 wire contract: the `A2A-Version: 1.0` header is REQUIRED (the server treats a
# missing header as 0.3 and rejects the call), the method is the gRPC-style `SendMessage`,
# and Message/Part carry no `kind` discriminator.
echo "==> [full] A2A SendMessage review (inline diff)"
read -r -d '' BODY <<'JSON'
{"id":"smoke-1","jsonrpc":"2.0","method":"SendMessage","params":{"message":{"messageId":"smoke-msg-1","role":"ROLE_USER","parts":[{"text":"review the following\n```diff\ndiff --git a/foo.py b/foo.py\nindex 1111111..2222222 100644\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 2\n y = 3\n```"}]}}}
JSON

curl -fsS "${CURL_CONNECT[@]}" --max-time 300 \
  -X POST "$BASE/" -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d "$BODY" -o "$TMPDIR_RUN/resp.json" || fail "SendMessage request failed"
RESP="$TMPDIR_RUN/resp.json" python3 - <<'PY' || fail "SendMessage response invalid"
import json, os
d = json.load(open(os.environ["RESP"]))
if "error" in d:
    raise SystemExit(f"    JSON-RPC error: {json.dumps(d['error'])[:300]}")
# A2A 1.0 wraps the Task in result.task, and delivers the review as an artifact.
# status.message is populated only on failure, so asserting on it would invert the test.
task = d.get("result", d).get("task", {})
state = (task.get("status") or {}).get("state", "")
parts = [p for a in (task.get("artifacts") or []) for p in (a.get("parts") or [])]
text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
assert state == "TASK_STATE_COMPLETED", f"task state {state!r}: {text[:300]}"
assert text.strip(), "empty agent reply"
assert "no output produced" not in text, "publish_output capture regression"
print(f"    review returned {len(text)} chars; first line:")
print("    " + (text.strip().splitlines() or [""])[0][:100])
PY

echo "==> FULL ROUND-TRIP PASSED."
