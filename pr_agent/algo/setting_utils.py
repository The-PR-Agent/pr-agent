from pr_agent.log import get_logger


def _as_bool_setting(value, default: bool = False, *, setting_name: str | None = None) -> bool:
    """Interpret a configured value as a boolean, warning when a named value is invalid."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
    if setting_name:
        get_logger().warning(
            f"Unrecognized boolean value for {setting_name}: {value!r}; using default {default}"
        )
    return default


def _as_ssl_verify_setting(value, default=True):
    """Interpret an SSL verify setting, leaving a custom CA bundle path unchanged.

    Env vars and TOML often deliver ``"false"`` as a string. requests treats any
    non-empty string as a CA bundle path, so that value would not disable
    verification unless it is converted to a bool first.
    """
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if value is None:
        return default
    return value


def normalize_setting_list(value, *, split_commas: bool = False) -> list:
    """Return a setting value as a list without iterating plain strings by character."""
    if not value:
        return []
    if isinstance(value, str):
        if split_commas:
            return [item.strip() for item in value.split(",") if item.strip()]
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]
