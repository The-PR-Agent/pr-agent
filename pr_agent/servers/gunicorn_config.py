import gc
import os

# from prometheus_client import multiprocess

# Sample Gunicorn configuration file.

#
# Server socket
#
#   bind - The socket to bind.
#
#       A string of the form: 'HOST', 'HOST:PORT', 'unix:PATH'.
#       An IP is a valid HOST.
#
#   backlog - The number of pending connections. This refers
#       to the number of clients that can be waiting to be
#       served. Exceeding this number results in the client
#       getting an error when attempting to connect. It should
#       only affect servers under significant load.
#
#       Must be a positive integer. Generally set in the 64-2048
#       range.
#

# bind = '0.0.0.0:5000'
bind = '0.0.0.0:3000'
backlog = 2048

#
# Worker processes
#
#   workers - The number of worker processes that this server
#       should keep alive for handling requests.
#
#       A positive integer generally in the 2-4 x $(NUM_CORES)
#       range. You'll want to vary this a bit to find the best
#       for your particular application's work load.
#
#   worker_class - The type of workers to use. The default
#       sync class should handle most 'normal' types of work
#       loads. You'll want to read
#       http://docs.gunicorn.org/en/latest/design.html#choosing-a-worker-type
#       for information on when you might want to choose one
#       of the other worker classes.
#
#       A string referring to a Python path to a subclass of
#       gunicorn.workers.base.Worker. The default provided values
#       can be seen at
#       http://docs.gunicorn.org/en/latest/settings.html#worker-class
#
#   worker_connections - For the eventlet and gevent worker classes
#       this limits the maximum number of simultaneous clients that
#       a single process can handle.
#
#       A positive integer generally set to around 1000.
#
#   timeout - If a worker does not notify the master process in this
#       number of seconds it is killed and a new worker is spawned
#       to replace it.
#
#       Generally set to thirty seconds. Only set this noticeably
#       higher if you're sure of the repercussions for sync workers.
#       For the non sync workers it just means that the worker
#       process is still communicating and is not tied to the length
#       of time required to handle a single request.
#
#   keepalive - The number of seconds to wait for the next request
#       on a Keep-Alive HTTP connection.
#
#       A positive integer. Generally set in the 1-5 seconds range.
#

# Each worker imports the full pr_agent graph (litellm alone is ~150MB), so the worker
# count is the dominant term in the server's memory footprint. Two things kept the old
# `cpu_count() * 2 + 1` default from being safe in a container:
#   - `multiprocessing.cpu_count()` reports the *host's* cores, ignoring the cgroup CPU
#     limit, so a pod capped at 500m on a 64-core node spawned 129 workers and was OOMKilled.
#   - `cores * 2 + 1` is gunicorn's formula for *sync* workers. These are async
#     UvicornWorkers on a purely I/O-bound workload, where a handful already saturate.
# Hence: read the real cgroup limit, then clamp hard. Override with GUNICORN_WORKERS.
MIN_WORKERS = 2  # >=2 so a worker blocked on a background task can't fail the health check
DEFAULT_MAX_WORKERS = 4

CGROUP_V2_CPU_MAX = '/sys/fs/cgroup/cpu.max'
CGROUP_V1_CPU_QUOTA = '/sys/fs/cgroup/cpu/cpu.cfs_quota_us'
CGROUP_V1_CPU_PERIOD = '/sys/fs/cgroup/cpu/cpu.cfs_period_us'


def _cgroup_cpu_limit():
    """Return the cgroup's effective CPU limit, or None when unlimited or unavailable."""
    try:  # cgroup v2
        with open(CGROUP_V2_CPU_MAX) as f:
            quota, period = f.read().split()
        if quota != 'max' and float(period) > 0:
            return float(quota) / float(period)
        return None
    except (OSError, ValueError):
        pass  # not a cgroup v2 host (or the file is unreadable) - try v1 below
    try:  # cgroup v1
        with open(CGROUP_V1_CPU_QUOTA) as f:
            quota = int(f.read())
        with open(CGROUP_V1_CPU_PERIOD) as f:
            period = int(f.read())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass  # no cgroup at all (macOS/Windows/bare metal) - caller falls back to CPU affinity
    return None


def _affinity_cpus():
    """CPUs this process may be scheduled on, or None when the platform cannot say."""
    try:
        return len(os.sched_getaffinity(0))  # respects cpuset pinning; Linux only
    except AttributeError:
        return os.cpu_count()


def available_cpus():
    """CPUs usable by this process: the stricter of the cgroup quota and CPU affinity.

    A pod can have both - a quota of 4 while pinned to 2 cores - so neither alone is the
    answer. Either may be absent, in which case the other stands on its own.
    """
    limits = [limit for limit in (_cgroup_cpu_limit(), _affinity_cpus()) if limit]
    if not limits:
        return 1
    return max(1, int(min(limits)))


def _env_int(name):
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


def compute_workers():
    explicit = _env_int('GUNICORN_WORKERS')
    if explicit is not None:
        return explicit
    max_workers = _env_int('GUNICORN_MAX_WORKERS') or DEFAULT_MAX_WORKERS
    floor = min(MIN_WORKERS, max_workers)  # an explicit ceiling below the floor wins
    return max(floor, min(available_cpus(), max_workers))


workers = compute_workers()

# Import the app once in the master and fork afterwards, so workers share that ~230MB
# heap copy-on-write instead of each building their own. Anything constructed at import
# is now inherited across the fork, so import-time clients must be fork-safe (see the
# pid-guarded secret provider in servers/gitlab_webhook.py).
preload_app = True
worker_connections = 1000
timeout = 240
keepalive = 2

#
#   spew - Install a trace function that spews every line of Python
#       that is executed when running the server. This is the
#       nuclear option.
#
#       True or False
#

spew = False

#
# Server mechanics
#
#   daemon - Detach the main Gunicorn process from the controlling
#       terminal with a standard fork/fork sequence.
#
#       True or False
#
#   raw_env - Pass environment variables to the execution environment.
#
#   pidfile - The path to a pid file to write
#
#       A path string or None to not write a pid file.
#
#   user - Switch worker processes to run as this user.
#
#       A valid user id (as an integer) or the name of a user that
#       can be retrieved with a call to pwd.getpwnam(value) or None
#       to not change the worker process user.
#
#   group - Switch worker process to run as this group.
#
#       A valid group id (as an integer) or the name of a user that
#       can be retrieved with a call to pwd.getgrnam(value) or None
#       to change the worker processes group.
#
#   umask - A mask for file permissions written by Gunicorn. Note that
#       this affects unix socket permissions.
#
#       A valid value for the os.umask(mode) call or a string
#       compatible with int(value, 0) (0 means Python guesses
#       the base, so values like "0", "0xFF", "0022" are valid
#       for decimal, hex, and octal representations)
#
#   tmp_upload_dir - A directory to store temporary request data when
#       requests are read. This will most likely be disappearing soon.
#
#       A path to a directory where the process owner can write. Or
#       None to signal that Python should choose one on its own.
#

daemon = False
raw_env = []
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

#
#   Logging
#
#   logfile - The path to a log file to write to.
#
#       A path string. "-" means log to stdout.
#
#   loglevel - The granularity of log output
#
#       A string of "debug", "info", "warning", "error", "critical"
#

errorlog = '-'
loglevel = 'info'
accesslog = None
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

#
# Process naming
#
#   proc_name - A base to use with setproctitle to change the way
#       that Gunicorn processes are reported in the system process
#       table. This affects things like 'ps' and 'top'. If you're
#       going to be running more than one instance of Gunicorn you'll
#       probably want to set a name to tell them apart. This requires
#       that you install the setproctitle module.
#
#       A string or None to choose a default of something like 'gunicorn'.
#

proc_name = None


#
# Server hooks
#
#   post_fork - Called just after a worker has been forked.
#
#       A callable that takes a server and worker instance
#       as arguments.
#
#   pre_fork - Called just prior to forking the worker subprocess.
#
#       A callable that accepts the same arguments as after_fork
#
#   pre_exec - Called just prior to forking off a secondary
#       master process during things like config reloading.
#
#       A callable that takes a server instance as the sole argument.
#


def when_ready(server):
    """Runs after `preload_app` has imported the app and before any worker is forked."""
    # Copy-on-write only saves memory while the shared pages stay clean, and CPython
    # dirties them on its own: the cyclic collector writes to the GC header of every
    # object it visits. Freezing moves everything allocated so far into a permanent
    # generation the collector never traverses, keeping those pages shared.
    gc.freeze()


def post_fork(server, worker):
    """Runs inside each worker right after it is forked."""
    # The webhook apps call setup_logger() at import, which under `preload_app` now runs
    # in the master. When CONFIG.ANALYTICS_FOLDER is set that opens `pr-agent.<pid>.log`
    # named for the *master*, and every worker inherits the same descriptor. Re-running it
    # here gives each worker its own file again. All three apps that use this config call
    # setup_logger identically, so repeating that call is enough.
    from pr_agent.config_loader import get_settings
    from pr_agent.log import LoggingFormat, setup_logger

    if get_settings().get("CONFIG.ANALYTICS_FOLDER", ""):
        setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
