from abc import ABC, abstractmethod
from enum import Enum


class Eligibility(Enum):
    NOT_ELIGIBLE = 0
    ELIGIBLE = 1
    TRIAL = 2


class IdentityProvider(ABC):
    @abstractmethod
    def verify_eligibility(self, git_provider, git_provider_id, pr_url):
        pass
