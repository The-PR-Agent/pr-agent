from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.devops.exceptions import AzureDevOpsClientRequestError
from azure.devops.v7_1.location.models import ConnectionData, Identity
from requests.exceptions import ConnectionError

from pr_agent.algo.comment_identity import PRReviewHeader, PRReviewIdentity
from pr_agent.algo.inline_comment_dedup import key_issue_body_with_markers
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider

BOT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def connection(monkeypatch):
    settings = get_settings()
    monkeypatch.setitem(settings.azure_devops_server, "agent_identity", "")
    monkeypatch.setitem(settings.azure_devops, "org", "https://dev.azure.com/test")
    monkeypatch.setitem(settings.azure_devops, "pat", "test-pat")
    connection = MagicMock()
    connection.clients.get_location_client.return_value.get_connection_data.return_value = ConnectionData(
        authenticated_user=Identity(id=BOT_ID), authorized_user=Identity(id=BOT_ID))
    monkeypatch.setattr("pr_agent.git_providers.azuredevops_provider.Connection", lambda **kwargs: connection)
    return connection


def test_identity_is_resolved_lazily_and_cached(connection):
    provider = AzureDevopsProvider()
    lookup = connection.clients.get_location_client.return_value.get_connection_data
    lookup.assert_not_called()

    assert provider.supports_review_finding_state() is True
    assert provider.is_comment_authored_by_pr_agent(SimpleNamespace(author=SimpleNamespace(id=BOT_ID))) is True
    assert provider.is_comment_authored_by_pr_agent({"author": {"id": OTHER_ID}}) is False
    assert provider.supports_review_finding_state() is True
    lookup.assert_called_once_with()


@pytest.mark.parametrize("pat", ["test-pat", ""])
def test_identity_lookup_reuses_the_comment_credentials(connection, monkeypatch, pat):
    monkeypatch.setitem(get_settings().azure_devops, "pat", pat)
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr("pr_agent.git_providers.azuredevops_provider.Connection", connect)
    credential = MagicMock()
    credential.get_token.return_value.token = "default-credential-token"
    default_credential = MagicMock(return_value=credential)
    monkeypatch.setattr("pr_agent.git_providers.azuredevops_provider.DefaultAzureCredential", default_credential)

    provider = AzureDevopsProvider()

    assert provider.supports_review_finding_state() is True
    assert connect.call_count == 1
    assert connect.call_args.kwargs["base_url"] == "https://dev.azure.com/test"
    assert connect.call_args.kwargs["creds"].password == (pat or "default-credential-token")
    assert provider.azure_devops_client is connection.clients.get_git_client.return_value
    assert default_credential.call_count == (0 if pat else 1)


@pytest.mark.parametrize("configured", [BOT_ID, [BOT_ID, OTHER_ID], "PR Agent"])
def test_explicit_identity_overrides_connection_identity(connection, monkeypatch, configured):
    monkeypatch.setitem(get_settings().azure_devops_server, "agent_identity", configured)
    provider = AzureDevopsProvider()

    assert provider.supports_review_finding_state() is (configured != "PR Agent")
    assert provider.is_comment_authored_by_pr_agent({"author": {"id": OTHER_ID}}) is isinstance(configured, list)
    connection.clients.get_location_client.assert_not_called()


def test_authorized_identity_takes_precedence_over_authenticated_identity(connection):
    connection.clients.get_location_client.return_value.get_connection_data.return_value = ConnectionData(
        authenticated_user=Identity(id=OTHER_ID), authorized_user=Identity(id=BOT_ID))
    provider = AzureDevopsProvider()

    assert provider.is_comment_authored_by_pr_agent({"author": {"id": BOT_ID}}) is True
    assert provider.is_comment_authored_by_pr_agent({"author": {"id": OTHER_ID}}) is False


def test_authenticated_identity_is_used_when_authorized_identity_is_absent(connection):
    connection.clients.get_location_client.return_value.get_connection_data.return_value = ConnectionData(
        authenticated_user=Identity(id=BOT_ID))

    assert AzureDevopsProvider().is_comment_authored_by_pr_agent({"author": {"id": BOT_ID}}) is True


@pytest.mark.parametrize("identity", [None, "", 123, "PR Agent", "00000000-0000-0000-0000-000000000000"])
def test_unusable_connection_identity_does_not_enable_updates(connection, identity):
    connection.clients.get_location_client.return_value.get_connection_data.return_value = ConnectionData(
        authenticated_user=Identity(id=OTHER_ID), authorized_user=Identity(id=identity))
    provider = AzureDevopsProvider()

    assert provider.supports_review_finding_state() is False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        provider.is_comment_authored_by_pr_agent({"author": {"id": OTHER_ID}})


@pytest.mark.parametrize("error", [AzureDevOpsClientRequestError("unavailable"), ConnectionError("unavailable")])
def test_failed_lookup_is_not_cached(connection, error):
    lookup = connection.clients.get_location_client.return_value.get_connection_data
    lookup.side_effect = [error, ConnectionData(authorized_user=Identity(id=BOT_ID))]
    provider = AzureDevopsProvider()

    assert provider.supports_review_finding_state() is False
    assert provider.is_comment_authored_by_pr_agent({"author": {"id": BOT_ID}}) is True
    assert lookup.call_count == 2


@pytest.mark.parametrize("lookup_fails", [False, True])
def test_unverified_existing_comment_is_never_updated(connection, lookup_fails):
    if lookup_fails:
        connection.clients.get_location_client.return_value.get_connection_data.side_effect = (
            AzureDevOpsClientRequestError("unavailable"))
    provider = AzureDevopsProvider()
    provider.workspace_slug = "project"
    provider.repo_slug = "repo"
    provider.pr_num = 1
    previous = SimpleNamespace(
        body=f"{PRReviewHeader.REGULAR.value}\n\n{PRReviewIdentity.REGULAR.value}\n\nOriginal review",
        author=SimpleNamespace(id=OTHER_ID),
    )
    provider.get_issue_comments = lambda: [previous]

    result = provider.publish_persistent_comment_full(
        "New review", initial_header=PRReviewHeader.REGULAR.value,
        identity_marker=PRReviewIdentity.REGULAR.value, require_agent_authorship=True, fallback_on_error=False)

    client = connection.clients.get_git_client.return_value
    client.update_comment.assert_not_called()
    if lookup_fails:
        assert result is None
        client.create_thread.assert_not_called()
    else:
        client.create_thread.assert_called_once()
    assert previous.body.endswith("Original review")


def test_lookalike_display_names_do_not_prove_authorship(connection):
    provider = AzureDevopsProvider()
    assert provider.is_comment_authored_by_pr_agent({"author": {"id": OTHER_ID, "displayName": BOT_ID}}) is False
    with pytest.raises(RuntimeError, match="cannot be verified"):
        provider.is_comment_authored_by_pr_agent({"author": {"displayName": BOT_ID}})


def test_identity_cache_is_scoped_to_the_provider_connection(connection):
    first = AzureDevopsProvider()
    assert first.is_comment_authored_by_pr_agent({"author": {"id": BOT_ID}}) is True
    connection.clients.get_location_client.return_value.get_connection_data.return_value = ConnectionData(
        authorized_user=Identity(id=OTHER_ID))
    second = AzureDevopsProvider()

    assert second.is_comment_authored_by_pr_agent({"author": {"id": BOT_ID}}) is False
    assert second.is_comment_authored_by_pr_agent({"author": {"id": OTHER_ID}}) is True
    assert first.is_comment_authored_by_pr_agent({"author": {"id": BOT_ID}}) is True


def test_automatically_resolved_identity_verifies_dismissed_findings(connection, monkeypatch):
    monkeypatch.setitem(get_settings().azure_devops, "default_comment_status", "closed")
    provider = AzureDevopsProvider()
    provider._threads_cache = [{
        "id": 1,
        "status": "wontFix",
        "comments": [{
            "id": 1, "author": {"id": BOT_ID},
            "content": key_issue_body_with_markers("The lock is never released.", "aabbccddeeff", "112233445566"),
        }],
        "threadContext": {"filePath": "/app.py", "rightFileStart": {"line": 2}},
    }]

    findings = list(provider._iter_review_threads())

    assert len(findings) == 1
    assert findings[0].authored_by_agent is True
    assert findings[0].status == "resolved"
