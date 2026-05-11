from html import escape

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def render_instruction_files(files: dict[str, str]) -> str:
    parts = [
        "You are being given instruction files. Follow them as project-specific guidance when reviewing code.",
        "<instruction_files>",
    ]

    for path, content in files.items():
        scope = path.rsplit("/", 1)[0] if "/" in path else "repo-root"
        parts.append(f'<file path="{escape(path, quote=True)}" scope="{escape(scope, quote=True)}">')
        parts.append("`````markdown")
        parts.append(content.rstrip())
        parts.append("`````")
        parts.append("</file>")
        parts.append("")

    parts.append("</instruction_files>")
    return "\n".join(parts)


def build_repo_context(git_provider) -> str:
    context_files = get_settings().config.get("repo_context_files", [])
    if not context_files:
        return ""

    max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        max_lines = max(0, int(max_lines))
    except (TypeError, ValueError):
        max_lines = 500

    files = {}
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

        files[file_path] = str(content).rstrip()

    if not files:
        return ""

    rendered_lines = render_instruction_files(files).splitlines()

    return "\n".join(rendered_lines[:max_lines]).strip()
