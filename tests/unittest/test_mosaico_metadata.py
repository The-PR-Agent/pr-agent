"""Observability metadata + langfuse_span no-op-safety tests.

parse_observability_metadata: all-3 -> all 3; missing-one -> PARTIAL dict (not {});
non-string value -> key omitted; non-Mapping -> {}; never raises.
langfuse_span: no-op-safe when langfuse unavailable AND when meta is partial/empty;
W3C transform locked (root-task-id de-hyphenated -> trace_id, last 16 hex of
super-task-id -> parent_span_id, context_id -> session_id)."""
import contextlib
import sys
import types

import pytest

from pr_agent.mosaico.observability import (langfuse_span,
                                            mosaico_log_context,
                                            parse_observability_metadata)


class TestParseObservabilityMetadata:
    def test_all_three(self):
        raw = {
            "mosaico-root-task-id": "r",
            "mosaico-super-task-id": "s",
            "mosaico-root-task-name": "n",
        }
        assert parse_observability_metadata(raw) == raw

    def test_missing_one_is_partial(self):
        raw = {"mosaico-root-task-id": "r", "mosaico-root-task-name": "n"}
        out = parse_observability_metadata(raw)
        assert out == {"mosaico-root-task-id": "r", "mosaico-root-task-name": "n"}
        assert out != {}

    def test_non_string_value_omitted(self):
        raw = {"mosaico-root-task-id": "r", "mosaico-super-task-id": 5}
        assert parse_observability_metadata(raw) == {"mosaico-root-task-id": "r"}

    def test_non_mapping_returns_empty(self):
        for bad in (None, [], "x", 7, ("a",)):
            assert parse_observability_metadata(bad) == {}

    def test_never_raises(self):
        # Exhaustive no-raise check across odd inputs.
        for bad in (None, {}, {"x": "y"}, [1, 2], "str", 0, object()):
            parse_observability_metadata(bad)


class TestLangfuseSpanNoOpSafety:
    def test_empty_meta_is_noop(self):
        ran = {"v": False}
        with langfuse_span({}, "ctx"):
            ran["v"] = True
        assert ran["v"] is True

    def test_none_meta_is_noop(self):
        with langfuse_span(None, "ctx"):
            pass  # must not raise

    def test_partial_meta_does_not_raise_when_langfuse_unavailable(self, monkeypatch):
        # Simulate langfuse import failure inside the CM; it must degrade to untraced.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "langfuse":
                raise ImportError("langfuse not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        ran = {"v": False}
        with langfuse_span({"mosaico-root-task-id": "r"}, "ctx"):
            ran["v"] = True
        assert ran["v"] is True  # body still executed despite langfuse failure

    def test_log_context_partial_and_empty(self):
        with mosaico_log_context({"mosaico-root-task-id": "r"}, "ctx"):
            pass
        with mosaico_log_context({}, None):
            pass


class TestLangfuseSpanW3CTransform:
    """Lock the W3C Trace Context transform (spec §observability lines 47-51).

    Existing tests use non-UUID stubs ("r"/"s") where ``.replace('-','')`` and
    ``[-16:]`` are no-ops, so they do NOT guard the transform. This installs a fake
    ``langfuse`` module to capture the trace_context/session_id actually passed in."""

    @pytest.mark.asyncio
    async def test_w3c_transform_locked(self, monkeypatch):
        captured = {}

        @contextlib.contextmanager
        def fake_propagate_attributes(session_id=None, trace_name=None):
            captured["session_id"] = session_id
            captured["trace_name"] = trace_name
            yield

        class FakeClient:
            @contextlib.contextmanager
            def start_as_current_observation(self, *, as_type=None, name=None, **kwargs):
                captured["trace_context"] = kwargs.get("trace_context")
                captured["name"] = name
                yield

        fake_langfuse = types.ModuleType("langfuse")
        fake_langfuse.get_client = lambda: FakeClient()
        fake_langfuse.propagate_attributes = fake_propagate_attributes
        monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)

        meta = parse_observability_metadata({
            "mosaico-root-task-id": "123e4567-e89b-12d3-a456-426614174000",
            "mosaico-super-task-id": "00112233-4455-6677-8899-aabbccddeeff",
            "mosaico-root-task-name": "root-task",
        })
        ran = {"v": False}
        with langfuse_span(meta, "ctx-id-42"):
            ran["v"] = True

        assert ran["v"] is True
        tc = captured["trace_context"]
        # root-task-id with the four UUIDv4 hyphens removed -> W3C 32-hex trace-id
        assert tc["trace_id"] == "123e4567e89b12d3a456426614174000"
        # last 16 hex digits of the de-hyphenated super-task-id -> W3C 16-hex parent-id
        assert tc["parent_span_id"] == "8899aabbccddeeff"
        # A2A context id -> Langfuse session id
        assert captured["session_id"] == "ctx-id-42"
        assert captured["trace_name"] == "root-task"
