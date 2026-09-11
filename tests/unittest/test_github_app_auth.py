from unittest.mock import MagicMock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider


class TestGithubAppAuth:
    def test_integer_app_id_produces_valid_jwt(self):
        """GitHub App ids are integers in the settings toml. PyJWT >=2.11 rejects a
        non string `iss` claim and PyGithub 1.59 passed the id through raw (#2955,
        previously #2210; upstream fixed the pass through in PyGithub#3272, which
        landed in 2.7.0, so the 2.10 pin ships it). The provider keeps casting to
        str before building the authentication, which is thus harmless on the 2.10
        pin while it stays a hard requirement for the older 1.59 behaviour.
        """
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

        settings = get_settings()
        original = {
            "GITHUB.DEPLOYMENT_TYPE": settings.get("GITHUB.DEPLOYMENT_TYPE", None),
            "GITHUB.PRIVATE_KEY": settings.get("GITHUB.PRIVATE_KEY", None),
            "GITHUB.APP_ID": settings.get("GITHUB.APP_ID", None),
        }
        settings.set("GITHUB.DEPLOYMENT_TYPE", "app")
        settings.set("GITHUB.PRIVATE_KEY", private_key_pem)
        settings.set("GITHUB.APP_ID", 123456)  # integer, as toml parses it
        try:
            provider = GithubProvider.__new__(GithubProvider)
            provider.installation_id = 987654
            provider.base_url = "https://api.github.com"
            provider._get_github_client()

            # Signing the app JWT is offline; PyJWT >=2.11 raises
            # "Issuer (iss) must be a string" here without the str() cast.
            token = provider.auth._app_auth.create_jwt()
            claims = jwt.decode(
                token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False}
            )
            assert claims["iss"] == "123456"
        finally:
            for name, value in original.items():
                settings.set(name, value)

    def test_client_defaults_to_no_pacing_and_no_retry(self, monkeypatch):
        """PyGithub 2.x turns on pacing and 10 retries by default; PR-Agent must
        stay behaviour-neutral unless configured. The [github] settings mirror the
        1.59 defaults and the client constructor applies them explicitly
        (https://github.com/The-PR-Agent/pr-agent/pull/3240)."""
        settings = get_settings()
        original = {
            "GITHUB.DEPLOYMENT_TYPE": settings.get("GITHUB.DEPLOYMENT_TYPE", None),
            "GITHUB.USER_TOKEN": settings.get("GITHUB.USER_TOKEN", None),
        }
        settings.set("GITHUB.DEPLOYMENT_TYPE", "user")
        settings.set("GITHUB.USER_TOKEN", "stub-token")
        captured = {}

        def fake_github(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("pr_agent.git_providers.github_provider.Github", fake_github)
        provider = GithubProvider.__new__(GithubProvider)
        provider.installation_id = None
        provider.base_url = "https://api.github.com"
        try:
            provider._get_github_client()
        finally:
            for name, value in original.items():
                settings.set(name, value)

        assert captured["seconds_between_requests"] == 0
        assert captured["seconds_between_writes"] == 0
        assert captured["retry"] is None

    def test_pacing_and_retry_settings_reach_the_client(self, monkeypatch):
        """Operators may opt into PyGithub 2.x throttling and backoff via the
        [github] settings; each knob must land on the constructor (PR #3240)."""
        settings = get_settings()
        original = {
            "GITHUB.DEPLOYMENT_TYPE": settings.get("GITHUB.DEPLOYMENT_TYPE", None),
            "GITHUB.USER_TOKEN": settings.get("GITHUB.USER_TOKEN", None),
            "GITHUB.SECONDS_BETWEEN_REQUESTS": settings.get("GITHUB.SECONDS_BETWEEN_REQUESTS", None),
            "GITHUB.SECONDS_BETWEEN_WRITES": settings.get("GITHUB.SECONDS_BETWEEN_WRITES", None),
            "GITHUB.API_RETRIES": settings.get("GITHUB.API_RETRIES", None),
        }
        settings.set("GITHUB.DEPLOYMENT_TYPE", "user")
        settings.set("GITHUB.USER_TOKEN", "stub-token")
        settings.set("GITHUB.SECONDS_BETWEEN_REQUESTS", 0.25)
        settings.set("GITHUB.SECONDS_BETWEEN_WRITES", 1.0)
        settings.set("GITHUB.API_RETRIES", 4)
        captured = {}

        def fake_github(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        monkeypatch.setattr("pr_agent.git_providers.github_provider.Github", fake_github)
        provider = GithubProvider.__new__(GithubProvider)
        provider.installation_id = None
        provider.base_url = "https://api.github.com"
        try:
            provider._get_github_client()
        finally:
            for name, value in original.items():
                settings.set(name, value)

        assert captured["seconds_between_requests"] == 0.25
        assert captured["seconds_between_writes"] == 1.0
        assert captured["retry"].total == 4

    def test_is_bot_user_reads_github_section_setting(self):
        """is_bot_user must honor `ignore_bot_pr` from the [github] section
        (#3017): configuration.toml sets it under [github], so reading
        GITHUB_APP.IGNORE_BOT_PR left the guard permanently off."""
        from pr_agent.servers.github_app import is_bot_user

        settings = get_settings()
        original = settings.get("GITHUB.IGNORE_BOT_PR", None)
        settings.set("GITHUB.IGNORE_BOT_PR", True)
        try:
            assert is_bot_user("some-bot[bot]", "Bot") is True
            assert is_bot_user("human", "User") is False
        finally:
            settings.set("GITHUB.IGNORE_BOT_PR", original)

    def test_is_bot_user_off_when_setting_unset(self):
        """With the setting off, bot senders are not filtered."""
        from pr_agent.servers.github_app import is_bot_user

        settings = get_settings()
        original = settings.get("GITHUB.IGNORE_BOT_PR", None)
        settings.set("GITHUB.IGNORE_BOT_PR", False)
        try:
            assert is_bot_user("some-bot[bot]", "Bot") is False
        finally:
            settings.set("GITHUB.IGNORE_BOT_PR", original)

    def test_ignore_bot_pr_ships_under_the_github_section(self):
        """The premise of #3017's fix, read from the file rather than settings.

        `is_bot_user` falls back to `[github]` only because that is the section
        `configuration.toml` ships the option in. If it ever moves, the fallback
        silently becomes the only reader of a key nobody sets, and the two tests
        above would still pass because they set `GITHUB.IGNORE_BOT_PR`
        themselves. Read through `get_settings()` and an environment override
        could answer for the file, so this opens the file.
        """
        import tomllib
        from pathlib import Path

        import pr_agent

        toml_path = Path(pr_agent.__file__).parent / "settings" / "configuration.toml"
        config = tomllib.loads(toml_path.read_text(encoding="utf-8"))

        assert "ignore_bot_pr" in config["github"], (
            "is_bot_user reads [github].ignore_bot_pr; configuration.toml must ship it there"
        )
        assert "ignore_bot_pr" not in config.get("github_app", {}), (
            "two homes for one option is what #3017 was: the [github_app] key is legacy only"
        )

    def test_legacy_github_app_key_still_overrides_the_github_section(self):
        """The legacy key wins where both are set, which is what makes it a fallback.

        Anyone who set `GITHUB_APP.IGNORE_BOT_PR` before #3068 keeps the
        behaviour they configured; removing the fallback turns this red, which
        is the whole point of the test.

        Both sections are snapshotted whole rather than by key: Dynaconf's
        `unset` does not remove a dotted key, so a key this test adds can only
        be taken back out by restoring the section it lives in.
        """
        import copy

        from pr_agent.servers.github_app import is_bot_user

        settings = get_settings()
        original_github = copy.deepcopy(settings.get("GITHUB", None))
        original_github_app = copy.deepcopy(settings.get("GITHUB_APP", None))
        settings.set("GITHUB.IGNORE_BOT_PR", True)
        settings.set("GITHUB_APP.IGNORE_BOT_PR", False)
        try:
            assert is_bot_user("dependabot[bot]", "Bot") is False
        finally:
            if original_github is not None:
                settings.unset("GITHUB", force=True)
                settings.set("GITHUB", original_github)
            if original_github_app is not None:
                settings.unset("GITHUB_APP", force=True)
                settings.set("GITHUB_APP", original_github_app)
