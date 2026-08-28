from os.path import abspath, dirname, join
from pathlib import Path
from typing import Optional

from dynaconf import Dynaconf
from starlette_context import context

PR_AGENT_TOML_KEY = 'pr-agent'

current_dir = dirname(abspath(__file__))

dynconf_kwargs = {'core_loaders': [], # DISABLE default loaders, otherwise will load toml files more than once.
                           'loaders': ['pr_agent.custom_merge_loader', 'dynaconf.loaders.env_loader'], # Use a custom loader to merge sections, but overwrite their overlapping values. Also support ENV variables to take precedence.
                           'root_path': join(current_dir, "settings"), #Used for Dynaconf.find_file() - So that root path points to settings folder, since we disabled all core loaders.
                           'merge_enabled': True  # In case more than one file is sent, merge them. Must be set to True, otherwise, a .toml file with section [XYZ] overwrites the entire section of a previous .toml file's [XYZ] and we want it to only overwrite the overlapping fields under such section
                           }
global_settings = Dynaconf(
    envvar_prefix=False,
    load_dotenv=False,  # Security: Don't load .env files
    settings_files=[join(current_dir, f) for f in [
        "settings/configuration.toml",
        "settings/ignore.toml",
        "settings/generated_code_ignore.toml",
        "settings/language_extensions.toml",
        "settings/pr_reviewer_prompts.toml",
        "settings/pr_questions_prompts.toml",
        "settings/pr_line_questions_prompts.toml",
        "settings/pr_description_prompts.toml",
        "settings/code_suggestions/pr_code_suggestions_prompts.toml",
        "settings/code_suggestions/pr_code_suggestions_prompts_not_decoupled.toml",
        "settings/code_suggestions/pr_code_suggestions_reflect_prompts.toml",
        "settings/pr_information_from_user_prompts.toml",
        "settings/pr_update_changelog_prompts.toml",
        "settings/pr_custom_labels.toml",
        "settings/pr_add_docs.toml",
        "settings/custom_labels.toml",
        "settings/pr_help_prompts.toml",
        "settings/pr_help_docs_prompts.toml",
        "settings/pr_help_docs_headings_prompts.toml",
        "settings/.secrets.toml",
        "settings_prod/.secrets.toml",
    ]],
    **dynconf_kwargs
)


def get_settings(use_context=False):
    """
    Retrieves the current settings.

    This function attempts to fetch the settings from the starlette_context's context object. If it fails,
    it defaults to the global settings defined outside of this function.

    Returns:
        Dynaconf: The current settings object, either from the context or the global default.
    """
    try:
        return context["settings"]
    except Exception:
        return global_settings


# Add local configuration from pyproject.toml of the project being reviewed
def _find_repository_root() -> Optional[Path]:
    """
    Identify project root directory by recursively searching for the .git directory in the parent directories.
    """
    cwd = Path.cwd().resolve()
    no_way_up = False
    while not no_way_up:
        no_way_up = cwd == cwd.parent
        if (cwd / ".git").is_dir():
            return cwd
        cwd = cwd.parent
    return None


def _find_pyproject() -> Optional[Path]:
    """
    Search for file pyproject.toml in the repository root.
    """
    repo_root = _find_repository_root()
    if repo_root:
        pyproject = repo_root / "pyproject.toml"
        return pyproject if pyproject.is_file() else None
    return None


pyproject_path = _find_pyproject()
if pyproject_path is not None:
    get_settings().load_file(pyproject_path, env=f'tool.{PR_AGENT_TOML_KEY}')


def apply_secrets_manager_config():
    """
    Retrieve configuration from AWS Secrets Manager and override existing settings
    """
    try:
        # Dynamic imports to avoid circular dependency (secret_providers imports config_loader)
        from pr_agent.secret_providers import get_secret_provider
        from pr_agent.log import get_logger

        secret_provider = get_secret_provider()
        if not secret_provider:
            return

        if (hasattr(secret_provider, 'get_all_secrets') and
            get_settings().get("CONFIG.SECRET_PROVIDER") == 'aws_secrets_manager'):
            try:
                secrets = secret_provider.get_all_secrets()
                if secrets:
                    apply_secrets_to_config(secrets)
                    get_logger().info("Applied AWS Secrets Manager configuration")
            except Exception as e:
                get_logger().error(f"Failed to apply AWS Secrets Manager config: {e}")
    except Exception as e:
        try:
            from pr_agent.log import get_logger
            get_logger().debug(f"Secret provider not configured: {e}")
        except:
            # Fail completely silently if log module is not available
            pass


def apply_secrets_to_config(secrets: dict):
    """
    Apply secret dictionary to configuration
    """
    try:
        # Dynamic import to avoid potential circular dependency
        from pr_agent.log import get_logger
    except:
        def get_logger():
            class DummyLogger:
                def debug(self, msg): pass
            return DummyLogger()

    for key, value in secrets.items():
        if '.' in key:  # nested key like "openai.key"
            parts = key.split('.')
            if len(parts) == 2:
                section, setting = parts
                section_upper = section.upper()
                setting_upper = setting.upper()

                # Set only when no existing value (prioritize environment variables)
                current_value = get_settings().get(f"{section_upper}.{setting_upper}")
                if current_value is None or current_value == "":
                    get_settings().set(f"{section_upper}.{setting_upper}", value)
                    get_logger().debug(f"Set {section}.{setting} from AWS Secrets Manager")
