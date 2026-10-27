import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools import ticket_pr_compliance_check as tpc
from tests.unittest._settings_helpers import restore_settings, snapshot_settings
from tests.unittest.test_ticket_pr_compliance_capabilities import (
    _BaseStubProvider,
    _CustomAzureSubstitute,
    _CustomGitHubSubstitute,
    _CustomGitLabSubstitute,
)

URL = "https://linear.app/acme/issue/ENG-123/old-title"


@pytest.fixture(autouse=True)
def linear_settings():
    snapshot = snapshot_settings(["linear.api_token", "linear.request_timeout", "config.max_ticket_count"])
    get_settings().set("linear.api_token", "test-linear-token")
    yield
    restore_settings(snapshot)


def issue_payload(**overrides):
    issue = {
        "id": "issue-uuid",
        "identifier": "ENG-123",
        "url": "https://linear.app/acme/issue/ENG-123/new-title",
        "title": "Fix race",
        "description": "x" * 25,
        "labels": {"nodes": [{"name": "bug"}, {"name": "urgent"}]},
    }
    issue.update(overrides)
    return {"data": {"issue": issue}}


class Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.json = AsyncMock(return_value=payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, response=None):
        self.post = MagicMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_find_linear_links_deduplicates_identity_in_description_order():
    first = URL + "?foo=bar#comment"
    other_workspace = "https://linear.app/other/issue/ENG-123"
    second = "https://linear.app/acme/issue/LONGTEAMKEY-42"
    text = f"[{first}]({first}) {URL} {other_workspace} <{second}> ENG-999"
    assert tpc.find_linear_tickets(text) == [first, other_workspace, second]
    assert tpc._get_linear_issue_identifier(first) == "ENG-123"


@pytest.mark.parametrize("text", [None, "", "ENG-123", "feature/ENG-123"])
def test_no_inferred_linear_references(text):
    assert tpc.find_linear_tickets(text) == []


@pytest.mark.parametrize("url", [
    "http://linear.app/acme/issue/ENG-123",
    "https://linear.app.evil.example/acme/issue/ENG-123",
    "https://evil.example/linear.app/acme/issue/ENG-123",
    "https://user@linear.app/acme/issue/ENG-123",
    "https://linear.app:444/acme/issue/ENG-123",
    "https://linear.app/acme/project/ENG-123",
    "https://linear.app/acme/issue/123",
])
def test_reject_noncanonical_linear_destinations(url):
    assert tpc.find_linear_tickets(url) == []
    with pytest.raises(ValueError):
        tpc._get_linear_issue_identifier(url)


@pytest.mark.parametrize(("value", "expected"), [
    (None, 10), ("bad", 10), (0, 10), (-1, 10), (float("inf"), 10),
    (float("nan"), 10), (10**1000, 10), (600, 60), ("2.5", 2.5),
])
def test_timeout_is_finite_positive_and_bounded(value, expected):
    get_settings().set("linear.request_timeout", value)
    assert tpc._get_linear_request_timeout() == expected


async def test_fixed_graphql_request_and_normalized_result():
    session = Session(Response(issue_payload()))
    result = await tpc._fetch_linear_ticket_content(session, URL, 20)
    args, kwargs = session.post.call_args
    assert args == ("https://api.linear.app/graphql",)
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"]["variables"] == {"id": "ENG-123"}
    query = kwargs["json"]["query"]
    assert "$id" in query and "ENG-123" not in query
    assert "labels(first: 20)" in query
    assert result == {
        "ticket_id": "ENG-123", "ticket_url": "https://linear.app/acme/issue/ENG-123/new-title",
        "title": "Fix race", "body": "x" * 20 + "...", "labels": "bug, urgent",
    }


async def test_optional_fields_and_malformed_labels_are_not_stringified():
    session = Session(Response(issue_payload(title=None, description={"bad": "shape"}, labels={
        "nodes": [{"name": "bug"}, None, {"name": 5}, {}, "bad"],
    })))
    result = await tpc._fetch_linear_ticket_content(session, URL, 10000)
    assert "ENG-123" in result["title"]
    assert result["body"] == ""
    assert result["labels"] == "bug"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"data": None}, {"data": {"issue": None}},
    issue_payload(id=""), issue_payload(identifier="ENG-124"),
    issue_payload(url="https://linear.app/other/issue/ENG-123"),
    issue_payload(url="https://evil.example/acme/issue/ENG-123"),
    {**issue_payload(), "errors": [{"message": "private-body-must-not-leak"}]},
])
async def test_rejects_missing_malformed_or_partial_graphql_data(payload):
    with pytest.raises((ValueError, RuntimeError)) as error:
        await tpc._fetch_linear_ticket_content(Session(Response(payload)), URL, 10000)
    assert "private-body-must-not-leak" not in str(error.value)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500])
async def test_http_failure_never_returns_placeholder_or_response_body(status):
    response = Response({"errors": [{"message": "private-body", "extensions": {"code": "RATELIMITED"}}]}, status)
    with pytest.raises(RuntimeError) as error:
        await tpc._fetch_linear_ticket_content(Session(response), URL, 10000)
    assert str(status) in str(error.value)
    assert "private-body" not in str(error.value)


async def test_malformed_json_is_skipped_without_exposing_response(monkeypatch):
    response = Response(None)
    response.json.side_effect = ValueError("private response content")
    logger = MagicMock()
    monkeypatch.setattr(tpc, "get_logger", lambda: logger)
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", lambda **kw: Session(response))
    assert await tpc._fetch_linear_ticket_contents([URL], 3, 10000) == []
    assert "private response content" not in str(logger.mock_calls)


@pytest.mark.parametrize("token", [None, "", "  ", 5])
async def test_missing_token_does_not_open_session(monkeypatch, token):
    get_settings().set("linear.api_token", token)
    session = MagicMock(side_effect=AssertionError("network must not start"))
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", session)
    assert await tpc._fetch_linear_ticket_contents([URL], 3, 10000) == []
    session.assert_not_called()


async def test_batch_uses_raw_personal_key_and_bounded_total_timeout(monkeypatch):
    session = MagicMock(return_value=Session(Response(issue_payload())))
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", session)
    get_settings().set("linear.api_token", " test-linear-token ")
    get_settings().set("linear.request_timeout", 100)
    assert len(await tpc._fetch_linear_ticket_contents([URL], 3, 10000)) == 1
    assert session.call_args.kwargs["headers"]["Authorization"] == "test-linear-token"
    assert session.call_args.kwargs["timeout"].total == 60


@pytest.mark.parametrize("limit", [0, -1])
async def test_nonpositive_limit_never_starts_network(monkeypatch, limit):
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", MagicMock(side_effect=AssertionError("network")))
    assert await tpc._fetch_linear_ticket_contents([URL], limit, 10000) == []


async def test_empty_references_never_start_network(monkeypatch):
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", MagicMock(side_effect=AssertionError("network")))
    assert await tpc._fetch_linear_ticket_contents([], 3, 10000) == []


@pytest.mark.parametrize("requested_limit", [3, 100])
async def test_batch_caps_attempts_not_successes_and_sanitizes_errors(monkeypatch, requested_limit):
    fetch = AsyncMock(side_effect=RuntimeError("test-linear-token private-body"))
    logger = MagicMock()
    monkeypatch.setattr(tpc, "_fetch_linear_ticket_content", fetch)
    monkeypatch.setattr(tpc, "get_logger", lambda: logger)
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", lambda **kw: Session())
    urls = [f"https://linear.app/acme/issue/ENG-{n}" for n in range(1, 7)]
    assert await tpc._fetch_linear_ticket_contents(urls, requested_limit, 10000) == []
    assert [call.args[1] for call in fetch.await_args_list] == urls[:3]
    assert "test-linear-token" not in str(logger.mock_calls)
    assert "private-body" not in str(logger.mock_calls)


async def test_concurrent_fetches_preserve_order_and_partial_success(monkeypatch):
    started = []
    all_started = asyncio.Event()

    async def fetch(session, url, limit):
        started.append(url)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 1)
        if url.endswith("ENG-2"):
            raise TimeoutError()
        return {"ticket_url": url}

    monkeypatch.setattr(tpc, "_fetch_linear_ticket_content", fetch)
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", lambda **kw: Session())
    urls = [f"https://linear.app/acme/issue/ENG-{n}" for n in range(1, 4)]
    assert await tpc._fetch_linear_ticket_contents(urls, 3, 10000) == [
        {"ticket_url": urls[0]}, {"ticket_url": urls[2]},
    ]


@pytest.mark.parametrize("child_abort", [False, True])
async def test_cancellation_drains_all_inflight_tasks(monkeypatch, child_abort):
    started, finished = [], []
    ready = asyncio.Event()

    async def fetch(session, url, limit):
        started.append(url)
        if len(started) == 3:
            ready.set()
        try:
            await ready.wait()
            if child_abort and url.endswith("ENG-1"):
                raise asyncio.CancelledError()
            await asyncio.Event().wait()
        finally:
            finished.append(url)

    monkeypatch.setattr(tpc, "_fetch_linear_ticket_content", fetch)
    monkeypatch.setattr(tpc.aiohttp, "ClientSession", lambda **kw: Session())
    urls = [f"https://linear.app/acme/issue/ENG-{n}" for n in range(1, 4)]
    task = asyncio.create_task(tpc._fetch_linear_ticket_contents(urls, 3, 10000))
    await asyncio.wait_for(ready.wait(), 1)
    if not child_abort:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert set(finished) == set(urls)


@pytest.mark.parametrize(("provider_cls", "native_ids"), [
    (_CustomGitHubSubstitute, [101, 102]), (_CustomGitLabSubstitute, [55]),
    (_CustomAzureSubstitute, [999]), (_BaseStubProvider, []),
])
async def test_linear_appends_after_existing_sources_on_all_capability_paths(monkeypatch, provider_cls, native_ids):
    provider = provider_cls()
    description = provider.get_user_description() if hasattr(provider, "get_user_description") else ""
    provider.get_user_description = lambda: description + " " + URL + " https://app.asana.com/0/99/123456789012"
    monkeypatch.setattr(tpc, "_fetch_asana_ticket_contents", AsyncMock(return_value=[{"ticket_id": "asana"}]))

    def jira(provider, tickets):
        assert [t["ticket_id"] for t in tickets] == native_ids + ["asana"]
        tickets.append({"ticket_id": "jira"})
        return tickets

    fetch = AsyncMock(return_value=[{"ticket_id": "ENG-123"}])
    monkeypatch.setattr(tpc, "add_jira_tickets", jira)
    monkeypatch.setattr(tpc, "_fetch_linear_ticket_contents", fetch)
    tickets = await tpc.extract_tickets(provider)
    assert [t["ticket_id"] for t in tickets] == native_ids + ["asana", "jira", "ENG-123"]
    assert fetch.call_args.args[0] == [URL]
    assert fetch.call_args.args[1] == 3


@pytest.mark.parametrize(("description", "expected"), [("No tickets", None), (URL, [])])
async def test_external_only_none_vs_recognized_failed_references(monkeypatch, description, expected):
    provider = _BaseStubProvider()
    provider.get_user_description = lambda: description
    monkeypatch.setattr(tpc, "_fetch_linear_ticket_contents", AsyncMock(return_value=[]))
    monkeypatch.setattr(tpc, "add_jira_tickets", lambda provider, tickets: tickets)
    assert await tpc.extract_tickets(provider) == expected
