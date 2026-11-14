
from pr_agent.algo.utils import _extract_labelled_yaml_block, load_yaml


def test_yaml_after_code_example():
    """Test YAML parsing when code example precedes YAML block"""
    input_text = """Explanation:
```go
func main() {}
```
Final answer:
```yaml
code_suggestions:
  - suggestion_score: 8
    why: Useful
```
"""
    result = load_yaml(input_text)
    assert result is not None
    assert 'code_suggestions' in result
    assert len(result['code_suggestions']) == 1
    assert result['code_suggestions'][0]['suggestion_score'] == 8

def test_yaml_with_code_block():
    """Test YAML parsing when code block precedes YAML block"""
    input_text = """Some text before

```python
def hello():
    print("hello")
```

```yaml
some_key: some_value
```

More text after
"""
    result = load_yaml(input_text)
    assert result is not None
    assert 'some_key' in result
    assert result['some_key'] == 'some_value'

def test_simple_yaml():
    """Test simple YAML parsing"""
    input_text = """```yaml
simple_key: simple_value
```"""
    result = load_yaml(input_text)
    assert result is not None
    assert 'simple_key' in result
    assert result['simple_key'] == 'simple_value'

if __name__ == "__main__":
    test_yaml_after_code_example()
    test_yaml_with_code_block()
    test_simple_yaml()
    print("All tests passed!")


def test_inner_fence_preserves_all_suggestions():
    input_text = (
        "Lead\n"
        "```yaml\n"
        "code_suggestions:\n"
        "  - suggestion_score: 8\n"
        "    why: |\n"
        "      ```yaml\n"
        "      nested: true\n"
        "      ```\n"
        "    relevant_line: 1\n"
        "  - suggestion_score: 9\n"
        "    why: Second suggestion\n"
        "    relevant_line: 2\n"
        "```\n"
    )
    result = load_yaml(input_text)
    assert isinstance(result, dict)
    assert "code_suggestions" in result
    assert len(result["code_suggestions"]) == 2


def test_trailing_list_not_returned():
    input_text = """Code example:
```python
print("hello")
```
Final answer:
```yaml
- a
- b
```"""
    result = load_yaml(input_text)
    assert isinstance(result, dict)


def test_bounded_traversal_200_fences():
    class CountingLines(list):
        visits = 0

        def __getitem__(self, index):
            self.visits += 1
            assert self.visits <= 2 * len(self)
            return super().__getitem__(index)

    class CountingText(str):
        def split(self, sep=None, maxsplit=-1):
            return CountingLines(super().split(sep, maxsplit))

    input_text = CountingText("\n".join(["```yaml"] * 200))
    assert _extract_labelled_yaml_block(input_text) is None


def test_labelled_list_legacy_baseline():
    result = load_yaml("Explanation:\n```yaml\n- a\n- b\n```\n")
    assert result == ["a", "b"]


def test_unlabelled_list_legacy_baseline():
    input_text = "Explanation:\n" + "```\n" + "- a\n" + "- b\n" + "```\n"
    result = load_yaml(input_text)
    assert result == ["a", "b"]


def test_python_fence_inside_why_preserves_all_suggestions():
    input_text = (
        "Lead\n"
        "```yaml\n"
        "code_suggestions:\n"
        "  - suggestion_score: 8\n"
        "    why: |\n"
        "      ```python\n"
        "      print(x)\n"
        "      ```\n"
        "      more prose\n"
        "    relevant_line: 1\n"
        "  - suggestion_score: 9\n"
        "    why: Second suggestion\n"
        "    relevant_line: 2\n"
        "```\n"
    )
    result = load_yaml(input_text)
    assert isinstance(result, dict)
    assert len(result["code_suggestions"]) == 2


def test_bare_fence_inside_why_preserves_all_suggestions():
    input_text = (
        "Lead\n"
        "```yaml\n"
        "code_suggestions:\n"
        "  - suggestion_score: 8\n"
        "    why: |\n"
        "      ```\n"
        "      x = 1\n"
        "      ```\n"
        "      more prose\n"
        "    relevant_line: 1\n"
        "  - suggestion_score: 9\n"
        "    why: Second suggestion\n"
        "    relevant_line: 2\n"
        "```\n"
    )
    result = load_yaml(input_text)
    assert isinstance(result, dict)
    assert len(result["code_suggestions"]) == 2


def test_indented_outer_yaml_fence_with_bare_fences_inside_scalar():
    input_text = (
        "Lead\n"
        "  ```yaml\n"
        "  code_suggestions:\n"
        "    - suggestion_score: 8\n"
        "      why: |\n"
        "        ```\n"
        "        x = 1\n"
        "        ```\n"
        "    - suggestion_score: 9\n"
        "      why: Second suggestion\n"
        "  ```\n"
        "After\n"
    )
    result = load_yaml(input_text)
    assert isinstance(result, dict)
    assert [item["suggestion_score"] for item in result["code_suggestions"]] == [8, 9]
    assert "x = 1" in result["code_suggestions"][0]["why"]


def test_indented_outer_close_retains_legacy_recovery():
    input_text = (
        "Lead\n"
        "```yaml\n"
        "code_suggestions:\n"
        "  - suggestion_score: 8\n"
        "    why: First\n"
        "  - suggestion_score: 9\n"
        "    why: Second\n"
        "  ```\n"
        "After\n"
    )
    result = load_yaml(input_text)
    assert isinstance(result, dict)
    assert [item["suggestion_score"] for item in result["code_suggestions"]] == [8, 9]


def test_block_scalar_chomping_and_indent_indicators_keep_bare_fences():
    for indicator in ("|-", ">-", "|2-"):
        input_text = (
            "Lead\n"
            "```yaml\n"
            "code_suggestions:\n"
            "  - suggestion_score: 8\n"
            f"    why: {indicator}\n"
            "      ```\n"
            "      example\n"
            "      ```\n"
            "  - suggestion_score: 9\n"
            "    why: Second\n"
            "```\n"
        )
        result = load_yaml(input_text)
        assert [item["suggestion_score"] for item in result["code_suggestions"]] == [8, 9]
        assert "example" in result["code_suggestions"][0]["why"]


def test_comment_containing_scalar_marker_is_not_block_scalar():
    input_text = "Lead\n```yaml\nvalue: 1 # note: |\n  ```\nAfter\n"
    assert load_yaml(input_text) == {"value": 1}


def test_indented_close_below_block_scalar_content_indent():
    input_text = "Lead\n```yaml\ncode_suggestions:\n  - why: |\n      text\n   ```\nAfter\n"
    assert load_yaml(input_text)["code_suggestions"][0]["why"] == "text"


def test_multiline_quoted_scalar_does_not_start_block_scalar():
    input_text = 'Lead\n```yaml\nvalue: "something\n  note: |\n   text"\n   ```\nAfter\n'
    assert load_yaml(input_text)["value"] == "something note: | text"


def test_scalar_scan_does_not_copy_and_rescan_suffixes(monkeypatch):
    from io import StringIO

    class CountingStream(StringIO):
        bytes_read = 0

        def read(self, size=-1):
            result = super().read(size)
            self.bytes_read += len(result)
            assert self.bytes_read <= len(input_text) + 4096
            return result

    monkeypatch.setattr("pr_agent.algo.utils.StringIO", CountingStream)
    input_text = "Lead\n```yaml\nvalue: " + " " * 50000 + "1\n```\n"
    assert _extract_labelled_yaml_block(input_text) is not None


def test_quoted_key_with_hash_and_explicit_scalar_indent():
    input_text = (
        "Lead\n```yaml\n"
        "'why # literal': |2-\n"
        "    deeply indented\n"
        "  ```\n"
        "  example\n"
        "  ```\n"
        "other: 1\n"
        "```\n"
    )
    result = load_yaml(input_text)
    assert "example" in result["why # literal"]
    assert result["other"] == 1


def test_list_block_scalar_with_commented_colon_and_explicit_indent():
    input_text = (
        "Lead\n```yaml\nitems:\n"
        "  - |2 # note: literal\n"
        "      more-indented\n"
        "    ```\n"
        "    example\n"
        "    ```\n"
        "other: 1\n```\n"
    )
    result = load_yaml(input_text)
    assert "example" in result["items"][0]
    assert result["other"] == 1


def test_quote_inside_plain_scalar_does_not_hide_later_block_scalar():
    input_text = (
        'Lead\n```yaml\ncode_suggestions:\n'
        '  - why: say "hello\n'
        '  - why: |\n'
        '      ```\n'
        '      code\n'
        '      ```\n'
        '  - why: last\n'
        '```\nAfter\n'
    )
    result = load_yaml(input_text)
    assert len(result['code_suggestions']) == 3
    assert 'code' in result['code_suggestions'][1]['why']
    assert result['code_suggestions'][2]['why'] == 'last'


def test_many_yaml_candidates_use_one_stream_and_bounded_read_ahead(monkeypatch):
    from io import StringIO

    class CountingStream(StringIO):
        instances = 0
        bytes_read = 0

        def __init__(self, value):
            super().__init__(value)
            type(self).instances += 1

        def read(self, size=-1):
            result = super().read(size)
            type(self).bytes_read += len(result)
            return result

    monkeypatch.setattr("pr_agent.algo.utils.StringIO", CountingStream)
    count = 300
    input_text = "Lead\n" + ("```yaml\n- a\n```\n" * count) + "```yaml\nanswer: 1\n```\n"
    assert _extract_labelled_yaml_block(input_text) == "answer: 1"
    assert CountingStream.instances == 1
    assert CountingStream.bytes_read <= len(input_text) + 4096 * (count + 1)


def test_quoted_yaml_sample_before_answer_keeps_final_block():
    input_text = """A sample from the diff:
```yaml
code_suggestions:
  - relevant_file: sample.py
```
Final answer:
```yaml
code_suggestions:
  - relevant_file: real.py
```
"""
    result = load_yaml(input_text)
    assert result['code_suggestions'][0]['relevant_file'] == 'real.py'


def test_last_labelled_yaml_wins_over_quoted_example():
    reply = (
        'Example:\n```yaml\ncode_suggestions:\n  - relevant_file: "evil.py"\n```\n'
        'Final answer:\n```yaml\ncode_suggestions:\n  - relevant_file: "real.py"\n```\n'
    )
    assert load_yaml(reply) == {"code_suggestions": [{"relevant_file": "real.py"}]}


def test_unclosed_final_labelled_yaml_wins_over_earlier_example():
    reply = (
        'Example:\n```yaml\ncode_suggestions:\n  - relevant_file: "evil.py"\n```\n'
        'Final answer:\n```yaml\ncode_suggestions:\n  - relevant_file: "real.py"\n'
    )
    assert load_yaml(reply) == {"code_suggestions": [{"relevant_file": "real.py"}]}


def test_labelled_yaml_parse_failure_does_not_log_input(monkeypatch):
    from unittest.mock import Mock

    logger = Mock()
    monkeypatch.setattr("pr_agent.algo.utils.get_logger", lambda: logger)
    reply = 'Example:\n```yaml\nprivate-canary: [bad\n```\nFinal:\n```yaml\nresult: ok\n```\n'
    assert _extract_labelled_yaml_block(reply) == 'result: ok'
    assert logger.debug.called
    assert all('private-canary' not in str(call) for call in logger.debug.call_args_list)


def test_indented_scalar_candidates_do_not_scan_remaining_suffix(monkeypatch):
    from io import StringIO

    class CountingStream(StringIO):
        bytes_read = 0

        def read(self, size=-1):
            result = super().read(size)
            type(self).bytes_read += len(result)
            return result

    monkeypatch.setattr("pr_agent.algo.utils.StringIO", CountingStream)
    input_text = "prose\n" + "  ```yaml\n  z\n  ```\n" * 600
    assert _extract_labelled_yaml_block(input_text) is None
    assert CountingStream.bytes_read <= 8 * len(input_text) + 4096


def test_long_scalar_with_many_fences_has_linear_scanner_reads(monkeypatch):
    from io import StringIO

    class CountingStream(StringIO):
        bytes_read = 0

        def read(self, size=-1):
            result = super().read(size)
            type(self).bytes_read += len(result)
            return result

    monkeypatch.setattr("pr_agent.algo.utils.StringIO", CountingStream)
    for opener, closer in [('value: |\n', ''), ('value: "start\n', '  end"\n')]:
        CountingStream.bytes_read = 0
        input_text = ('Lead\n```yaml\n' + opener + ('  ```\n  content\n' * 1600)
                      + closer + 'other: 1\n```\n')
        candidate = _extract_labelled_yaml_block(input_text)
        assert candidate is not None
        assert 'other: 1' in candidate
        assert CountingStream.bytes_read <= 8 * len(input_text) + 4096


def test_unclosed_labelled_block_preserves_terminal_newline():
    assert _extract_labelled_yaml_block('Prose\n```yaml\nanswer: 1\n') == 'answer: 1\n'
