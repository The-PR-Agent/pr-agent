import asyncio

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

DEFAULT_PROGRESS_GIF_WIDTH = 48
DEFAULT_PROGRESS_GIF_URL = "https://www.qodo.ai/images/pr_agent/dual_ball_loading-crop.gif"


def get_progress_gif_url() -> str:
    configured_url = get_settings().config.get("progress_gif_url", "").strip()
    return configured_url or DEFAULT_PROGRESS_GIF_URL


def get_progress_gif_width() -> int:
    configured_width = get_settings().config.get("progress_gif_width", DEFAULT_PROGRESS_GIF_WIDTH)
    try:
        width = int(configured_width)
    except (TypeError, ValueError):
        return DEFAULT_PROGRESS_GIF_WIDTH

    if width <= 0:
        return DEFAULT_PROGRESS_GIF_WIDTH

    return width


def build_progress_comment() -> str:
    gif_url = get_progress_gif_url()
    gif_width = get_progress_gif_width()

    return (
        "## Generating PR code suggestions\n\n"
        "\nWork in progress ...<br>\n"
        f"<img src=\"{gif_url}\" alt=\"Work in progress\" width=\"{gif_width}\">"
    )


def chunk_progress_line(completed: int, total: int, failed: int = 0) -> str:
    """Render the `analyzed X of Y chunks` note shown while a chunked run is still working."""
    if total <= 0:
        return ""
    line = f"analyzed {min(completed, total)} of {total} chunks"
    if failed > 0:
        line += f", {failed} chunk{'s' if failed != 1 else ''} failed"
    return line


def supports_editable_progress_comment(git_provider) -> bool:
    """Check whether a published progress comment can later be edited in place or removed.

    Return False for a provider that can do neither, such as plain diff, which writes every
    non-temporary comment straight to its output; editing one there would publish a stale
    progress document ahead of the final result.
    """
    return (git_provider.is_supported("edit_comment")
            and git_provider.is_supported("remove_comment"))


def edit_comment_safely(git_provider, comment, body: str, *, label: str = "progress") -> bool:
    """Edit a comment, logging and swallowing provider failures so the run is never broken."""
    try:
        result = git_provider.edit_comment(comment, body)
    except Exception as error:
        get_logger().warning(f"Failed to edit {label} comment: {error}")
        return False
    if result is False:
        get_logger().warning(f"Failed to edit {label} comment")
        return False
    return True


class ChunkProgressReporter:
    """Rewrite a progress comment in place as the chunks of a chunked run settle.

    A chunked run makes several model calls (plus retries) that can take minutes, so the
    published placeholder is kept current instead of frozen. Reporting is best effort: a
    provider without `edit_comment` never gets a reporter, and an edit that fails mid-run is
    logged and dropped so the result is unaffected.

    Every counter change takes a lock that is held until the edit lands, so the comment cannot
    end up showing an older count than the one the tool already recorded. `edit_comment` is a
    blocking provider call, so it runs in a worker thread rather than on the event loop that
    the sibling chunk coroutines still need.
    """

    def __init__(self, git_provider, comment, base_body: str, total: int, body_builder,
                 *, label: str = "progress", completed: int = 0, failed: int = 0):
        self.git_provider = git_provider
        self.comment = comment
        self.base_body = base_body
        self.total = max(int(total), 0)
        self.body_builder = body_builder
        self.label = label
        self.completed = max(int(completed), 0)
        self.failed = max(int(failed), 0)
        self._last_body = base_body
        self._lock = asyncio.Lock()

    @classmethod
    def create(cls, git_provider, comment, base_body, *, total, body_builder,
               label: str = "progress", completed: int = 0):
        """Build a reporter, or None when there is no comment this provider can edit back."""
        if comment is None or not base_body or not total:
            return None
        if not supports_editable_progress_comment(git_provider):
            return None
        return cls(git_provider, comment, base_body, total, body_builder,
                   label=label, completed=completed)

    async def reset_to_base(self) -> None:
        """Restore the published placeholder, so a new attempt does not inherit the last counts."""
        async with self._lock:
            await self._write(self.base_body)

    async def extend_total(self, count: int) -> None:
        """Account for chunks that will be retried, so a retry round stays within the total."""
        async with self._lock:
            self.total += max(int(count), 0)
            await self._report()

    async def record_settled(self, count: int = 1) -> None:
        """Note chunks that finished, successfully or not, and republish the progress line."""
        async with self._lock:
            self.completed += max(int(count), 0)
            await self._report()

    async def set_failed(self, count: int) -> None:
        """Note how many chunks have produced no usable result, and republish the progress line."""
        async with self._lock:
            self.failed = max(int(count), 0)
            await self._report()

    async def _report(self) -> None:
        await self._write(self.body_builder(
            chunk_progress_line(self.completed, self.total, self.failed)))

    async def _write(self, body: str) -> None:
        # set_failed(0) after a clean batch reproduces the body the last settled chunk already
        # published; editing again would be a redundant write.
        if body == self._last_body:
            return
        self._last_body = body
        await asyncio.to_thread(edit_comment_safely, self.git_provider, self.comment, body,
                                label=self.label)
