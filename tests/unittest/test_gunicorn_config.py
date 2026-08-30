import pytest

from pr_agent.servers import gunicorn_config


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Detach every test from the host's env vars, CPU affinity, and real cgroup files."""
    monkeypatch.delenv("GUNICORN_WORKERS", raising=False)
    monkeypatch.delenv("GUNICORN_MAX_WORKERS", raising=False)
    monkeypatch.setattr(gunicorn_config.os, "sched_getaffinity", lambda pid: set(range(64)), raising=False)
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
        monkeypatch.delattr(gunicorn_config.os, "sched_getaffinity", raising=False)
        monkeypatch.setattr(gunicorn_config.os, "cpu_count", lambda: 64)
        assert gunicorn_config.available_cpus() == 4

    def test_fractional_cpu_limit_rounds_up_to_one(self, monkeypatch, tmp_path):
        # The reported pod: `cpu: 500m`. Must never yield 0 workers.
        write_cgroup_v2(monkeypatch, tmp_path, "50000 100000\n")
        assert gunicorn_config.available_cpus() == 1

    def test_falls_back_to_affinity_when_uncapped(self, monkeypatch):
        monkeypatch.setattr(gunicorn_config.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)
        assert gunicorn_config.available_cpus() == 8

    def test_affinity_wins_when_stricter_than_quota(self, monkeypatch, tmp_path):
        # A pod can be both quota-limited and cpuset-pinned; the tighter bound applies.
        write_cgroup_v2(monkeypatch, tmp_path, "400000 100000\n")  # quota = 4 CPUs
        monkeypatch.setattr(gunicorn_config.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
        assert gunicorn_config.available_cpus() == 2

    def test_quota_wins_when_stricter_than_affinity(self, monkeypatch, tmp_path):
        write_cgroup_v2(monkeypatch, tmp_path, "200000 100000\n")  # quota = 2 CPUs
        monkeypatch.setattr(gunicorn_config.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
        assert gunicorn_config.available_cpus() == 2

    def test_survives_a_platform_that_reports_no_cpus(self, monkeypatch):
        monkeypatch.delattr(gunicorn_config.os, "sched_getaffinity", raising=False)
        monkeypatch.setattr(gunicorn_config.os, "cpu_count", lambda: None)
        assert gunicorn_config.available_cpus() == 1


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

    def test_module_level_workers_is_a_usable_count(self):
        # `workers` is computed at import, before this module's fixtures can isolate the
        # environment, so it can legitimately reflect a GUNICORN_WORKERS set by the host.
        # Only assert what holds regardless of that: gunicorn gets a positive int.
        assert isinstance(gunicorn_config.workers, int)
        assert gunicorn_config.workers >= 1


def test_preload_app_enabled():
    assert gunicorn_config.preload_app is True


class TestPostFork:
    @pytest.fixture
    def recorded_setup_logger(self, monkeypatch):
        import pr_agent.log

        calls = []
        monkeypatch.setattr(pr_agent.log, "setup_logger", lambda **kwargs: calls.append(kwargs))
        return calls

    @pytest.fixture
    def analytics_folder(self):
        from pr_agent.config_loader import get_settings

        original = get_settings().get("CONFIG.ANALYTICS_FOLDER", "")

        def _set(value):
            get_settings().set("CONFIG.ANALYTICS_FOLDER", value)

        yield _set
        get_settings().set("CONFIG.ANALYTICS_FOLDER", original)

    def test_noop_without_analytics_folder(self, recorded_setup_logger, analytics_folder):
        analytics_folder("")
        gunicorn_config.post_fork(server=None, worker=None)
        assert recorded_setup_logger == []

    def test_reopens_analytics_log_in_the_worker(self, recorded_setup_logger, analytics_folder, tmp_path):
        # Under preload the sink was opened in the master and named for the master's pid;
        # the worker must open its own.
        analytics_folder(str(tmp_path))
        gunicorn_config.post_fork(server=None, worker=None)
        assert len(recorded_setup_logger) == 1


def test_when_ready_freezes_gc(monkeypatch):
    calls = []
    monkeypatch.setattr(gunicorn_config.gc, "freeze", lambda: calls.append(True))
    gunicorn_config.when_ready(server=None)
    assert calls == [True]
