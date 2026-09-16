from pr_agent.algo.utils import fix_json_escape_char


class TestFixJsonEscapeChar:
    def test_empty_or_missing_json_returns_empty_dict(self):
        assert fix_json_escape_char("") == {}
        assert fix_json_escape_char("   ") == {}
        assert fix_json_escape_char(None) == {}

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
