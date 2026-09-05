from types import SimpleNamespace

import pytest

from pr_agent.git_providers.git_provider import redact_credentials


@pytest.mark.parametrize("url, expected", [
    ("https://ghp_SECRET@github.com/acme/repo.git", "https://github.com/acme/repo.git"),
    ("https://oauth2:glpat-SECRET@gitlab.acme.com/t/p.git", "https://gitlab.acme.com/t/p.git"),
    ("https://user:pw@bitbucket.acme.com/scm/t/p.git", "https://bitbucket.acme.com/scm/t/p.git"),
])
def test_url_userinfo_is_stripped(url, expected):
    assert redact_credentials(url) == expected


@pytest.mark.parametrize("url, expected", [
    # scheme longer than the bounded quantifier in _URL_USERINFO_RE
    ("git+ssh+a-very-long-custom-scheme-name://oauth2:glpat-SECRET@host.example/t/p.git",
     "git+ssh+a-very-long-custom-scheme-name://host.example/t/p.git"),
    # Bitbucket builds x-token-auth:<bearer>@, and a JWT runs past any bound worth writing.
    ("https://x-token-auth:" + "A" * 4000 + "@bitbucket.org/acme/repo.git",
     "https://bitbucket.org/acme/repo.git"),
])
def test_a_long_scheme_or_credential_does_not_leave_a_redaction_gap(url, expected):
    assert redact_credentials(url) == expected


def test_url_without_credentials_is_unchanged():
    url = "https://github.com/acme/repo.git"
    assert redact_credentials(url) == url


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "Basic", "token"])
def test_authorization_header_value_is_masked(scheme):
    text = f"http.extraHeader=Authorization: {scheme} SUPER-SECRET-VALUE"
    redacted = redact_credentials(text)
    assert "SUPER-SECRET-VALUE" not in redacted
    assert "<redacted>" in redacted


def test_called_process_error_argv_is_redacted():
    argv_text = (
        "Command '['git', 'clone', '-c', "
        "'http.extraHeader=Authorization: Bearer BBDC-SECRET', "
        "'https://oauth2:glpat-SECRET@gitlab.acme.com/t/p.git', '/tmp/x']' "
        "returned non-zero exit status 128."
    )
    redacted = redact_credentials(argv_text)
    assert "BBDC-SECRET" not in redacted
    assert "glpat-SECRET" not in redacted


def test_empty_input_is_safe():
    assert redact_credentials(None) == ""
    assert redact_credentials("") == ""


class TestCloneUrlValidationLogs:
    """Keep credentials out of the validation-failure logs in _prepare_clone_url_with_token.

    Each provider derives a value from a configured base URL and prints it next to the
    redacted repository URL, so those derived values must be redacted too.

    The providers are built with __new__ and given only the attributes the method under
    test reads: their __init__ performs network setup, and subclassing to override it
    would leave a subclass whose __init__ never calls super().
    """

    @staticmethod
    def _capture(call):
        import io

        from pr_agent.log import get_logger

        buffer = io.StringIO()
        handler_id = get_logger().add(buffer, level="DEBUG", format="{message}", colorize=False)
        try:
            call()
        finally:
            get_logger().remove(handler_id)
        return buffer.getvalue()

    def test_keep_the_gitlab_scheme_out_of_the_logs(self):
        """Redact the GitLab scheme, which is everything before "gitlab." and so carries
        any userinfo the configured URL embeds."""
        from pr_agent.git_providers.gitlab_provider import GitLabProvider

        provider = GitLabProvider.__new__(GitLabProvider)
        provider.gl = SimpleNamespace(oauth_token=None, private_token=None)

        logged = self._capture(lambda: provider._prepare_clone_url_with_token(
            "https://oauth2:glpat-SECRET@gitlab.example.com/team/docs.git"))

        assert logged, "nothing was logged, so the assertion below would be vacuous"
        assert "glpat-SECRET" not in logged

    def test_keep_the_github_base_url_out_of_the_logs(self):
        """Redact the GitHub base URL, since the host slice derived from it keeps any
        userinfo but loses the scheme the redaction regex anchors on."""
        from pr_agent.git_providers.github_provider import GithubProvider

        provider = GithubProvider.__new__(GithubProvider)
        provider.auth = SimpleNamespace(token="ghp_TOKEN")
        provider.base_url_html = "https://user:GHSECRET@ghe.example"

        logged = self._capture(lambda: provider._prepare_clone_url_with_token(
            "https://ghe.example/org/repo.git"))

        assert logged, "nothing was logged, so the assertion below would be vacuous"
        assert "GHSECRET" not in logged

    def test_keep_the_gitea_base_url_out_of_the_logs(self):
        """Redact the Gitea base URL for the same reason as the GitHub copy."""
        from pr_agent.git_providers.gitea_provider import GiteaProvider

        provider = GiteaProvider.__new__(GiteaProvider)
        provider.gitea_access_token = "gt_TOKEN"
        provider.base_url = "https://user:GTSECRET@gitea.example"

        logged = self._capture(lambda: provider._prepare_clone_url_with_token(
            "https://gitea.example/org/repo.git"))

        assert logged, "nothing was logged, so the assertion below would be vacuous"
        assert "GTSECRET" not in logged
