import tomllib
from pathlib import Path

import pytest

from pr_agent import git_providers

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile"
LAMBDA_DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile.lambda"

INTEGRATION_EXTRAS = (
    "github",
    "gitlab",
    "bitbucket",
    "azure",
    "codecommit",
    "gitea",
    "google",
    "mosaico",
)


def _project_metadata():
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]


def test_provider_sdks_are_not_base_dependencies_and_all_is_complete():
    project = _project_metadata()
    base_dependencies = set(project["dependencies"])
    extras = project["optional-dependencies"]

    expected_all = {
        requirement
        for extra in INTEGRATION_EXTRAS
        for requirement in extras[extra]
    }

    assert base_dependencies.isdisjoint(expected_all)
    assert set(extras["all"]) == expected_all


def test_docker_syncs_keep_the_full_integration_set():
    docker_sync_lines = [
        line.strip()
        for line in DOCKERFILE.read_text().splitlines()
        if line.lstrip().startswith("RUN uv sync")
    ]
    lambda_sync_lines = [
        line.strip()
        for line in LAMBDA_DOCKERFILE.read_text().splitlines()
        if line.lstrip().startswith("RUN uv sync")
    ]

    assert docker_sync_lines
    assert lambda_sync_lines
    assert all("--extra all" in line for line in docker_sync_lines)
    assert all("--extra all" in line for line in lambda_sync_lines)


def test_missing_provider_dependency_points_to_the_matching_extra(monkeypatch):
    registry = git_providers._LazyGitProviderRegistry(
        {"github": ("pr_agent.git_providers.github_provider", "GithubProvider")}
    )

    def missing_dependency(_module_name):
        raise ModuleNotFoundError("No module named 'github'", name="github")

    monkeypatch.setattr(git_providers, "import_module", missing_dependency)

    with pytest.raises(ImportError, match=r"pr-agent\[github\]"):
        registry["github"]
