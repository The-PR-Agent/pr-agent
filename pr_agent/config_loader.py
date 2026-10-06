import copy
import tomllib
from os.path import abspath, dirname, join
from pathlib import Path
from typing import Optional

from dynaconf import Dynaconf
from dynaconf.loaders import env_loader
from starlette_context import context

from pr_agent.config_security import filter_repo_host_only_keys

PR_AGENT_TOML_KEY = 'pr-agent'

current_dir = dirname(abspath(__file__))

dynconf_kwargs = {'core_loaders': [],  # DISABLE default loaders, otherwise will load toml files more than once.
                           # Use a custom loader to merge sections, but overwrite their overlapping
                           # values. Also support ENV variables to take precedence.
                           'loaders': ['pr_agent.custom_merge_loader', 'dynaconf.loaders.env_loader'],
                           # Used for Dynaconf.find_file() - So that root path points to settings folder,
                           # since we disabled all core loaders.
                           'root_path': join(current_dir, "settings"),
                           # Multi-file section-field merging is done by pr_agent.custom_merge_loader
                           # itself (it accumulates fields across files and calls set() with a full
                           # section). Keeping dynaconf merge disabled makes settings.set() and
                           # SECTION__KEY env vars replace list values instead of appending to them
                           # (dynaconf >= 3.3 appends when merge_enabled is on); a section-level set()
                           # replaces the whole section, so always pass a full one.
                           "merge_enabled": False
                           }
global_settings = Dynaconf(
    envvar_prefix=False,
    load_dotenv=False,  # Security: Don't load .env files
    settings_files=[join(current_dir, f) for f in [
        "settings/configuration.toml",
        "settings/ignore.toml",
        "settings/generated_code_ignore.toml",
        "settings/language_extensions.toml",
        "settings/prompt_fragments.toml",
        "settings/pr_reviewer_prompts.toml",
        "settings/pr_questions_prompts.toml",
        "settings/pr_line_questions_prompts.toml",
        "settings/pr_description_prompts.toml",
        "settings/pr_description_only_files_prompts.toml",
        "settings/pr_description_only_description_prompts.toml",
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


def get_verbosity_level() -> int:
    """Return config.verbosity_level as an int, falling back to the quietest level.

    The value is compared with >= at many call sites, so a quoted number in a settings file
    would otherwise raise in the middle of a command.
    """
    value = get_settings().config.get("verbosity_level", 0)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        from pr_agent.log import get_logger
        get_logger().warning(f"verbosity_level is not a number ({value!r}), using 0")
        return 0


# Add local configuration from pyproject.toml of the project being reviewed
def _find_repository_root() -> Optional[Path]:
    """
    Identify project root directory by recursively searching for the .git directory in the parent directories.
    """
    cwd = Path.cwd().resolve()
    no_way_up = False
    while not no_way_up:
        no_way_up = cwd == cwd.parent
        if (cwd / ".git").exists():
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


def _apply_pyproject_settings(pyproject_path: Path) -> None:
    """
    Merge the `[tool.pr-agent]` table of the reviewed repository's pyproject.toml into the settings.

    pyproject.toml belongs to the repository under review, so wherever PR-Agent runs from a checkout
    of that repository (GitHub Action, CLI in CI) its contents are contributor-controlled. It is
    therefore bounded by the same host-only filter as the repository's .pr_agent.toml: without it a
    repository could reach host-level capabilities through its packaging metadata, e.g.
    `config.extra_config_url`, which the next apply_repo_settings() fetches over HTTP(S) and merges
    unfiltered (SSRF, auth-header exfiltration, wholesale setting override). Repo sections are
    merged into the current values rather than replacing them, so keys absent from pyproject.toml
    keep their defaults, and env vars are replayed last to stay the highest precedence layer.
    """
    from pr_agent.custom_merge_loader import MAX_TOML_SIZE_IN_BYTES, validate_file_security
    from pr_agent.log import get_logger

    settings = get_settings()
    try:
        if pyproject_path.stat().st_size > MAX_TOML_SIZE_IN_BYTES:
            get_logger().warning(
                f"pyproject.toml exceeds {MAX_TOML_SIZE_IN_BYTES} bytes; skipping it")
            return
        with open(pyproject_path, "rb") as f:
            parsed_toml = tomllib.load(f)
        tool = parsed_toml.get("tool")
        if not isinstance(tool, dict):
            return
        sections = tool.get(PR_AGENT_TOML_KEY)
        if not isinstance(sections, dict) or not sections:
            return
        # Same pre-parse rejection the other config sources apply: forbidden Dynaconf directives
        # (includes, preload, custom loaders, ...) must not reach the settings object.
        validate_file_security(sections, "pyproject.toml")
        applied_sections = []
        for section, contents in sections.items():
            if not isinstance(contents, dict) or not contents:
                get_logger().debug(f"Skipping non-table or empty section [{section}] from pyproject.toml")
                continue
            contents = filter_repo_host_only_keys(section, contents, source="pyproject.toml")
            if not contents:
                continue
            section_dict = copy.deepcopy(settings.as_dict().get(section.upper(), {}))
            for key, value in contents.items():
                # Dynaconf looks up keys case-insensitively, so replace the existing key (whatever
                # its casing) instead of leaving a duplicate beside it.
                for existing_key in list(section_dict):
                    if existing_key.lower() == key.lower():
                        del section_dict[existing_key]
                section_dict[key] = value
            settings.unset(section)
            settings.set(section, section_dict, merge=False)
            applied_sections.append(section)
        env_loader.load(settings)
        # Log section names only: a pyproject.toml may carry secrets, just like .pr_agent.toml.
        get_logger().info(f"Applied pyproject.toml settings (sections: {sorted(applied_sections)})")
    except Exception as e:
        get_logger().warning(f"Failed to apply pyproject.toml settings from {pyproject_path}: {e}")


pyproject_path = _find_pyproject()
if pyproject_path is not None:
    _apply_pyproject_settings(pyproject_path)


def apply_secrets_manager_config():
    """
    Retrieve configuration from AWS Secrets Manager and override existing settings
    """
    try:
        # Dynamic imports to avoid circular dependency (secret_providers imports config_loader)
        from pr_agent.log import get_logger
        from pr_agent.secret_providers import get_secret_provider

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
