import asyncio
from unittest.mock import Mock

import httpx
import pytest

from pr_agent.algo.ai_handlers import openrouter_model_info as model_info
from pr_agent.algo.token_budget import FallbackEligibleError, ModelContextPreparationError


@pytest.fixture
def mock_http(monkeypatch):
    client_class = httpx.AsyncClient
    requests = []
    monkeypatch.delenv("LITELLM_OFFLINE_MODE", raising=False)

    def install(handler):
        def respond(request):
            requests.append(request)
            return handler(request)

        def client(**kwargs):
            assert kwargs == {"timeout": model_info.MODEL_CONTEXT_TIMEOUT, "follow_redirects": False}
            return client_class(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(model_info.httpx, "AsyncClient", client)
        return requests

    return install


@pytest.mark.parametrize("model_id", ["example/model", "example/model:free"])
async def test_exact_context_and_request_contract(mock_http, model_id):
    requests = mock_http(lambda _: httpx.Response(200, json={
        "data": {"id": model_id, "context_length": 8192, "top_provider": {"context_length": 4096}}
    }))
    assert await model_info.fetch_model_context(model_id) == 8192
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/model/" + model_id.replace(":", "%3A")
    with httpx.Client(trust_env=False) as client:
        expected = client.build_request(
            "GET", request.url, headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
    assert request.headers == expected.headers
    assert all(value == 5.0 for value in request.extensions["timeout"].values())


@pytest.mark.parametrize("model_id", [
    "model", "/model", "example/", "example/a/b", "../model", "example/..", "example/%2e%2e",
    "example/a\\b", "example/a?b", "example/a\n", "example/" + "a" * 513,
])
async def test_malformed_ids_do_not_create_client(monkeypatch, model_id):
    def unexpected_client(**kwargs):
        pytest.fail("Malformed IDs must not create an HTTP client")

    monkeypatch.setattr(model_info.httpx, "AsyncClient", unexpected_client)
    monkeypatch.delenv("LITELLM_OFFLINE_MODE", raising=False)
    assert await model_info.fetch_model_context(model_id) is None


async def test_offline_mode_has_zero_http(mock_http, monkeypatch):
    requests = mock_http(lambda _: pytest.fail("Offline mode must not issue an HTTP request"))
    monkeypatch.setenv("LITELLM_OFFLINE_MODE", "true")
    assert await model_info.fetch_model_context("example/model") is None
    assert requests == []


@pytest.mark.parametrize("payload", [
    [], {"data": []}, {"data": {"id": "example/alias", "context_length": 8192}},
    *({"data": {"id": "example/model", "context_length": value}} for value in [True, 0, -1, 1.5, "8192"]),
    {"data": {"id": "example/model", "top_provider": {"context_length": 8192},
              "max_input_tokens": 4096, "max_output_tokens": 4096}},
])
async def test_invalid_metadata(mock_http, payload):
    mock_http(lambda _: httpx.Response(200, json=payload))
    assert await model_info.fetch_model_context("example/model") is None


@pytest.mark.parametrize("status,body", [
    (302, b""), (404, b""), (408, b""), (429, b""), (503, b""), (200, b"not json"),
])
async def test_http_and_json_failures(mock_http, status, body):
    requests = mock_http(lambda _: httpx.Response(status, content=body, headers={"Location": "https://example.org/"}))
    if status in (408, 429, 503):
        with pytest.raises(ModelContextPreparationError, match=f"HTTP {status}"):
            await model_info.fetch_model_context("example/model")
    else:
        assert await model_info.fetch_model_context("example/model") is None
    assert len(requests) == 1


async def test_stream_size_bound(mock_http):
    consumed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(20):
                consumed.append(index)
                yield b" " * (64 * 1024)

    mock_http(lambda _: httpx.Response(200, stream=Body()))
    assert await model_info.fetch_model_context("example/model") is None
    assert len(consumed) == 17


async def test_nonidentity_encoding_is_rejected_without_consuming_stream(mock_http):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Nonidentity-encoded response bodies must not be consumed")
            yield b""

    mock_http(lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=Body()))
    assert await model_info.fetch_model_context("example/model") is None


async def test_missing_http_client_dependency_is_fallback_eligible(monkeypatch):
    client = Mock(side_effect=ImportError("private proxy details"))
    monkeypatch.setattr(model_info.httpx, "AsyncClient", client)
    monkeypatch.delenv("LITELLM_OFFLINE_MODE", raising=False)

    with pytest.raises(ModelContextPreparationError, match="HTTP client dependency unavailable") as failure:
        await model_info.fetch_model_context("example/model")

    assert "private proxy details" not in str(failure.value)


async def test_total_deadline_uses_timeout_constant(mock_http, monkeypatch):
    async def hang(_):
        await asyncio.Event().wait()

    monkeypatch.setattr(model_info, "MODEL_CONTEXT_TIMEOUT", 0.01)
    requests = mock_http(hang)
    with pytest.raises(FallbackEligibleError, match="TimeoutError"):
        await model_info.fetch_model_context("example/model")
    assert len(requests) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError("private details"), asyncio.CancelledError()])
async def test_transport_error_and_cancellation(mock_http, monkeypatch, error):
    def fail(_):
        raise error

    logger = Mock()
    monkeypatch.setattr(model_info, "get_logger", lambda: logger)
    mock_http(fail)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await model_info.fetch_model_context("example/model")
        logger.info.assert_not_called()
    else:
        with pytest.raises(FallbackEligibleError, match="ConnectError") as failure:
            await model_info.fetch_model_context("example/model")
        assert "private details" not in str(failure.value)
        assert (
            "Set config.custom_model_max_tokens to a verified positive input-token limit to skip this lookup."
            in str(failure.value)
        )
        logger.info.assert_called_once_with("OpenRouter model context unavailable: ConnectError")
