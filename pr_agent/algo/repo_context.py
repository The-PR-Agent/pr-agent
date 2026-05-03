from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def build_repo_context(git_provider) -> str:
    context_files = get_settings().config.get("repo_context_files", [])
    if not context_files:
        return ""

    max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        max_lines = max(0, int(max_lines))
    except (TypeError, ValueError):
        max_lines = 500

    rendered_lines = []
    for file_path in context_files:
        if not isinstance(file_path, str) or not file_path.strip():
            get_logger().warning("Skipping invalid repo context file path", artifact={"file_path": file_path})
            continue

        file_path = file_path.strip()
        try:
            content = git_provider.get_repo_file_content(file_path)
        except Exception as e:
            get_logger().warning(f"Failed to load repo context file: {file_path}", artifact={"error": str(e)})
            continue

        if not content:
            get_logger().debug(f"Repo context file is empty or missing: {file_path}")
            continue

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")

        file_lines = [f"## {file_path}", *str(content).strip().splitlines()]
        remaining_lines = max_lines - len(rendered_lines)
        if remaining_lines <= 0:
            break

        if rendered_lines:
            rendered_lines.append("")
            remaining_lines -= 1

        rendered_lines.extend(file_lines[:remaining_lines])

    return "\n".join(rendered_lines).strip()
