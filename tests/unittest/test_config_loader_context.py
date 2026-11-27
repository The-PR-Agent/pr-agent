import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette_context import context, request_cycle_context

from pr_agent.config_loader import get_settings, global_settings, safe_request_cycle_context
from pr_agent.servers import lambda_handoff


def test_get_settings_can_bypass_request_context():
    request_settings = copy.deepcopy(global_settings)

    with request_cycle_context({"settings": request_settings}):
        assert get_settings() is request_settings
        unscoped = get_settings(use_context=False)

    assert unscoped is global_settings


def test_get_settings_uses_global_settings_without_context():
    assert get_settings() is global_settings
    assert get_settings(use_context=False) is global_settings


class _CustomBaseSignal(BaseException):
    pass


@pytest.mark.parametrize("exc_cls", [RuntimeError, _CustomBaseSignal])
def test_safe_request_cycle_context_resets_and_restores_outer_scope_on_exception(exc_cls):
    outer_settings = copy.deepcopy(global_settings)
    inner_settings = copy.deepcopy(global_settings)
    caught = None

    with safe_request_cycle_context({"settings": outer_settings, "marker": "outer"}):
        try:
            with safe_request_cycle_context({"settings": inner_settings, "marker": "inner"}):
                context["leaked"] = True
                raise exc_cls("abort")
        except (RuntimeError, _CustomBaseSignal) as exc:
            caught = exc

        assert type(caught) is exc_cls
        assert get_settings() is outer_settings
        assert context["marker"] == "outer"
        assert "leaked" not in context

    assert not context.exists()
    assert get_settings() is global_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_lambda_handoff_resets_request_context_when_handler_raises(monkeypatch, provider):
    baseline_instructions = get_settings().pr_reviewer.extra_instructions
    servers_pkg = sys.modules["pr_agent.servers"]
    if provider == "github":
        async def fail_github(*_args, **_kwargs):
            get_settings().pr_reviewer.extra_instructions = "handoff-leaked"
            context["repo_settings"] = {"leaked": True}
            raise RuntimeError("transient github failure")

        fake_github_app = SimpleNamespace(handle_request=fail_github)
        monkeypatch.setitem(sys.modules, "pr_agent.servers.github_app", fake_github_app)
        monkeypatch.setattr(servers_pkg, "github_app", fake_github_app, raising=False)
        event = lambda_handoff.build_handoff_event(
            "github", {"installation": {"id": 99}}, {"x-github-event": "pull_request"}
        )
    else:
        async def fail_gitlab(*_args, **_kwargs):
            get_settings().pr_reviewer.extra_instructions = "handoff-leaked"
            context["repo_settings"] = {"leaked": True}
            raise RuntimeError("transient gitlab failure")

        fake_gitlab_webhook = SimpleNamespace(
            restore_gitlab_handoff_auth=AsyncMock(),
            process_verified_gitlab_delivery=fail_gitlab,
            gitlab_delivery_id=lambda headers: None,
        )
        monkeypatch.setitem(sys.modules, "pr_agent.servers.gitlab_webhook", fake_gitlab_webhook)
        monkeypatch.setattr(servers_pkg, "gitlab_webhook", fake_gitlab_webhook, raising=False)
        event = lambda_handoff.build_handoff_event(
            "gitlab", {"object_kind": "merge_request"}, {"x-gitlab-event-uuid": "u1"}
        )

    with pytest.raises(RuntimeError, match="transient"):
        await lambda_handoff._run_handoff(lambda_handoff.extract_handoff(event))

    assert not context.exists()
    assert get_settings() is global_settings
    assert get_settings().pr_reviewer.extra_instructions == baseline_instructions
