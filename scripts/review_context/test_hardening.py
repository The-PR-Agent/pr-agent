"""Hostile-repository checks for graph_bundle_ts.py.

Install requirements.txt with --require-hashes, verify/extract grammars.sha256, then run:
GRAMMAR_CACHE=/path/to/grammars PYTHONPATH=. uv run pytest scripts/review_context -q
"""
import contextlib
import errno
import importlib.util
import io
import os
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from tree_sitter import QueryError

BUILDER = Path(__file__).with_name("graph_bundle_ts.py")
# Secret-named files sit beside changed config files, where the sibling-file section would otherwise read them.
MARKERS = ("OUTSIDE_TREE_MARKER", "UNTRACKED_MARKER", "SECRETS_TOML_MARKER", "DOT_SECRETS_MARKER",
           "CREDENTIALS_YAML_MARKER", "OVERSIZED_MARKER", "DOT_ENV_MARKER", "PEM_MARKER")


@pytest.fixture(scope="module")
def builder_module():
    os.environ["TREE_SITTER_LANGUAGE_PACK_LIBS_DIR"] = os.environ["GRAMMAR_CACHE"]
    spec = importlib.util.spec_from_file_location("graph_bundle_ts", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def acl_repo_tmp():
    # Some ephemeral /tmp mounts lack POSIX ACLs; test on the checkout filesystem.
    with tempfile.TemporaryDirectory(prefix=".review_context_acl_", dir=BUILDER.parents[2]) as path:
        yield Path(path)


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, msg):
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return sh(repo, "git", "rev-parse", "HEAD")


def run_builder(repo, base, head, out, *extra):
    cmd = [sys.executable, str(BUILDER), "--repo", str(repo), "--base", base, "--head", head, "--pr", "1",
           "--out-dir", str(out), *extra]
    if os.environ.get("GRAMMAR_CACHE"):
        cmd += ["--grammar-cache", os.environ["GRAMMAR_CACHE"]]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


@pytest.fixture
def hostile(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("def stolen():\n    return 'OUTSIDE_TREE_MARKER'\n")
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    sh(repo, "git", "init", "-q")
    (pkg / "core.py").write_text("def helper(x):\n    return x + 1\n\n\ndef use():\n    return helper(1)\n")
    (pkg / "settings.toml").write_text("[core]\nvalue = 1\n")
    (pkg / "app.yaml").write_text("core:\n  value: 1\n")
    base = commit(repo, "base")

    (pkg / "core.py").write_text("def helper(x, y=0):\n    return x + y + 1\n\n\ndef use():\n    return helper(1, 2)\n")
    (pkg / "settings.toml").write_text("[core]\nvalue = 2\n")
    (pkg / "app.yaml").write_text("core:\n  value: 2\n")
    os.symlink(outside, pkg / "linked.py")
    (pkg / "secrets.toml").write_text("[core]\nvalue = 'SECRETS_TOML_MARKER'\n")
    (pkg / ".SECRETS.toml").write_text("[core]\nvalue = 'DOT_SECRETS_MARKER'\n")
    (pkg / "credentials.yaml").write_text("core:\n  value: CREDENTIALS_YAML_MARKER\n")
    (pkg / ".env").write_text("TOKEN=DOT_ENV_MARKER\n")
    (pkg / "private.pem").write_text("PEM_MARKER\n")
    (pkg / "settings_copy.toml").write_text("[core]\nvalue = 3\n")  # a harmless sibling that must still show up
    (pkg / "big.py").write_text("def big():\n    return 'OVERSIZED_MARKER'\n" + "# pad\n" * 200_000)
    (pkg / "deep.py").write_text("x = " + "[" * 30_000 + "]" * 30_000 + "\n\n\ndef deep_use():\n    return helper(3)\n")
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    head = commit(repo, "head")

    # A poisoned local config and an untracked file, as on a reused runner: none of it may run or leak.
    pwned = tmp_path / "pwned"
    sh(repo, "git", "config", "diff.evil.textconv", f"sh -c 'touch {pwned}-textconv; cat'")
    sh(repo, "git", "config", "core.fsmonitor", f"sh -c 'touch {pwned}-fsmonitor'")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text(f"#!/bin/sh\ntouch {pwned}-hook\n")
    (hooks / "post-checkout").chmod(0o755)
    sh(repo, "git", "config", "core.hooksPath", str(hooks))
    (pkg / "untracked.py").write_text("def ghost():\n    return 'UNTRACKED_MARKER'\n")
    return repo, base, head, tmp_path


def test_head_tree_filters_unsafe_files(hostile, builder_module):
    repo, _base, head, _tmp = hostile
    tree = builder_module.HeadTree(str(repo), head)
    try:
        assert tree.is_file("pkg/core.py")
        assert tree.is_file("pkg/settings_copy.toml")
        for name in ("linked.py", "untracked.py", "big.py", "secrets.toml", ".SECRETS.toml",
                     "credentials.yaml", ".env", "private.pem"):
            assert not tree.is_file(f"pkg/{name}"), name
            assert tree.read_text(f"pkg/{name}") is None, name
    finally:
        tree.close()


@pytest.mark.parametrize("extra", [(), ("--generic",)])
def test_hostile_repo_leaks_nothing_and_runs_nothing(hostile, extra):
    repo, base, head, tmp = hostile
    out = tmp / "out"
    r = run_builder(repo, base, head, out, *extra)
    assert r.returncode == 0, r.stderr
    bundle = (out / "pr1.G6.md").read_text()
    assert "helper" in bundle
    assert "settings_copy.toml" in bundle  # the sibling section ran, so the secret-named siblings were skipped
    for marker in MARKERS:
        assert marker not in bundle, marker
    assert not list(tmp.glob("pwned-*"))


def test_bundle_does_not_need_a_head_checkout(hostile):
    repo, base, head, tmp = hostile
    sh(repo, "git", "checkout", "-q", "--detach", base)  # working tree no longer matches the head
    r = run_builder(repo, base, head, tmp / "out")
    assert r.returncode == 0, r.stderr
    assert "def helper(x, y=0)" in (tmp / "out" / "pr1.G6.md").read_text()


def test_timeout_exits_cleanly_without_a_bundle(hostile, monkeypatch):
    repo, base, head, tmp = hostile
    slow = tmp / "slowbin"
    slow.mkdir()
    (slow / "git").write_text(f"#!/bin/sh\nsleep 5\nexec {shutil.which('git')} \"$@\"\n")
    (slow / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{slow}{os.pathsep}{os.environ['PATH']}")
    # Subprocesses inherit the parent's blocked signal mask.
    original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        r = run_builder(repo, base, head, tmp / "out", "--timeout", "1")
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
    assert r.returncode == 2
    assert "TimeoutError" in r.stderr
    assert not (tmp / "out" / "pr1.G6.md").exists()


def test_pending_sigalrm_exits_cleanly_without_a_bundle(hostile):
    repo, base, head, tmp = hostile
    exec_builder = (
        "import os, signal, sys\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})\n"
        "signal.raise_signal(signal.SIGALRM)\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
    )
    cmd = [sys.executable, "-c", exec_builder, str(BUILDER), "--repo", str(repo), "--base", base,
           "--head", head, "--pr", "1", "--out-dir", str(tmp / "out"), "--timeout", "1",
           "--grammar-cache", os.environ["GRAMMAR_CACHE"]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "TimeoutError" in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1
    assert not (tmp / "out" / "pr1.G6.md").exists()


def test_timeout_signal_bypasses_generic_error_handlers():
    # Use a fresh process so pending signals and changed masks cannot affect other tests.
    script = (
        "import runpy, signal, sys\n"
        "module = runpy.run_path(sys.argv[1], run_name='test_import')\n"
        "signal.signal(signal.SIGALRM, module['_timeout_handler'](1))\n"
        "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})\n"
        "try:\n"
        "    try:\n"
        "        signal.raise_signal(signal.SIGALRM)\n"
        "    except OSError:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
        "except module['_SignalTimeout']:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def _wrapped_native_timeout(module):
    # py-tree-sitter's Query constructor can wrap a signal raised by re.compile() as QueryError.
    try:
        module._timeout_handler(1)(signal.SIGALRM, None)
    except module._SignalTimeout as exc:
        raise QueryError("Invalid predicate: regular expression error") from exc


@pytest.mark.parametrize("stage", ["primary", "inherited", "language_detection"])
def test_native_wrapped_timeout_is_not_swallowed(builder_module, monkeypatch, stage):
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "get_language", lambda _name: object())
        m.setattr(builder_module, "get_tags_query", lambda name: name)

        def query(_language, tag):
            if tag == ("javascript" if stage == "inherited" else "typescript"):
                _wrapped_native_timeout(builder_module)
            return object()

        m.setattr(builder_module, "Query", query)
        if stage == "language_detection":
            m.setattr(builder_module, "detect_language_from_extension", lambda _ext:
                      _wrapped_native_timeout(builder_module))
        with pytest.raises(builder_module._SignalTimeout, match="build exceeded 1 s"):
            if stage == "language_detection":
                builder_module.tags_langs()
            else:
                builder_module.Tags("typescript", (".ts",))


@pytest.mark.parametrize("stage", ["parser", "query_cursor"])
def test_native_wrapped_timeout_during_parse(builder_module, monkeypatch, stage):
    class Parser:
        def parse(self, _src):
            if stage == "parser":
                _wrapped_native_timeout(builder_module)
            return SimpleNamespace(root_node=object())

    tagger = object.__new__(builder_module.Tags)
    tagger._parser = Parser()
    tagger.drop = set()
    tagger.queries = [object()]
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "to_ir", lambda *_args: object())
        m.setattr(builder_module, "walk", lambda _root: [])
        m.setattr(builder_module, "QueryCursor", lambda _query: SimpleNamespace(
            matches=lambda _root: _wrapped_native_timeout(builder_module)))
        with pytest.raises(builder_module._SignalTimeout, match="build exceeded 1 s"):
            tagger.parse("x")


def test_native_timeout_during_query_match_iteration(builder_module, monkeypatch):
    tagger = object.__new__(builder_module.Tags)
    tagger.queries = [object()]

    def matches(_root):
        yield 1
        _wrapped_native_timeout(builder_module)

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "QueryCursor", lambda _query: SimpleNamespace(matches=matches))
        with pytest.raises(builder_module._SignalTimeout, match="build exceeded 1 s"):
            list(tagger._matches(None))


def test_closing_match_iterator_during_timeout_does_not_raise(builder_module, monkeypatch):
    tagger = object.__new__(builder_module.Tags)
    tagger.queries = [object()]
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "QueryCursor", lambda _query: SimpleNamespace(matches=lambda _root: [1, 2]))
        gen = tagger._matches(None)
        assert next(gen) == 1
        m.setattr(builder_module, "_TIMED_OUT", "build exceeded 1 s")
        gen.close()  # GeneratorExit must not become an unraisable exception on garbage collection.


def test_wrapped_error_keeps_timeout_diagnostic(builder_module, monkeypatch, capsys):
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", "build exceeded 1 s")
        m.setattr(builder_module, "_block_sigalrm", contextlib.nullcontext)
        m.setattr(builder_module, "_disarm_timeout", lambda _seconds: True)
        with pytest.raises(SystemExit) as exit_info:
            builder_module._emit_cli_failure(QueryError("Invalid predicate"), 1)
        assert exit_info.value.code == 2
    assert capsys.readouterr().err.strip() == "review-context build failed: TimeoutError: build exceeded 1 s"


def test_cli_native_wrapped_timeout_exits_two_without_bundle(tmp_path):
    # Replace only grammar interfaces; the CLI still installs and handles a real SIGALRM.
    script = (
        "import runpy, signal, sys, types\n"
        "from tree_sitter import QueryError\n"
        "ts = types.ModuleType('tree_sitter')\n"
        "ts.Parser = lambda language: object()\n"
        "ts.QueryCursor = lambda query: object()\n"
        "def query(*_args):\n"
        "    try: signal.raise_signal(signal.SIGALRM)\n"
        "    except BaseException as exc: raise QueryError('Invalid predicate') from exc\n"
        "ts.Query = query\n"
        "pack = types.ModuleType('tree_sitter_language_pack')\n"
        "class Registry:\n"
        "    @staticmethod\n"
        "    def new(): return Registry()\n"
        "    def get_language(self, name): return object()\n"
        "pack.LanguageRegistry = Registry\n"
        "pack.detect_language_from_extension = lambda ext: 'python' if ext == 'py' else None\n"
        "pack.get_tags_query = lambda name: '(tags)' if name == 'python' else ''\n"
        "sys.modules['tree_sitter'] = ts\n"
        "sys.modules['tree_sitter_language_pack'] = pack\n"
        "sys.argv = [sys.argv[1], '--repo', '.', '--base', 'x', '--head', 'y', '--pr', '1',\n"
        "            '--out-dir', sys.argv[2], '--grammar-cache', sys.argv[2], '--timeout', '1']\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER), str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert result.stderr.strip() == "review-context build failed: TimeoutError: build exceeded 1 s"
    assert not (tmp_path / "pr1.G6.md").exists()


def test_timeout_during_head_cleanup_does_not_publish(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            builder_module._timeout_handler(1)(signal.SIGALRM, None)

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        with pytest.raises(builder_module._SignalTimeout):
            builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
    assert not (tmp_path / "pr1.G6.md").exists()


def test_direct_build_still_publishes_bundle(builder_module, monkeypatch, tmp_path, capsys):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            assert not force

    args = SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path))
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        assert builder_module.build(args) == {"pr": 1}
    assert (tmp_path / "pr1.G6.md").read_text() == "bundle"
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))
    assert capsys.readouterr().out.strip() == '{"pr": 1}'


def test_direct_build_does_not_modify_caller_alarm(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            assert not force

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, lambda *_args: None)
        signal.alarm(10)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(builder_module.build, SimpleNamespace(repo=".", head="x",
                                                                       out_dir=str(tmp_path))).result(timeout=5)
            assert result == {"pr": 1}
            assert signal.alarm(0) >= 1
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.parametrize("mask", [0o022, 0o077, 0o177])
def test_staged_bundle_is_private_and_preserves_umask(builder_module, monkeypatch, tmp_path, capsys, mask):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            assert not force

    out_path = tmp_path / "pr1.G6.md"
    staging_dir_modes = []
    staging_file_modes = []
    original_open = os.open

    def observed_open(path, flags, mode=0o600, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "bundle.tmp" and dir_fd is not None:
            staging_dir_modes.append(stat.S_IMODE(os.fstat(dir_fd).st_mode))
            staging_file_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args: (str(out_path), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "open", observed_open)
        original = os.umask(mask)
        try:
            builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
        finally:
            os.umask(original)
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o666 & ~mask
    assert staging_dir_modes == [0o700]  # Unpublished bytes cannot be reached through the directory.
    assert staging_file_modes == [0o666 & ~mask]
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))
    assert capsys.readouterr().out.strip() == '{"pr": 1}'


@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX ACL regression")
def test_staged_bundle_preserves_default_acl(builder_module, monkeypatch, acl_repo_tmp):
    tmp_path = acl_repo_tmp
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    # ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER;
    # deny other users while preserving a named-user entry.
    entries = ((1, 7, 0xffffffff), (2, 4, os.getuid()), (4, 4, 0xffffffff),
               (16, 4, 0xffffffff), (32, 0, 0xffffffff))
    default_acl = struct.pack("=I", 2) + b"".join(struct.pack("=HHI", *e) for e in entries)
    try:
        os.setxattr(tmp_path, "system.posix_acl_default", default_acl)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.EACCES, errno.EPERM, errno.EINVAL):
            pytest.skip(f"default ACL is unavailable: {exc}")
        raise

    reference = tmp_path / "ordinary-file"
    reference.write_text("reference")
    out_path = tmp_path / "pr1.G6.md"
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args: (str(out_path), "bundle", {"pr": 1}))
        builder_module.build(SimpleNamespace(repo=".", head="x", out_dir=str(tmp_path)))
    assert stat.S_IMODE(reference.stat().st_mode) & 0o007 == 0
    assert stat.S_IMODE(out_path.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)
    assert os.getxattr(out_path, "system.posix_acl_access") == os.getxattr(
        reference, "system.posix_acl_access")


@pytest.mark.skipif(not hasattr(os, "O_DIRECTORY"), reason="directory descriptor required")
def test_staged_bundle_works_without_procfs(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    original_chmod = os.chmod

    def without_procfs(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError("procfs unavailable")
        return original_chmod(path, *args, **kwargs)

    out_path = tmp_path / "pr1.G6.md"
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args: (str(out_path), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "chmod", without_procfs)
        original = os.umask(0o177)
        try:
            builder_module.build(SimpleNamespace(repo=".", head="x", out_dir=str(tmp_path)))
        finally:
            os.umask(original)
    assert out_path.read_text() == "bundle"
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))


@pytest.mark.skipif(not hasattr(stat, "S_ISGID"), reason="setgid not supported")
def test_staged_bundle_preserves_setgid_group(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    os.chmod(tmp_path, 0o2700)
    if not tmp_path.stat().st_mode & stat.S_ISGID:
        pytest.skip("setgid directories unavailable")
    reference = tmp_path / "ordinary-file"
    reference.write_text("reference")
    staged_modes = []
    original_open = os.open

    def observed_open(path, flags, mode=0o600, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "bundle.tmp" and dir_fd is not None:
            staged_modes.append(stat.S_IMODE(os.fstat(dir_fd).st_mode))
        return fd

    out_path = tmp_path / "pr1.G6.md"
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args: (str(out_path), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "open", observed_open)
        builder_module.build(SimpleNamespace(repo=".", head="x", out_dir=str(tmp_path)))
    assert staged_modes == [0o2700]
    assert out_path.stat().st_gid == reference.stat().st_gid == tmp_path.stat().st_gid
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))


@pytest.mark.skipif(sys.platform != "linux", reason="Linux setgid chmod behavior")
def test_staged_bundle_fails_closed_if_setgid_cannot_be_preserved(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    os.chmod(tmp_path, 0o2700)
    if not tmp_path.stat().st_mode & stat.S_ISGID:
        pytest.skip("setgid directories unavailable")
    original_fchmod, original_mkdir = os.fchmod, os.mkdir

    def restrictive_mkdir(path, mode=0o777, *, dir_fd=None):
        original_mkdir(path, mode, dir_fd=dir_fd)
        if str(path).startswith(str(tmp_path / ".pr_bundle_")):
            os.chmod(path, 0o2600)  # Require a chmod even on hosts with inherited default ACLs.

    def strip_unprivileged_setgid(fd, mode):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and mode & stat.S_ISGID:
            mode &= ~stat.S_ISGID  # Linux strips this for owners outside the inherited group.
        return original_fchmod(fd, mode)

    out_path = tmp_path / "pr1.G6.md"
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args: (str(out_path), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "fchmod", strip_unprivileged_setgid)
        m.setattr(builder_module.os, "mkdir", restrictive_mkdir)
        original = os.umask(0o177)
        try:
            with pytest.raises(PermissionError, match="not private"):
                builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
        finally:
            os.umask(original)
    assert not out_path.exists()  # Never publish a bundle with the wrong group.
    assert not list(tmp_path.glob(".pr_bundle_*"))


def test_staging_directory_swap_cannot_redirect_bundle(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    attacker = tmp_path / "attacker"
    attacker.mkdir()
    moved = tmp_path / "moved-staging"
    swapped = []
    original_open = os.open

    def swapped_open(path, flags, mode=0o600, *, dir_fd=None):
        if path == "bundle.tmp" and dir_fd is not None:
            source = next(tmp_path.glob(".pr_bundle_*.tmp"))
            source.rename(moved)
            source.symlink_to(attacker, target_is_directory=True)
            swapped.append(source)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    out_path = tmp_path / "pr1.G6.md"
    try:
        with monkeypatch.context() as m:
            m.setattr(builder_module, "_TIMED_OUT", None)
            m.setattr(builder_module, "HeadTree", Head)
            m.setattr(builder_module, "_build_content", lambda _args: (str(out_path), "bundle", {"pr": 1}))
            m.setattr(builder_module.os, "open", swapped_open)
            builder_module.build(SimpleNamespace(repo=".", head="x", out_dir=str(tmp_path)))
        assert out_path.read_text() == "bundle"
        assert not list(attacker.iterdir())  # A swapped parent path cannot receive unfinished content.
    finally:
        for source in swapped:
            source.unlink()
        if moved.exists():
            moved.rmdir()


def test_staging_directory_creation_timeout_is_cleaned_up(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    unmasked = []

    def interrupted_mkdir(*args, **kwargs):
        if str(args[0]).startswith(str(tmp_path / ".pr_bundle_")):
            unmasked.append(signal.SIGALRM not in signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            original_mkdir(*args, **kwargs)
            signal.raise_signal(signal.SIGALRM)  # Interrupt after creation, before mkdir can return.
        else:
            original_mkdir(*args, **kwargs)

    original_mkdir = os.mkdir
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "mkdir", interrupted_mkdir)
        previous_handler = signal.signal(signal.SIGALRM, builder_module._timeout_handler(1))
        previous_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        try:
            with pytest.raises(builder_module._SignalTimeout):
                builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
        finally:
            signal.signal(signal.SIGALRM, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))
    assert not (tmp_path / "pr1.G6.md").exists()
    assert unmasked == [True]


def test_staging_directory_open_failure_is_cleaned_up(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    original_open = os.open

    def exhausted_open(path, flags, mode=0o600, *, dir_fd=None):
        if str(path).startswith(str(tmp_path / ".pr_bundle_")):
            raise OSError(errno.EMFILE, "too many open files")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "open", exhausted_open)
        with pytest.raises(OSError, match="too many open files"):
            builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))
    assert not (tmp_path / "pr1.G6.md").exists()


def test_staging_directory_close_interruption_still_cleans_up(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    original_open, original_close = os.open, os.close
    staging_fds = set()

    def observed_open(path, flags, mode=0o600, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).startswith(str(tmp_path / ".pr_bundle_")):
            staging_fds.add(fd)
        return fd

    def interrupted_close(fd):
        original_close(fd)
        if fd in staging_fds:
            raise builder_module._SignalTimeout("build exceeded 1 s")

    def abort_publish():
        raise RuntimeError("publish not attempted")

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "open", observed_open)
        m.setattr(builder_module.os, "close", interrupted_close)
        with pytest.raises(builder_module._SignalTimeout, match="build exceeded"):
            builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)),
                                 before_publish=abort_publish)
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))
    assert not (tmp_path / "pr1.G6.md").exists()


def test_staging_name_collision_preserves_existing_files(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    existing = tmp_path / ".pr_bundle_collision.tmp"
    existing.mkdir()
    (existing / "keep").write_text("keep me")
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        m.setattr(builder_module.secrets, "token_hex", lambda _n: "collision")
        with pytest.raises(FileExistsError, match="unique bundle staging directory"):
            builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
    assert (existing / "keep").read_text() == "keep me"
    assert not (tmp_path / "pr1.G6.md").exists()


def test_temporary_file_is_removed_after_open_failure(builder_module, monkeypatch, tmp_path):
    class Head:
        def __init__(self, _repo, _head):
            pass

        def close(self, force=False):
            pass

    def fail_open(*_args, **_kwargs):
        raise OSError("cannot open temporary file")

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "HeadTree", Head)
        m.setattr(builder_module, "_build_content", lambda _args:
                  (str(tmp_path / "pr1.G6.md"), "bundle", {"pr": 1}))
        m.setattr(builder_module.os, "fdopen", fail_open)
        with pytest.raises(OSError, match="cannot open temporary file"):
            builder_module.build(SimpleNamespace(repo=".", head="unused", out_dir=str(tmp_path)))
    assert not (tmp_path / "pr1.G6.md").exists()
    assert not list(tmp_path.glob(".pr_bundle_*.tmp"))


def test_pending_timeout_prevents_publication_and_cleans_temporary_file(tmp_path):
    script = (
        "import importlib.util, signal, sys, types\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "out = Path(sys.argv[2])\n"
        "class Head:\n"
        "    def __init__(self, *_args): pass\n"
        "    def close(self, force=False): pass\n"
        "mod.HeadTree = Head\n"
        "mod._build_content = lambda args: (str(out / 'pr1.G6.md'), 'bundle', {'pr': 1})\n"
        "signal.signal(signal.SIGALRM, mod._timeout_handler(1))\n"
        "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})\n"
        "def before_publish():\n"
        "    signal.raise_signal(signal.SIGALRM)\n"
        "    mod._reconcile_timeout_before_publish(1)\n"
        "try:\n"
        "    mod.build(types.SimpleNamespace(repo='.', head='x', out_dir=str(out)), before_publish)\n"
        "except mod._SignalTimeout:\n"
        "    assert not (out / 'pr1.G6.md').exists()\n"
        "    assert not list(out.glob('.pr_bundle_*.tmp'))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER), str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_direct_build_does_not_disarm_pending_caller_alarm(tmp_path):
    script = (
        "import importlib.util, signal, sys, types\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "out = Path(sys.argv[2])\n"
        "class Head:\n"
        "    def __init__(self, *_args): pass\n"
        "    def close(self, force=False): pass\n"
        "mod.HeadTree = Head\n"
        "mod._build_content = lambda args: (str(out / 'pr1.G6.md'), 'bundle', {'pr': 1})\n"
        "handler = lambda *_args: None\n"
        "signal.signal(signal.SIGALRM, handler)\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})\n"
        "signal.raise_signal(signal.SIGALRM)\n"
        "mod.build(types.SimpleNamespace(repo='.', head='x', out_dir=str(out)))\n"
        "assert (out / 'pr1.G6.md').read_text() == 'bundle'\n"
        "assert signal.SIGALRM in signal.sigpending()\n"
        "assert signal.getsignal(signal.SIGALRM) is handler\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER), str(tmp_path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_disarming_alarm_preserves_registered_signal_handler():
    script = (
        "import importlib.util, signal, sys\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "handler = mod._timeout_handler(1)\n"
        "signal.signal(signal.SIGALRM, handler)\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})\n"
        "signal.raise_signal(signal.SIGALRM)\n"
        "assert mod._disarm_timeout(1)\n"
        "assert signal.getsignal(signal.SIGALRM) is handler\n"
        "assert signal.SIGALRM not in signal.sigpending()\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_timeout_kills_stalled_git_batch_process(hostile, tmp_path):
    repo, _base, head, _tmp = hostile
    slow = tmp_path / "slowbin"
    slow.mkdir()
    (slow / "git").write_text(f"#!/bin/sh\ncase \"$*\" in *'cat-file --batch'*) exec sleep 8;; esac\n"
                              f"exec {shutil.which('git')} \"$@\"\n")
    (slow / "git").chmod(0o755)
    script = (
        "import importlib.util, signal, sys\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "tree = mod.HeadTree(sys.argv[2], sys.argv[3])\n"
        "signal.signal(signal.SIGALRM, mod._timeout_handler(1))\n"
        "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})\n"
        "signal.alarm(1)\n"
        "try:\n"
        "    try: tree.read_text('pkg/core.py')\n"
        "    finally: tree.close()\n"
        "except mod._SignalTimeout:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    env = {**os.environ, "PATH": f"{slow}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER), str(repo), head],
                            capture_output=True, text=True, timeout=5, env=env)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process descendants regression")
def test_timeout_kills_git_wrapper_descendants(hostile, tmp_path):
    repo, _base, head, _tmp = hostile
    slow = tmp_path / "slowbin"
    slow.mkdir()
    childfile = tmp_path / "child.pid"
    (slow / "git").write_text("#!/bin/sh\ncase \"$*\" in\n"
                              "  *'cat-file --batch'*) sleep 30 & echo \"$!\" > \"$CHILD_PID\";\n"
                              "    wait \"$!\"; exit $?;;\n"
                              "esac\n"
                              f"exec {shutil.which('git')} \"$@\"\n")
    (slow / "git").chmod(0o755)
    script = (
        "import importlib.util, signal, sys\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "tree = mod.HeadTree(sys.argv[2], sys.argv[3])\n"
        "signal.signal(signal.SIGALRM, mod._timeout_handler(1))\n"
        "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})\n"
        "signal.alarm(1)\n"
        "try:\n"
        "    try: tree.read_text('pkg/core.py')\n"
        "    finally: tree.close()\n"
        "except mod._SignalTimeout:\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    env = {**os.environ, "CHILD_PID": str(childfile), "PATH": f"{slow}{os.pathsep}{os.environ['PATH']}"}
    try:
        result = subprocess.run([sys.executable, "-c", script, str(BUILDER), str(repo), head],
                                capture_output=True, text=True, timeout=5, env=env)
        assert result.returncode == 0, result.stderr
        child_pid = int(childfile.read_text())
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.exists():
            try:
                assert proc_stat.read_text().split()[2] == "Z", "Git wrapper child is still running"
            except (FileNotFoundError, ProcessLookupError):
                pass  # The system's parent reaper already removed it.
    finally:
        if childfile.exists():
            try:
                os.kill(int(childfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass  # The test child may already have been reaped by its parent.


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd regression")
def test_recycled_descendant_pid_is_not_signaled(builder_module, monkeypatch):
    root, child = 1234, 5678
    states = iter([(root, 100), (1, 200)])  # Child exited; the PID now belongs to init.
    sent = []
    original_open = os.open

    def fake_listdir(path):
        return [str(root)] if path == f"/proc/{root}/task" else [str(child)] if path == (
            f"/proc/{child}/task") else []

    def fake_open(path, *args, **kwargs):
        if path == f"/proc/{root}/task/{root}/children":
            return io.StringIO(str(child))
        if path == f"/proc/{child}/task/{child}/children":
            return io.StringIO("")
        if path == f"/proc/{child}/stat":
            parent, start = next(states)
            fields = ["S", str(parent), *("0" for _ in range(17)), str(start)]
            return io.StringIO(f"{child} (fake) {' '.join(fields)}")
        raise FileNotFoundError(path)

    with monkeypatch.context() as m:
        m.setattr(builder_module.os, "listdir", fake_listdir)
        m.setattr("builtins.open", fake_open)
        m.setattr(builder_module.os, "pidfd_open", lambda _pid: original_open(os.devnull, os.O_RDONLY))
        m.setattr(builder_module.signal, "pidfd_send_signal", lambda *_args: sent.append(True))
        builder_module._kill_git_descendants(root)
    assert not sent


def test_alarm_remains_active_during_git_process_startup(hostile):
    repo, _base, head, _tmp = hostile
    script = (
        "import importlib.util, signal, sys, time\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "tree = mod.HeadTree(sys.argv[2], sys.argv[3])\n"
        "mod.subprocess.Popen = lambda *_args, **_kwargs: time.sleep(8)\n"
        "signal.signal(signal.SIGALRM, mod._timeout_handler(1))\n"
        "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})\n"
        "signal.alarm(1)\n"
        "try: tree.read_text('pkg/core.py')\n"
        "except mod._SignalTimeout: sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(BUILDER), str(repo), head],
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 0, result.stderr


def test_external_sigterm_terminates_builder_and_git(hostile, tmp_path):
    repo, _base, head, _tmp = hostile
    slow = tmp_path / "slowbin"
    slow.mkdir()
    pidfile = tmp_path / "git.pid"
    (slow / "git").write_text("#!/bin/sh\ncase \"$*\" in\n"
                              "  *'cat-file --batch'*) echo \"$$\" > \"$CAT_PID_PATH\"; exec sleep 30;;\n"
                              "esac\n"
                              f"exec {shutil.which('git')} \"$@\"\n")
    (slow / "git").chmod(0o755)
    script = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('builder', sys.argv[1])\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "tree = mod.HeadTree(sys.argv[2], sys.argv[3]); mod.HEAD = tree\n"
        "tree.read_text('pkg/core.py')\n"
    )
    env = {**os.environ, "CAT_PID_PATH": str(pidfile), "PATH": f"{slow}{os.pathsep}{os.environ['PATH']}"}
    worker = subprocess.Popen([sys.executable, "-c", script, str(BUILDER), str(repo), head],
                              start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        cat_pid = None
        for _ in range(100):
            try:
                pid_text = pidfile.read_text().strip()
            except FileNotFoundError:
                pid_text = ""
            if pid_text.isdecimal():
                cat_pid = int(pid_text)
                break
            if worker.poll() is not None:
                break
            time.sleep(0.03)
        assert cat_pid is not None, "Git subprocess did not start"
        assert os.getpgid(cat_pid) == os.getpgid(worker.pid)
        os.killpg(worker.pid, signal.SIGTERM)
        worker.wait(timeout=3)
        assert worker.returncode == -signal.SIGTERM
        for _ in range(30):
            try:
                os.kill(cat_pid, 0)
            except ProcessLookupError:
                break
            proc_stat = Path(f"/proc/{cat_pid}/stat")
            if proc_stat.exists():
                try:
                    status = proc_stat.read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    break
                if status == "Z":
                    break  # Already killed, merely waiting for the system's parent reaper.
            time.sleep(0.03)
        else:
            pytest.fail("Git process outlived external SIGTERM")
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=3)


def test_forced_close_kills_child_before_flushing_stdin(builder_module):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        child.stdin.write(b"pending data" * 200)
        tree = object.__new__(builder_module.HeadTree)
        tree._cat = child
        start = time.monotonic()
        tree.close(force=True)
        assert time.monotonic() - start < 3
        assert child.returncode is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_grammar_loading_does_not_swallow_the_timeout(builder_module, monkeypatch):
    def alarm(*_args):
        builder_module._timeout_handler(1)(signal.SIGALRM, None)

    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "detect_language_from_extension", alarm)
        with pytest.raises(builder_module._SignalTimeout):
            builder_module.tags_langs()
    with monkeypatch.context() as m:
        m.setattr(builder_module, "_TIMED_OUT", None)
        m.setattr(builder_module, "get_language", lambda _name: object())
        m.setattr(builder_module, "Query", lambda *_args: object())
        m.setattr(builder_module, "get_tags_query", lambda n: alarm() if n == "javascript" else "(tags)")
        with pytest.raises(builder_module._SignalTimeout):
            builder_module.Tags("typescript", (".ts",))


def test_failure_exits_cleanly_without_a_bundle(hostile):
    repo, base, _head, tmp = hostile
    r = run_builder(repo, base, "0" * 40, tmp / "out")
    assert r.returncode == 2
    assert r.stderr.strip().startswith("review-context build failed:")
    assert len(r.stderr.strip().splitlines()) == 1
    assert not (tmp / "out" / "pr1.G6.md").exists()


@pytest.mark.parametrize("path,expected", [
    ("pkg/core.py", "`pkg/core.py`"),
    ("pkg/`quoted`.py", "``pkg/`quoted`.py``"),
    ("```<img src=x>```", "```` ```<img src=x>``` ````"),
    (" leading.py ", "`  leading.py  `"),
    ("pkg/line\n## forged\r\t.py", r"`pkg/line\n## forged\r\t.py`"),
    ("pkg/\u2028line.py", r"`pkg/\u2028line.py`"),
])
def test_path_code_spans_cannot_break_headings(builder_module, path, expected):
    assert builder_module.md_code(path) == expected


def test_bundle_escapes_paths_in_headings(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q")
    path = repo / "`[link](https:evil)`.py"
    path.write_text("import os\n\ndef helper(x):\n    return x + 1\n")
    base = commit(repo, "base")
    path.write_text("import os\n\ndef helper(x):\n    return x + 2\n")
    head = commit(repo, "head")
    result = run_builder(repo, base, head, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    bundle = (tmp_path / "out/pr1.G6.md").read_text()
    assert "### Imports of `` `[link](https:evil)`.py ``" in bundle
    assert "### function `` `[link](https:evil)`.py::helper ``" in bundle


def test_missing_grammars_fail_without_download(hostile, monkeypatch):
    repo, base, head, tmp = hostile
    empty = tmp / "empty-grammars"
    empty.mkdir()
    monkeypatch.setenv("GRAMMAR_CACHE", str(empty))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp / "cache"))
    result = run_builder(repo, base, head, tmp / "out")
    assert result.returncode == 2
    assert "LanguageNotFound" in result.stderr
    assert not (tmp / "out/pr1.G6.md").exists()
    assert not list(empty.iterdir())
    assert not (tmp / "cache").exists()
