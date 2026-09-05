import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PUBLISH_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish.yml"
INVALID_VERSION_ERROR = (
    "Version must be a valid SemVer 2.0.0 value (for example, 0.35.0 or 0.35.0-rc.1)."
)


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
    assert "::error::SemVer build metadata is not supported for releases." in resolve_step["run"]

    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            assert "inputs.version" not in step.get("run", "")


@pytest.mark.parametrize(
    ("event_name", "ref_name", "input_version", "expected_version", "expected_tag"),
    [
        ("workflow_dispatch", "main", "1.2.3", "1.2.3", "v1.2.3"),
        ("workflow_dispatch", "main", "1.0.0-alpha.1", "1.0.0-alpha.1", "v1.0.0-alpha.1"),
        ("workflow_dispatch", "main", "1.0.0-x.7.z.92", "1.0.0-x.7.z.92", "v1.0.0-x.7.z.92"),
        ("workflow_dispatch", "main", f"1.2.3-{'a' * 97}", f"1.2.3-{'a' * 97}", f"v1.2.3-{'a' * 97}"),
        ("release", "v2.0.0-rc.1", "", "2.0.0-rc.1", "v2.0.0-rc.1"),
    ],
)
def test_release_version_writes_expected_outputs(
    tmp_path: Path,
    event_name: str,
    ref_name: str,
    input_version: str,
    expected_version: str,
    expected_tag: str,
) -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    resolve_step = next(
        step for step in workflow["jobs"]["prepare"]["steps"] if step.get("name") == "Resolve version"
    )
    output = tmp_path / "github-output"
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

    assert result.returncode == 0
    assert output.read_text().splitlines() == [
        f"version={expected_version}",
        f"tag={expected_tag}",
        f"sha={'a' * 40}",
    ]


@pytest.mark.parametrize(
    ("event_name", "ref_name", "input_version", "expected_error"),
    [
        ("workflow_dispatch", "main", "1.2", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "01.2.3", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.0.0-01", INVALID_VERSION_ERROR),
        ("workflow_dispatch", "main", "1.0.0-alpha..1", INVALID_VERSION_ERROR),
        (
            "workflow_dispatch",
            "main",
            "1.2.3+build.7",
            "SemVer build metadata is not supported for releases.",
        ),
        (
            "workflow_dispatch",
            "main",
            "1.2.3-rc.1+build.7",
            "SemVer build metadata is not supported for releases.",
        ),
        ("workflow_dispatch", "main", "1.2.3+", INVALID_VERSION_ERROR),
        (
            "release",
            "v1.2.3+build.7",
            "",
            "SemVer build metadata is not supported for releases.",
        ),
        (
            "workflow_dispatch",
            "main",
            f"1.2.3-{'a' * 98}",
            "Version is too long for release image tags (maximum 103 characters).",
        ),
    ],
)
def test_release_version_rejects_unsupported_or_invalid_versions(
    tmp_path: Path,
    event_name: str,
    ref_name: str,
    input_version: str,
    expected_error: str,
) -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    resolve_step = next(
        step for step in workflow["jobs"]["prepare"]["steps"] if step.get("name") == "Resolve version"
    )
    output = tmp_path / "github-output"
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

    assert result.returncode != 0
    assert expected_error in result.stdout + result.stderr
    if expected_error == INVALID_VERSION_ERROR:
        assert f"Rejected version: {input_version}" in result.stdout
    assert output.read_text() == "existing-output\n"


@pytest.mark.parametrize("line_break", ["\n", "\r"])
def test_release_version_cannot_inject_workflow_outputs(tmp_path: Path, line_break: str) -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text())
    resolve_step = next(
        step for step in workflow["jobs"]["prepare"]["steps"] if step.get("name") == "Resolve version"
    )
    output = tmp_path / "github-output"
    output.write_text("existing-output\n")
    env = {
        **os.environ,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF_NAME": "main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_OUTPUT": str(output),
        "RELEASE_INPUT_VERSION": f"1.2.3{line_break}sha=attacker",
    }

    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", resolve_step["run"]],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert output.read_text() == "existing-output\n"
