from unittest import mock

import pytest
from fastapi.testclient import TestClient
from gitlab.exceptions import GitlabAuthenticationError, GitlabGetError

from pr_agent.config_loader import get_settings
from pr_agent.servers import gitlab_webhook

MR_URL = "https://gitlab.example.com/group/project/-/merge_requests/39"


def _provider_error(response_code: int) -> ValueError:
    """The error get_git_provider_with_context raises when the provider constructor fails."""
    error = ValueError(f"Failed to get git provider for {MR_URL}")
    error.__cause__ = GitlabGetError(f"{response_code}: error", response_code=response_code)
    return error


@pytest.fixture
def sleep():
    with mock.patch("pr_agent.servers.gitlab_webhook.asyncio.sleep", new_callable=mock.AsyncMock) as patched:
        yield patched


@pytest.fixture
def get_provider():
    with mock.patch("pr_agent.servers.gitlab_webhook.get_git_provider_with_context") as patched:
        yield patched


async def test_returns_provider_without_waiting_when_lookup_succeeds(get_provider, sleep):
    provider = await gitlab_webhook.get_new_mr_provider(MR_URL)

    assert provider is get_provider.return_value
    get_provider.assert_called_once_with(pr_url=MR_URL)
    sleep.assert_not_awaited()


async def test_retries_404_until_the_merge_request_is_visible(get_provider, sleep):
    found = mock.Mock()
    get_provider.side_effect = [_provider_error(404), _provider_error(404), found]

    provider = await gitlab_webhook.get_new_mr_provider(MR_URL)

    assert provider is found
    assert get_provider.call_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2]


async def test_persistent_404_is_raised_after_the_full_backoff(get_provider, sleep):
    get_provider.side_effect = _provider_error(404)

    with pytest.raises(ValueError, match="Failed to get git provider"):
        await gitlab_webhook.get_new_mr_provider(MR_URL)

    assert get_provider.call_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4]


@pytest.mark.parametrize("response_code", [401, 403, 500])
async def test_other_gitlab_errors_are_not_retried(get_provider, sleep, response_code):
    get_provider.side_effect = _provider_error(response_code)

    with pytest.raises(ValueError):
        await gitlab_webhook.get_new_mr_provider(MR_URL)

    get_provider.assert_called_once()
    sleep.assert_not_awaited()


async def test_non_gitlab_failure_is_not_retried(get_provider, sleep):
    error = ValueError("GitLab personal access token is not set in the config file")
    get_provider.side_effect = error

    with pytest.raises(ValueError) as raised:
        await gitlab_webhook.get_new_mr_provider(MR_URL)

    assert raised.value is error
    get_provider.assert_called_once()
    sleep.assert_not_awaited()


async def test_each_retry_is_logged(get_provider, sleep):
    get_provider.side_effect = [_provider_error(404), mock.Mock()]

    with mock.patch("pr_agent.servers.gitlab_webhook.get_logger") as get_logger:
        await gitlab_webhook.get_new_mr_provider(MR_URL)

    get_logger.return_value.warning.assert_called_once()
    assert "retrying in 1s" in get_logger.return_value.warning.call_args.args[0]


async def test_404_from_the_provider_constructor_survives_the_provider_factory_wrapping(sleep):
    constructor = mock.Mock(side_effect=[
        GitlabGetError("404 Not found", response_code=404),
        GitlabAuthenticationError("401 Unauthorized", response_code=401),
    ])
    settings = mock.Mock()
    settings.config.git_provider = "gitlab"
    settings.get.return_value = None

    with mock.patch("pr_agent.git_providers.get_settings", return_value=settings), \
            mock.patch.dict("pr_agent.git_providers._GIT_PROVIDERS", {"gitlab": constructor}):
        with pytest.raises(ValueError) as raised:
            await gitlab_webhook.get_new_mr_provider(MR_URL)

    assert isinstance(raised.value.__cause__, GitlabAuthenticationError)
    assert constructor.call_count == 2
    sleep.assert_awaited_once_with(1)


def _open_event():
    return {
        "object_kind": "merge_request",
        "user": {"username": "author", "id": 7},
        "object_attributes": {"action": "open", "url": MR_URL},
    }


@pytest.fixture
def open_event(monkeypatch):
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "token")
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setattr(gitlab_webhook, "is_bot_user", lambda data: False)
    monkeypatch.setattr(gitlab_webhook, "should_process_pr_logic", lambda data: True)
    monkeypatch.setattr(gitlab_webhook, "apply_repo_settings", mock.Mock())
    perform = mock.AsyncMock()
    monkeypatch.setattr(gitlab_webhook, "_perform_commands_gitlab", perform)
    client = TestClient(gitlab_webhook.app)

    def post():
        return client.post("/webhook", json=_open_event(), headers={"X-Gitlab-Token": "secret"})

    post.perform = perform
    return post


def test_open_event_runs_commands_once_the_merge_request_is_visible(open_event, sleep, get_provider):
    get_provider.side_effect = [_provider_error(404), mock.Mock()]

    assert open_event().status_code == 200

    assert get_provider.call_count == 2
    open_event.perform.assert_awaited_once()


def test_open_event_still_skips_when_the_merge_request_never_appears(open_event, sleep, get_provider):
    get_provider.side_effect = _provider_error(404)

    with pytest.raises(ValueError, match="Failed to get git provider"):
        open_event()

    assert get_provider.call_count == 4
    open_event.perform.assert_not_awaited()
