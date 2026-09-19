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
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PR_AGENT_SOURCE = ROOT / "pr_agent"
SEARCH_ROOTS = (ROOT / "pr_agent", ROOT / "tests")

# Treat these decorators as entry points reached from outside Python.
_ENTRY_POINT_DECORATORS = (
    "get", "post", "put", "delete", "patch", "route", "head", "options",
    "on_event", "exception_handler", "middleware", "websocket",
)
# Treat these decorators as references through the descriptor protocol.
_INDIRECT_DECORATORS = ("property", "setter", "deleter", "validator", "abstractmethod")

# Record live definitions with no in-repo reference and name their callers.
_ALLOWLIST = {
    ("pr_agent/algo/utils.py", "convert_str_to_datetime"): "public utility retained for compatibility",
    (
        "pr_agent/git_providers/azuredevops_provider.py",
        "get_existing_inline_comment_fingerprints",
    ): "called dynamically through getattr in inline_comment_dedup.py",
    ("pr_agent/git_providers/gerrit_provider.py", "show"): "public Gerrit command wrapper",
    (
        "pr_agent/git_providers/git_provider.py",
        "get_lines_link_original_file",
    ): "optional GitProvider interface method",
    (
        "pr_agent/git_providers/github_provider.py",
        "get_lines_link_original_file",
    ): "optional GitProvider interface implementation",
    ("pr_agent/log/__init__.py", "critical"): "StructuredLogger compatibility method",
    ("pr_agent/log/__init__.py", "json_format"): "public logging formatter",
    ("pr_agent/servers/gerrit_server.py", "get_body"): "server request-body helper",
    ("pr_agent/servers/gitea_app.py", "get_body"): "server request-body helper",
    ("pr_agent/servers/github_app.py", "get_body"): "server request-body helper",
    (
        "pr_agent/tools/pr_reviewer.py",
        "auto_approve_logic",
    ): "feature-gated review helper retained for configuration compatibility",
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


class _DefinitionVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.class_depth = 0
        self.found = []

    def visit_ClassDef(self, node):
        self.class_depth += 1
        self.generic_visit(node)
        self.class_depth -= 1

    def visit_FunctionDef(self, node):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node)

    def _visit_function(self, node):
        if not _is_skipped(node):
            self.found.append((self.path, node.name, node.lineno, self.class_depth > 0))
        self.generic_visit(node)


def _definitions() -> list[tuple[str, str, int, bool]]:
    """Return every definition as (relative path, name, line, is method)."""
    found = []
    for path in sorted(PR_AGENT_SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        visitor = _DefinitionVisitor(path.relative_to(ROOT).as_posix())
        visitor.visit(tree)
        found.extend(visitor.found)
    return found


def _reference_counts(roots: tuple[Path, ...] = SEARCH_ROOTS) -> Counter[str]:
    """Count executable name and attribute references in Python sources."""
    # Exclude this file because it names every allowlisted definition.
    this_file = Path(__file__).resolve()
    counts = Counter()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path.resolve() == this_file:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    counts[node.id] += 1
                elif isinstance(node, ast.Attribute):
                    counts[node.attr] += 1
    return counts


def _unreferenced_definitions(
    definitions: list[tuple[str, str, int, bool]], reference_counts: Counter[str]
) -> set[tuple[str, str]]:
    """Return definitions whose name lacks enough executable references."""
    by_name = defaultdict(list)
    for path, name, line, is_method in definitions:
        by_name[(name, is_method)].append((path, name, line))

    unreferenced = set()
    for (name, is_method), locations in by_name.items():
        required_references = 1 if is_method else len(locations)
        if reference_counts[name] < required_references:
            # Flag every colliding definition because a name-only AST cannot
            # safely decide which same-named definition owns the references.
            unreferenced.update((path, name) for path, name, _ in locations)
    return unreferenced


def test_every_definition_is_reachable_or_allowlisted():
    unreferenced = _unreferenced_definitions(_definitions(), _reference_counts())
    allowlisted = set(_ALLOWLIST)

    assert unreferenced <= allowlisted, (
        "definitions in pr_agent/ have no executable reference in pr_agent/ or tests/; "
        f"remove them or allowlist them: {sorted(unreferenced - allowlisted)}"
    )
    assert allowlisted - unreferenced == set(), (
        "allowlist has stale entries (those definitions are now referenced or no "
        f"longer present): {sorted(allowlisted - unreferenced)}"
    )


def test_comments_and_strings_do_not_count_as_references(tmp_path):
    source = tmp_path / "example.py"
    source.write_text(
        'def unused():\n    pass\n\ntext = "unused"\n# unused\n',
        encoding="utf-8",
    )
    assert _reference_counts((tmp_path,))["unused"] == 0


def test_duplicate_definitions_need_a_reference_for_each_one():
    definitions = [
        ("first.py", "load_auth", 1, False),
        ("second.py", "load_auth", 1, False),
    ]

    assert _unreferenced_definitions(definitions, Counter(load_auth=1)) == {
        ("first.py", "load_auth"),
        ("second.py", "load_auth"),
    }
