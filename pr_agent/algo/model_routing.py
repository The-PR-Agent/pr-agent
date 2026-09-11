"""Route a small pull request to a cheaper primary model.

The [model_routing] settings hold an ordered list of rules, each naming a model and the largest
pull request it takes, measured in diff hunks and changed files. Both counts come from the
provider's diff, so they do not depend on which model's tokenizer would count the tokens.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import ModelType
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def count_hunks(diff_files: List[FilePatchInfo]) -> int:
    """Count the diff hunks across the files. A patch without hunk headers still counts as one."""
    total = 0
    for diff_file in diff_files:
        patch = getattr(diff_file, "patch", "") or ""
        if not patch.strip():
            continue
        total += sum(1 for line in patch.splitlines() if line.startswith("@@")) or 1
    return total


def _limit(rule, key: str) -> Optional[int]:
    value = rule.get(key)
    if value is None or value == "":
        return None
    return int(value)


def route_primary_model(model_type: ModelType, git_provider) -> Optional[Tuple[str, Optional[str]]]:
    """Return the (model, deployment_id) a routing rule picks for this pull request.

    None keeps the configured primary. Only a call for the regular model is routed: a tool that
    asked for the weak or the reasoning tier made that choice deliberately. Rules are checked in
    order and the first one whose limits the pull request fits wins.
    """
    if model_type != ModelType.REGULAR or git_provider is None:
        return None
    settings = get_settings()
    if not settings.get("model_routing.enable", False):
        return None
    rules = settings.get("model_routing.rules", None) or []
    if not rules:
        return None

    diff_files = git_provider.get_diff_files()
    num_files = len(diff_files)
    num_hunks = count_hunks(diff_files)
    size = f"{num_hunks} hunks in {num_files} files"
    global_deployment_id = settings.get("openai.deployment_id", None)

    for rule in rules:
        try:
            model = rule.get("model")
            max_hunks = _limit(rule, "max_hunks")
            max_files = _limit(rule, "max_files")
        except (AttributeError, TypeError, ValueError):
            model = max_hunks = max_files = None
        if not model or (max_hunks is None and max_files is None):
            get_logger().warning(f"Ignoring model routing rule without a model or a limit: {rule}")
            continue
        if max_hunks is not None and num_hunks > max_hunks:
            continue
        if max_files is not None and num_files > max_files:
            continue
        deployment_id = rule.get("deployment_id") or None
        if global_deployment_id and not deployment_id:
            get_logger().warning(f"Model routing rule for '{model}' has no deployment_id while "
                                 f"openai.deployment_id is set, skipping it")
            continue
        get_logger().info(f"Model routing: {size}, using '{model}' as the primary model")
        return model, deployment_id

    get_logger().info(f"Model routing: {size} fit no rule, keeping the configured primary model")
    return None
