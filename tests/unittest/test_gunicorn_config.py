import pytest

from pr_agent.servers import gunicorn_config


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Detach every test from the host's env vars and real cgroup files."""
    monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
    monkeypatch.delenv("GUNICORN_MAX_WORKERS", raising=False)
    for attr in ("CGROUP_V2_CPU_MAX", "CGROUP_V1_CPU_QUOTA", "CGROUP_V1_CPU_PERIOD"):
        monkeypatch.setattr(gunicorn_config, attr, str(tmp_path / "missing"))


def write_cgroup_v2(monkeypatch, tmp_path, content):
    path = tmp_path / "cpu.max"
    path.write_text(content)
    monkeypatch.setattr(gunicorn_config, "CGROUP_V2_CPU_MAX", str(path))


def write_cgroup_v1(monkeypatch, tmp_path, quota, period):
    quota_path = tmp_path / "cpu.cfs_quota_us"
    period_path = tmp_path / "cpu.cfs_period_us"
    quota_path.write_text(quota)
    period_path.write_text(period)
    monkeypatch.setattr(gunicorn_config, "CGROUP_V1_CPU_QUOTA", str(quota_path))
    monkeypatch.setattr(gunicorn_config, "CGROUP_V1_CPU_PERIOD", str(period_path))


class TestCgroupCpuLimit:
    @pytest.mark.parametrize("content,expected", [
        ("200000 100000\n", 2.0),
        ("50000 100000\n", 0.5),
        ("max 100000\n", None),  # no CPU limit set on the pod
    ])
    def test_cgroup_v2(self, monkeypatch, tmp_path, content, expected):
        write_cgroup_v2(monkeypatch, tmp_path, content)
        assert gunicorn_config._cgroup_cpu_limit() == expected

    def test_cgroup_v2_malformed_falls_through(self, monkeypatch, tmp_path):
        write_cgroup_v2(monkeypatch, tmp_path, "garbage\n")
        assert gunicorn_config._cgroup_cpu_limit() is None

    @pytest.mark.parametrize("quota,period,expected", [
        ("150000", "100000", 1.5),
        ("-1", "100000", None),  # cgroup v1 sentinel for "unlimited"
    ])
    def test_cgroup_v1(self, monkeypatch, tmp_path, quota, period, expected):
        write_cgroup_v1(monkeypatch, tmp_path, quota, period)
        assert gunicorn_config._cgroup_cpu_limit() == expected

    def test_no_cgroup_files(self):
        assert gunicorn_config._cgroup_cpu_limit() is None


class TestAvailableCpus:
    def test_prefers_cgroup_limit_over_host_cores(self, monkeypatch, tmp_path):
        write_cgroup_v2(monkeypatch, tmp_path, "400000 100000\n")
        monkeypatch.setattr(gunicorn_config.os, "cpu_count", lambda: 64)
        assert gunicorn_config.available_cpus() == 4

    def test_fractional_cpu_limit_rounds_up_to_one(self, monkeypatch, tmp_path):
        # The reported pod: `cpu: 500m`. Must never yield 0 workers.
        write_cgroup_v2(monkeypatch, tmp_path, "50000 100000\n")
        assert gunicorn_config.available_cpus() == 1

    def test_falls_back_to_affinity_when_uncapped(self, monkeypatch):
        monkeypatch.setattr(gunicorn_config.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
        assert gunicorn_config.available_cpus() == 8


class TestComputeWorkers:
    def test_explicit_override_wins(self, monkeypatch, tmp_path):
        write_cgroup_v2(monkeypatch, tmp_path, "100000 100000\n")
        monkeypatch.setenv("GUNICORN_WORKERS", "9")
        assert gunicorn_config.compute_workers() == 9

    @pytest.mark.parametrize("value", ["abc", "0", "-3", "2.5"])
    def test_invalid_override_is_rejected(self, monkeypatch, value):
        monkeypatch.setenv("GUNICORN_WORKERS", value)
        with pytest.raises(ValueError):
            gunicorn_config.compute_workers()

    def test_blank_override_is_ignored(self, monkeypatch, tmp_path):
        write_cgroup_v2(monkeypatch, tmp_path, "300000 100000\n")
        monkeypatch.setenv("GUNICORN_WORKERS", "")
        assert gunicorn_config.compute_workers() == 3

    def test_caps_a_large_host(self, monkeypatch):
        # The regression: an uncapped pod on a 64-core node used to get 129 workers.
        monkeypatch.setattr(gunicorn_config, "available_cpus", lambda: 64)
        assert gunicorn_config.compute_workers() == gunicorn_config.DEFAULT_MAX_WORKERS

    def test_keeps_minimum_for_health_check_isolation(self, monkeypatch):
        monkeypatch.setattr(gunicorn_config, "available_cpus", lambda: 1)
        assert gunicorn_config.compute_workers() == gunicorn_config.MIN_WORKERS

    def test_max_workers_env_lowers_the_ceiling(self, monkeypatch):
        monkeypatch.setattr(gunicorn_config, "available_cpus", lambda: 64)
        monkeypatch.setenv("GUNICORN_MAX_WORKERS", "3")
        assert gunicorn_config.compute_workers() == 3

    def test_max_workers_env_below_minimum_wins(self, monkeypatch):
        monkeypatch.setattr(gunicorn_config, "available_cpus", lambda: 64)
        monkeypatch.setenv("GUNICORN_MAX_WORKERS", "1")
        assert gunicorn_config.compute_workers() == 1

    def test_module_level_workers_within_bounds(self):
        assert gunicorn_config.MIN_WORKERS <= gunicorn_config.workers <= gunicorn_config.DEFAULT_MAX_WORKERS


def test_preload_app_enabled():
    assert gunicorn_config.preload_app is True


def test_when_ready_freezes_gc(monkeypatch):
    calls = []
    monkeypatch.setattr(gunicorn_config.gc, "freeze", lambda: calls.append(True))
    gunicorn_config.when_ready(server=None)
    assert calls == [True]
