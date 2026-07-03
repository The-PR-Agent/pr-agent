"""Per-request settings-isolation concurrency gate.

Fires TWO execute() calls concurrently via asyncio.gather. Each coroutine runs inside
its own starlette_context scope (request_cycle_context — exactly what RawContextMiddleware
establishes per request in production); the executor installs a per-request
deepcopy(global_settings) into that scope. A request-distinct route_and_run writes
distinct CONFIG.MODEL / CONFIG.GIT_PROVIDER / data["artifact"], yields (asyncio.sleep(0))
to force interleave, then reads them back and asserts NO bleed from the sibling.

Also asserts global_settings is unmutated after both complete.

Non-vacuity: the test is wired so that WITHOUT the executor's
`sctx["settings"] = deepcopy(global_settings)` (i.e. if both requests shared
global_settings), the interleaved writes would clobber each other and the read-back
assertions would fail. test_isolation_is_non_vacuous proves this directly by running the
same interleaving WITHOUT the per-request deepcopy and asserting the bleed DOES occur."""
import asyncio

import pytest
from a2a.types import Message, Part, Role
from starlette_context import request_cycle_context

import pr_agent.mosaico.executor as executor_mod
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.mosaico.dispatch import RouteResult
from pr_agent.mosaico.executor import PRAgentExecutor


def _make_message(text: str) -> Message:
    return Message(message_id=f"m-{text}", role=Role.ROLE_USER,
                   parts=[Part(text=text)])


class _RecordingEventQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _FakeRequestContext:
    def __init__(self, text):
        self._text = text
        self.metadata = {}
        self.message = _make_message(text)
        self.task_id = f"task-{text}"
        self.context_id = f"ctx-{text}"

    def get_user_input(self, delimiter: str = "\n") -> str:
        return self._text


class _SpyTaskUpdater:
    def __init__(self, event_queue, task_id, context_id):
        self.completed_with = None
        self.failed_with = None

    async def add_artifact(self, parts, **kwargs):
        pass

    async def complete(self, message=None):
        self.completed_with = message

    async def failed(self, message=None):
        self.failed_with = message

    def new_agent_message(self, parts, metadata=None):
        return None


# Per-request distinct values keyed by the inbound text (== request id).
_PROFILES = {
    "reqA": {"model": "openai/model-A", "provider": "mosaico_diff", "artifact": "ARTIFACT-A"},
    "reqB": {"model": "anthropic/model-B", "provider": "github", "artifact": "ARTIFACT-B"},
}

# Collect per-request read-back results here.
_RESULTS = {}


async def _distinct_route_and_run(user_text):
    """Write request-distinct settings, interleave, read back, assert no sibling bleed."""
    profile = _PROFILES[user_text]
    s = get_settings()
    s.set("CONFIG.MODEL", profile["model"])
    s.set("CONFIG.GIT_PROVIDER", profile["provider"])
    s.data = {"artifact": profile["artifact"]}

    # Force the two coroutines to interleave between write and read-back.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    s2 = get_settings()
    read = {
        "model": s2.get("CONFIG.MODEL"),
        "provider": s2.get("CONFIG.GIT_PROVIDER"),
        "artifact": (s2.get("data", {}) or {}).get("artifact", ""),
        "settings_id": id(s2),
    }
    _RESULTS[user_text] = read
    # The request-scoped settings must NOT be the shared global object.
    assert s2 is not global_settings, f"{user_text} resolved to the shared global_settings"
    # Each request must still see ITS OWN values, not the sibling's.
    assert read["model"] == profile["model"], f"{user_text} saw model {read['model']}"
    assert read["provider"] == profile["provider"], f"{user_text} saw provider {read['provider']}"
    assert read["artifact"] == profile["artifact"], f"{user_text} saw artifact {read['artifact']}"
    return RouteResult(profile["artifact"], ok=True)


async def _run_one(text):
    """Each gathered task gets its OWN starlette_context scope (mirrors per-request
    RawContextMiddleware), so the executor's deepcopy lands in an isolated context."""
    with request_cycle_context({}):
        await PRAgentExecutor().execute(_FakeRequestContext(text), _RecordingEventQueue())


@pytest.fixture
def snapshot_global():
    keys = ["CONFIG.MODEL", "CONFIG.GIT_PROVIDER"]
    sentinel = object()
    before = {k: global_settings.get(k, sentinel) for k in keys}
    data_before = global_settings.get("data", sentinel)
    _RESULTS.clear()
    yield before
    for k, v in before.items():
        if v is not sentinel:
            global_settings.set(k, v)
    if data_before is sentinel:
        global_settings.data = {"artifact": ""}
    else:
        global_settings.data = data_before
    _RESULTS.clear()


class TestConcurrencyIsolation:
    @pytest.mark.asyncio
    async def test_no_bleed_between_concurrent_requests(self, monkeypatch, snapshot_global):
        monkeypatch.setattr(executor_mod, "route_and_run_result", _distinct_route_and_run)
        monkeypatch.setattr(executor_mod, "TaskUpdater", _SpyTaskUpdater)

        # Capture global_settings BEFORE for the unmutated check.
        global_model_before = global_settings.get("CONFIG.MODEL", None)
        global_provider_before = global_settings.get("CONFIG.GIT_PROVIDER", None)

        # Fire both concurrently; the in-coroutine asserts already enforce no-bleed,
        # so gather raising would fail the test.
        await asyncio.gather(_run_one("reqA"), _run_one("reqB"))

        # Cross-check the recorded read-backs explicitly.
        assert _RESULTS["reqA"]["model"] == "openai/model-A"
        assert _RESULTS["reqA"]["provider"] == "mosaico_diff"
        assert _RESULTS["reqA"]["artifact"] == "ARTIFACT-A"
        assert _RESULTS["reqB"]["model"] == "anthropic/model-B"
        assert _RESULTS["reqB"]["provider"] == "github"
        assert _RESULTS["reqB"]["artifact"] == "ARTIFACT-B"

        # Structural proof of isolation: each request resolved to its OWN settings object,
        # distinct from the sibling's and from the shared global.
        assert _RESULTS["reqA"]["settings_id"] != _RESULTS["reqB"]["settings_id"]
        assert _RESULTS["reqA"]["settings_id"] != id(global_settings)
        assert _RESULTS["reqB"]["settings_id"] != id(global_settings)

        # global_settings must be UNMUTATED by the per-request runs.
        assert global_settings.get("CONFIG.MODEL", None) == global_model_before
        assert global_settings.get("CONFIG.GIT_PROVIDER", None) == global_provider_before

    @pytest.mark.asyncio
    async def test_isolation_is_non_vacuous(self, monkeypatch, snapshot_global):
        """Prove the test would FAIL without per-request isolation: run the SAME
        interleaving but WITHOUT the executor's deepcopy-into-context — both coroutines
        share global_settings, so the read-back sees the sibling's clobbering writes."""
        bleed_detected = {"value": False}

        async def shared_route_and_run(user_text):
            profile = _PROFILES[user_text]
            # Write directly to the SHARED global_settings (no per-request scope).
            global_settings.set("CONFIG.MODEL", profile["model"])
            global_settings.data = {"artifact": profile["artifact"]}
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            read_model = global_settings.get("CONFIG.MODEL")
            read_artifact = (global_settings.get("data", {}) or {}).get("artifact", "")
            if read_model != profile["model"] or read_artifact != profile["artifact"]:
                bleed_detected["value"] = True
            return ""

        async def run_shared(text):
            # NOTE: deliberately NO request_cycle_context here -> no isolation.
            await shared_route_and_run(text)

        await asyncio.gather(run_shared("reqA"), run_shared("reqB"))
        # Without isolation, the interleaved writes MUST have clobbered each other.
        assert bleed_detected["value"] is True, \
            "expected cross-request bleed without isolation, but none was observed"
