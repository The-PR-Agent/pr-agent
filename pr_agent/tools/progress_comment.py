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
    """Keep a run's progress current as the chunks of a chunked run settle.

    A chunked run makes several model calls (plus retries) that can take minutes, so the
    published progress is kept current instead of frozen. Two parallel sinks exist: the
    in-place rewrite of a progress comment (when one was published), and the output summary
    of the provider's in-progress check runs (``update_check_run_progress`` — automatic
    commands publish no progress comment, so the check run is their only visible channel).
    Reporting is best effort: an edit or update that fails mid-run is logged and dropped so
    the result is unaffected.

    Every counter change takes a lock that is held until the writes land, so a sink cannot
    end up showing an older count than the one the tool already recorded. The provider calls
    block, so they run in a worker thread rather than on the event loop that the sibling
    chunk coroutines still need.
    """

    def __init__(self, git_provider, comment, base_body: str, total: int, body_builder,
                 *, label: str = "progress", completed: int = 0, failed: int = 0,
                 check_run_sink=None):
        self.git_provider = git_provider
        self.comment = comment
        self.base_body = base_body
        self.total = max(int(total), 0)
        self.body_builder = body_builder
        self.label = label
        self.completed = max(int(completed), 0)
        self.failed = max(int(failed), 0)
        self._check_run_sink = check_run_sink
        self._last_body = base_body
        self._lock = asyncio.Lock()

    @classmethod
    def create(cls, git_provider, comment, base_body, *, total, body_builder,
               label: str = "progress", completed: int = 0):
        """Build a reporter, or None when no sink is available.

        The comment sink needs a published comment and a provider that can edit and remove
        comments. When there is no such comment — automatic commands publish none — the
        provider's check-run progress method serves alone; a run with both gets both.
        """
        if not base_body or not total:
            return None
        check_run_sink = getattr(git_provider, "update_check_run_progress", None)
        check_run_sink = check_run_sink if callable(check_run_sink) else None
        if comment is None:
            return cls(git_provider, None, base_body, total, body_builder,
                       label=label, completed=completed, check_run_sink=check_run_sink) \
                if check_run_sink is not None else None
        if not supports_editable_progress_comment(git_provider):
            # Without an editable comment the run falls back to the check run alone.
            if check_run_sink is None:
                return None
            return cls(git_provider, None, base_body, total, body_builder,
                       label=label, completed=completed, check_run_sink=check_run_sink)
        return cls(git_provider, comment, base_body, total, body_builder,
                   label=label, completed=completed, check_run_sink=check_run_sink)

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
        line = chunk_progress_line(self.completed, self.total, self.failed)
        await self._write(self.body_builder(line), line)

    async def _write(self, body: str, line: str = "") -> None:
        # set_failed(0) after a clean batch reproduces the body the last settled chunk already
        # published; writing again would be a redundant provider call on every sink.
        if body == self._last_body:
            return
        self._last_body = body
        if self.comment is not None:
            await asyncio.to_thread(edit_comment_safely, self.git_provider, self.comment, body,
                                    label=self.label)
        if self._check_run_sink is not None:
            await asyncio.to_thread(self._update_check_run_safely, line)

    def _update_check_run_safely(self, line: str) -> None:
        try:
            self._check_run_sink(line)
        except Exception as error:
            get_logger().warning(f"Failed to update the {self.label} check run: {error}")
