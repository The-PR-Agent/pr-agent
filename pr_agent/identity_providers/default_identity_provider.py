from pr_agent.identity_providers.identity_provider import Eligibility, IdentityProvider


class DefaultIdentityProvider(IdentityProvider):
    def verify_eligibility(self, git_provider, git_provider_id, pr_url):
        return Eligibility.ELIGIBLE
