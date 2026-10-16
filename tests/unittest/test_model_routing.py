"""
Tests for [model_routing]: a small pull request gets a cheaper primary model, the configured
fallbacks still follow it, and every other call keeps the model it asked for.
"""
import asyncio

import pytest

from pr_agent.algo.model_routing import count_hunks, route_primary_model
from pr_agent.algo.pr_processing import (
    FallbackEligibleError,
    get_effective_fallback_chain,
    retry_with_fallback_models,
)
from pr_agent.algo.run_details import get_run_details, init_run_details
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import SENTINEL, restore_settings, snapshot_settings

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
            raise FallbackEligibleError(f"{model} failed")
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

    def test_routed_model_repeating_in_fallbacks_keeps_distinct_fallbacks_first(self, settings):
        settings.set("config.fallback_models", ["tiny-model", "fallback-1"])
        calls = _models_tried(_pr(num_files=1, hunks_per_file=1), fail=("tiny-model",))
        assert calls == ["tiny-model", "fallback-1"]

    def test_routed_collision_recovers_with_the_displaced_primary(self, settings):
        settings.set("config.fallback_models", ["tiny-model"])
        init_run_details()
        calls = _models_tried(_pr(num_files=1, hunks_per_file=1), fail=("tiny-model",))
        assert calls == ["tiny-model", "primary-model"]
        assert get_run_details().model_used == "primary-model"
        assert get_run_details().fallback_used is True

    def test_successful_routed_collision_does_not_attempt_fallbacks(self, settings):
        settings.set("config.fallback_models", ["tiny-model"])
        init_run_details()
        assert _models_tried(_pr(num_files=1, hunks_per_file=1)) == ["tiny-model"]
        assert get_run_details().model_used == "tiny-model"
        assert get_run_details().fallback_used is False

    @pytest.mark.parametrize("deployment", [SENTINEL, None, ""])
    @pytest.mark.parametrize("fallback_deployments", [SENTINEL, None, [], "", [None], [""], " "])
    def test_empty_deployments_match_routed_endpoints(self, settings, deployment, fallback_deployments):
        restore_settings({"openai.deployment_id": deployment,
                          "openai.fallback_deployments": fallback_deployments})
        settings.set("config.fallback_models", ["tiny-model"])
        original_deployment = settings.get("openai.deployment_id")
        previous_chain = get_effective_fallback_chain()
        expected = (("tiny-model", None), ("primary-model", None))
        observed = []

        async def fake_f(model):
            observed.append((model, settings.get("openai.deployment_id")))
            assert get_effective_fallback_chain() == expected
            raise FallbackEligibleError("endpoint failed")

        with pytest.raises(Exception, match="Failed to generate prediction with any model"):
            asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(1, 1)))

        assert observed == list(expected)
        assert settings.get("openai.deployment_id") == original_deployment
        assert get_effective_fallback_chain() == previous_chain

    @pytest.mark.parametrize("outcome", ["routed-success", "primary-success", "exhausted", "fatal"])
    @pytest.mark.parametrize("fallback_deployments", [["", None, ""], ",,"])
    def test_mixed_empty_deployments_preserve_primary_and_restore_context(self, settings, outcome,
                                                                        fallback_deployments):
        settings.set("openai.deployment_id", "")
        settings.set("config.fallback_models", ["tiny-model", "primary-model", "tiny-model"])
        settings.set("openai.fallback_deployments", fallback_deployments)
        expected = (("tiny-model", None), ("primary-model", None))
        previous_chain = get_effective_fallback_chain()
        observed = []

        async def fake_f(model):
            observed.append((model, settings.get("openai.deployment_id")))
            assert get_effective_fallback_chain() == expected
            if outcome == "fatal":
                raise ValueError("not eligible for fallback")
            if outcome == "exhausted" or (outcome == "primary-success" and model == "tiny-model"):
                raise FallbackEligibleError("endpoint failed")
            return model

        if outcome in ("fatal", "exhausted"):
            error = ValueError if outcome == "fatal" else Exception
            with pytest.raises(error):
                asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(1, 1)))
        else:
            result = asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(1, 1)))
            assert result == ("tiny-model" if outcome == "routed-success" else "primary-model")

        assert observed == list(expected[:1] if outcome in ("fatal", "routed-success") else expected)
        assert settings.get("openai.deployment_id") == ""
        assert get_effective_fallback_chain() == previous_chain

    @pytest.mark.parametrize("routed", [True, False])
    @pytest.mark.parametrize("deployment", [None, "", " "])
    @pytest.mark.parametrize("fallbacks", [[], ["fallback-1"]])
    def test_noncolliding_chains_keep_raw_deployments(self, settings, routed, deployment, fallbacks):
        settings.set("openai.deployment_id", "")
        settings.set("config.fallback_models", fallbacks)
        settings.set("openai.fallback_deployments", [deployment])
        settings.set("model_routing.enable", routed)
        expected = (("tiny-model", None) if routed else ("primary-model", ""),)
        if fallbacks:
            expected += (("fallback-1", deployment),)
        observed = []

        async def fake_f(model):
            observed.append((model, settings.get("openai.deployment_id")))
            assert get_effective_fallback_chain() == expected
            raise FallbackEligibleError("endpoint failed")

        with pytest.raises(Exception, match="Failed to generate prediction with any model"):
            asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(1, 1)))

        assert observed == list(expected)
        assert settings.get("openai.deployment_id") == ""

    @pytest.mark.parametrize("fallbacks, routed, expected", [
        ([("tiny-model", "tiny-deployment")], ("tiny-model", "tiny-deployment"),
         [("tiny-model", "tiny-deployment"), ("primary-model", "primary-deployment")]),
        ([("fallback-1", "other"), ("tiny-model", "tiny-deployment"), ("fallback-1", "other")],
         ("tiny-model", "tiny-deployment"),
         [("tiny-model", "tiny-deployment"), ("fallback-1", "other"), ("primary-model", "primary-deployment")]),
        ([("tiny-model", "tiny-deployment"), ("primary-model", "primary-deployment"), ("fallback-1", "other")],
         ("tiny-model", "tiny-deployment"),
         [("tiny-model", "tiny-deployment"), ("primary-model", "primary-deployment"), ("fallback-1", "other")]),
        ([("primary-model", "primary-deployment")], ("primary-model", "primary-deployment"),
         [("primary-model", "primary-deployment")]),
        ([("tiny-model", "other-deployment")], ("tiny-model", "tiny-deployment"),
         [("tiny-model", "tiny-deployment"), ("tiny-model", "other-deployment")]),
        ([], ("tiny-model", "tiny-deployment"), [("tiny-model", "tiny-deployment")]),
        ([("fallback-1", "other"), ("fallback-1", "other")], ("tiny-model", "tiny-deployment"),
         [("tiny-model", "tiny-deployment"), ("fallback-1", "other"), ("fallback-1", "other")]),
    ])
    def test_routed_chain_exhaustion_preserves_endpoint_order_and_restores_context(self, settings,
                                                                                  fallbacks, routed, expected):
        settings.set("openai.deployment_id", "primary-deployment")
        settings.set("config.fallback_models", [model for model, _ in fallbacks])
        # Surplus deployments are ignored, not attached to the restored primary.
        settings.set("openai.fallback_deployments", [deployment for _, deployment in fallbacks] + ["surplus"])
        settings.set("model_routing.rules", [
            {"max_hunks": 3, "model": routed[0], "deployment_id": routed[1]},
        ])
        observed = []
        previous_chain = get_effective_fallback_chain()

        async def fake_f(model):
            observed.append((model, get_settings().get("openai.deployment_id")))
            assert get_effective_fallback_chain() == tuple(expected)
            raise FallbackEligibleError("endpoint failed")

        with pytest.raises(Exception, match="Failed to generate prediction with any model") as error:
            asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(num_files=1, hunks_per_file=1)))

        assert observed == expected
        assert f"any model of {[model for model, _ in expected]}" in str(error.value)
        assert get_settings().get("openai.deployment_id") == "primary-deployment"
        assert get_effective_fallback_chain() == previous_chain

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
                raise FallbackEligibleError("tiny failed")
            return model

        asyncio.run(retry_with_fallback_models(fake_f, git_provider=_pr(num_files=1, hunks_per_file=1)))

        assert observed == [("tiny-model", "tiny-deployment"), ("fallback-1", "fallback-deployment")]


class TestLazyHunkCounting:
    @pytest.fixture
    def counted_hunks(self, monkeypatch):
        from pr_agent.algo import model_routing

        calls = []

        def tracked(files):
            calls.append(files)
            return count_hunks(files)

        monkeypatch.setattr(model_routing, "count_hunks", tracked)
        return calls

    def test_file_only_rule_never_counts_hunks(self, settings, counted_hunks):
        settings.set("model_routing.rules", [
            {"max_files": 2, "model": "file-model"},
        ])
        provider = _pr(num_files=1, hunks_per_file=100)

        assert route_primary_model(ModelType.REGULAR, provider) == ("file-model", None)
        assert counted_hunks == []
        assert provider.calls == 1

    def test_file_limit_rejects_all_rules_without_scanning_patches(self, settings, counted_hunks):
        settings.set("model_routing.rules", [
            {"max_files": 1, "max_hunks": 100, "model": "tiny-model"},
            {"max_files": 2, "max_hunks": 100, "model": "small-model"},
        ])

        assert route_primary_model(ModelType.REGULAR, _pr(num_files=3, hunks_per_file=100)) is None
        assert counted_hunks == []

    def test_hunks_counted_once_only_after_file_limit_passes(self, settings, counted_hunks):
        settings.set("model_routing.rules", [
            {"max_files": 1, "max_hunks": 100, "model": "wrong-model"},
            {"max_files": 3, "max_hunks": 1, "model": "too-small"},
            {"max_hunks": 6, "model": "right-model"},
        ])
        provider = _pr(num_files=2, hunks_per_file=2)

        assert route_primary_model(ModelType.REGULAR, provider) == ("right-model", None)
        assert len(counted_hunks) == 1
        assert provider.calls == 1

    def test_zero_hunks_are_counted_only_once(self, settings, counted_hunks):
        settings.set("model_routing.rules", [
            {"max_hunks": -1, "model": "not-matching"},
            {"max_hunks": 0, "model": "empty-model"},
        ])

        assert route_primary_model(ModelType.REGULAR, _pr(num_files=0, hunks_per_file=0)) == (
            "empty-model", None
        )
        assert len(counted_hunks) == 1

    def test_skips_invalid_rules_without_counting_hunks(self, settings, counted_hunks):
        settings.set("model_routing.rules", [
            "not-a-rule",
            {"model": "bad-limit", "max_hunks": "invalid"},
            {"max_files": 1, "model": "file-model"},
        ])

        assert route_primary_model(ModelType.REGULAR, _pr(num_files=1, hunks_per_file=3)) == (
            "file-model", None
        )
        assert counted_hunks == []

    def test_deployment_ineligible_rules_do_not_scan_hunks(self, settings, counted_hunks, monkeypatch):
        from unittest.mock import MagicMock

        from pr_agent.algo import model_routing

        logger = MagicMock()
        monkeypatch.setattr(model_routing, "get_logger", lambda: logger)
        settings.set("openai.deployment_id", "primary-deployment")
        settings.set("model_routing.rules", [
            {"max_files": 2, "max_hunks": 100, "model": "missing-deployment"},
        ])
        provider = _pr(num_files=1, hunks_per_file=10)

        assert route_primary_model(ModelType.REGULAR, provider) is None
        assert counted_hunks == []
        assert provider.calls == 1
        logger.warning.assert_called_once()
        assert "no deployment_id" in logger.warning.call_args.args[0]

    def test_deployment_eligible_later_rule_counts_hunks_once(self, settings, counted_hunks):
        settings.set("openai.deployment_id", "primary-deployment")
        settings.set("model_routing.rules", [
            {"max_hunks": 100, "model": "missing-deployment"},
            {"max_hunks": 1, "deployment_id": "small-deployment", "model": "too-small"},
            {"max_hunks": 5, "deployment_id": "large-deployment", "model": "large-model"},
        ])
        provider = _pr(num_files=1, hunks_per_file=3)

        assert route_primary_model(ModelType.REGULAR, provider) == ("large-model", "large-deployment")
        assert len(counted_hunks) == 1
        assert provider.calls == 1
