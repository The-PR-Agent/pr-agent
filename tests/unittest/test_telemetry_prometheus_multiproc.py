"""Tests for the prometheus multiprocess state directory provisioning.

The state directory is the one piece of the exporter the gunicorn master
touches before workers import prometheus_client, and a predictable default in
a world-writable prefix like /tmp must not be redirectable to a foreign
location or shared with other users. These tests lock in the symlink and
ownership refusals and the 0700 creation.
"""

import os
import stat

import pytest

from pr_agent.telemetry.prometheus_multiproc import (
    PROMETHEUS_MULTIPROC_DIR_ENV,
    ensure_prometheus_multiproc_dir,
)


def test_fresh_dir_created_0700_and_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv(PROMETHEUS_MULTIPROC_DIR_ENV, "stale")
    state_dir = tmp_path / "prometheus-state"

    result = ensure_prometheus_multiproc_dir(str(state_dir))

    assert result == str(state_dir)
    assert os.environ[PROMETHEUS_MULTIPROC_DIR_ENV] == str(state_dir)
    assert state_dir.is_dir()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_existing_owned_dir_accepted_and_tightened_to_0700(tmp_path):
    state_dir = tmp_path / "prometheus-state"
    state_dir.mkdir(mode=0o755)

    ensure_prometheus_multiproc_dir(str(state_dir))

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_symlink_is_refused(tmp_path):
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "prometheus-state"
    link.symlink_to(real)

    with pytest.raises(RuntimeError, match="symlinked"):
        ensure_prometheus_multiproc_dir(str(link))


def test_dir_owned_by_foreign_uid_is_refused(tmp_path, monkeypatch):
    state_dir = tmp_path / "prometheus-state"
    state_dir.mkdir()
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr("pr_agent.telemetry.prometheus_multiproc.os.getuid", lambda: foreign_uid)

    with pytest.raises(RuntimeError, match="owned by uid"):
        ensure_prometheus_multiproc_dir(str(state_dir))
