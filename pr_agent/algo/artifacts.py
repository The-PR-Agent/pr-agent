import os
from pathlib import Path
from typing import Optional

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


ARTIFACT_PARSERS = {}


def register_parser(artifact_type: str):
    def decorator(func):
        ARTIFACT_PARSERS[artifact_type] = func
        return func
    return decorator


@register_parser("generic")
def parse_generic(content: str, label: str) -> str:
    header = f"CI Artifact: {label}" if label else "CI Artifact"
    return (
        f"{header}\n"
        f"=====\n"
        f"{content}\n"
        f"=====\n"
        f"Consider this artifact as additional context when analyzing the PR. "
        f"It was produced by a prior CI step."
    )


@register_parser("terraform_plan")
def parse_terraform_plan(content: str, label: str) -> str:
    header = f"Terraform Plan Output: {label}" if label else "Terraform Plan Output"
    return (
        f"{header}\n"
        f"=====\n"
        f"{content}\n"
        f"=====\n"
        f"This is the Terraform execution plan for the infrastructure changes in this PR. "
        f"Use it to verify that the code changes produce the intended infrastructure modifications. "
        f"Flag any unexpected resource deletions or risky changes."
    )


@register_parser("test_report")
def parse_test_report(content: str, label: str) -> str:
    header = f"Test Results: {label}" if label else "Test Results"
    return (
        f"{header}\n"
        f"=====\n"
        f"{content}\n"
        f"=====\n"
        f"These are the test results from the CI pipeline for this PR. "
        f"If there are failures, correlate them with the code changes in the diff. "
        f"Note any tests that are newly failing."
    )


def resolve_artifact_path(path: str) -> Optional[Path]:
    if not path:
        return None

    artifact_path = Path(path)
    if artifact_path.is_absolute():
        return artifact_path if artifact_path.is_file() else None

    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    if workspace:
        resolved = Path(workspace) / artifact_path
        if resolved.is_file():
            return resolved

    resolved = artifact_path.resolve()
    if resolved.is_file():
        return resolved

    return None


def _read_and_truncate(path: Path, max_size: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, IOError) as e:
        get_logger().warning(f"Failed to read artifact file {path}: {e}")
        return ""

    if len(content) > max_size:
        content = content[:max_size] + "\n\n[... content truncated due to size limit ...]"
    return content


def load_artifact_content(tool_key: str) -> str:
    try:
        artifacts_settings = get_settings().get("ARTIFACTS", {})
    except AttributeError:
        return ""

    if not artifacts_settings:
        return ""

    enabled = artifacts_settings.get("enable", False)
    if not enabled:
        return ""

    artifact_path_str = artifacts_settings.get("artifact_path", "")
    if not artifact_path_str:
        return ""

    target_tools = artifacts_settings.get("target_tools",
                                          ["pr_reviewer", "pr_description", "pr_code_suggestions"])
    if tool_key not in target_tools:
        return ""

    artifact_path = resolve_artifact_path(artifact_path_str)
    if not artifact_path:
        get_logger().warning(
            f"Artifact file not found: '{artifact_path_str}' "
            f"(GITHUB_WORKSPACE={os.environ.get('GITHUB_WORKSPACE', 'not set')})"
        )
        return ""

    max_size = int(artifacts_settings.get("max_artifact_size", 50000))
    content = _read_and_truncate(artifact_path, max_size)
    if not content:
        return ""

    artifact_type = artifacts_settings.get("artifact_type", "generic")
    label = artifacts_settings.get("artifact_label", "") or artifact_path.name

    parser = ARTIFACT_PARSERS.get(artifact_type, ARTIFACT_PARSERS["generic"])
    return parser(content, label)
