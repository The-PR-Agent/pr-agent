"""
Comprehensive unit tests for Dynaconf fresh_vars functionality.

These tests verify that the fresh_vars feature works correctly with the custom_merge_loader,
particularly for the GitLab credentials use case where values should be reloaded from disk
on each access rather than being cached.

The tests are designed to detect if fresh_vars is broken due to custom loader changes,
such as those introduced in https://github.com/the-pr-agent/pr-agent/pull/2087.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from dynaconf import Dynaconf

# Import get_settings at module level to complete the import chain and avoid circular import issues
# This ensures pr_agent.config_loader is fully loaded before custom_merge_loader is used in tests
from pr_agent.config_loader import get_settings  # noqa: F401


# Module-level helper function
def create_dynaconf_with_custom_loader(temp_dir, secrets_file):
    """
    Create a Dynaconf instance matching the production configuration.

    This mimics the config_loader.py setup with:
    - core_loaders disabled
    - custom_merge_loader and env_loader enabled
    - merge_enabled = True

    Note: fresh_vars should be configured via FRESH_VARS_FOR_DYNACONF environment variable,
    which is the only way to configure it in pr-agent.

    Args:
        temp_dir: Temporary directory path
        secrets_file: Path to secrets file

    Returns:
        Dynaconf instance configured like production
    """
    return Dynaconf(
        core_loaders=[],
        loaders=["pr_agent.custom_merge_loader", "dynaconf.loaders.env_loader"],
        root_path=temp_dir,
        merge_enabled=True,
        envvar_prefix=False,
        load_dotenv=False,
        settings_files=[str(secrets_file)],
    )


class TestFreshVarsGitLabScenario:
    """
    Test fresh_vars functionality for the GitLab credentials use case.

    This class tests the specific scenario where:
    - FRESH_VARS_FOR_DYNACONF='["GITLAB"]' is set
    - .secrets.toml contains gitlab.personal_access_token and gitlab.shared_secret
    - Values should be reloaded from disk on each access (not cached)
    """

    def setup_method(self):
        """Set up temporary directory and files for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / ".secrets.toml"

    def teardown_method(self):
        """Clean up temporary files after each test."""
        import shutil

        if hasattr(self, "temp_dir") and Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    def create_secrets_toml(self, personal_access_token="initial_token", shared_secret="initial_secret"):
        """
        Create a .secrets.toml file with GitLab credentials.

        Args:
            personal_access_token: The GitLab personal access token value
            shared_secret: The GitLab shared secret value
        """
        content = f"""[gitlab]
personal_access_token = "{personal_access_token}"
shared_secret = "{shared_secret}"
"""
        self.secrets_file.write_text(content)

    def test_gitlab_personal_access_token_reload(self):
        """
        Test that gitlab.personal_access_token is reloaded when marked as fresh.

        This is the critical test for the user's use case. It verifies that:
        1. Initial value is loaded correctly
        2. After modifying the file, the new value is returned (not cached)
        3. This works with the custom_merge_loader
        """
        # Create initial secrets file
        self.create_secrets_toml(personal_access_token="token_v1", shared_secret="secret_v1")

        # Set FRESH_VARS_FOR_DYNACONF environment variable (the only way to configure fresh_vars in pr-agent)
        with patch.dict(os.environ, {"FRESH_VARS_FOR_DYNACONF": '["GITLAB"]'}):
            # Create Dynaconf with GITLAB marked as fresh via env var
            settings = create_dynaconf_with_custom_loader(self.temp_dir, self.secrets_file)

            # First access - should return initial value
            first_token = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert first_token == "token_v1", "Initial personal_access_token should be 'token_v1'"

        # Modify the secrets file
        self.create_secrets_toml(personal_access_token="token_v2_updated", shared_secret="secret_v1")

        # Second access - should return NEW value (not cached)
        second_token = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert second_token == "token_v2_updated", (
            "After file modification, personal_access_token should be reloaded to 'token_v2_updated'"
        )

        # Verify the values are different (fresh_vars working)
        assert first_token != second_token, "fresh_vars should cause values to be reloaded, not cached"

    def test_gitlab_multiple_fields_reload(self):
        """
        Test that both gitlab fields reload together when GITLAB is marked as fresh.

        This verifies that fresh_vars works correctly when multiple fields
        in the same section are modified simultaneously.
        """
        # Create initial secrets file
        self.create_secrets_toml(personal_access_token="token_v1", shared_secret="secret_v1")

        # Set FRESH_VARS_FOR_DYNACONF environment variable
        with patch.dict(os.environ, {"FRESH_VARS_FOR_DYNACONF": '["GITLAB"]'}):
            # Create Dynaconf with GITLAB marked as fresh via env var
            settings = create_dynaconf_with_custom_loader(self.temp_dir, self.secrets_file)

            # First access - both fields
            first_token = settings.GITLAB.PERSONAL_ACCESS_TOKEN
            first_secret = settings.GITLAB.SHARED_SECRET
            assert first_token == "token_v1"
            assert first_secret == "secret_v1"

            # Modify both fields in the secrets file
            self.create_secrets_toml(
                personal_access_token="token_v2_both_updated", shared_secret="secret_v2_both_updated"
            )

            # Second access - both fields should be updated
            second_token = settings.GITLAB.PERSONAL_ACCESS_TOKEN
            second_secret = settings.GITLAB.SHARED_SECRET

            assert second_token == "token_v2_both_updated", "personal_access_token should be reloaded"
            assert second_secret == "secret_v2_both_updated", "shared_secret should be reloaded"

            # Verify both fields were reloaded
            assert first_token != second_token, "personal_access_token should not be cached"
            assert first_secret != second_secret, "shared_secret should not be cached"


class TestFreshVarsCustomLoaderIntegration:
    """
    Test fresh_vars integration with custom_merge_loader.

    These tests verify that fresh_vars works correctly when using the
    custom_merge_loader instead of Dynaconf's default core loaders.
    """

    def setup_method(self):
        """Set up temporary directory and files for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / ".secrets.toml"

    def teardown_method(self):
        """Clean up temporary files after each test."""
        import shutil

        if hasattr(self, "temp_dir") and Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    def create_secrets_toml(self, personal_access_token="initial_token", shared_secret="initial_secret"):
        """Create a .secrets.toml file with GitLab credentials."""
        content = f"""[gitlab]
personal_access_token = "{personal_access_token}"
shared_secret = "{shared_secret}"
"""
        self.secrets_file.write_text(content)

    def test_fresh_vars_without_core_loaders(self):
        """
        Critical test: Verify fresh_vars works when core_loaders are disabled.

        This test detects if the bug exists where fresh_vars stops working
        when core_loaders=[] is set. This is the key issue that may have been
        introduced by the custom_merge_loader changes.

        Expected behavior:
        - If fresh_vars works: second_value != first_value
        - If fresh_vars is broken: second_value == first_value (cached)
        """
        # Create initial secrets file
        self.create_secrets_toml(personal_access_token="token_before_bug_test")

        # Set FRESH_VARS_FOR_DYNACONF environment variable
        with patch.dict(os.environ, {"FRESH_VARS_FOR_DYNACONF": '["GITLAB"]'}):
            # Create Dynaconf WITHOUT core loaders but WITH fresh_vars via env var
            settings = create_dynaconf_with_custom_loader(self.temp_dir, self.secrets_file)

            # First access
            first_value = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert first_value == "token_before_bug_test", "Initial value should be loaded correctly"

        # Modify the file
        self.create_secrets_toml(personal_access_token="token_after_bug_test")

        # Second access - THIS IS THE CRITICAL CHECK
        second_value = settings.GITLAB.PERSONAL_ACCESS_TOKEN

        # If this assertion fails, fresh_vars is broken with custom_merge_loader
        assert second_value == "token_after_bug_test", (
            "CRITICAL: fresh_vars should reload the value even with core_loaders=[]"
        )

        assert first_value != second_value, "CRITICAL: Values should be different, indicating fresh_vars is working"

    def test_custom_loader_respects_fresh_vars(self):
        """
        Test that custom_merge_loader respects the fresh_vars configuration.

        Verifies that when a section is marked as fresh, the custom loader
        doesn't cache values from that section.
        """
        # Create initial secrets file with multiple sections
        content = """[gitlab]
personal_access_token = "gitlab_token_v1"

[github]
user_token = "github_token_v1"
"""
        self.secrets_file.write_text(content)

        # Set FRESH_VARS_FOR_DYNACONF environment variable (only GITLAB)
        with patch.dict(os.environ, {"FRESH_VARS_FOR_DYNACONF": '["GITLAB"]'}):
            # Create Dynaconf with only GITLAB marked as fresh via env var
            settings = create_dynaconf_with_custom_loader(self.temp_dir, self.secrets_file)

            # Access both sections
            gitlab_token_1 = settings.GITLAB.PERSONAL_ACCESS_TOKEN
            github_token_1 = settings.GITHUB.USER_TOKEN

            # Modify both sections
            content = """[gitlab]
personal_access_token = "gitlab_token_v2"

[github]
user_token = "github_token_v2"
"""
            self.secrets_file.write_text(content)

            # Access again
            gitlab_token_2 = settings.GITLAB.PERSONAL_ACCESS_TOKEN
            github_token_2 = settings.GITHUB.USER_TOKEN

            # GITLAB should be reloaded (marked as fresh)
            assert gitlab_token_2 == "gitlab_token_v2", "GITLAB section should be reloaded (marked as fresh)"
            assert gitlab_token_1 != gitlab_token_2, "GITLAB values should not be cached"

            # GITHUB should be cached (not marked as fresh)
            assert github_token_2 == "github_token_v1", "GITHUB section should be cached (not marked as fresh)"
            assert github_token_1 == github_token_2, "GITHUB values should be cached"


class TestFreshVarsBasicFunctionality:
    """
    Test basic fresh_vars functionality and edge cases.

    These tests verify fundamental fresh_vars behavior and ensure
    the feature works as expected in various scenarios.
    """

    def setup_method(self):
        """Set up temporary directory and files for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.secrets_file = Path(self.temp_dir) / ".secrets.toml"

    def teardown_method(self):
        """Clean up temporary files after each test."""
        import shutil

        if hasattr(self, "temp_dir") and Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)

    def create_secrets_toml(self, personal_access_token="initial_token"):
        """Create a .secrets.toml file with GitLab credentials."""
        content = f"""[gitlab]
personal_access_token = "{personal_access_token}"
"""
        self.secrets_file.write_text(content)

    def test_gitlab_credentials_not_cached_when_fresh(self):
        """
        Test that GitLab credentials are not cached when marked as fresh.

        This verifies the core requirement: when GITLAB is in fresh_vars,
        accessing the credentials multiple times should reload from disk
        each time, not return a cached value.
        """
        # Create initial secrets file
        self.create_secrets_toml(personal_access_token="no_cache_v1")

        # Set FRESH_VARS_FOR_DYNACONF environment variable
        with patch.dict(os.environ, {"FRESH_VARS_FOR_DYNACONF": '["GITLAB"]'}):
            # Create Dynaconf with GITLAB marked as fresh via env var
            settings = create_dynaconf_with_custom_loader(self.temp_dir, self.secrets_file)

            # Access the token multiple times before modification
            access_1 = settings.GITLAB.PERSONAL_ACCESS_TOKEN
            access_2 = settings.GITLAB.PERSONAL_ACCESS_TOKEN
            access_3 = settings.GITLAB.PERSONAL_ACCESS_TOKEN

        # All should return the same value (file hasn't changed)
        assert access_1 == access_2 == access_3 == "no_cache_v1", (
            "Multiple accesses before modification should return same value"
        )

        # Modify the file
        self.create_secrets_toml(personal_access_token="no_cache_v2")

        # Access again - should get new value immediately
        access_4 = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert access_4 == "no_cache_v2", "First access after modification should return new value"

        # Verify no caching occurred
        assert access_1 != access_4, "Value should change after file modification (no caching)"

        # Modify again
        self.create_secrets_toml(personal_access_token="no_cache_v3")

        # Access again - should get newest value
        access_5 = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert access_5 == "no_cache_v3", "Second modification should also be detected"

        # Verify the progression
        assert access_1 != access_4 != access_5, "Each modification should result in a different value (no caching)"

    def test_fresh_vars_works_with_default_loaders(self):
        """
        Test that fresh_vars works correctly with Dynaconf's default core loaders.

        This is a control test to prove that fresh_vars functionality works
        as expected when using the standard Dynaconf configuration (with core_loaders).
        This helps isolate the bug to the custom_merge_loader configuration.
        """
        # Create initial secrets file
        self.create_secrets_toml(personal_access_token="default_v1")

        # Create Dynaconf with DEFAULT loaders (not custom_merge_loader)
        settings = Dynaconf(
            # Use default core_loaders (don't disable them)
            root_path=self.temp_dir,
            merge_enabled=True,
            envvar_prefix=False,
            load_dotenv=False,
            settings_files=[str(self.secrets_file)],
            fresh_vars=["GITLAB"],
        )

        # First access
        first_value = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert first_value == "default_v1"

        # Modify file
        self.create_secrets_toml(personal_access_token="default_v2")

        # Second access - should be reloaded with default loaders
        second_value = settings.GITLAB.PERSONAL_ACCESS_TOKEN
        assert second_value == "default_v2", (
            "With default loaders, fresh_vars SHOULD work correctly. "
            "If this test fails, the issue is not specific to custom_merge_loader."
        )

        assert first_value != second_value, "Values should be different when using default loaders with fresh_vars"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
