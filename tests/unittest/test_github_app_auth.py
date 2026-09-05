import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_agent.config_loader import get_settings
from pr_agent.git_providers.github_provider import GithubProvider


class TestGithubAppAuth:
    def test_integer_app_id_produces_valid_jwt(self):
        """GitHub App ids are integers in the settings toml, but PyJWT >=2.11 rejects a
        non-string `iss` claim and PyGithub 1.59 passes the id through raw (#2955,
        previously #2210; fixed upstream in PyGithub#3272, which we don't ship yet).
        The provider must cast to str before building the authentication.
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
