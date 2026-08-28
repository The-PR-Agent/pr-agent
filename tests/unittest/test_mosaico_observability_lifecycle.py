import asyncio
import contextlib
import sys
import types

import pytest

from pr_agent.mosaico.observability import langfuse_span


def _install_fake_langfuse(monkeypatch, *, teardown_failure=False):
    @contextlib.contextmanager
    def fake_propagate_attributes(**kwargs):
        yield

    class FakeClient:
        @contextlib.contextmanager
        def start_as_current_observation(self, **kwargs):
            try:
                yield
            finally:
                if teardown_failure:
                    raise RuntimeError("span flush failed")

    fake_langfuse = types.ModuleType("langfuse")
    fake_langfuse.get_client = lambda: FakeClient()
    fake_langfuse.propagate_attributes = fake_propagate_attributes
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)


def test_langfuse_span_preserves_body_exception(monkeypatch):
    _install_fake_langfuse(monkeypatch)

    with pytest.raises(ValueError, match="route failed"):
        with langfuse_span({"mosaico-root-task-id": "root"}, "ctx"):
            raise ValueError("route failed")


def test_langfuse_span_preserves_body_exception_when_teardown_fails(monkeypatch):
    _install_fake_langfuse(monkeypatch, teardown_failure=True)

    with pytest.raises(ValueError, match="route failed"):
        with langfuse_span({"mosaico-root-task-id": "root"}, "ctx"):
            raise ValueError("route failed")


def test_langfuse_span_preserves_cancellation(monkeypatch):
    _install_fake_langfuse(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        with langfuse_span({"mosaico-root-task-id": "root"}, "ctx"):
            raise asyncio.CancelledError()


def test_langfuse_span_does_not_mask_success_when_teardown_fails(monkeypatch):
    _install_fake_langfuse(monkeypatch, teardown_failure=True)

    with langfuse_span({"mosaico-root-task-id": "root"}, "ctx"):
        pass
