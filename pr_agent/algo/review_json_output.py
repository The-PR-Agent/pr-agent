import json
from contextvars import ContextVar

from pr_agent.log import get_logger

review_json_output_path: ContextVar[str | None] = ContextVar("review_json_output_path", default=None)
hosted_markdown_export: ContextVar[bool] = ContextVar("hosted_markdown_export", default=False)
hosted_markdown_artifact: ContextVar[str | None] = ContextVar("hosted_markdown_artifact", default=None)
hosted_markdown_config_error: ContextVar[bool] = ContextVar("hosted_markdown_config_error", default=False)


def write_review_json_output(review: dict, path: str | None = None) -> None:
    path = path or review_json_output_path.get()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(review, fh, indent=2)
            fh.write("\n")
    except (OSError, TypeError, ValueError) as error:
        get_logger().error(f"Failed to write structured review to {path}: {error}")
        raise
