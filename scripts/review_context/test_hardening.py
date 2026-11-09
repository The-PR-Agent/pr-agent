"""Hostile-repository checks for graph_bundle_ts.py.

Run: uv run --no-project --with pytest --with tree-sitter-language-pack==1.22.1 pytest scripts/review_context -q
Set GRAMMAR_CACHE to a prefetched grammar directory to run offline.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BUILDER = Path(__file__).with_name("graph_bundle_ts.py")
# Secret-named files sit beside changed config files, where the sibling-file section would otherwise read them.
MARKERS = ("OUTSIDE_TREE_MARKER", "UNTRACKED_MARKER", "SECRETS_TOML_MARKER", "DOT_SECRETS_MARKER",
           "CREDENTIALS_YAML_MARKER", "OVERSIZED_MARKER", "DOT_ENV_MARKER", "PEM_MARKER")


@pytest.fixture(scope="module")
def builder_module():
    spec = importlib.util.spec_from_file_location("graph_bundle_ts", BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, msg):
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return sh(repo, "git", "rev-parse", "HEAD")


def run_builder(repo, base, head, out, *extra):
    cmd = [sys.executable, str(BUILDER), "--repo", str(repo), "--base", base, "--head", head, "--pr", "1",
           "--out-dir", str(out), "--generic", *extra]
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


def test_hostile_repo_leaks_nothing_and_runs_nothing(hostile):
    repo, base, head, tmp = hostile
    out = tmp / "out"
    r = run_builder(repo, base, head, out)
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
    r = run_builder(repo, base, head, tmp / "out", "--timeout", "1")
    assert r.returncode == 2
    assert "TimeoutError" in r.stderr
    assert not (tmp / "out" / "pr1.G6.md").exists()


def test_failure_exits_cleanly_without_a_bundle(hostile):
    repo, base, _head, tmp = hostile
    r = run_builder(repo, base, "0" * 40, tmp / "out")
    assert r.returncode == 2
    assert r.stderr.strip().startswith("review-context build failed:")
    assert len(r.stderr.strip().splitlines()) == 1
    assert not (tmp / "out" / "pr1.G6.md").exists()
