"""Request policy shared by every agent entrypoint, independent of transport."""

from enum import Enum

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

RULE_FIELDS = {
    "repo_full_name": "ignore_repositories",
    "sender": "ignore_pr_authors",
    "title": "ignore_pr_title",
    "labels": "ignore_pr_labels",
    "source_branch": "ignore_pr_source_branches",
    "target_branch": "ignore_pr_target_branches",
}


class RequestOutcome(Enum):
    """Distinct non-failure outcome; callers must handle it before command follow-up."""

    SKIPPED = "skipped"

    def __bool__(self):
        # A skipped command did not execute successfully. Use identity to distinguish failure.
        return False


def enforce_request_policy(pr_url) -> bool:
    # Keep the existing matching semantics as the single policy implementation.
    from pr_agent.servers.utils import should_process_pr_logic

    required = {field for field, rule in RULE_FIELDS.items() if get_settings().get(f"config.{rule}", [])}
    if not required:
        return True
    from pr_agent.git_providers import get_git_provider_with_context

    provider = get_git_provider_with_context(pr_url)
    metadata = provider.get_request_policy_metadata(required)
    missing = required - metadata.keys()
    unknown = {field for field in required if metadata.get(field) is None}
    if missing or unknown:
        raise ValueError(f"Required request policy metadata is unavailable: {sorted(missing | unknown)}")
    if not should_process_pr_logic(**metadata, raise_on_error=True):
        get_logger().info("Request ignored by policy")
        return False
    return True


def policy_value(value, *path):
    """Read provider metadata from a dict or SDK model without swallowing API failures."""
    for key in path:
        value = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        if value is None:
            return None
    return value


def policy_metadata(*, title, sender, repo_full_name, source_branch, target_branch, labels=()):
    return dict(title=title, sender=sender, repo_full_name=repo_full_name,
                source_branch=source_branch, target_branch=target_branch, labels=labels)
