from types import SimpleNamespace

from pr_agent.algo.cli_args import CliArgs
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
from pr_agent.git_providers.gitea_provider import GiteaProvider
from pr_agent.git_providers.github_provider import GithubProvider
from pr_agent.git_providers.gitlab_provider import GitLabProvider


def _make_stub(token="ghp_SECRETTOKEN", base_url_html="https://github.com", deployment_type="user"):
    """Build a real GithubProvider instance without running __init__, setting only the
    attributes used by _prepare_clone_url_with_token (inherited base helpers stay available)."""
    stub = object.__new__(GithubProvider)
    stub.auth = SimpleNamespace(token=token)
    stub.base_url_html = base_url_html
    stub.deployment_type = deployment_type
    return stub


def _prepare(stub, url):
    return stub._prepare_clone_url_with_token(url)


class TestGithubCloneUrlValidation:
    """Regression tests for issue #2445: weak hostname validation in
    _prepare_clone_url_with_token allowed embedding GITHUB_TOKEN into clone
    URLs pointing at attacker-controlled hosts that merely *contained* the
    string 'github.com'."""

    def test_legitimate_github_url_is_accepted(self):
        stub = _make_stub()
        clone_url = _prepare(stub, "https://github.com/Codium-ai/pr-agent-pro.git")
        assert clone_url == "https://ghp_SECRETTOKEN@github.com/Codium-ai/pr-agent-pro.git"

    def test_legitimate_github_url_without_scheme_is_accepted(self):
        stub = _make_stub()
        clone_url = _prepare(stub, "github.com/Codium-ai/pr-agent-pro.git")
        assert clone_url == "https://ghp_SECRETTOKEN@github.com/Codium-ai/pr-agent-pro.git"

    def test_app_deployment_uses_git_prefix(self):
        stub = _make_stub(deployment_type="app")
        clone_url = _prepare(stub, "https://github.com/owner/repo.git")
        assert clone_url == "https://git:ghp_SECRETTOKEN@github.com/owner/repo.git"

    def test_lookalike_subdomain_host_is_rejected(self):
        # The core exploit: 'github.com.<attacker>' contains 'github.com' but is a different host.
        stub = _make_stub()
        assert _prepare(stub, "github.com.attacker.example/owner/repo") is None
        assert _prepare(stub, "https://github.com.attacker.example/owner/repo") is None

    def test_unrelated_host_with_github_com_in_path_is_rejected(self):
        stub = _make_stub()
        assert _prepare(stub, "https://attacker.example/github.com/owner/repo") is None

    def test_userinfo_injected_host_is_rejected(self):
        # 'github.com' placed in the userinfo, real host is the attacker's.
        stub = _make_stub()
        assert _prepare(stub, "https://github.com@attacker.example/owner/repo") is None

    def test_embedded_credentials_are_rejected(self):
        stub = _make_stub()
        assert _prepare(stub, "https://user:pass@github.com/owner/repo") is None

    def test_missing_repo_path_is_rejected(self):
        stub = _make_stub()
        assert _prepare(stub, "https://github.com") is None
        assert _prepare(stub, "https://github.com/") is None

    def test_path_with_space_is_rejected(self):
        # Spaces survive urlparse (unlike tab/CR/LF, which it strips), so a space in the path could
        # inject extra git arguments when the url is interpolated into a command string. Reject it.
        stub = _make_stub()
        assert _prepare(stub, "https://github.com/a -c core.fsmonitor=evil /repo.git") is None

    def test_enterprise_host_exact_match(self):
        stub = _make_stub(base_url_html="https://github.acme.com")
        assert _prepare(stub, "https://github.acme.com/owner/repo.git") == \
            "https://ghp_SECRETTOKEN@github.acme.com/owner/repo.git"
        # github.com is NOT the enterprise host -> rejected
        assert _prepare(stub, "https://github.com/owner/repo.git") is None


class TestSiblingProvidersCloneUrlValidation:
    """Issue #2445 (class fix): the same weak substring host check existed in the
    GitLab, Gitea and Bitbucket providers, each embedding its own token. Validate
    that the lookalike-host exfiltration is now rejected across providers while
    legitimate hosts still work."""

    @staticmethod
    def _stub(cls, **attrs):
        stub = object.__new__(cls)
        for k, v in attrs.items():
            setattr(stub, k, v)
        return stub

    def test_gitlab_accepts_configured_host_and_rejects_lookalike(self):
        stub = self._stub(GitLabProvider,
                          gl=SimpleNamespace(oauth_token="glpat-TOKEN", private_token=None),
                          gitlab_url="https://gitlab.com")
        assert stub._prepare_clone_url_with_token("https://gitlab.com/qodo/autoscraper.git") == \
            "https://oauth2:glpat-TOKEN@gitlab.com/qodo/autoscraper.git"
        assert stub._prepare_clone_url_with_token("https://gitlab.com.attacker.example/o/r") is None
        assert stub._prepare_clone_url_with_token("https://gitlab.com@attacker.example/o/r") is None

    def test_gitlab_self_hosted_host(self):
        stub = self._stub(GitLabProvider,
                          gl=SimpleNamespace(oauth_token="glpat-TOKEN", private_token=None),
                          gitlab_url="https://gitlab.codium-inc.com")
        assert stub._prepare_clone_url_with_token("https://gitlab.codium-inc.com/qodo/x.git") == \
            "https://oauth2:glpat-TOKEN@gitlab.codium-inc.com/qodo/x.git"
        # gitlab.com is not the configured self-hosted host
        assert stub._prepare_clone_url_with_token("https://gitlab.com/qodo/x.git") is None

    def test_gitlab_self_hosted_non_standard_port_is_preserved(self):
        stub = self._stub(GitLabProvider,
                          gl=SimpleNamespace(oauth_token="glpat-TOKEN", private_token=None),
                          gitlab_url="https://gitlab.codium-inc.com:8443")
        assert stub._prepare_clone_url_with_token("https://gitlab.codium-inc.com:8443/qodo/x.git") == \
            "https://oauth2:glpat-TOKEN@gitlab.codium-inc.com:8443/qodo/x.git"

    def test_gitea_accepts_configured_host_and_rejects_lookalike(self):
        stub = self._stub(GiteaProvider, gitea_access_token="gt-TOKEN", base_url="https://gitea.com")
        assert stub._prepare_clone_url_with_token("https://gitea.com/owner/repo.git") == \
            "https://gt-TOKEN@gitea.com/owner/repo.git"
        assert stub._prepare_clone_url_with_token("https://gitea.com.attacker.example/o/r") is None

    def test_bitbucket_cloud_accepts_org_and_rejects_lookalike(self):
        stub = self._stub(BitbucketProvider, auth_type="bearer", bearer_token="bb-TOKEN", basic_token=None)
        assert stub._prepare_clone_url_with_token("https://bitbucket.org/o/r.git") == \
            "https://x-token-auth:bb-TOKEN@bitbucket.org/o/r.git"
        assert stub._prepare_clone_url_with_token("https://bitbucket.org.attacker.example/o/r") is None
        assert stub._prepare_clone_url_with_token("https://bitbucket.org@attacker.example/o/r") is None

    def test_bitbucket_server_rebuilds_from_trusted_host(self):
        stub = self._stub(BitbucketServerProvider, bearer_token="bs-TOKEN",
                          bitbucket_server_url="https://bitbucket.mycompany.com")
        # Token travels as an Authorization header, so the returned url must point at the trusted host.
        assert stub._prepare_clone_url_with_token("https://bitbucket.mycompany.com/scm/proj/repo.git") == \
            "https://bitbucket.mycompany.com/scm/proj/repo.git"
        # 'bitbucket.attacker.example' merely contains 'bitbucket.' -> must be rejected.
        assert stub._prepare_clone_url_with_token("https://bitbucket.attacker.example/scm/proj/repo.git") is None
        assert stub._prepare_clone_url_with_token(
            "https://bitbucket.mycompany.com.attacker.example/scm/proj/repo.git") is None
        # Bitbucket Server interpolates the url into a git command; a space-injected git flag must be rejected.
        assert stub._prepare_clone_url_with_token(
            "https://bitbucket.mycompany.com/a -c core.fsmonitor=evil /repo.git") is None


class TestCliArgsHelpDocsBlocklist:
    """Issue #2445: untrusted comment commands must not be able to override the
    /help_docs clone target (or related path/branch) at runtime."""

    def test_repo_url_override_is_forbidden(self):
        is_valid, _ = CliArgs.validate_user_args(["--pr_help_docs.repo_url=https://github.com/x/y"])
        assert is_valid is False

    def test_repo_url_override_double_underscore_is_forbidden(self):
        is_valid, _ = CliArgs.validate_user_args(["--pr_help_docs__repo_url=https://github.com/x/y"])
        assert is_valid is False

    def test_repo_default_branch_override_is_forbidden(self):
        is_valid, _ = CliArgs.validate_user_args(["--pr_help_docs.repo_default_branch=main"])
        assert is_valid is False

    def test_docs_path_override_is_forbidden(self):
        is_valid, _ = CliArgs.validate_user_args(["--pr_help_docs.docs_path=docs"])
        assert is_valid is False

    def test_unrelated_help_docs_arg_is_allowed(self):
        is_valid, _ = CliArgs.validate_user_args(["--pr_help_docs.exclude_root_readme=true"])
        assert is_valid is True
