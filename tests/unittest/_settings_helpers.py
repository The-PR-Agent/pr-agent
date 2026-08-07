"""Test-only helpers for snapshotting/restoring Dynaconf settings.

Dynaconf's ``settings.unset(key, force=True)`` does not reliably remove a
dotted leaf (e.g. ``"openai.deployment_id"``) after that leaf was created
via ``settings.set(...)``. The leaf survives inside the parent section's
``DynaBox``, which causes state to leak between tests that share the global
settings singleton.

These helpers provide a SENTINEL-based snapshot so that keys which were
originally absent are truly removed (not just rewritten to ``None``) during
restore, including for dotted keys.
"""

from contextlib import suppress

from pr_agent.config_loader import get_settings

SENTINEL = object()


def snapshot_settings(keys):
    """Capture current values for ``keys``; missing keys map to ``SENTINEL``."""
    settings = get_settings()
    return {key: settings.get(key, SENTINEL) for key in keys}


def _remove_key(settings, key):
    """Best-effort removal of ``key`` (supports dotted leaves)."""
    if "." in key:
        section_name, leaf = key.split(".", 1)
        container = getattr(settings, section_name, None)
        if container is None:
            return
        # DynaBox lookups are case-insensitive, but ``pop`` requires the
        # stored casing. Find the matching key and pop it.
        for stored in list(container.keys()):
            if stored.lower() == leaf.lower():
                # ``pop`` with a default never raises KeyError; we narrow to
                # ``(AttributeError, TypeError)`` to tolerate exotic container
                # types that don't implement a dict-like ``pop``. Any other
                # exception is unexpected and should surface so tests fail
                # loudly rather than mask Dynaconf state leaks.
                with suppress(AttributeError, TypeError):
                    container.pop(stored, None)
                return
        return
    # ``settings.unset`` raises ``KeyError`` for keys that were never set;
    # that case is benign for restore. Any other exception (e.g. a Dynaconf
    # internal error) must propagate so that a broken cleanup is visible
    # instead of silently leaking state across tests.
    with suppress(KeyError):
        settings.unset(key, force=True)


def restore_settings(snapshot):
    """Restore ``snapshot``; truly remove entries whose snapshot is SENTINEL.

    Because ``merge_enabled=True`` causes ``settings.set()`` to *append* list
    values rather than replace them, the key is always removed first via
    ``_remove_key`` before setting the restored value.
    """
    settings = get_settings()
    for key, value in snapshot.items():
        _remove_key(settings, key)
        if value is not SENTINEL:
            settings.set(key, value)
