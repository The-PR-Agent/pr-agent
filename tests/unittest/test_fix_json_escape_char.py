from pr_agent.algo.utils import fix_json_escape_char, try_fix_json


class TestFixJsonEscapeChar:
    def test_valid_json(self):
        """Return unchanged when input JSON is already valid"""
        text = '{"a": 1, "b": "ok"}'
        expected_output = {"a": 1, "b": "ok"}
        assert fix_json_escape_char(text) == expected_output

    def test_single_control_char(self):
        """Remove a single ASCII control-character"""
        text = '{"msg": "hel\x01lo"}'
        expected_output = {"msg": "hel lo"}
        assert fix_json_escape_char(text) == expected_output

    def test_multiple_control_chars(self):
        """Remove multiple control-characters recursively"""
        text = '{"x": "A\x02B\x03C"}'
        expected_output = {"x": "A B C"}
        assert fix_json_escape_char(text) == expected_output

    def test_truncated_json_fails_cleanly(self):
        """Return an empty dict when there is no offending character to replace"""
        text = '{"msg": "unterminated"'
        assert fix_json_escape_char(text) == {}


class TestTryFixJson:
    def test_code_feedback_without_completed_item_fails_cleanly(self):
        review = '{"review": {"Code feedback": [{"body": "truncated"'
        assert try_fix_json(review) == {}
