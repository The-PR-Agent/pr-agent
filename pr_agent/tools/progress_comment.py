from pr_agent.config_loader import get_settings

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
