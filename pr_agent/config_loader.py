import copy
import json
import os
from os.path import abspath, dirname, join
from pathlib import Path
from typing import Any, Optional

from dynaconf import Dynaconf
from starlette_context import context

PR_AGENT_TOML_KEY = 'pr-agent'
MCP_CONFIG_ENV_VAR = "MCP_CONFIG_PATH"

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


def _get_logger():
    try:
        from pr_agent.log import get_logger

        return get_logger()
    except ImportError:
        class DummyLogger:
            def debug(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

            def warning(self, *args, **kwargs):
                return None

            def error(self, *args, **kwargs):
                return None

        return DummyLogger()



def _strip_json_comments(content: str) -> str:
    """Strip line and block comments from JSONC-style config while preserving newlines."""
    stripped = []
    in_string = False
    in_line_comment = False
    in_block_comment = False
    is_escaped = False
    index = 0

    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                stripped.append(char)
            index += 1
            continue

        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
                continue
            if char == "\n":
                stripped.append(char)
            index += 1
            continue

        if in_string:
            stripped.append(char)
            if is_escaped:
                is_escaped = False
            elif char == "\\":
                is_escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            stripped.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue

        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue

        stripped.append(char)
        index += 1

    return "".join(stripped)


def _strip_json_trailing_commas(content: str) -> str:
    """Strip trailing commas outside strings so common JSONC files can be parsed."""
    stripped = []
    in_string = False
    is_escaped = False
    index = 0

    while index < len(content):
        char = content[index]

        if in_string:
            stripped.append(char)
            if is_escaped:
                is_escaped = False
            elif char == "\\":
                is_escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            stripped.append(char)
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < len(content) and content[lookahead] in {" ", "\t", "\r", "\n"}:
                lookahead += 1
            if lookahead < len(content) and content[lookahead] in {"]", "}"}:
                index += 1
                continue

        stripped.append(char)
        index += 1

    return "".join(stripped)


def _resolve_mcp_config_path() -> Path:
    env_path = os.getenv(MCP_CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()
    configured_path = get_settings().get("MCP.CONFIG_PATH")
    if configured_path is None or str(configured_path).lower() in {"none", ""}:
        return Path("mcp_config.json").expanduser()
    return Path(str(configured_path)).expanduser()



def _normalize_mcp_servers(config_data: dict[str, Any]) -> dict[str, Any]:
    servers = config_data.get("servers")
    if servers is None:
        servers = config_data.get("mcpServers")
    if servers is None:
        raise ValueError("MCP config must define either 'servers' or 'mcpServers'")
    if not isinstance(servers, dict):
        raise ValueError("MCP server definitions must be a JSON object")
    return servers


def load_mcp_server_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise FileNotFoundError(f"MCP config file not found: {config_path}")
    config_text = config_path.read_text(encoding="utf-8")
    try:
        normalized = _strip_json_trailing_commas(_strip_json_comments(config_text))
        config_data = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid MCP config JSON in {config_path}: {exc}") from exc
    if not isinstance(config_data, dict):
        raise ValueError("MCP config root must be a JSON object")
    servers = _normalize_mcp_servers(config_data)
    return {"servers": servers}


def _parse_bool_setting(value: Any, default: bool = False) -> bool:
    """Safely parse a boolean setting value, correctly handling string 'false'/'true'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", ""}
    if value is None:
        return default
    return bool(value)


def apply_mcp_server_config():
    logger = _get_logger()
    config_path = _resolve_mcp_config_path()
    fail_on_invalid = _parse_bool_setting(get_settings().get("MCP.FAIL_ON_INVALID_CONFIG", False))
    try:
        if not config_path.exists():
            logger.debug(f"MCP config file not found, skipping load: {config_path}")
            return
        config_data = load_mcp_server_config(config_path)
        settings = get_settings()
        settings.set("MCP.SERVERS", config_data["servers"], merge=False)
        settings.set("MCP.SERVER_CONFIG", config_data, merge=False)
        settings.set("MCP.ACTIVE_CONFIG_PATH", str(config_path), merge=False)
        logger.info(f"Loaded MCP server configuration from {config_path}")
    except (ValueError, OSError, FileNotFoundError) as exc:
        logger.error(f"Failed to load MCP server configuration from {config_path}: {exc}")
        if fail_on_invalid:
            raise



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


def load_repo_pyproject_settings(pyproject_path: Optional[Path] = None, settings=None):
    """Load repository pyproject settings while preserving trusted MCP configuration."""
    if pyproject_path is None:
        pyproject_path = _find_pyproject()
    if pyproject_path is None:
        return

    if settings is None:
        settings = get_settings()

    trusted_mcp_settings = copy.deepcopy(dict(settings.get("MCP", {}) or {}))
    settings.load_file(pyproject_path, env=f"tool.{PR_AGENT_TOML_KEY}")
    settings.set("MCP", trusted_mcp_settings, merge=False)


pyproject_path = _find_pyproject()
load_repo_pyproject_settings(pyproject_path=pyproject_path)

apply_mcp_server_config()


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
                def debug(self, msg):
                    return None
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
