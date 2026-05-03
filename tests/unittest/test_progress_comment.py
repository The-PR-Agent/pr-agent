from unittest.mock import MagicMock, patch

from pr_agent.tools.progress_comment import (DEFAULT_PROGRESS_GIF_URL,
                                             DEFAULT_PROGRESS_GIF_WIDTH,
                                             build_progress_comment,
                                             get_progress_gif_url,
                                             parse_progress_gif_url)


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_url_defaults_to_https_url(mock_get_settings):
    mock_settings = MagicMock()
    mock_settings.config.get.return_value = ""
    mock_get_settings.return_value = mock_settings

    progress_gif_url = get_progress_gif_url()

    assert progress_gif_url == DEFAULT_PROGRESS_GIF_URL


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_url_uses_config_override(mock_get_settings):
    custom_url = "  https://example.com/custom.gif  "

    mock_settings = MagicMock()
    mock_settings.config.get.return_value = custom_url
    mock_get_settings.return_value = mock_settings

    progress_gif_url = get_progress_gif_url()

    assert progress_gif_url == "https://example.com/custom.gif"


def test_build_progress_comment_contains_expected_elements():
    gif_url = "https://example.com/custom.gif#width=150"

    progress_comment = build_progress_comment(gif_url)

    assert "Generating PR code suggestions" in progress_comment
    assert "Work in progress ..." in progress_comment
    assert '<img src="https://example.com/custom.gif" width="150">' in progress_comment


def test_build_progress_comment_defaults_to_width_48():
    gif_url = "https://example.com/custom.gif"

    progress_comment = build_progress_comment(gif_url)

    assert f'<img src="https://example.com/custom.gif" width="{DEFAULT_PROGRESS_GIF_WIDTH}">' in progress_comment


def test_parse_progress_gif_url_invalid_width_uses_default():
    gif_url, gif_width = parse_progress_gif_url("https://example.com/custom.gif#width=abc")

    assert gif_url == "https://example.com/custom.gif"
    assert gif_width == DEFAULT_PROGRESS_GIF_WIDTH


def test_parse_progress_gif_url_non_positive_width_uses_default():
    gif_url_zero, gif_width_zero = parse_progress_gif_url("https://example.com/custom.gif#width=0")
    gif_url_negative, gif_width_negative = parse_progress_gif_url("https://example.com/custom.gif#width=-10")

    assert gif_url_zero == "https://example.com/custom.gif"
    assert gif_width_zero == DEFAULT_PROGRESS_GIF_WIDTH
    assert gif_url_negative == "https://example.com/custom.gif"
    assert gif_width_negative == DEFAULT_PROGRESS_GIF_WIDTH


def test_parse_progress_gif_url_empty_width_uses_default():
    gif_url, gif_width = parse_progress_gif_url("https://example.com/custom.gif#width=")

    assert gif_url == "https://example.com/custom.gif"
    assert gif_width == DEFAULT_PROGRESS_GIF_WIDTH


def test_parse_progress_gif_url_valid_width():
    gif_url, gif_width = parse_progress_gif_url("https://example.com/custom.gif#width=150")

    assert gif_url == "https://example.com/custom.gif"
    assert gif_width == 150
