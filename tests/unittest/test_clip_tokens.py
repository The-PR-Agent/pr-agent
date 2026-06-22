from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo.token_handler import TokenEncoder
from pr_agent.algo.utils import clip_tokens


class TestClipTokens:
    """Comprehensive test suite for the clip_tokens function."""

    def test_empty_input_text(self):
        """Test that empty input returns empty string."""
        assert clip_tokens("", 10) == ""
        assert clip_tokens(None, 10) is None

    def test_text_under_token_limit(self):
        """Test that text under the token limit is returned unchanged."""
        text = "Short text"
        max_tokens = 100
        result = clip_tokens(text, max_tokens)
        assert result == text

    def test_text_exactly_at_token_limit(self):
        """Test text that is exactly at the token limit."""
        text = "This is exactly at the limit"
        # Mock the token encoder to return exact limit
        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 10  # Exactly 10 tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, 10)
            assert result == text

    def test_text_over_token_limit_with_three_dots(self):
        """Test text over token limit with three dots addition."""
        text = "This is a longer text that should be clipped when it exceeds the token limit"
        max_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20  # 20 tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)
            assert result.endswith("\n...(truncated)")
            assert len(result) < len(text)

    def test_text_over_token_limit_without_three_dots(self):
        """Test text over token limit without three dots addition."""
        text = "This is a longer text that should be clipped"
        max_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20  # 20 tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens, add_three_dots=False)
            assert not result.endswith("\n...(truncated)")
            assert len(result) < len(text)

    def test_negative_max_tokens(self):
        """Test that negative max_tokens returns empty string."""
        text = "Some text"
        result = clip_tokens(text, -1)
        assert result == ""

        result = clip_tokens(text, -100)
        assert result == ""

    def test_zero_max_tokens(self):
        """Test that zero max_tokens returns empty string."""
        text = "Some text"
        result = clip_tokens(text, 0)
        assert result == ""

    def test_delete_last_line_functionality(self):
        """Test the delete_last_line parameter functionality."""
        text = "Line 1\nLine 2\nLine 3\nLine 4"
        max_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20  # 20 tokens
            mock_encoder.return_value = mock_tokenizer

            # Without delete_last_line
            result_normal = clip_tokens(text, max_tokens, delete_last_line=False)

            # With delete_last_line
            result_deleted = clip_tokens(text, max_tokens, delete_last_line=True)

            # The result with delete_last_line should be shorter or equal
            assert len(result_deleted) <= len(result_normal)

    def test_pre_computed_num_input_tokens(self):
        """Test using pre-computed num_input_tokens parameter."""
        text = "This is a test text"
        max_tokens = 10
        num_input_tokens = 15

        # Should not call the encoder when num_input_tokens is provided
        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_encoder.return_value = None  # Should not be called

            result = clip_tokens(text, max_tokens, num_input_tokens=num_input_tokens)
            assert result.endswith("\n...(truncated)")
            mock_encoder.assert_not_called()

    def test_pre_computed_tokens_under_limit(self):
        """Test pre-computed tokens under the limit."""
        text = "Short text"
        max_tokens = 20
        num_input_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_encoder.return_value = None  # Should not be called

            result = clip_tokens(text, max_tokens, num_input_tokens=num_input_tokens)
            assert result == text
            mock_encoder.assert_not_called()

    def test_special_characters_and_unicode(self):
        """Test text with special characters and Unicode content."""
        text = "Special chars: @#$%^&*()_+ áéíóú 中문 🚀 emoji"
        max_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20  # 20 tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)
            assert isinstance(result, str)
            assert len(result) < len(text)

    def test_multiline_text_handling(self):
        """Test handling of multiline text."""
        text = "Line 1\nLine 2\nLine 3\nLine 4\nLine 5"
        max_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20  # 20 tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)
            assert isinstance(result, str)

    def test_very_long_text(self):
        """Test with very long text."""
        text = "A" * 10000  # Very long text
        max_tokens = 10

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 5000  # Many tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)
            assert len(result) < len(text)
            assert result.endswith("\n...(truncated)")

    def test_encoder_exception_handling(self):
        """Test handling of encoder exceptions."""
        text = "Test text"
        max_tokens = 10

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_encoder.side_effect = Exception("Encoder error")

            # Should return original text when encoder fails
            result = clip_tokens(text, max_tokens)
            assert result == text

    def test_zero_division_scenario(self):
        """Test scenario that could lead to division by zero."""
        text = "Test"
        max_tokens = 10

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = []  # Empty tokens (could cause division by zero)
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)
            # Should handle gracefully and return original text
            assert result == text

    def test_various_edge_cases(self):
        """Test various edge cases."""
        # Single character
        assert clip_tokens("A", 1000) == "A"

        # Only whitespace
        text = "   \n  \t  "
        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 10
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, 5)
            assert isinstance(result, str)

        # Text with only newlines
        text = "\n\n\n\n"
        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 10
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, 2, delete_last_line=True)
            assert isinstance(result, str)

    def test_parameter_combinations(self):
        """Test different parameter combinations."""
        text = "Multi\nline\ntext\nfor\ntesting"
        max_tokens = 5

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20
            mock_encoder.return_value = mock_tokenizer

            # Test all combinations
            combinations = [
                (True, True),   # add_three_dots=True, delete_last_line=True
                (True, False),  # add_three_dots=True, delete_last_line=False
                (False, True),  # add_three_dots=False, delete_last_line=True
                (False, False), # add_three_dots=False, delete_last_line=False
            ]

            for add_dots, delete_line in combinations:
                result = clip_tokens(text, max_tokens,
                                     add_three_dots=add_dots,
                                     delete_last_line=delete_line)
                assert isinstance(result, str)
                if add_dots and len(result) > 0:
                    assert result.endswith("\n...(truncated)") or result == text

    def test_num_output_chars_zero_scenario(self):
        """Test scenario where num_output_chars becomes zero or negative."""
        text = "Short"
        max_tokens = 1

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 1000  # Many tokens for short text
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)
            # When num_output_chars is 0 or negative, should return empty string
            assert result == ""

    def test_logging_on_exception(self):
        """Test that exceptions are properly logged."""
        text = "Test text"
        max_tokens = 10

        # Patch the logger at the module level where it's imported
        with patch('pr_agent.algo.utils.get_logger') as mock_logger:
            mock_log_instance = MagicMock()
            mock_logger.return_value = mock_log_instance

            with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
                mock_encoder.side_effect = Exception("Test exception")

                result = clip_tokens(text, max_tokens)

                # Should log the warning
                mock_log_instance.warning.assert_called_once()
                # Should return original text
                assert result == text

    def test_factor_safety_calculation(self):
        """Test that the 0.9 factor (10% reduction) works correctly."""
        text = "Test text that should be reduced by 10 percent for safety"
        max_tokens = 10

        with patch.object(TokenEncoder, 'get_token_encoder') as mock_encoder:
            mock_tokenizer = MagicMock()
            mock_tokenizer.encode.return_value = [1] * 20  # 20 tokens
            mock_encoder.return_value = mock_tokenizer

            result = clip_tokens(text, max_tokens)

            # The result should be shorter due to the 0.9 factor
            # Characters per token = len(text) / 20
            # Expected chars = int(0.9 * (len(text) / 20) * 10)
            expected_chars = int(0.9 * (len(text) / 20) * 10)

            # Result should be around expected_chars length (plus truncation text)
            if result.endswith("\n...(truncated)"):
                actual_content = result[:-len("\n...(truncated)")]
                assert len(actual_content) <= expected_chars + 5  # Some tolerance

    # Test the original basic functionality to ensure backward compatibility
    def test_clip_original_functionality(self):
        """Test original functionality from the existing test."""
        text = "line1\nline2\nline3\nline4\nline5\nline6"
        max_tokens = 25
        result = clip_tokens(text, max_tokens)
        assert result == text

        max_tokens = 10
        result = clip_tokens(text, max_tokens)
        expected_results = 'line1\nline2\nline3\n\n...(truncated)'
        assert result == expected_results