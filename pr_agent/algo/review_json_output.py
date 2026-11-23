import json
import os
from contextvars import ContextVar

from pr_agent.log import get_logger

review_json_output_path: ContextVar[str | None] = ContextVar("review_json_output_path", default=None)
hosted_markdown_export: ContextVar[bool] = ContextVar("hosted_markdown_export", default=False)
hosted_markdown_artifact: ContextVar[str | None] = ContextVar("hosted_markdown_artifact", default=None)
hosted_markdown_config_error: ContextVar[bool] = ContextVar("hosted_markdown_config_error", default=False)


def same_output_file(first: str | os.PathLike, second: str | os.PathLike) -> bool:
    try:
        if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
            return True  # The output files need not exist yet.
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


def write_review_json_output(review: dict, path: str | None = None) -> None:
    path = path or review_json_output_path.get()
    if not path:
        return
    try:
        # Serialize before opening so a failure cannot truncate an existing file.
        text = json.dumps(review, indent=2) + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except (OSError, TypeError, ValueError) as error:
        get_logger().error(f"Failed to write structured review to {path}: {error}")
        raise
