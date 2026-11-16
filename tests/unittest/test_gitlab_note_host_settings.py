import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import utils as git_utils
from tests.unittest._reaction_helpers import _RecordingProvider
from tests.unittest.test_gitlab_webhook_outcome_reactions import _note_event


@pytest.fixture(autouse=True)
def isolate_host_environment(monkeypatch):
    for key in ("GITLAB__PERSONAL_ACCESS_TOKEN", "GITLAB__AUTH_TYPE", "GITLAB__URL", "GITLAB__SSL_VERIFY",
                "CONFIG__HTTP_REQUEST_TIMEOUT", "CONFIG__EXTRA_CONFIG_URL", "PR_AGENT_EXTRA_CONFIG_URL"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("timeout", [7.5, 180])
@pytest.mark.parametrize("bootstrap_pat", ["bootstrap-test-token", None])
def test_note_provider_construction_uses_external_host_settings(monkeypatch, tmp_path, timeout, bootstrap_pat):
    import pr_agent.servers.gitlab_webhook as webhook

    config = tmp_path / "host.toml"
    config.write_text(
        f'[config]\nhttp_request_timeout = {timeout}\n'
        '[gitlab]\npersonal_access_token = "external-test-token"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(config))
    monkeypatch.setitem(get_settings().config, "use_repo_settings_file", False)
    monkeypatch.setitem(get_settings().config, "http_request_timeout", 60)
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", bootstrap_pat)
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setattr(webhook, "is_bot_user", lambda data: False)
    provider = _RecordingProvider()
    construction = []

    def get_provider(pr_url):
        if not construction:
            construction.append((get_settings().get("config.http_request_timeout"),
                                 get_settings().get("gitlab.personal_access_token")))
        return provider

    async def dispatch(api_url, body, log_context, sender_id, notify=None):
        git_utils.apply_repo_settings(api_url)
        if notify:
            notify()
        return True

    monkeypatch.setattr(webhook, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(webhook, "handle_request", dispatch)
    with TestClient(webhook.app) as client:
        response = client.post("/webhook", json=_note_event(), headers={"X-Gitlab-Token": "secret"})

    assert response.status_code == 200
    assert construction == [(timeout, "external-test-token")]


@pytest.mark.parametrize("body", ["Ordinary comment", "review this change"])
def test_non_command_notes_do_not_load_host_settings_or_construct_provider(monkeypatch, body):
    import pr_agent.servers.gitlab_webhook as webhook

    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "bootstrap-test-token")
    monkeypatch.setattr(webhook, "is_bot_user", lambda data: False)
    calls = []
    monkeypatch.setattr(webhook, "apply_host_settings", lambda: calls.append("host"))
    monkeypatch.setattr(webhook, "get_git_provider_with_context", lambda **kwargs: calls.append("provider"))
    event = _note_event()
    event["object_attributes"]["note"] = body
    with TestClient(webhook.app) as client:
        response = client.post("/webhook", json=event, headers={"X-Gitlab-Token": "secret"})
    assert response.status_code == 200
    assert calls == []


@pytest.mark.parametrize("timeout", [7.5, 180])
def test_first_gitlab_http_request_uses_external_host_settings(monkeypatch, tmp_path, timeout):
    import pr_agent.servers.gitlab_webhook as webhook

    config = tmp_path / "host.toml"
    config.write_text(
        f'[config]\nhttp_request_timeout = {timeout}\n'
        '[gitlab]\npersonal_access_token = "external-test-token"\nssl_verify = false\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(config))
    monkeypatch.setitem(get_settings().config, "http_request_timeout", 60)
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "bootstrap-test-token")
    monkeypatch.setitem(get_settings().gitlab, "auth_type", "oauth_token")
    monkeypatch.setitem(get_settings().gitlab, "url", "https://gitlab.example.com")
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setattr(webhook, "is_bot_user", lambda data: False)
    sent = []

    def send(session, request, **kwargs):
        sent.append((kwargs["timeout"], request.headers["Authorization"], kwargs["verify"]))
        raise requests.Timeout("stop the mocked initial lookup")

    monkeypatch.setattr(requests.Session, "send", send)
    with TestClient(webhook.app) as client, pytest.raises(ValueError, match="Failed to get git provider"):
        client.post("/webhook", json=_note_event(), headers={"X-Gitlab-Token": "secret"})

    assert sent == [(timeout, "Bearer external-test-token", False)]


@pytest.mark.parametrize("token_key", ["personal_access_token", "PERSONAL_ACCESS_TOKEN"])
@pytest.mark.parametrize("env_override", [False, True])
def test_authenticated_webhook_pat_survives_host_and_environment_replay(monkeypatch, tmp_path, env_override, token_key):
    import pr_agent.servers.gitlab_webhook as webhook

    config = tmp_path / "host.toml"
    config.write_text(f'[gitlab]\n{token_key} = "host-test-token"\n', encoding="utf-8")
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(config))
    monkeypatch.setitem(get_settings().config, "use_repo_settings_file", True)
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "bootstrap-test-token")
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "different-shared-secret")
    if env_override:
        monkeypatch.setenv("GITLAB__PERSONAL_ACCESS_TOKEN", "env-host-test-token")
    secret = json.dumps({"webhook_token": "webhook-secret", "gitlab_token": "project-test-token"})
    monkeypatch.setattr(webhook, "get_fork_safe_secret_provider",
                        lambda: SimpleNamespace(get_secret=lambda name: secret))
    monkeypatch.setattr(webhook, "is_bot_user", lambda data: False)
    observed = []
    provider = _RecordingProvider()

    def get_provider(pr_url):
        observed.append(get_settings().get("gitlab.personal_access_token"))
        return provider

    async def dispatch(api_url, body, log_context, sender_id, notify=None):
        git_utils.apply_repo_settings(api_url)
        observed.append(get_settings().get("gitlab.personal_access_token"))
        if notify:
            notify()
        return True

    monkeypatch.setattr(webhook, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(webhook, "handle_request", dispatch)
    with TestClient(webhook.app) as client:
        response = client.post("/webhook", json=_note_event(),
                               headers={"X-Gitlab-Token": "project-secret:webhook-secret"})
    assert response.status_code == 200
    assert observed
    assert set(observed) == {"project-test-token"}


@pytest.mark.parametrize("bootstrap_pat", [None, "bootstrap-test-token"])
async def test_host_acquisition_yields_to_the_event_loop(monkeypatch, tmp_path, bootstrap_pat):
    import pr_agent.servers.gitlab_webhook as webhook

    config = tmp_path / "host.toml"
    config.write_text('[gitlab]\npersonal_access_token = "external-test-token"\n', encoding="utf-8")
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(config))
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", bootstrap_pat)
    monkeypatch.setattr(webhook, "is_bot_user", lambda data: False)
    started, released = threading.Event(), threading.Event()
    acquisitions, observed = [], []
    resolve = git_utils._resolve_extra_config_to_file

    def acquire(source):
        acquisitions.append((source, threading.get_ident()))
        started.set()
        assert released.wait(2), "host acquisition blocked the event loop"
        return resolve(source)

    def provider(pr_url):
        observed.append(get_settings().get("gitlab.personal_access_token"))
        return _RecordingProvider()

    async def dispatch(*args, **kwargs):
        return True

    async def release_acquisition():
        assert await asyncio.to_thread(started.wait, 2), "host acquisition never started"
        released.set()

    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    monkeypatch.setattr(webhook, "get_git_provider_with_context", provider)
    monkeypatch.setattr(webhook, "handle_request", dispatch)
    releaser = asyncio.create_task(release_acquisition())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webhook.app), base_url="http://test") as client:
            response = await client.post("/webhook", json=_note_event(), headers={"X-Gitlab-Token": "secret"})
        await releaser
    finally:
        released.set()
        if not releaser.done():
            releaser.cancel()
        await asyncio.gather(releaser, return_exceptions=True)
    assert response.status_code == 200
    assert acquisitions[0][0] == str(config)
    assert acquisitions[0][1] != threading.get_ident()
    assert observed == ["external-test-token"]


async def test_invalid_webhook_secret_never_loads_host_settings(monkeypatch):
    import pr_agent.servers.gitlab_webhook as webhook

    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", None)
    calls = []
    monkeypatch.setattr(webhook, "apply_host_settings", lambda: calls.append(True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webhook.app), base_url="http://test") as client:
        response = await client.post("/webhook", content=b"not-json", headers={"X-Gitlab-Token": "wrong-secret"})
    assert response.status_code == 401
    assert not calls


async def test_cancelled_authentication_does_not_parse_or_dispatch_the_webhook(monkeypatch, tmp_path):
    import pr_agent.servers.gitlab_webhook as webhook

    config = tmp_path / "host.toml"
    config.write_text('[gitlab]\npersonal_access_token = "external-test-token"\n', encoding="utf-8")
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(config))
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", None)
    started, released, finished = threading.Event(), threading.Event(), threading.Event()
    worker_results = []
    apply_host = webhook.apply_host_settings

    def blocked_host_settings():
        started.set()
        try:
            assert released.wait(2), "test did not release the host worker"
            apply_host()
            worker_results.append(get_settings().get("gitlab.personal_access_token"))
        finally:
            finished.set()

    monkeypatch.setattr(webhook, "apply_host_settings", blocked_host_settings)
    request = SimpleNamespace(headers={"X-Gitlab-Token": "secret"}, json=AsyncMock(return_value=_note_event()))
    background = BackgroundTasks()
    with request_cycle_context({}):
        task = asyncio.create_task(webhook.gitlab_webhook(background, request))
        try:
            assert await asyncio.to_thread(started.wait, 2), "host acquisition never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            request.json.assert_not_awaited()
            assert not background.tasks
        finally:
            released.set()
            assert await asyncio.to_thread(finished.wait, 2), "host worker did not finish"
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert worker_results == ["external-test-token"]
    assert get_settings().get("gitlab.personal_access_token") is None
