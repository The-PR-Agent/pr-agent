from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def load_repo_best_practices_md(git_provider, tool_name: str = "improve") -> str:
    """Fetch best_practices.md from the repo default branch.

    Returns text (possibly truncated to ``[best_practices].max_lines_allowed``)
    or an empty string when disabled, missing, or unreadable. Result is cached
    in starlette_context for the duration of the request so multiple tools
    share a single fetch.
    """
    settings = get_settings()
    if not settings.get("best_practices.enable_repo_best_practices_md", True):
        return ""
    try:
        cached = context.get("best_practices_md", None)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    file_path = settings.get("best_practices.repo_best_practices_md_path", "best_practices.md") or "best_practices.md"
    raw = b""
    try:
        raw = git_provider.get_pr_agent_repo_custom_file(file_path) or b""
    except Exception as e:
        get_logger().warning(f"Failed to fetch {file_path} from repo: {e}")
    if isinstance(raw, (bytes, bytearray)):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw or "")
    if not text.strip():
        try:
            context["best_practices_md"] = ""
        except Exception:
            pass
        return ""
    line_count = text.count("\n") + 1
    get_logger().info(
        f"Loaded {file_path} from repo ({len(text)} bytes, {line_count} lines) for '{tool_name}' tool"
    )
    raw_max_lines = settings.get("best_practices.max_lines_allowed", 800)
    try:
        max_lines = int(raw_max_lines) if raw_max_lines else 800
    except (TypeError, ValueError):
        get_logger().warning(
            f"Invalid best_practices.max_lines_allowed={raw_max_lines!r}; falling back to 800"
        )
        max_lines = 800
    lines = text.splitlines()
    if len(lines) > max_lines:
        get_logger().warning(
            f"Truncating {file_path} from {len(lines)} to {max_lines} lines "
            f"(see [best_practices].max_lines_allowed)"
        )
        text = "\n".join(lines[:max_lines])
    try:
        context["best_practices_md"] = text
    except Exception:
        pass
    return text
