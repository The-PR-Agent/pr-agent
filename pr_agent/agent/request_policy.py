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


def enforce_request_policy(pr_url, provider=None) -> bool:
    # Keep the existing matching semantics as the single policy implementation.
    from pr_agent.servers.utils import get_include_pr_labels, should_process_pr_logic

    include_pr_labels = get_include_pr_labels()
    required = {field for field, rule in RULE_FIELDS.items() if get_settings().get(f"config.{rule}", [])}
    if include_pr_labels:
        required.add("labels")
    if not required:
        return True
    from pr_agent.git_providers import get_git_provider, get_git_provider_with_context

    try:
        # Reuse the provider only when it is exactly the selected class after repository settings.
        if provider is None:
            provider = get_git_provider_with_context(pr_url)
        else:
            provider_class = get_git_provider()
            if type(provider) is not provider_class:
                provider = provider_class(pr_url)
        metadata = provider.get_request_policy_metadata(required)
        # Exclusion-only rules tolerate missing metadata. The opt-in label gate
        # requires readable labels and re-raises matcher errors.
        if not should_process_pr_logic(**metadata, raise_on_error=bool(include_pr_labels)):
            get_logger().info("Request ignored by policy")
            return False
    except Exception as error:
        # A positive label gate cannot safely allow a request without label metadata.
        if include_pr_labels:
            get_logger().warning(f"Unable to verify required PR labels ({type(error).__name__}); skipping request")
            return False
        # Preserve the existing fail-open behavior for exclusion-only rules.
        get_logger().warning(f"Unable to evaluate request policy ({type(error).__name__}); continuing request")
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
