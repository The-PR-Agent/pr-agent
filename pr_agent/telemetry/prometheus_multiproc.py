"""Prometheus multiprocess state-directory setup.

`prometheus_client` decides whether metrics are multiprocess-capable when its
`metrics` module is first imported, by looking at the `PROMETHEUS_MULTIPROC_DIR`
environment variable. Under gunicorn's fork model (`preload_app = True`) the
state directory must therefore exist and the variable be set *before* any
worker imports prometheus_client.

This module imports nothing but the standard library so it is safe to call from
the gunicorn master (via `gunicorn_config.when_ready`) and from
`pr_agent.telemetry.config`, both before any worker has imported
prometheus_client.
"""

import os

PROMETHEUS_MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"


def ensure_prometheus_multiproc_dir(path: str) -> str:
    """Set ``PROMETHEUS_MULTIPROC_DIR`` and create the directory.

    Returns the normalized path. Idempotent; safe to call from the gunicorn
    master and from each worker's first telemetry init.
    """
    env = PROMETHEUS_MULTIPROC_DIR_ENV
    if path:
        os.environ[env] = path
    os.makedirs(os.environ[env], exist_ok=True)
    return os.environ[env]


def prometheus_multiproc_dir() -> str | None:
    """The configured multiprocess state directory, if any."""
    return os.environ.get(PROMETHEUS_MULTIPROC_DIR_ENV)
