from unittest.mock import patch

from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider


def test_clone_keeps_bearer_token_out_of_process_arguments(tmp_path):
    provider = BitbucketServerProvider.__new__(BitbucketServerProvider)
    provider.bearer_token = "secret bearer token"
    captured = {}

    def fake_run(args, env, **kwargs):
        captured["args"] = args
        captured["env"] = env

    with patch("pr_agent.git_providers.bitbucket_server_provider.get_git_ssl_env", return_value={}), \
            patch("pr_agent.git_providers.bitbucket_server_provider.subprocess.run", side_effect=fake_run):
        provider._clone_inner("https://bitbucket.example/scm/team/repo.git", str(tmp_path / "repo"))

    assert "secret bearer token" not in " ".join(captured["args"])
    assert captured["env"]["GIT_CONFIG_COUNT"] == "1"
    assert captured["env"]["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert captured["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer secret bearer token"
