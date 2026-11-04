import json
import os
import stat
import tempfile
from contextlib import suppress
from contextvars import ContextVar

from pr_agent.log import get_logger

review_json_output_path: ContextVar[str | None] = ContextVar("review_json_output_path", default=None)


def write_review_json_output(review: dict, path: str | None = None) -> None:
    path = path or review_json_output_path.get()
    if not path:
        return
    temporary_path = None
    try:
        # Follow output symlinks as open(..., "w") does, but publish the completed
        # file only after serialization and closing have both succeeded.
        destination = os.path.realpath(path)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=os.path.dirname(destination),
            prefix=".pr-agent-review-", suffix=".tmp", delete=False,
        ) as fh:
            temporary_path = fh.name
            try:
                os.chmod(temporary_path, stat.S_IMODE(os.stat(destination).st_mode))
            except FileNotFoundError:
                pass
            json.dump(review, fh, indent=2)
            fh.write("\n")
        os.replace(temporary_path, destination)
        temporary_path = None
    except (OSError, TypeError, ValueError) as error:
        get_logger().error(f"Failed to write structured review to {path}: {error}")
        raise
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                os.unlink(temporary_path)
