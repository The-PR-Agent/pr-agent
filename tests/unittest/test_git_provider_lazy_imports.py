import subprocess
import sys

_PROVIDER_MODULES = {
    "azure": "pr_agent.git_providers.azuredevops_provider",
    "bitbucket": "pr_agent.git_providers.bitbucket_provider",
    "bitbucket_server": "pr_agent.git_providers.bitbucket_server_provider",
    "codecommit": "pr_agent.git_providers.codecommit_provider",
    "gerrit": "pr_agent.git_providers.gerrit_provider",
    "gitea": "pr_agent.git_providers.gitea_provider",
    "github": "pr_agent.git_providers.github_provider",
    "gitlab": "pr_agent.git_providers.gitlab_provider",
    "local": "pr_agent.git_providers.local_git_provider",
    "plain-diff": "pr_agent.git_providers.plain_diff_provider",
}


def _run_python(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_importing_git_providers_does_not_import_builtin_provider_modules():
    source = f"""
import sys

import pr_agent.git_providers  # noqa: F401

provider_modules = {list(_PROVIDER_MODULES.values())!r}
loaded = [module for module in provider_modules if module in sys.modules]
assert loaded == [], loaded
"""

    result = _run_python(source)

    assert result.returncode == 0, result.stderr


def test_registry_loads_only_selected_builtin_provider():
    source = f"""
import sys

from pr_agent import git_providers

provider_modules = {list(_PROVIDER_MODULES.values())!r}
provider_class = git_providers._GIT_PROVIDERS["github"]

assert provider_class.__name__ == "GithubProvider"
loaded = [module for module in provider_modules if module in sys.modules]
assert loaded == ["pr_agent.git_providers.github_provider"], loaded
"""

    result = _run_python(source)

    assert result.returncode == 0, result.stderr


def test_package_level_provider_import_stays_compatible_and_lazy():
    source = f"""
import sys

from pr_agent.git_providers import GitLabProvider

provider_modules = {list(_PROVIDER_MODULES.values())!r}
assert GitLabProvider.__name__ == "GitLabProvider"
loaded = [module for module in provider_modules if module in sys.modules]
assert loaded == ["pr_agent.git_providers.gitlab_provider"], loaded
"""

    result = _run_python(source)

    assert result.returncode == 0, result.stderr
