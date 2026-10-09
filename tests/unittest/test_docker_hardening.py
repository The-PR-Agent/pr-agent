"""Guard runtime identities, external image pins and the Docker build context.

These configuration checks do not replace container builds or startup smoke tests.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICE_TARGETS = (
    "github_app", "bitbucket_app", "bitbucket_server_webhook", "github_polling",
    "gitlab_webhook", "azure_devops_webhook", "gitea_app", "mosaico_agent",
)


def _stage_users():
    users = {}
    stage = None
    for line in (ROOT / "docker/Dockerfile").read_text().splitlines():
        match = re.match(r"FROM (\S+) AS (\S+)", line, re.IGNORECASE)
        if match:
            parent, stage = match.groups()
            users[stage] = users.get(parent, "0")
        elif line.startswith("USER "):
            users[stage] = line.split()[1]
    return users


@pytest.mark.parametrize("target", SERVICE_TARGETS)
def test_service_targets_configure_non_root_identity(target):
    assert _stage_users()[target] == "10001:10001"


@pytest.mark.parametrize("target", ("github_action", "cli", "test"))
def test_runner_and_development_targets_configure_root(target):
    assert _stage_users()[target] in ("0", "root")


@pytest.mark.parametrize("filename", ("Dockerfile", "Dockerfile.lambda"))
def test_external_build_images_are_digest_pinned(filename):
    content = (ROOT / "docker" / filename).read_text()
    images = re.findall(r"^FROM (\S+)", content, re.MULTILINE)
    images += re.findall(r"^COPY --from=(\S+)", content, re.MULTILINE)
    external = [image for image in images if ":" in image or "/" in image]
    assert external
    assert all(re.fullmatch(r".+:[^@]+@sha256:[0-9a-f]{64}", image) for image in external)


def test_dockerignore_lists_both_supported_secret_files():
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    for folder in ("settings", "settings_prod"):
        assert f"pr_agent/{folder}/.secrets.toml" in patterns
        assert f"pr_agent/{folder}/.secrets_template.toml" not in patterns
