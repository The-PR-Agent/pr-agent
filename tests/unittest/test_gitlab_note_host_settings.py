import asyncio
import copy
import io
import json
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import requests
from fastapi.testclient import TestClient
from starlette.background import BackgroundTasks
from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import utils as git_utils
from pr_agent.log import get_logger
from tests.unittest._reaction_helpers import _RecordingProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings
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
    acquisitions = []
    resolve = git_utils._resolve_extra_config_to_file

    def get_provider(pr_url):
        if not construction:
            construction.append((get_settings().get("config.http_request_timeout"),
                                 get_settings().get("gitlab.personal_access_token")))
        return provider

    def acquire(source):
        acquisitions.append(source)
        return resolve(source)

    async def dispatch(api_url, body, log_context, sender_id, notify=None):
        git_utils.apply_repo_settings(api_url)
        if notify:
            notify()
        return True

    monkeypatch.setattr(webhook, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    monkeypatch.setattr(webhook, "handle_request", dispatch)
    with TestClient(webhook.app) as client:
        response = client.post("/webhook", json=_note_event(), headers={"X-Gitlab-Token": "secret"})

    assert response.status_code == 200
    assert construction == [(timeout, "external-test-token")]
    assert acquisitions == [str(config)]


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


@pytest.mark.parametrize("source_kind", ["local", "file", "remote"])
def test_host_settings_replay_uses_one_snapshot_per_request(monkeypatch, tmp_path, source_kind):
    config = tmp_path / "host.toml"
    config.write_text('[config]\nmodel = "host-model"\n', encoding="utf-8")
    acquisitions, messages = [], []
    resolve = git_utils._resolve_extra_config_to_file
    source = str(config) if source_kind == "local" else config.as_uri()
    if source_kind == "remote":
        source = ("https://url-user:url-pass@config.example/path-sentinel/host.toml"
                  "?token=query-sentinel#fragment-sentinel")
    expected_source = "https://config.example" if source_kind == "remote" else source

    def acquire(requested):
        acquisitions.append(requested)
        return (str(config), False) if source_kind == "remote" else resolve(requested)

    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    monkeypatch.setattr(git_utils, "get_logger", lambda: SimpleNamespace(info=messages.append, warning=messages.append))
    for expected in ("host-model", "next-host-model"):
        with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
            get_settings().set("config.extra_config_url", source)
            git_utils.apply_host_settings()
            assert get_settings().get("config.model") == expected
            get_settings().set("config.model", "command-model")
            config.write_text('[config]\nmodel = "next-host-model"\n', encoding="utf-8")
            git_utils.apply_host_settings()
            assert get_settings().get("config.model") == expected
    assert acquisitions == [source, source]
    assert len(messages) == 4
    assert all(f"settings from {expected_source} (sections merged:" in message for message in messages)
    assert all(secret not in "\n".join(messages) for secret in (
        "url-user", "url-pass", "path-sentinel", "query-sentinel", "fragment-sentinel",
    ))


@pytest.mark.parametrize("redirect_source", [True, False])
def test_note_command_replays_the_initial_host_source(monkeypatch, tmp_path, redirect_source):
    import pr_agent.servers.gitlab_webhook as webhook

    initial, other = tmp_path / "host.toml", tmp_path / "other.toml"
    other.write_text('[config]\nhttp_request_timeout = 99\n', encoding="utf-8")
    next_source = str(other) if redirect_source else ""
    initial.write_text(
        f'[config]\nhttp_request_timeout = 7.5\nextra_config_url = {json.dumps(next_source)}\n', encoding="utf-8",
    )
    monkeypatch.setitem(get_settings().config, "extra_config_url", str(initial))
    monkeypatch.setitem(get_settings().gitlab, "shared_secret", "secret")
    monkeypatch.setitem(get_settings().gitlab, "personal_access_token", "bootstrap-test-token")
    monkeypatch.setattr(webhook, "is_bot_user", lambda data: False)
    provider = _RecordingProvider()
    timeouts, acquisitions = [], []
    resolve = git_utils._resolve_extra_config_to_file

    def acquire(source):
        acquisitions.append(source)
        return resolve(source)

    def get_provider(pr_url):
        timeouts.append(get_settings().get("config.http_request_timeout"))
        return provider

    async def dispatch(api_url, body, log_context, sender_id, notify=None):
        get_settings().set("config.http_request_timeout", 60)
        git_utils.apply_host_settings()
        timeouts.append(get_settings().get("config.http_request_timeout"))
        return True

    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    monkeypatch.setattr(webhook, "get_git_provider_with_context", get_provider)
    monkeypatch.setattr(webhook, "handle_request", dispatch)
    with TestClient(webhook.app) as client:
        response = client.post("/webhook", json=_note_event(), headers={"X-Gitlab-Token": "secret"})
    assert response.status_code == 200
    assert timeouts == [7.5, 7.5]
    assert acquisitions == [str(initial)]


def test_failed_host_acquisition_is_retried_by_the_next_request(monkeypatch):
    acquisitions = []

    def acquire(source):
        acquisitions.append(source)
        return None, False

    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    for _ in range(2):
        with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
            get_settings().set("config.extra_config_url", "https://config.example/host.toml")
            git_utils.apply_host_settings()
            git_utils.apply_host_settings()
    assert len(acquisitions) == 2


@pytest.mark.parametrize("content, suffix", [
    ('[config]\nmodel = "untrusted-model"\n', ".txt"),
    ('dynaconf_include = ["other.toml"]\n[config]\nmodel = "untrusted-model"\n', ".toml"),
])
def test_host_snapshot_preserves_loader_security_checks(tmp_path, content, suffix):
    config = tmp_path / f"host{suffix}"
    config.write_text(content, encoding="utf-8")
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("config.extra_config_url", str(config))
        get_settings().set("config.model", "original-model")
        git_utils.apply_host_settings()
        git_utils.apply_host_settings()
        assert get_settings().get("config.model") == "original-model"


def test_host_settings_without_request_context_are_not_cached(tmp_path):
    config = tmp_path / "host.toml"
    snapshot = snapshot_settings(["config.extra_config_url", "config.model"])
    try:
        get_settings().set("config.extra_config_url", str(config))
        for model in ("first-host-model", "second-host-model"):
            config.write_text(f'[config]\nmodel = "{model}"\n', encoding="utf-8")
            git_utils.apply_host_settings()
            assert get_settings().get("config.model") == model
    finally:
        restore_settings(snapshot)


@pytest.mark.parametrize("source", [None, "", "  ", 42])
def test_missing_host_source_still_restores_authenticated_credentials(monkeypatch, source):
    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file",
                        lambda *_: pytest.fail("No host acquisition is needed"))
    with request_cycle_context({
        "settings": copy.deepcopy(global_settings),
        "authenticated_provider_settings": {"gitlab.personal_access_token": "verified-test-token"},
    }):
        get_settings().set("config.extra_config_url", source)
        get_settings().set("gitlab.personal_access_token", "changed-test-token")
        git_utils.apply_host_settings()
        assert get_settings().get("gitlab.personal_access_token") == "verified-test-token"


@pytest.mark.parametrize("stage", ["load", "merge"])
def test_host_application_failure_reports_stage_without_secret_content(monkeypatch, tmp_path, stage):
    path = tmp_path / "host.toml"
    path.write_text('[config]\nmodel = "host-model"\n', encoding="utf-8")

    def fail():
        raise ValueError("personal_access_token=private-test-secret")

    def loader(*_args, **_kwargs):
        if stage == "load":
            fail()
        return SimpleNamespace(as_dict=fail)

    monkeypatch.setattr(git_utils, "Dynaconf", loader)
    output = io.StringIO()
    sink = get_logger().add(output, level="WARNING", format="{message}")
    try:
        git_utils._apply_settings_from_file(str(path), "extra", display_name="<host settings>")
    finally:
        get_logger().remove(sink)
    assert f"Failed to {stage} extra settings from <host settings>: ValueError" in output.getvalue()
    assert "private-test-secret" not in output.getvalue()


def test_replay_failure_removes_temporary_file_and_keeps_snapshot(monkeypatch, tmp_path):
    config = tmp_path / "host.toml"
    config.write_text('[config]\nmodel = "host-model"\n', encoding="utf-8")
    write = git_utils._write_settings_temp
    paths, messages = [], []

    def failing_write(payload, registered):
        path = write(payload, registered)
        paths.append(path)
        if len(paths) == 1:
            raise OSError("mock replay write failure")
        return path

    monkeypatch.setattr(git_utils, "_write_settings_temp", failing_write)
    monkeypatch.setattr(git_utils, "get_logger", lambda: SimpleNamespace(info=lambda _: None, warning=messages.append))
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("config.extra_config_url", str(config))
        get_settings().set("config.model", "original-model")
        git_utils.apply_host_settings()
        assert get_settings().get("config.model") == "original-model"
        config.write_text('[config]\nmodel = "changed-model"\n', encoding="utf-8")
        git_utils.apply_host_settings()
        assert get_settings().get("config.model") == "host-model"
    assert all(not Path(path).exists() for path in paths)
    assert config.exists()

    assert messages == [f"Failed to replay extra host settings from {config}: OSError"]


@pytest.mark.parametrize("limit", ["MAX_TOML_SIZE_IN_BYTES", "_MAX_EXTRA_CONFIG_BYTES"])
def test_oversized_local_host_snapshot_is_not_loaded(monkeypatch, tmp_path, limit):
    config = tmp_path / "host.toml"
    config.write_text('[config]\nmodel = "large-host-model"\n', encoding="utf-8")
    monkeypatch.setattr(git_utils, limit, 16)
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("config.extra_config_url", str(config))
        get_settings().set("config.model", "original-model")
        git_utils.apply_host_settings()
        assert get_settings().get("config.model") == "original-model"


def test_cached_host_settings_keep_environment_precedence(monkeypatch, tmp_path):
    config = tmp_path / "host.toml"
    config.write_text('[gitlab]\npersonal_access_token = "file-test-token"\n', encoding="utf-8")
    monkeypatch.setenv("GITLAB__PERSONAL_ACCESS_TOKEN", "env-test-token")
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("config.extra_config_url", str(config))
        for _ in range(2):
            git_utils.apply_host_settings()
            assert get_settings().get("gitlab.personal_access_token") == "env-test-token"


def test_cached_host_layer_keeps_repository_and_directory_precedence(monkeypatch, tmp_path):
    config = tmp_path / "host.toml"
    config.write_text(
        '[config]\nmodel = "host-model"\nhttp_request_timeout = 17.5\n'
        '[pr_reviewer]\nextra_instructions = "host-instructions"\n', encoding="utf-8",
    )
    acquisitions, repo_reads = [], []
    resolve = git_utils._resolve_extra_config_to_file

    def acquire(source):
        acquisitions.append(source)
        return resolve(source)

    def repository_settings():
        repo_reads.append(True)
        return ('[config]\nmodel = "repository-model"\nhttp_request_timeout = 600\n'
                '[pr_reviewer]\nextra_instructions = "repository-instructions"\n')

    provider = SimpleNamespace(get_repo_settings=repository_settings)
    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    monkeypatch.setattr(git_utils, "get_git_provider_with_context", lambda url: provider)
    monkeypatch.setattr(git_utils, "_get_per_directory_settings", lambda provider: [
        ("src/.pr_agent.toml", '[pr_reviewer]\nextra_instructions = "directory-instructions"\n'),
    ])
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}):
        get_settings().set("config.extra_config_url", str(config))
        get_settings().set("config.use_repo_settings_file", True)
        git_utils.apply_host_settings()
        assert not repo_reads
        assert get_settings().get("pr_reviewer.extra_instructions") == "host-instructions"
        for _ in range(2):
            git_utils.apply_repo_settings("https://gitlab.example/group/repo/-/merge_requests/1")
            assert get_settings().get("config.model") == "repository-model"
            assert get_settings().get("pr_reviewer.extra_instructions") == "directory-instructions"
            assert get_settings().get("config.http_request_timeout") == 17.5
            get_settings().set("config.model", "command-model")
    assert acquisitions == [str(config)]
    assert repo_reads == [True]


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


@pytest.mark.parametrize("invocation_scope", [False, True])
@pytest.mark.parametrize("override_pat", [False, True])
def test_nested_cli_reloads_host_state_without_inherited_replay_guards(
    monkeypatch, tmp_path, override_pat, invocation_scope
):
    from starlette_context import context

    from pr_agent import cli

    outer, inner = tmp_path / "outer.toml", tmp_path / "inner.toml"
    outer.write_text('[config]\nmodel = "outer-model"\n', encoding="utf-8")
    inner_content = '[config]\nmodel = "inner-model"\n'
    if override_pat:
        inner_content += '[gitlab]\npersonal_access_token = "inner-test-token"\n'
    inner.write_text(inner_content, encoding="utf-8")
    acquisitions, observed = [], []
    resolve = git_utils._resolve_extra_config_to_file

    def acquire(source):
        acquisitions.append(source)
        return resolve(source)

    class Agent:
        async def handle_request(self, *_args, **_kwargs):
            git_utils.apply_host_settings()
            observed.append((get_settings().get("config.model"),
                             get_settings().get("gitlab.personal_access_token")))
            return True

    monkeypatch.setattr(git_utils, "_resolve_extra_config_to_file", acquire)
    monkeypatch.setattr(cli, "PRAgent", Agent)
    monkeypatch.setattr(cli, "litellm_callbacks_registered", lambda: False)
    monkeypatch.setattr(cli, "inject_artifact_context", lambda: None)
    outer_scope = git_utils.host_settings_scope() if invocation_scope else nullcontext()
    with request_cycle_context({"settings": copy.deepcopy(global_settings)}), outer_scope:
        get_settings().set("config.extra_config_url", str(outer))
        git_utils.apply_host_settings()
        context["authenticated_provider_settings"] = {"gitlab.personal_access_token": "outer-test-token"}
        get_settings().set("gitlab.personal_access_token", "outer-test-token")
        get_settings().set("config.extra_config_url", str(inner))
        cli.run(inargs=["--pr_url=https://example.com/org/repo/pull/1", f"--extra_config_url={inner}", "review"])
        git_utils.apply_host_settings()
        if not invocation_scope:
            assert context["external_host_settings_source"] == str(outer)
        assert context["authenticated_provider_settings"]["gitlab.personal_access_token"] == "outer-test-token"
        assert get_settings().get("config.model") == "outer-model"
        assert get_settings().get("gitlab.personal_access_token") == "outer-test-token"
    expected_pat = "inner-test-token" if override_pat else "outer-test-token"
    assert observed == [("inner-model", expected_pat)]
    assert acquisitions == [str(outer), str(inner)]


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
    assert len(acquisitions) == 1
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
