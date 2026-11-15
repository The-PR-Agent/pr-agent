import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish.yml"
MAIN_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/publish-main-image.yml"
INVALID_VERSION_ERROR = (
    "Version must be a valid SemVer 2.0.0 value (for example, 0.35.0 or 0.35.0-rc.1)."
)
METADATA_ERROR = "SemVer build metadata is not supported for releases."


def test_release_version_input_is_not_interpolated_into_shell() -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    ref_guard_step = next(
        step
        for step in workflow["jobs"]["prepare"]["steps"]
        if step.get("name") == "Reject workflow_dispatch from non-main ref"
    )
    resolve_step = next(
        step for step in workflow["jobs"]["prepare"]["steps"] if step.get("name") == "Resolve version"
    )

    assert (
        'echo "::error::workflow_dispatch must be triggered from the main branch (got $GITHUB_REF)."'
        in ref_guard_step["run"]
    )
    assert resolve_step["env"]["RELEASE_INPUT_VERSION"] == "${{ inputs.version }}"
    assert 'VERSION="$RELEASE_INPUT_VERSION"' in resolve_step["run"]
    assert '[[ "$VERSION" =~ $SEMVER ]]' in resolve_step["run"]
    assert "grep -P" not in resolve_step["run"]
    assert f"::error::{INVALID_VERSION_ERROR}" in resolve_step["run"]
    assert f"::error::{METADATA_ERROR}" in resolve_step["run"]

    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            assert "inputs.version" not in step.get("run", "")


@pytest.mark.parametrize(
    ("event_name", "ref_name", "input_version", "expected"),
    [
        ("workflow_dispatch", "main", "1.2.3", ("1.2.3", "v1.2.3")),
        ("workflow_dispatch", "main", "1.0.0-alpha.1", ("1.0.0-alpha.1", "v1.0.0-alpha.1")),
        ("workflow_dispatch", "main", "1.0.0-x.7.z.92", ("1.0.0-x.7.z.92", "v1.0.0-x.7.z.92")),
        ("workflow_dispatch", "main", f"1.2.3-{'a' * 97}", (f"1.2.3-{'a' * 97}", f"v1.2.3-{'a' * 97}")),
        ("release", "v2.0.0-rc.1", "", ("2.0.0-rc.1", "v2.0.0-rc.1")),
        ("workflow_dispatch", "main", "1.2", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "01.2.3", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.0.0-01", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.0.0-alpha..1", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.2.3\nsha=attacker", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.2.3\rsha=attacker", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.2.3+build.7", METADATA_ERROR),
        ("workflow_dispatch", "main", "1.2.3-rc.1+build.7", METADATA_ERROR),
        ("workflow_dispatch", "main", "1.2.3+", INVALID_VERSION_ERROR),
        ("release", "v1.2.3+build.7", "", METADATA_ERROR),
        (
            "workflow_dispatch",
            "main",
            f"1.2.3-{'a' * 98}",
            "Version is too long for release image tags (maximum 103 characters).",
        ),
    ],
)
def test_release_version_resolves_or_rejects_without_injecting_outputs(
    tmp_path: Path,
    event_name: str,
    ref_name: str,
    input_version: str,
    expected: tuple[str, str] | str,
) -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    resolve_step = next(
        step for step in workflow["jobs"]["prepare"]["steps"] if step.get("name") == "Resolve version"
    )
    output = tmp_path / "github-output"
    if isinstance(expected, str):
        output.write_text("existing-output\n")
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_REF_NAME": ref_name,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_OUTPUT": str(output),
        "RELEASE_INPUT_VERSION": input_version,
    }

    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", resolve_step["run"]],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    if isinstance(expected, tuple):
        assert result.returncode == 0
        assert output.read_text().splitlines() == [f"version={expected[0]}", f"tag={expected[1]}", f"sha={'a' * 40}"]
    else:
        assert result.returncode != 0
        assert expected in result.stdout + result.stderr
        if expected == INVALID_VERSION_ERROR and "\n" not in input_version and "\r" not in input_version:
            assert f"Rejected version: {input_version}" in result.stdout
        assert output.read_text() == "existing-output\n"


def test_release_finalize_keeps_uv_lock_in_sync() -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    finalize_steps = workflow["jobs"]["finalize"]["steps"]
    bump_step = next(
        step for step in finalize_steps if step.get("name") == "Bump pyproject.toml and sync uv.lock"
    )
    commit_step = next(
        step for step in finalize_steps if step.get("name") == "Commit & push bump (fast-forward only)"
    )

    run = bump_step["run"]
    assert run.index("scripts/set_pyproject_version.py") < run.index("uv lock")
    assert "git diff --quiet pyproject.toml uv.lock" in commit_step["run"]
    assert "git add pyproject.toml uv.lock" in commit_step["run"]


@pytest.mark.parametrize("revision", ["main", "older-main", "annotated-tag", "side-branch", "missing-main"])
def test_published_commit_must_belong_to_main(tmp_path: Path, revision: str) -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    steps = workflow["jobs"]["prepare"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Require published commit to be on main")
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert steps.index(checkout) < steps.index(guard)
    assert "if" not in guard

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True, stderr=subprocess.PIPE).strip()

    git("init", "-b", "main")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "base")
    older_main = git("rev-parse", "HEAD")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "main")
    main = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", main)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "tag", "-a", "v1.2.3", "-m", "release")
    git("checkout", "-b", "side", older_main)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "side")
    sha = {
        "main": main,
        "older-main": older_main,
        "annotated-tag": git("rev-parse", "v1.2.3"),
        "side-branch": git("rev-parse", "HEAD"),
        "missing-main": main,
    }[revision]
    if revision == "missing-main":
        git("update-ref", "-d", "refs/remotes/origin/main")

    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", guard["run"]],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_SHA": sha},
        check=False,
        capture_output=True,
        text=True,
    )

    if revision in {"side-branch", "missing-main"}:
        assert result.returncode != 0
        assert "The published commit must be part of main history." in result.stdout
    else:
        assert result.returncode == 0, result.stderr


def test_pypi_build_is_separate_from_trusted_publisher() -> None:
    jobs = yaml.safe_load(PUBLISH_WORKFLOW.read_text())["jobs"]
    build = jobs["build-pypi"]
    publisher = jobs["publish-pypi"]
    assert build["timeout-minutes"] == 20
    assert publisher["timeout-minutes"] == 20
    assert build["needs"] == "prepare"
    assert build["permissions"] == {"contents": "read"}
    assert "environment" not in build
    assert "secrets." not in str(build)
    assert publisher["needs"] == ["prepare", "build-pypi"]
    assert publisher["environment"] == "release"
    assert publisher["permissions"] == {"id-token": "write"}
    assert jobs["publish-docker"]["needs"] == "prepare"
    assert "prepare" in jobs["finalize"]["needs"]
    assert "publish-pypi" in jobs["finalize"]["needs"]

    upload = next(step for step in build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    download, publish = publisher["steps"]
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"]["name"] == upload["with"]["name"]
    assert download["with"]["path"] == upload["with"]["path"] == "dist/"
    assert upload["with"]["if-no-files-found"] == "error"
    assert publish["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert publish["with"]["attestations"] is True
    assert publish["with"]["skip-existing"] is True
    assert "password" not in publish["with"]
    assert "user" not in publish["with"]
    assert "secrets." not in str(publisher)


def test_release_build_tools_are_pinned() -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    build = next(step for step in workflow["jobs"]["build-pypi"]["steps"] if step.get("name") == "Build distributions")
    install, command = build["run"].splitlines()
    assert install.startswith("python -m pip install build==") and command == "python -m build"
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text())
    assert all("==" in requirement for requirement in project["build-system"]["requires"])


def test_main_image_only_builds_trusted_main_and_publishes_the_review_tag() -> None:
    workflow = yaml.safe_load(MAIN_WORKFLOW.read_text())
    assert workflow[True] == {"push": {"branches": ["main"]}}  # PyYAML parses the key "on" as True.
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "publish-main-github-action", "cancel-in-progress": False}
    job, = workflow["jobs"].values()
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert "environment" not in job
    checkout, setup, build, login, publish = job["steps"]
    assert checkout["with"] == {"ref": "refs/heads/main", "persist-credentials": False}
    release_steps = yaml.safe_load(PUBLISH_WORKFLOW.read_text())["jobs"]["publish-docker"]["steps"]
    for step in (checkout, setup, build, login):
        assert any(step["uses"] == release.get("uses") for release in release_steps)
    tag = "pragent/pr-agent:github_action-main"
    assert build["with"] == {
        "context": ".", "file": "docker/Dockerfile", "target": "github_action", "platforms": "linux/amd64",
        "push": False, "load": True, "tags": tag,
        "labels": "org.opencontainers.image.revision=${{ steps.checkout.outputs.commit }}",
        "cache-from": "type=gha,scope=github_action-main", "cache-to": "type=gha,mode=max,scope=github_action-main",
    }
    assert checkout["id"] == "checkout"
    assert login["with"] == {
        "username": "${{ secrets.DOCKERHUB_USERNAME }}", "password": "${{ secrets.DOCKERHUB_TOKEN }}",
    }
    assert str(workflow).count("secrets.") == 2
    assert publish["run"].splitlines()[-1] == f"docker push {tag}"
    assert f"uses: docker://{tag}" in (REPOSITORY_ROOT / ".github/workflows/pr-agent-review.yaml").read_text()


@pytest.mark.parametrize("revision", ["current", "stale", "missing-main", "remote-error"])
def test_main_image_push_requires_current_remote_main(tmp_path: Path, revision: str) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True, stderr=subprocess.PIPE).strip()

    git("init", "-b", "main", "origin")
    commit = ("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m")
    git("-C", "origin", *commit, "base")
    git("clone", str(tmp_path / "origin"), "workspace")
    if revision == "stale":
        git("-C", "origin", *commit, "new main")
    elif revision == "missing-main":
        git("-C", "origin", "update-ref", "-d", "refs/heads/main")
    elif revision == "remote-error":
        git("-C", "workspace", "remote", "set-url", "origin", str(tmp_path / "missing"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PUSH_LOG"\n')
    docker.chmod(0o755)
    push_log = tmp_path / "push-log"
    publish = yaml.safe_load(MAIN_WORKFLOW.read_text())["jobs"]["publish"]["steps"][-1]
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", publish["run"]], cwd=tmp_path / "workspace",
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "PUSH_LOG": str(push_log), "GITHUB_SHA": "0" * 40},  # A delayed event may refer to an older commit.
        capture_output=True, text=True, check=False,
    )
    assert (result.returncode == 0) == (revision in {"current", "stale"}), result.stderr
    if revision == "current":
        assert push_log.read_text() == "push pragent/pr-agent:github_action-main\n"
    else:
        assert not push_log.exists()
