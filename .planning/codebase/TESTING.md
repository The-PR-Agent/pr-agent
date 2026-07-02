# Testing Patterns

**Analysis Date:** 2026-07-02

## Test Framework

**Runner:**
- pytest 9.0.2
- Config: `pyproject.toml` (`[tool.pytest.ini_options]`)

**Async Support:**
- pytest-asyncio >= 1.3.0
- `asyncio_mode = "auto"` (no need for explicit `@pytest.mark.asyncio` on every test in most cases)

**Coverage:**
- pytest-cov 7.0.0

**Run Commands:**
```bash
pytest                    # Run all tests
pytest tests/unittest/    # Unit tests only
pytest tests/e2e_tests/   # E2E tests only
pytest --cov=pr_agent     # With coverage
pytest -k "test_name"     # Run specific test
```

## Test File Organization

**Location:**
- Separate `tests/` directory at project root (not co-located with source)

**Structure:**
```
tests/
├── e2e_tests/
│   ├── e2e_utils.py              # Shared E2E helpers
│   ├── test_bitbucket_app.py
│   ├── test_gitea_app.py
│   ├── test_github_app.py
│   └── test_gitlab_webhook.py
├── health_test/
├── unittest/
│   ├── _settings_helpers.py      # Shared test utilities (underscore prefix)
│   ├── test_clip_tokens.py
│   ├── test_convert_to_markdown.py
│   ├── test_pr_description.py
│   └── ... (~80 test files)
```

**Naming:**
- Test files: `test_<module_or_feature>.py`
- Test classes: `class Test<Feature>:`
- Test functions: `def test_<behavior>():`
- Helper modules: `_<name>.py` (underscore prefix, not collected by pytest)

## Test Structure

**Class-based grouping:**
```python
class TestClipTokens:
    """Comprehensive test suite for the clip_tokens function."""

    def test_empty_input_text(self):
        """Test that empty input returns empty string."""
        assert clip_tokens("", 10) == ""

    def test_text_under_token_limit(self):
        """Test that text under the token limit is returned unchanged."""
        text = "Short text"
        result = clip_tokens(text, 100)
        assert result == text
```

**Function-based (flat) tests:**
```python
def test_primary_model_success_invoked_once_and_returns_value():
    snapshot = _snapshot_settings()
    try:
        get_settings().set("config.model", "primary-model")
        # ... test logic ...
        assert result == "primary-result"
    finally:
        _restore_settings(snapshot)
```

**Patterns:**
- Classes group related tests for a single function/feature
- No `setUp`/`tearDown` methods; use try/finally for settings cleanup
- Docstrings on test methods describe expected behavior
- Direct assertions with `assert` (no assertEqual, assertTrue wrappers)

## Mocking

**Framework:** `unittest.mock` (stdlib)

**Common Patterns:**

Patching `__init__` to bypass construction:
```python
from unittest.mock import patch

def _make_instance(prediction_yaml: str):
    """Create a PRDescription instance, bypassing __init__."""
    with patch.object(PRDescription, '__init__', lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.prediction = prediction_yaml
    obj.keys_fix = KEYS_FIX
    return obj
```

Patching module-level functions:
```python
@patch('pr_agent.tools.pr_description.get_settings')
def test_diagram_not_starting_with_fence_is_removed(self, mock_get_settings):
    mock_get_settings.return_value = _mock_settings()
    # ... test logic ...
```

Patching with `monkeypatch` (pytest native):
```python
async def test_chat_completion_passes_seed(monkeypatch):
    monkeypatch.setattr(litellm_handler, "get_settings", lambda: FakeSettings(...))
    with patch("pr_agent.algo.ai_handlers.litellm_ai_handler.acompletion", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _mock_response()
        handler = litellm_handler.LiteLLMAIHandler()
        await handler.chat_completion(model="gpt-4o", system="sys", user="usr", temperature=0)
    assert mock_call.call_args.kwargs["seed"] == 123
```

Mock object construction:
```python
mock_git_provider = Mock()
mock_git_provider.get_line_link.return_value = 'https://github.com/...'
```

**What to Mock:**
- `get_settings()` - Configuration singleton (most commonly mocked)
- AI handlers / `acompletion` - LLM API calls
- Git provider methods - Network calls to GitHub/GitLab/Bitbucket APIs
- `boto3` clients - AWS service calls
- `TokenEncoder.get_token_encoder` - Tokenizer initialization

**What NOT to Mock:**
- Pure utility functions under test (`clip_tokens`, `extend_patch`, `fix_json_escape_char`)
- Data classes and types (`FilePatchInfo`, `EDIT_TYPE`)
- String processing and formatting logic

## Fixtures and Factories

**Fake Settings Pattern:**
```python
class FakeBox:
    def __init__(self, values=None, **attrs):
        self._values = values or {}
        for key, value in attrs.items():
            setattr(self, key, value)
    def get(self, key, default=None):
        return self._values.get(key, default)

class FakeSettings:
    def __init__(self, config_values=None):
        self.config = FakeBox(config_values or {}, reasoning_effort=None, ...)
    def get(self, key, default=None):
        return self._settings_values.get(key, default)
```

**Settings Snapshot/Restore Pattern** (from `tests/unittest/_settings_helpers.py`):
```python
from tests.unittest._settings_helpers import SENTINEL, restore_settings, snapshot_settings

_TRACKED_KEYS = ("config.model", "config.fallback_models", ...)

def test_something():
    snapshot = snapshot_settings(_TRACKED_KEYS)
    try:
        get_settings().set("config.model", "test-model")
        # ... test logic ...
    finally:
        restore_settings(snapshot)
```

**Bare Provider Pattern** (bypass `__init__` for testing static methods):
```python
def _bare_provider():
    """Create a GithubProvider without running __init__ (no network/auth)."""
    return GithubProvider.__new__(GithubProvider)
```

**Mock Content Maps** (simulate file content retrieval):
```python
def mock_get_content_of_file(self, project_key, repo_slug, filename, at=None):
    content_map = {
        '9c1cffdd...': 'file\nwith\nsome\nlines\n',
        '2a116544...': 'file\nwith\nmultiple\nlines\n',
    }
    return content_map.get(at, '')
```

**Location:**
- No shared `conftest.py` file; helpers are in `_settings_helpers.py`
- Factory functions defined at module top within each test file
- Test data defined as module-level constants or inline

## Coverage

**Requirements:** Not enforced in CI (no coverage threshold configured)

**View Coverage:**
```bash
pytest --cov=pr_agent --cov-report=html
pytest --cov=pr_agent --cov-report=term-missing
```

## Test Types

**Unit Tests** (`tests/unittest/`):
- ~80 test files covering individual functions and classes
- Isolated from network/APIs via mocking
- Fast execution, no external dependencies required
- Focus on: utility functions, parsers, formatters, providers (static methods), settings logic

**E2E Tests** (`tests/e2e_tests/`):
- 4 test files for major git platforms (GitHub, GitLab, Bitbucket, Gitea)
- Require live API credentials and real repositories
- Create actual PRs, wait for processing, verify tool outputs
- Use polling loops with `time.sleep(60)` for async processing
- Not intended for routine CI runs

**Health Tests** (`tests/health_test/`):
- Lightweight operational checks

## Common Patterns

**Async Testing:**
```python
import asyncio
import pytest

@pytest.mark.asyncio
async def test_chat_completion_passes_seed(monkeypatch):
    # ... async setup ...
    await handler.chat_completion(model="gpt-4o", system="sys", user="usr")
    assert mock_call.call_args.kwargs["seed"] == 123

# Alternative: use asyncio.run for sync test functions
def test_primary_model_success():
    result = asyncio.run(retry_with_fallback_models(fake_f))
    assert result == "primary-result"
```

**Error Testing:**
```python
def test_invalid_url_missing_pull_segment(self):
    p = _bare_provider()
    with pytest.raises(ValueError):
        p._parse_pr_url("https://github.com/owner/repo/issues/1")

# With cause chain verification
async def test_rejects_seed_for_model(monkeypatch):
    with pytest.raises(FakeAPIError) as exc_info:
        await handler.chat_completion(model="claude-opus-4-8", ...)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert str(exc_info.value.__cause__) == "Seed (123) is not supported..."
```

**Parametrized Tests:**
```python
@pytest.mark.parametrize("model", [
    "anthropic/claude-opus-4-8",
    "claude-opus-4-8",
    # ...
])
async def test_rejects_seed_for_various_models(model, monkeypatch):
    # ...
```

**Settings Manipulation (directly on global):**
```python
get_settings(use_context=False).set("CONFIG.CLI_MODE", True)
get_settings(use_context=False).config.allow_dynamic_context = False
```

## Test Conventions Summary

| Convention | Pattern |
|-----------|---------|
| Test discovery | `test_*.py` files in `tests/` |
| Class grouping | `class Test<Feature>:` for related tests |
| Flat functions | `def test_<behavior>():` for standalone scenarios |
| Assertions | Plain `assert` statements (no custom matchers) |
| Mocking | `unittest.mock.patch` / `monkeypatch` |
| Settings cleanup | Snapshot/restore via `_settings_helpers.py` or try/finally |
| Async | `@pytest.mark.asyncio` + `AsyncMock` or `asyncio.run()` |
| No shared conftest | Helpers in `_settings_helpers.py`, factories per-file |

---

*Testing analysis: 2026-07-02*
