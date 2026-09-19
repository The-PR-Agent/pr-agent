"""
Tests for [model_routing]: a small pull request gets a cheaper primary model, the configured
fallbacks still follow it, and every other call keeps the model it asked for.
"""
import asyncio

import pytest

from pr_agent.algo.model_routing import count_hunks, route_primary_model
from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_TRACKED_KEYS = (
    "config.model",
    "config.model_weak",
    "config.fallback_models",
    "openai.deployment_id",
    "openai.fallback_deployments",
    "model_routing.enable",
    "model_routing.rules",
)


def _patch(hunks: int) -> str:
    return "".join(f"@@ -{i},1 +{i},2 @@\n line\n+added\n" for i in range(1, hunks + 1))


def _file(name: str, hunks: int, patch: str = None) -> FilePatchInfo:
    return FilePatchInfo(base_file="", head_file="", patch=_patch(hunks) if patch is None else patch, filename=name)


class _Provider:
    def __init__(self, files):
        self.files = files
        self.calls = 0

    def get_diff_files(self):
        self.calls += 1
        return self.files


def _pr(num_files: int, hunks_per_file: int) -> _Provider:
    return _Provider([_file(f"f{i}.py", hunks_per_file) for i in range(num_files)])


@pytest.fixture
def settings():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    s = get_settings()
    s.set("config.model", "primary-model")
    s.set("config.model_weak", "weak-model")
    s.set("config.fallback_models", ["fallback-1"])
    s.set("openai.deployment_id", None)
    s.set("openai.fallback_deployments", [])
    s.set("model_routing.enable", True)
    s.set("model_routing.rules", [
        {"max_hunks": 3, "model": "tiny-model"},
        {"max_hunks": 10, "max_files": 4, "model": "small-model"},
    ])
    yield s
    restore_settings(snapshot)


def _models_tried(git_provider, model_type=ModelType.REGULAR, fail=()):
    calls = []

    async def fake_f(model):
        calls.append(model)
        if model in fail:
            raise RuntimeError(f"{model} failed")
        return model

    asyncio.run(retry_with_fallback_models(fake_f, model_type=model_type, git_provider=git_provider))
    return calls


class TestCountHunks:
    def test_counts_hunk_headers_across_files(self):
        assert count_hunks([_file("a.py", 2), _file("b.py", 3)]) == 5

    def test_patch_without_headers_counts_as_one_hunk(self):
        assert count_hunks([_file("a.py", 0, patch="+whole file\n"), _file("b.py", 0, patch="")]) == 1

    def test_no_files(self):
        assert count_hunks([]) == 0


class TestRouting:
    def test_small_pr_routes_to_the_first_matching_rule(self, settings):
        assert _models_tried(_pr(num_files=1, hunks_per_file=2)) == ["tiny-model"]

    def test_medium_pr_skips_to_the_next_rule(self, settings):
        assert _models_tried(_pr(num_files=2, hunks_per_file=3)) == ["small-model"]

    def test_files_limit_is_checked_too(self, settings):
        # 5 hunks fit the second rule, 5 files do not
        assert _models_tried(_pr(num_files=5, hunks_per_file=1)) == ["primary-model"]

    def test_large_pr_keeps_the_configured_primary(self, settings):
        assert _models_tried(_pr(num_files=3, hunks_per_file=10)) == ["primary-model"]

    def test_configured_fallbacks_follow_the_routed_primary(self, settings):
        calls = _models_tried(_pr(num_files=1, hunks_per_file=1), fail=("tiny-model",))
        assert calls == ["tiny-model", "fallback-1"]

    def test_weak_tier_is_not_routed(self, settings):
        provider = _pr(num_files=1, hunks_per_file=1)
        assert _models_tried(provider, model_type=ModelType.WEAK) == ["weak-model"]
        assert provider.calls == 0

    def test_disabled_routing_does_not_touch_the_provider(self, settings):
        settings.set("model_routing.enable", False)
        provider = _pr(num_files=1, hunks_per_file=1)
        assert _models_tried(provider) == ["primary-model"]
        assert provider.calls == 0

    def test_no_rules_does_not_touch_the_provider(self, settings):
        settings.set("model_routing.rules", [])
        provider = _pr(num_files=1, hunks_per_file=1)
        assert _models_tried(provider) == ["primary-model"]
        assert provider.calls == 0

    def test_call_without_a_git_provider_keeps_the_primary(self, settings):
        assert _models_tried(None) == ["primary-model"]

    def test_rule_without_a_model_or_a_limit_is_ignored(self, settings):
        settings.set("model_routing.rules", [
            {"max_hunks": 3},
            {"model": "catch-all-model"},
            "not-a-rule",
            {"max_hunks": 3, "model": "tiny-model"},
        ])
        assert _models_tried(_pr(num_files=1, hunks_per_file=1)) == ["tiny-model"]

    def test_routed_primary_is_recorded_as_the_model_used(self, settings):
        init_run_details()
        _models_tried(_pr(num_files=1, hunks_per_file=1))
        details = get_run_details()
        assert details.model_used == "tiny-model"
        assert details.fallback_used is False


class TestAzureDeployments:
    def test_rule_without_a_deployment_is_skipped_when_a_deployment_is_configured(self, settings):
        settings.set("openai.deployment_id", "primary-deployment")
        settings.set("model_routing.rules", [
            {"max_hunks": 3, "model": "tiny-model"},
            {"max_hunks": 3, "model": "small-model", "deployment_id": "small-deployment"},
        ])
        routed = route_primary_model(ModelType.REGULAR, _pr(num_files=1, hunks_per_file=1))
        assert routed == ("small-model", "small-deployment")

    def test_rule_deployment_is_used_for_the_call_and_restored_after(self, settings):
        settings.set("openai.deployment_id", "primary-deployment")
        settings.set("model_routing.rules", [
            {"max_hunks": 3, "model": "tiny-model", "deployment_id": "tiny-deployment"},
        ])
        observed = []

        async def fake_f(model):
            observed.append((model, get_settings().get("openai.deployment_id")))
            return model

        asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(num_files=1, hunks_per_file=1)))

        assert observed == [("tiny-model", "tiny-deployment")]
        assert get_settings().get("openai.deployment_id") == "primary-deployment"

    def test_fallback_deployments_stay_paired_with_their_models(self, settings):
        settings.set("openai.deployment_id", "primary-deployment")
        settings.set("openai.fallback_deployments", ["fallback-deployment"])
        settings.set("model_routing.rules", [
            {"max_hunks": 3, "model": "tiny-model", "deployment_id": "tiny-deployment"},
        ])
        observed = []

        async def fake_f(model):
            observed.append((model, get_settings().get("openai.deployment_id")))
            if model == "tiny-model":
                raise RuntimeError("tiny failed")
            return model

        asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(num_files=1, hunks_per_file=1)))

        assert observed == [("tiny-model", "tiny-deployment"), ("fallback-1", "fallback-deployment")]
