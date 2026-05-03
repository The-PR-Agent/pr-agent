from pr_agent.config_loader import get_settings

DEFAULT_PROGRESS_GIF_WIDTH = 48
DEFAULT_PROGRESS_GIF_URL = (
    f"https://codium.ai/images/pr_agent/dual_ball_loading-crop.gif#width={DEFAULT_PROGRESS_GIF_WIDTH}"
)


def get_progress_gif_url() -> str:
    configured_url = get_settings().config.get("progress_gif_url", "").strip()
    return configured_url or DEFAULT_PROGRESS_GIF_URL


def parse_progress_gif_url(progress_gif_url: str) -> tuple[str, int]:
    if "#width=" not in progress_gif_url:
        return progress_gif_url, DEFAULT_PROGRESS_GIF_WIDTH

    base_url, width_str = progress_gif_url.rsplit("#width=", maxsplit=1)
    try:
        width = int(width_str)
    except ValueError:
        return base_url, DEFAULT_PROGRESS_GIF_WIDTH

    if width <= 0:
        return base_url, DEFAULT_PROGRESS_GIF_WIDTH

    return base_url, width


def build_progress_comment(progress_gif_url: str) -> str:
    gif_url, gif_width = parse_progress_gif_url(progress_gif_url)

    return (
        "## Generating PR code suggestions\n\n"
        "\nWork in progress ...<br>\n"
        f"<img src=\"{gif_url}\" width=\"{gif_width}\">"
    )
