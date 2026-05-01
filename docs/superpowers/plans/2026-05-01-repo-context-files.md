# Repo Context Files Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add explicit config-driven repository context files that are loaded from the target repository and injected into review, describe, and improve prompts.

**Architecture:** Add a small context-loading helper in `pr_agent/algo/repo_context.py` that reads `[config].repo_context_files`, asks the active git provider for each file, formats loaded content with file headers, and enforces a total line cap. Add a base git-provider method plus a GitHub implementation for fetching repository files from the default branch. Tool classes add `repo_context` to prompt variables, and prompt templates render it only when present.

**Tech Stack:** Python 3.12, Dynaconf settings, PyGithub provider APIs, Jinja2 prompt templates, pytest.

---

### Task 1: Add Repo Context Loader

**Files:**
- Create: `pr_agent/algo/repo_context.py`
- Test: `tests/unittest/test_repo_context.py`

- [x] **Step 1: Write failing loader tests**

Add `tests/unittest/test_repo_context.py`:

```python
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.config_loader import get_settings


class FakeProvider:
    def __init__(self, files):
        self.files = files
        self.requested_paths = []

    def get_repo_file_content(self, file_path: str):
        self.requested_paths.append(file_path)
        return self.files.get(file_path)


def test_build_repo_context_returns_empty_when_no_files_configured():
    original_files = get_settings().config.get("repo_context_files", [])
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", [])

        assert build_repo_context(FakeProvider({"AGENTS.md": "repo purpose"})) == ""
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)


def test_build_repo_context_fetches_and_formats_configured_files():
    original_files = get_settings().config.get("repo_context_files", [])
    original_max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", 500)
        provider = FakeProvider({
            "AGENTS.md": "# Agent Guide\nUse focused tests.",
            "CONTRIBUTING.md": "Keep PRs small.",
        })

        context = build_repo_context(provider)

        assert context == (
            "## AGENTS.md\n"
            "# Agent Guide\n"
            "Use focused tests.\n\n"
            "## CONTRIBUTING.md\n"
            "Keep PRs small."
        )
        assert provider.requested_paths == ["AGENTS.md", "CONTRIBUTING.md"]
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)


def test_build_repo_context_skips_missing_and_invalid_files():
    original_files = get_settings().config.get("repo_context_files", [])
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", ["", 7, "MISSING.md", "AGENTS.md"])
        provider = FakeProvider({"AGENTS.md": "Loaded context"})

        assert build_repo_context(provider) == "## AGENTS.md\nLoaded context"
        assert provider.requested_paths == ["MISSING.md", "AGENTS.md"]
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)


def test_build_repo_context_enforces_total_line_cap():
    original_files = get_settings().config.get("repo_context_files", [])
    original_max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", ["AGENTS.md", "CONTRIBUTING.md"])
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", 4)
        provider = FakeProvider({
            "AGENTS.md": "one\ntwo\nthree",
            "CONTRIBUTING.md": "four\nfive",
        })

        context = build_repo_context(provider)

        assert context == "## AGENTS.md\none\ntwo\nthree"
    finally:
        get_settings().set("CONFIG.REPO_CONTEXT_FILES", original_files)
        get_settings().set("CONFIG.REPO_CONTEXT_MAX_LINES", original_max_lines)
```

- [x] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py -q`

Expected: FAIL during import with `ModuleNotFoundError: No module named 'pr_agent.algo.repo_context'`.

- [x] **Step 3: Add minimal loader implementation**

Create `pr_agent/algo/repo_context.py`:

```python
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def build_repo_context(git_provider) -> str:
    context_files = get_settings().config.get("repo_context_files", [])
    if not context_files:
        return ""

    max_lines = get_settings().config.get("repo_context_max_lines", 500)
    try:
        max_lines = max(0, int(max_lines))
    except (TypeError, ValueError):
        max_lines = 500

    rendered_lines = []
    for file_path in context_files:
        if not isinstance(file_path, str) or not file_path.strip():
            get_logger().warning("Skipping invalid repo context file path", artifact={"file_path": file_path})
            continue

        file_path = file_path.strip()
        try:
            content = git_provider.get_repo_file_content(file_path)
        except Exception as e:
            get_logger().warning(f"Failed to load repo context file: {file_path}", artifact={"error": str(e)})
            continue

        if not content:
            get_logger().debug(f"Repo context file is empty or missing: {file_path}")
            continue

        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")

        file_lines = [f"## {file_path}", *str(content).strip().splitlines()]
        remaining_lines = max_lines - len(rendered_lines)
        if remaining_lines <= 0:
            break

        if rendered_lines:
            rendered_lines.append("")
            remaining_lines -= 1

        rendered_lines.extend(file_lines[:remaining_lines])

    return "\n".join(rendered_lines).strip()
```

- [x] **Step 4: Run tests to verify pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py -q`

Expected: PASS.

### Task 2: Add Provider Fetch Method And Defaults

**Files:**
- Modify: `pr_agent/settings/configuration.toml`
- Modify: `pr_agent/git_providers/git_provider.py`
- Modify: `pr_agent/git_providers/github_provider.py`
- Test: `tests/unittest/test_repo_context.py`

- [x] **Step 1: Write failing provider tests**

Append to `tests/unittest/test_repo_context.py`:

```python
from unittest.mock import Mock

from pr_agent.git_providers.git_provider import GitProvider
from pr_agent.git_providers.github_provider import GithubProvider


def test_base_provider_repo_file_content_returns_empty():
    provider = GitProvider.__new__(GitProvider)

    assert provider.get_repo_file_content("AGENTS.md") == ""


def test_github_provider_fetches_repo_file_content_from_default_branch():
    provider = GithubProvider.__new__(GithubProvider)
    provider.repo_obj = Mock()
    provider.repo_obj.get_contents.return_value.decoded_content = b"repo context"

    assert provider.get_repo_file_content("AGENTS.md") == "repo context"
    provider.repo_obj.get_contents.assert_called_once_with("AGENTS.md")
```

- [x] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py -q`

Expected: FAIL because `get_repo_file_content` does not exist.

- [x] **Step 3: Add config defaults and provider methods**

In `pr_agent/settings/configuration.toml`, under `[config]`, add:

```toml
repo_context_files = []
repo_context_max_lines = 500
```

In `pr_agent/git_providers/git_provider.py`, add near `get_repo_settings`:

```python
    def get_repo_file_content(self, file_path: str):
        return ""
```

In `pr_agent/git_providers/github_provider.py`, add near `get_repo_settings`:

```python
    def get_repo_file_content(self, file_path: str):
        try:
            contents = self.repo_obj.get_contents(file_path).decoded_content
            if isinstance(contents, bytes):
                return contents.decode("utf-8", errors="replace")
            return contents
        except Exception as e:
            get_logger().warning(f"Failed to load repo file: {file_path}, error: {e}")
            return ""
```

- [x] **Step 4: Run tests to verify pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py -q`

Expected: PASS.

### Task 3: Inject Repo Context Into Tool Variables And Prompts

**Files:**
- Modify: `pr_agent/tools/pr_reviewer.py`
- Modify: `pr_agent/tools/pr_description.py`
- Modify: `pr_agent/tools/pr_code_suggestions.py`
- Modify: `pr_agent/settings/pr_reviewer_prompts.toml`
- Modify: `pr_agent/settings/pr_description_prompts.toml`
- Modify: `pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.toml`
- Modify: `pr_agent/settings/code_suggestions/pr_code_suggestions_prompts_not_decoupled.toml`
- Test: `tests/unittest/test_repo_context.py`

- [x] **Step 1: Write failing variable and prompt tests**

Append to `tests/unittest/test_repo_context.py`:

```python
from jinja2 import Environment, StrictUndefined

from pr_agent.settings import pr_description_prompts, pr_reviewer_prompts


def test_reviewer_prompt_renders_repo_context_block():
    variables = {
        "extra_instructions": "",
        "repo_context": "## AGENTS.md\nRepo purpose",
        "require_can_be_split_review": False,
        "related_tickets": "",
        "require_estimate_contribution_time_cost": False,
        "require_score": False,
        "require_tests": True,
        "question_str": "",
        "require_security_review": True,
        "require_todo_scan": False,
        "require_estimate_effort_to_review": True,
        "num_max_findings": 3,
        "num_pr_files": 1,
        "is_ai_metadata": False,
    }

    rendered = Environment(undefined=StrictUndefined).from_string(
        pr_reviewer_prompts.pr_review_prompt.system
    ).render(variables)

    assert "Repository context:" in rendered
    assert "## AGENTS.md" in rendered


def test_description_prompt_renders_repo_context_block():
    variables = {
        "extra_instructions": "",
        "repo_context": "## AGENTS.md\nRepo purpose",
        "enable_custom_labels": False,
        "custom_labels_class": "",
        "enable_semantic_files_types": True,
        "include_file_summary_changes": True,
        "enable_pr_diagram": False,
    }

    rendered = Environment(undefined=StrictUndefined).from_string(
        pr_description_prompts.pr_description_prompt.system
    ).render(variables)

    assert "Repository context:" in rendered
    assert "## AGENTS.md" in rendered
```

- [x] **Step 2: Run tests to verify failure**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py -q`

Expected: FAIL because prompt templates do not render `repo_context`.

- [x] **Step 3: Wire loader into tools**

In `pr_agent/tools/pr_reviewer.py`, import:

```python
from pr_agent.algo.repo_context import build_repo_context
```

Add to `self.vars`:

```python
            "repo_context": build_repo_context(self.git_provider),
```

Repeat the same import and variable addition in `pr_agent/tools/pr_description.py` and `pr_agent/tools/pr_code_suggestions.py`.

- [x] **Step 4: Add prompt blocks**

In review, description, and both code suggestion prompt TOML files, add after the `extra_instructions` block:

```jinja
{%- if repo_context %}


Repository context:
======
{{ repo_context }}
======
{% endif %}
```

- [x] **Step 5: Run tests to verify pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py -q`

Expected: PASS.

### Task 4: Regression Verification

**Files:**
- No new files

- [x] **Step 1: Run focused tests**

Run: `PYTHONPATH=. ./.venv/bin/pytest tests/unittest/test_repo_context.py tests/unittest/test_pr_description.py -q`

Expected: PASS.

- [x] **Step 2: Inspect diff**

Run: `git diff -- pr_agent tests docs/superpowers/plans/2026-05-01-repo-context-files.md`

Expected: Diff is scoped to repo context loader, provider method/defaults, prompt wiring, tests, and this plan.

- [x] **Step 3: Commit implementation**

Run:

```bash
git add pr_agent tests docs/superpowers/plans/2026-05-01-repo-context-files.md
git commit -m "feat: add configurable repo context files"
```

Expected: Commit succeeds with only relevant files staged.
