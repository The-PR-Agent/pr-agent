from pathlib import Path
import tomllib #tomllib should be used instead of Py toml for Python 3.11+

from jinja2.exceptions import SecurityError

from pr_agent.log import get_logger

def load(obj, env=None, silent=True, key=None, filename=None):
    """
    Load and merge TOML configuration files into a Dynaconf settings object using a secure, in-house loader.
    This loader:
    - Replaces list and dict fields instead of appending/updating (non-default Dynaconf behavior).
    - Enforces several security checks (e.g., disallows includes/preloads and enforces .toml files).
    - Supports optional single-key loading.
    - Supports Dynaconf's fresh_vars feature for dynamic reloading.
    Args:
        obj: The Dynaconf settings instance to update.
        env: The current environment name (upper case). Defaults to 'DEVELOPMENT'. Note: currently unused.
        silent (bool): If True, suppress exceptions and log warnings/errors instead.
        key (str | None): Load only this top-level key (section) if provided; otherwise, load all keys from the files.
        filename (str | None): Custom filename for tests (not used when settings_files are provided).
    Returns:
        None
    """

    MAX_TOML_SIZE_IN_BYTES = 100 * 1024 * 1024 # Prevent out of mem. exceptions by limiting to 100 MBs which is sufficient for up to 1M lines

    # Get the list of files to load
    # TODO: hasattr(obj, 'settings_files') for some reason returns False. Need to use 'settings_file'
    settings_files = obj.settings_files if hasattr(obj, 'settings_files') else (
        obj.settings_file) if hasattr(obj, 'settings_file') else []
    if not settings_files or not isinstance(settings_files, list):
        get_logger().warning("No settings files specified, or missing keys "
                             "(tried looking for 'settings_files' or 'settings_file'), or not a list. Skipping loading.",
                             artifact={'toml_obj_attributes_names': dir(obj)})
        return

    # Storage for all loaded data
    accumulated_data = {}

    # Security: Check for forbidden configuration options
    if hasattr(obj, 'includes') and obj.includes:
        if not silent:
            raise SecurityError("Configuration includes forbidden option: 'includes'. Skipping loading.")
        get_logger().error("Configuration includes forbidden option: 'includes'. Skipping loading.")
        return
    if hasattr(obj, 'preload') and obj.preload:
        if not silent:
            raise SecurityError("Configuration includes forbidden option: 'preload'. Skipping loading.")
        get_logger().error("Configuration includes forbidden option: 'preload'. Skipping loading.")
        return

    for settings_file in settings_files:
        try:
            # Load the TOML file
            file_path = Path(settings_file)
            # Security: Only allow .toml files
            if file_path.suffix.lower() != '.toml':
                get_logger().warning(f"Only .toml files are allowed. Skipping: {settings_file}")
                continue

            if not file_path.exists():
                get_logger().warning(f"Settings file not found: {settings_file}. Skipping it.")
                continue

            if file_path.stat().st_size > MAX_TOML_SIZE_IN_BYTES:
                get_logger().warning(f"Settings file too large (> {MAX_TOML_SIZE_IN_BYTES} bytes): {settings_file}. Skipping it.")
                continue

            with open(file_path, 'rb') as f:
                file_data = tomllib.load(f)

            # Handle sections (like [config], [default], etc.)
            if not isinstance(file_data, dict):
                get_logger().warning(f"TOML root is not a table in '{settings_file}'. Skipping.")
                continue

            # Security: Check file contents for forbidden directives
            validate_file_security(file_data, settings_file)

            for section_name, section_data in file_data.items():
                if not isinstance(section_data, dict):
                    get_logger().warning(f"Section '{section_name}' in '{settings_file}' is not a table. Skipping.")
                    continue
                for field, field_value in section_data.items():
                    if section_name not in accumulated_data:
                        accumulated_data[section_name] = {}
                    accumulated_data[section_name][field] = field_value

        except Exception as e:
            if not silent:
                raise e
            get_logger().exception(f"Exception loading settings file: {settings_file}. Skipping.")

    # Update the settings object
    for k, v in accumulated_data.items():
        # For fresh_vars support: key parameter is uppercase, but accumulated_data keys are lowercase
        if key is None or key.upper() == k.upper():
            obj.set(k, v)

def validate_file_security(file_data, filename):
    """
    Validate that the config file does not contain security-sensitive directives.

    Args:
        file_data: Parsed TOML data representing the configuration contents.
        filename: The name or path of the file being validated (used for error messages).

    Raises:
        SecurityError: If forbidden directives are found within the configuration, or if data too nested.
    """
    MAX_DEPTH = 50

    # Check for forbidden keys
    # Comprehensive list of forbidden keys with explanations
    forbidden_keys_to_reasons = {
        # Include mechanisms - allow loading arbitrary files
        'dynaconf_include': 'allows including other config files dynamically',
        'dynaconf_includes': 'allows including other config files dynamically',
        'includes': 'allows including other config files dynamically',

        # Preload mechanisms - allow loading files before main config
        'preload': 'allows preloading files with potential code execution',
        'preload_for_dynaconf': 'allows preloading files with potential code execution',
        'preloads': 'allows preloading files with potential code execution',

        # Merge controls - could be used to manipulate config behavior
        'dynaconf_merge': 'allows manipulating merge behavior',
        'dynaconf_merge_enabled': 'allows manipulating merge behavior',
        'merge_enabled': 'allows manipulating merge behavior',

        # Loader controls - allow changing how configs are loaded
        'loaders_for_dynaconf': 'allows overriding loaders to execute arbitrary code',
        'loaders': 'allows overriding loaders to execute arbitrary code',
        'core_loaders': 'allows overriding core loaders',
        'core_loaders_for_dynaconf': 'allows overriding core loaders',

        # Settings module - allows loading Python modules
        'settings_module': 'allows loading Python modules with code execution',
        'settings_file_for_dynaconf': 'could override settings file location',
        'settings_files_for_dynaconf': 'could override settings file location',

        # Environment variable prefix manipulation
        'envvar_prefix': 'allows changing environment variable prefix',
        'envvar_prefix_for_dynaconf': 'allows changing environment variable prefix',
    }

    # Check at the top level and in all sections
    def check_dict(data, path="", max_depth=MAX_DEPTH):
        if max_depth <= 0:
            raise SecurityError(
                f"Maximum nesting depth exceeded at {path}. "
                f"Possible attempt to cause stack overflow."
            )

        for key, value in data.items():
            full_path = f"{path}.{key}" if path else key

            if key.lower() in forbidden_keys_to_reasons:
                raise SecurityError(
                    f"Security error in {filename}: "
                    f"Forbidden directive '{key}' found at {full_path}. Reason: {forbidden_keys_to_reasons[key.lower()]}"
                )

            # Recursively check nested dicts
            if isinstance(value, dict):
                check_dict(value, path=full_path, max_depth=(max_depth - 1))

    check_dict(file_data, max_depth=MAX_DEPTH)
