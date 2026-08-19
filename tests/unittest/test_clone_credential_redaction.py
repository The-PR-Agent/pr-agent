import pytest

from pr_agent.git_providers.git_provider import redact_credentials


@pytest.mark.parametrize("url, expected", [
    ("https://ghp_SECRET@github.com/acme/repo.git", "https://github.com/acme/repo.git"),
    ("https://oauth2:glpat-SECRET@gitlab.acme.com/t/p.git", "https://gitlab.acme.com/t/p.git"),
    ("https://user:pw@bitbucket.acme.com/scm/t/p.git", "https://bitbucket.acme.com/scm/t/p.git"),
])
def test_url_userinfo_is_stripped(url, expected):
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
