
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
    input_text = "```python\nprint('hi')\n```\n```yaml\n- x\n- y\n```\n"
    result = load_yaml(input_text)
    assert result == {}
