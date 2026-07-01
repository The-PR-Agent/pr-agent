import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers.bitbucket_app import should_process_pr_logic as bitbucket_should_process_pr_logic
from pr_agent.servers.github_app import should_process_pr_logic as github_should_process_pr_logic
from pr_agent.servers.gitlab_webhook import should_process_pr_logic as gitlab_should_process_pr_logic


def make_bitbucket_payload(full_name):
    return {
        "data": {
            "pullrequest": {
                "title": "Test PR",
                "source": {"branch": {"name": "feature/test"}},
                "destination": {
                    "branch": {"name": "main"},
                    "repository": {"full_name": full_name}
                }
            },
            "actor": {"username": "user", "type": "user"}
        }
    }

def make_github_body(full_name):
    return {
        "pull_request": {},
        "repository": {"full_name": full_name},
        "sender": {"login": "user"}
    }

def make_gitlab_body(full_name):
    return {
        "object_attributes": {"title": "Test MR"},
        "project": {"path_with_namespace": full_name}
    }

PROVIDERS = [
    ("github", github_should_process_pr_logic, make_github_body),
    ("bitbucket", bitbucket_should_process_pr_logic, make_bitbucket_payload),
    ("gitlab", gitlab_should_process_pr_logic, make_gitlab_body),
]

class TestIgnoreRepositories:
    def setup_method(self):
        get_settings().set("CONFIG.IGNORE_REPOSITORIES", [])

    @pytest.mark.parametrize("provider_name, provider_func, body_func", PROVIDERS)
    def test_should_ignore_matching_repository(self, provider_name, provider_func, body_func):
        get_settings().set("CONFIG.IGNORE_REPOSITORIES", ["org/repo-to-ignore"])
        body = {
            "pull_request": {},
            "repository": {"full_name": "org/repo-to-ignore"},
            "sender": {"login": "user"}
        }
        result = provider_func(body_func(body["repository"]["full_name"]))
        # print(f"DEBUG: Provider={provider_name}, test_should_ignore_matching_repository, result={result}")
        assert result is False, f"{provider_name}: PR from ignored repository should be ignored (return False)"

    @pytest.mark.parametrize("provider_name, provider_func, body_func", PROVIDERS)
    def test_should_not_ignore_non_matching_repository(self, provider_name, provider_func, body_func):
        get_settings().set("CONFIG.IGNORE_REPOSITORIES", ["org/repo-to-ignore"])
        body = {
            "pull_request": {},
            "repository": {"full_name": "org/other-repo"},
            "sender": {"login": "user"}
        }
        result = provider_func(body_func(body["repository"]["full_name"]))
        # print(f"DEBUG: Provider={provider_name}, test_should_not_ignore_non_matching_repository, result={result}")
        assert result is True, f"{provider_name}: PR from non-ignored repository should not be ignored (return True)"

    @pytest.mark.parametrize("provider_name, provider_func, body_func", PROVIDERS)
    def test_should_not_ignore_when_config_empty(self, provider_name, provider_func, body_func):
        get_settings().set("CONFIG.IGNORE_REPOSITORIES", [])
        body = {
            "pull_request": {},
            "repository": {"full_name": "org/repo-to-ignore"},
            "sender": {"login": "user"}
        }
        result = provider_func(body_func(body["repository"]["full_name"]))
        # print(f"DEBUG: Provider={provider_name}, test_should_not_ignore_when_config_empty, result={result}")
        assert result is True, f"{provider_name}: PR should not be ignored if ignore_repositories config is empty" 