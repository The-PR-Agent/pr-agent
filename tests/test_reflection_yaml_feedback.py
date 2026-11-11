
import pytest
from pr_agent.algo.utils import load_yaml

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
