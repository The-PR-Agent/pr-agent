# Coding Conventions

**Analysis Date:** 2026-07-02

## Naming Patterns

**Files:**
- snake_case for all Python modules: `pr_processing.py`, `token_handler.py`, `litellm_ai_handler.py`
- Test files prefixed with `test_`: `test_clip_tokens.py`, `test_pr_description.py`
- Private/helper modules prefixed with underscore: `_settings_helpers.py`

**Functions:**
- snake_case throughout: `get_pr_diff()`, `clip_tokens()`, `retry_with_fallback_models()`
- Private methods prefixed with underscore: `_prepare_data()`, `_parse_pr_url()`
- Factory/builder helpers prefixed with `_make_`: `_make_instance()`, `_make_reviewer()`

**Variables:**
- snake_case for locals and instance attributes: `self.git_provider`, `self.main_pr_language`
- UPPER_SNAKE_CASE for module-level constants: `MAX_FILES_ALLOWED_FULL`, `OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD`, `MODEL_RETRIES`
- Constants defined at module top, after imports

**Types/Classes:**
- PascalCase for classes: `PRDescription`, `LiteLLMAIHandler`, `GitProvider`
- PascalCase for Enums: `ModelType`, `ReasoningEffort`, `PRReviewHeader`
- Enums inherit from `(str, Enum)` for string-serializable constants
- Pydantic `BaseModel` for structured data: `Range` in `pr_agent/algo/utils.py`
- `TypedDict` for dictionary shapes: `TodoItem` in `pr_agent/algo/utils.py`

## Code Style

**Formatting:**
- Ruff (configured in `pyproject.toml`)
- Line length: 120 characters
- No explicit formatter (black/autopep8) configured; Ruff handles style

**Linting:**
- Ruff with rules: E (pycodestyle), F (pyflakes), B (flake8-bugbear), I001/I002 (isort)
- `# noqa: E501` used inline for intentionally long lines (test assertions)
- Bandit for security scanning (configured in `pyproject.toml`, skips B101 assert)
- `lint.exclude = ["api/code_completions"]`

## Import Organization

**Order:**
1. `from __future__ import annotations` (when used)
2. Standard library: `os`, `re`, `asyncio`, `json`, `copy`, `traceback`
3. Third-party: `litellm`, `openai`, `yaml`, `fastapi`, `pydantic`
4. Local imports: `from pr_agent.algo.utils import ...`, `from pr_agent.config_loader import get_settings`

**Style:**
- Explicit imports preferred over star imports
- Multi-symbol imports wrapped in parentheses across lines:
  ```python
  from pr_agent.algo.pr_processing import (OUTPUT_BUFFER_TOKENS_HARD_THRESHOLD,
                                           get_pr_diff,
                                           get_pr_diff_multiple_patchs,
                                           retry_with_fallback_models)
  ```
- isort auto-fixed via Ruff (I001 rule)

**Path Aliases:**
- None. All imports use full dotted paths from `pr_agent` root package.

## Error Handling

**Patterns:**
- Try/except with logging and graceful fallback (never crash silently):
  ```python
  try:
      # operation
  except Exception as e:
      get_logger().error(f"Description {e}")
      return fallback_value
  ```
- Specific exceptions caught where possible: `RateLimitExceededException`, `ClientError`
- Re-raise after logging when the caller needs to know:
  ```python
  except RateLimitExceededException as e:
      get_logger().error(f"Rate limit exceeded. original message {e}")
      raise
  ```
- Functions return empty string `""`, empty list `[]`, or original input on failure (never `None` unless explicitly documented)
- `ValueError` raised for invalid input in parsers (e.g., `_parse_pr_url`)

## Logging

**Framework:** Loguru via `pr_agent/log/__init__.py`

**Access pattern:**
```python
from pr_agent.log import get_logger
get_logger().info("message")
get_logger().warning("message", artifact={"key": value})
get_logger().error(f"Description {e}")
```

**Levels used:**
- `debug` - Internal flow tracing
- `info` - Normal operations, configuration choices
- `warning` - Recoverable issues, fallbacks, misconfigurations
- `error` - Failures that affect output
- `exception` - Failures with full traceback

**Structured data:** Pass `artifact={}` kwarg for structured metadata attached to log entries.

## Comments

**When to Comment:**
- Module-level docstrings for complex classes (especially `__init__` methods)
- Inline comments for non-obvious logic or configuration choices
- `# noqa` annotations for intentional lint suppressions with rule code

**Docstrings:**
- Triple-quoted, Google-style for public APIs:
  ```python
  def __init__(self, pr_url: str, args: list = None):
      """
      Initialize the PRDescription object...
      Args:
          pr_url (str): The URL of the pull request.
          args (list, optional): List of arguments. Defaults to None.
      """
  ```
- Not universally applied; many internal functions lack docstrings

## Function Design

**Size:** Functions tend to be medium-to-large (20-80 lines). No strict limit enforced.

**Parameters:**
- Type hints on parameters and return types for public APIs
- Optional params use `= None` default, not `Optional[X]` alone
- `partial` used for deferred handler construction: `ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler`

**Return Values:**
- Single return type preferred
- Tuples for multi-value returns: `Tuple[str, int]`
- Empty string or empty collection as "no result" (not None, unless input was None)

## Module Design

**Exports:**
- No `__all__` defined in most modules
- `__init__.py` files used for re-exports from subpackages (e.g., `pr_agent/git_providers/__init__.py`)

**Abstract Base Classes:**
- `ABC` + `@abstractmethod` for provider interfaces: `GitProvider`, `BaseAiHandler`
- Concrete implementations in separate files per provider

**Configuration:**
- Dynaconf singleton accessed via `get_settings()` from `pr_agent/config_loader.py`
- Request-scoped settings via starlette context (deep-copied per request)
- TOML files in `pr_agent/settings/` for defaults
- Settings accessed as dotted attributes: `get_settings().pr_description.enable_semantic_files_types`

## Async Patterns

- `async def` for AI handler methods and webhook handlers
- `asyncio.run()` used at entry points (CLI)
- `acompletion` from litellm for async LLM calls
- `tenacity` retry decorators for resilient API calls

## Design Patterns

**Provider Pattern:**
- Abstract base (`GitProvider`, `BaseAiHandler`) with multiple concrete implementations
- Factory functions: `get_git_provider()`, `get_git_provider_with_context()`

**Settings/Configuration:**
- Dynaconf global singleton with per-request context override
- TOML-based defaults with environment variable overrides

**Template Rendering:**
- Jinja2 with `StrictUndefined` for prompt templates
- Variables dict pattern: `self.vars = {"title": ..., "diff": ...}`

---

*Convention analysis: 2026-07-02*
