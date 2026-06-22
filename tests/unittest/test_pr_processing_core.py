import pytest

import pr_agent.algo.pr_processing as pr_processing
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings


class FakeTokenHandler:
    def __init__(self, prompt_tokens=100):
        self.prompt_tokens = prompt_tokens

    def count_tokens(self, patch):
        return len(patch.split())


class FakeProvider:
    def __init__(self, files):
        self.files = files

    def get_diff_files(self):
        return self.files

    def get_languages(self):
        return {"Python": 100}


def test_generate_full_patch_keeps_remaining_files_when_patch_exceeds_soft_budget():
    settings = get_settings()
    original_verbosity_level = settings.config.verbosity_level
    settings.config.verbosity_level = 0
    token_handler = FakeTokenHandler(prompt_tokens=100)
    file_dict = {
        "small.py": {"patch": "+ small change", "tokens": 10, "edit_type": EDIT_TYPE.MODIFIED},
        "large.py": {"patch": "+ " + "large " * 80, "tokens": 250, "edit_type": EDIT_TYPE.MODIFIED},
        "second_small.py": {"patch": "+ second change", "tokens": 10, "edit_type": EDIT_TYPE.MODIFIED},
    }

    try:
        total_tokens, patches, remaining_files, files_in_patch = pr_processing.generate_full_patch(
            convert_hunks_to_line_numbers=False,
            file_dict=file_dict,
            max_tokens_model=1800,
            remaining_files_list_prev=list(file_dict),
            token_handler=token_handler,
        )

        assert total_tokens > token_handler.prompt_tokens
        assert "## File: 'small.py'" in patches[0]
        assert "## File: 'second_small.py'" in patches[1]
        assert remaining_files == ["large.py"]
        assert files_in_patch == ["small.py", "second_small.py"]
    finally:
        settings.config.verbosity_level = original_verbosity_level


def test_get_all_models_uses_requested_model_type_and_string_fallbacks():
    settings = get_settings()
    original = {
        "model": settings.config.model,
        "model_weak": settings.get("config.model_weak", None),
        "model_reasoning": settings.get("config.model_reasoning", None),
        "fallback_models": settings.get("config.fallback_models", []),
    }
    try:
        settings.config.model = "regular-model"
        settings.config.model_weak = "weak-model"
        settings.config.model_reasoning = "reasoning-model"
        settings.config.fallback_models = "fallback-a, fallback-b"

        assert pr_processing._get_all_models(ModelType.REGULAR) == ["regular-model", "fallback-a", "fallback-b"]
        assert pr_processing._get_all_models(ModelType.WEAK) == ["weak-model", "fallback-a", "fallback-b"]
        assert pr_processing._get_all_models(ModelType.REASONING) == ["reasoning-model", "fallback-a", "fallback-b"]
    finally:
        settings.config.model = original["model"]
        settings.config.model_weak = original["model_weak"]
        settings.config.model_reasoning = original["model_reasoning"]
        settings.config.fallback_models = original["fallback_models"]


def test_get_all_deployments_rejects_short_fallback_deployment_list():
    settings = get_settings()
    original_deployment_id = settings.get("openai.deployment_id", None)
    original_fallback_deployments = settings.get("openai.fallback_deployments", [])
    try:
        settings.set("openai.deployment_id", "primary")
        settings.set("openai.fallback_deployments", ["fallback-a"])

        with pytest.raises(ValueError, match="less than the number of models"):
            pr_processing._get_all_deployments(["model-a", "model-b", "model-c"])
    finally:
        settings.set("openai.deployment_id", original_deployment_id)
        settings.set("openai.fallback_deployments", original_fallback_deployments)


def test_get_pr_multi_diffs_clips_large_patch_when_policy_is_clip(monkeypatch):
    settings = get_settings()
    original = {
        "patch_extra_lines_before": settings.config.patch_extra_lines_before,
        "patch_extra_lines_after": settings.config.patch_extra_lines_after,
        "large_patch_policy": settings.config.get("large_patch_policy", "skip"),
        "verbosity_level": settings.config.verbosity_level,
    }
    settings.config.patch_extra_lines_before = 0
    settings.config.patch_extra_lines_after = 0
    settings.config.large_patch_policy = "clip"
    settings.config.verbosity_level = 0

    file_info = FilePatchInfo(
        base_file="old\n",
        head_file="new\n",
        patch="@@ -1 +1 @@\n-old\n+" + ("new " * 200),
        filename="large.py",
        edit_type=EDIT_TYPE.MODIFIED,
    )
    provider = FakeProvider([file_info])
    token_handler = FakeTokenHandler(prompt_tokens=100)

    monkeypatch.setattr(pr_processing, "sort_files_by_main_languages", lambda languages, files: [{"files": files}])
    monkeypatch.setattr(pr_processing, "get_max_tokens", lambda model: 1700)
    monkeypatch.setattr(pr_processing, "clip_tokens", lambda patch, *args, **kwargs: "clipped patch")

    try:
        diffs = pr_processing.get_pr_multi_diffs(
            provider, token_handler, "tiny-model", max_calls=2, add_line_numbers=False
        )

        assert diffs == ["clipped patch"]
    finally:
        settings.config.patch_extra_lines_before = original["patch_extra_lines_before"]
        settings.config.patch_extra_lines_after = original["patch_extra_lines_after"]
        settings.config.large_patch_policy = original["large_patch_policy"]
        settings.config.verbosity_level = original["verbosity_level"]
