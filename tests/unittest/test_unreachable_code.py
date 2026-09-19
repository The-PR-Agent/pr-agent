"""Regression guard for unreachable code.

Every function and method defined in pr_agent/ must be referenced somewhere in
pr_agent/, tests/ or docs/. Detection is deliberately conservative: entry points
that are named from outside Python (route handlers, Lambda handlers) and members
of third-party interfaces called by the library that owns them are recorded in
the allowlist with a note pointing at the caller.

Abstract methods, properties, validators and dunders are skipped: they are
reached through the interface or the descriptor protocol rather than by name.

Adding a definition nothing calls fails this test, so the dead-code ledger
cleared in #3182 cannot silently regrow.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PR_AGENT_SOURCE = ROOT / "pr_agent"
SEARCH_ROOTS = (ROOT / "pr_agent", ROOT / "tests", ROOT / "docs")
SEARCH_SUFFIXES = (".py", ".md", ".toml", ".yaml", ".yml")

# Decorators that mark a definition as reachable from outside Python.
_ENTRY_POINT_DECORATORS = (
    "get", "post", "put", "delete", "patch", "route", "head", "options",
    "on_event", "exception_handler", "middleware", "websocket",
)
# Decorators after which the name is reached through the descriptor protocol.
_INDIRECT_DECORATORS = ("property", "setter", "deleter", "validator", "abstractmethod")

# Definitions with no in-repo reference that are nonetheless live. Each entry
# documents what calls it.
_ALLOWLIST = {
    (
        "pr_agent/servers/github_lambda_webhook.py",
        "lambda_handler",
    ): "AWS Lambda entry point, named in docker/Dockerfile.lambda",
    (
        "pr_agent/servers/gitlab_lambda_webhook.py",
        "lambda_handler",
    ): "AWS Lambda entry point, named in docker/Dockerfile.lambda",
    (
        "pr_agent/algo/ai_handlers/litellm_ai_handler.py",
        "get_aws_security_credentials",
    ): "google.auth.aws.AwsSecurityCredentialsSupplier interface method, called by google-auth",
    (
        "pr_agent/algo/ai_handlers/litellm_ai_handler.py",
        "get_aws_region",
    ): "google.auth.aws.AwsSecurityCredentialsSupplier interface method, called by google-auth",
}


def _is_skipped(node) -> bool:
    name = node.name
    if name.startswith("__") and name.endswith("__"):
        return True
    for decorator in node.decorator_list:
        rendered = ast.unparse(decorator)
        attribute = rendered.split("(")[0].split(".")[-1]
        if any(marker in rendered for marker in _INDIRECT_DECORATORS):
            return True
        if "." in rendered and attribute in _ENTRY_POINT_DECORATORS:
            return True
    return False


def _definitions() -> dict[tuple[str, str], int]:
    """Map (relative path, name) to the line the definition starts on."""
    found = {}
    for path in sorted(PR_AGENT_SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_skipped(node):
                continue
            found.setdefault(
                (path.relative_to(ROOT).as_posix(), node.name), node.lineno
            )
    return found


def _corpus() -> list[str]:
    # This file names every allowlisted definition, so reading it would make
    # each of them look referenced.
    this_file = Path(__file__).resolve()
    texts = []
    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in SEARCH_SUFFIXES:
                continue
            if path.resolve() == this_file:
                continue
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return texts


def _is_referenced(name: str, corpus: list[str]) -> bool:
    occurrence = re.compile(r"\b" + re.escape(name) + r"\b")
    definition = re.compile(
        r"[ \t]*(?:async[ \t]+)?(?:def|class)[ \t]+" + re.escape(name) + r"\b"
    )
    for text in corpus:
        for match in occurrence.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            if definition.match(text, line_start):
                continue
            return True
    return False


def test_every_definition_is_reachable_or_allowlisted():
    corpus = _corpus()
    unreferenced = {
        location
        for location in _definitions()
        if not _is_referenced(location[1], corpus)
    }
    allowlisted = set(_ALLOWLIST)

    assert unreferenced <= allowlisted, (
        "definitions in pr_agent/ have no reference in pr_agent/, tests/ or docs/; "
        f"remove them or allowlist them: {sorted(unreferenced - allowlisted)}"
    )
    assert allowlisted - unreferenced == set(), (
        "allowlist has stale entries (those definitions are now referenced or no "
        f"longer present): {sorted(allowlisted - unreferenced)}"
    )
