import json
from contextvars import ContextVar
from typing import Optional

from pr_agent.log import get_logger

review_json_output_path: ContextVar[Optional[str]] = ContextVar("review_json_output_path", default=None)


def write_review_json_output(review: dict) -> None:
    path = review_json_output_path.get()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(review, fh, indent=2)
            fh.write("\n")
    except (OSError, TypeError, ValueError) as error:
        get_logger().error(f"Failed to write structured review to {path}: {error}")
        raise
