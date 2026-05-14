from unittest.mock import MagicMock, patch

from pr_agent.tools.progress_comment import (DEFAULT_PROGRESS_GIF_URL,
                                             DEFAULT_PROGRESS_GIF_WIDTH,
                                             build_progress_comment,
                                             get_progress_gif_url,
                                             get_progress_gif_width)


def _mock_settings(mock_get_settings, values):
    mock_settings = MagicMock()
    mock_settings.config.get.side_effect = lambda key, default=None: values.get(key, default)
    mock_get_settings.return_value = mock_settings


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_url_defaults_to_https_url(mock_get_settings):
    _mock_settings(mock_get_settings, {})

    assert get_progress_gif_url() == DEFAULT_PROGRESS_GIF_URL


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_url_uses_config_override(mock_get_settings):
    _mock_settings(mock_get_settings, {"progress_gif_url": "  https://example.com/custom.gif  "})

    assert get_progress_gif_url() == "https://example.com/custom.gif"


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_width_defaults_to_48(mock_get_settings):
    _mock_settings(mock_get_settings, {})

    assert get_progress_gif_width() == DEFAULT_PROGRESS_GIF_WIDTH


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_width_uses_config_override(mock_get_settings):
    _mock_settings(mock_get_settings, {"progress_gif_width": 150})

    assert get_progress_gif_width() == 150


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_width_parses_string_value(mock_get_settings):
    _mock_settings(mock_get_settings, {"progress_gif_width": "150"})

    assert get_progress_gif_width() == 150


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_width_invalid_value_uses_default(mock_get_settings):
    _mock_settings(mock_get_settings, {"progress_gif_width": "abc"})

    assert get_progress_gif_width() == DEFAULT_PROGRESS_GIF_WIDTH


@patch("pr_agent.tools.progress_comment.get_settings")
def test_get_progress_gif_width_non_positive_value_uses_default(mock_get_settings):
    _mock_settings(mock_get_settings, {"progress_gif_width": 0})
    assert get_progress_gif_width() == DEFAULT_PROGRESS_GIF_WIDTH

    _mock_settings(mock_get_settings, {"progress_gif_width": -10})
    assert get_progress_gif_width() == DEFAULT_PROGRESS_GIF_WIDTH


@patch("pr_agent.tools.progress_comment.get_settings")
def test_build_progress_comment_contains_expected_elements(mock_get_settings):
    _mock_settings(mock_get_settings, {
        "progress_gif_url": "https://example.com/custom.gif",
        "progress_gif_width": 150,
    })

    progress_comment = build_progress_comment()

    assert "Generating PR code suggestions" in progress_comment
    assert "Work in progress ..." in progress_comment
    assert '<img src="https://example.com/custom.gif" alt="Work in progress" width="150">' in progress_comment


@patch("pr_agent.tools.progress_comment.get_settings")
def test_build_progress_comment_uses_defaults(mock_get_settings):
    _mock_settings(mock_get_settings, {})

    progress_comment = build_progress_comment()

    assert f'<img src="{DEFAULT_PROGRESS_GIF_URL}" alt="Work in progress" width="{DEFAULT_PROGRESS_GIF_WIDTH}">' in progress_comment
