"""Exercise MOSAICO's span filtering and correlation with the installed Langfuse SDK."""

import httpx
import langfuse
from langfuse._client.resource_manager import LangfuseResourceManager
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from pr_agent.mosaico import server
from pr_agent.mosaico.observability import AGENT_NAME, langfuse_span


def test_langfuse_exports_existing_spans_and_preserves_trace_context(monkeypatch):
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "true")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
    real_langfuse = langfuse.Langfuse
    clients = []
    # Keep this local provider isolated; Langfuse caches clients by public key.
    monkeypatch.setattr(LangfuseResourceManager, "_instances", {})
    monkeypatch.setattr(server, "langfuse_env_present", lambda: True)

    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))) as http_client:
        def create_client(**kwargs):
            client = real_langfuse(
                public_key="pk-lf-mosaico-export-test",
                secret_key="sk-lf-test-only",
                base_url="http://localhost:4318",
                tracer_provider=provider,
                span_exporter=exporter,
                httpx_client=http_client,
                tracing_enabled=True,
                sample_rate=1.0,
                **kwargs,
            )
            clients.append(client)
            return client

        monkeypatch.setattr(langfuse, "Langfuse", create_client)
        try:
            server._configure_langfuse()
            assert len(clients) == 1, "MOSAICO must successfully initialize the real Langfuse SDK"
            client = clients[0]
            monkeypatch.setattr(langfuse, "get_client", lambda: client)
            metadata = {
                "mosaico-root-task-id": "123e4567-e89b-12d3-a456-426614174000",
                "mosaico-super-task-id": "00112233-4455-6677-8899-aabbccddeeff",
                "mosaico-root-task-name": "export-test",
            }
            with langfuse_span(metadata, "test-session"):
                for scope in ("litellm", "framework", "a2a-python-sdk", "a2a-python-sdk-child"):
                    with provider.get_tracer(scope).start_as_current_span(f"operation-{scope}"):
                        pass

            client.flush()
            spans = {span.name: span for span in exporter.get_finished_spans()}
            assert set(spans) == {
                AGENT_NAME, "operation-litellm", "operation-framework", "operation-a2a-python-sdk-child"
            }
            root = spans[AGENT_NAME]
            assert root.context.trace_id == int("123e4567e89b12d3a456426614174000", 16)
            assert root.parent.span_id == int("8899aabbccddeeff", 16)
            assert root.attributes["session.id"] == "test-session"
            assert root.attributes["langfuse.trace.name"] == "export-test"
            for name in ("litellm", "framework", "a2a-python-sdk-child"):
                assert spans[f"operation-{name}"].context.trace_id == root.context.trace_id
                assert spans[f"operation-{name}"].parent.span_id == root.context.span_id
        finally:
            for client in clients:
                client.shutdown()
            provider.shutdown()
