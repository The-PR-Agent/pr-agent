import json
import os
import secrets
import stat
from contextlib import suppress
from contextvars import ContextVar

from pr_agent.log import get_logger

review_json_output_path: ContextVar[str | None] = ContextVar("review_json_output_path", default=None)


def write_review_json_output(review: dict, path: str | None = None) -> None:
    path = path or review_json_output_path.get()
    if not path:
        return
    temporary_path = None
    descriptor = None
    try:
        # Follow output symlinks as open(..., "w") does, but publish the completed
        # file only after serialization and closing have both succeeded.
        destination = os.path.realpath(path)
        try:
            destination_stat = os.stat(destination)
        except FileNotFoundError:
            destination_stat = None
        if destination_stat is not None and not stat.S_ISREG(destination_stat.st_mode):
            # Keep explicit streams such as /dev/stdout and named pipes as
            # streams; replacing a device or FIFO would change the destination.
            with open(destination, "w", encoding="utf-8") as fh:
                json.dump(review, fh, indent=2)
                fh.write("\n")
            return
        if destination_stat is not None:
            # Replacing an inode only needs directory permission; retain the
            # original writer's refusal to overwrite a read-only destination.
            existing_descriptor = os.open(destination, os.O_WRONLY)
            os.close(existing_descriptor)
        candidate_path = os.path.join(os.path.dirname(destination), f".pr-agent-review-{secrets.token_hex(16)}.tmp")
        # Exclusive creation prevents collisions and keeps open(..., "w")'s
        # umask-derived permissions for a new output rather than forcing 0600.
        descriptor = os.open(candidate_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        temporary_path = candidate_path
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            descriptor = None
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
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary_path is not None:
            with suppress(OSError):
                os.unlink(temporary_path)
