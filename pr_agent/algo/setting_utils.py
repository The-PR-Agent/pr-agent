def _as_bool_setting(value, default: bool = False) -> bool:
    """Interpret a configured value as a boolean, falling back for invalid values."""
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
    return default


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
