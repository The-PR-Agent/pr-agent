from pr_agent.config_loader import global_settings
from pr_agent.settings_validator import (validate_current_config,
                                         validate_settings_overrides)


class TestValidateSettingsOverrides:
    def test_known_section_and_key_produce_no_warnings(self):
        overrides = {"pr_reviewer": {"extra_instructions": "focus on tests"}}
        assert validate_settings_overrides(overrides, global_settings, source="test") == []

    def test_unknown_key_in_known_section_warns(self):
        overrides = {"pr_reviewer": {"extra_instrcutions": "typo"}}
        warnings = validate_settings_overrides(overrides, global_settings, source="test")
        assert len(warnings) == 1
        assert "extra_instrcutions" in warnings[0]
        assert "pr_reviewer" in warnings[0]

    def test_unknown_section_warns(self):
        overrides = {"pr_reviwer": {"extra_instructions": "typo in section"}}
        warnings = validate_settings_overrides(overrides, global_settings, source="test")
        assert len(warnings) == 1
        assert "pr_reviwer" in warnings[0]

    def test_free_form_sections_are_skipped(self):
        overrides = {"custom_labels": {"my_label": {"description": "anything"}},
                     "best_practices": {"anything_goes": True}}
        assert validate_settings_overrides(overrides, global_settings, source="test") == []

    def test_credential_sections_are_skipped(self):
        overrides = {"openai": {"key": "sk-..."}, "gitlab": {"personal_access_token": "glpat"}}
        assert validate_settings_overrides(overrides, global_settings, source="test") == []

    def test_section_casing_is_ignored(self):
        overrides = {"PR_REVIEWER": {"EXTRA_INSTRUCTIONS": "x"}}
        assert validate_settings_overrides(overrides, global_settings, source="test") == []

    def test_non_dict_section_is_skipped(self):
        overrides = {"pr_reviewer": "not-a-dict"}
        assert validate_settings_overrides(overrides, global_settings, source="test") == []


class TestValidateCurrentConfig:
    def _fake_settings(self, values: dict):
        class FakeSettings:
            def get(self, key, default=None):
                return values.get(key, default)

        return FakeSettings()

    def test_default_config_has_no_errors(self):
        result = validate_current_config(global_settings)
        assert result["errors"] == []

    def test_unknown_git_provider_is_error(self):
        settings = self._fake_settings({
            "config.git_provider": "no_such_provider",
            "config.model": "gpt-4o",
            "config.fallback_models": ["gpt-4o"],
        })
        result = validate_current_config(settings)
        assert any("no_such_provider" in e for e in result["errors"])

    def test_unresolvable_model_is_error(self):
        settings = self._fake_settings({
            "config.git_provider": "github",
            "config.model": "not-a-real-model-name",
            "config.fallback_models": [],
        })
        result = validate_current_config(settings)
        assert any("not-a-real-model-name" in e for e in result["errors"])
        assert not any("config.fallback_models is not set" in e for e in result["errors"])

    def test_comma_separated_fallback_models_are_validated_individually(self):
        settings = self._fake_settings({
            "config.git_provider": "github",
            "config.model": "gpt-4o",
            "config.fallback_models": "gpt-4o, gpt-4o-mini",
        })
        result = validate_current_config(settings)
        assert result["errors"] == []

    def test_empty_fallback_models_list_is_allowed(self):
        settings = self._fake_settings({
            "config.git_provider": "github",
            "config.model": "gpt-4o",
            "config.fallback_models": [],
        })
        result = validate_current_config(settings)
        assert result["errors"] == []

    def test_missing_credentials_is_warning_not_error(self):
        settings = self._fake_settings({
            "config.git_provider": "gitlab",
            "config.model": "gpt-4o",
            "config.fallback_models": ["gpt-4o"],
        })
        result = validate_current_config(settings)
        assert result["errors"] == []
        assert any("gitlab" in w for w in result["warnings"])
