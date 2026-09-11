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
    """Set ``PROMETHEUS_MULTIPROC_DIR`` and create or validate the directory.

    Returns the normalized path. Idempotent; safe to call from the gunicorn
    master and from each worker's first telemetry init.

    The state files hold per-pid metric data that any local user with read
    access could observe, so a predictable default inside a world-writable
    prefix like /tmp must not be redirected or shared: a symlink is refused,
    the directory is created (and tightened to) 0700, and a path owned by
    another user is rejected instead of followed.
    """
    env = PROMETHEUS_MULTIPROC_DIR_ENV
    if path:
        os.environ[env] = path
    target = os.environ[env]
    if os.path.islink(target):
        raise RuntimeError(
            f"Refusing to use a symlinked prometheus multiprocess state directory: {target!r}"
        )
    os.makedirs(target, mode=0o700, exist_ok=True)
    st = os.stat(target)
    if st.st_uid != os.getuid():
        raise RuntimeError(
            f"Prometheus multiprocess state directory {target!r} is owned by uid {st.st_uid}, "
            f"not the current user's uid {os.getuid()}"
        )
    os.chmod(target, 0o700)
    return target


def prometheus_multiproc_dir() -> str | None:
    """The configured multiprocess state directory, if any."""
    return os.environ.get(PROMETHEUS_MULTIPROC_DIR_ENV)
