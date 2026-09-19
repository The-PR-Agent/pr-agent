"""Contract: no tool may reach a git-provider method its provider has declared unsupported.

`test_git_provider_method_contract.py` pins what providers DO for a given capability;
this file pins what CALLERS in `pr_agent/tools/` do with the answer. A tool that calls a
capability-gated `self.git_provider.<method>(...)` without checking `is_supported()` first
raises (or silently corrupts output) on any provider that declines that capability: #2932
was exactly this, for `get_labels` in `pr_reviewer.py`.

Scope is intentionally the ENCLOSING FUNCTION, not the enclosing class: a guard several
frames away (a flag cached in `__init__`, a raise before a later call) is real, but it is
also invisible to the next person editing the callee, which is the failure mode this test
exists to catch. Every current call site either already checks locally or was given a
local check by this change (see `pr_update_changelog.py::_push_changelog_update` and
`pr_reviewer.py::_get_user_answers`) precisely so this test can be scoped this way without
false positives.
"""
import ast
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "pr_agent" / "tools"
PROVIDERS_DIR = Path(__file__).resolve().parent.parent.parent / "pr_agent" / "git_providers"

# Base GitProvider methods that at least one provider declines (see
# `_provider_declinable_capabilities` below), mapped to the `is_supported()` capability
# string that guards each. This mapping is domain knowledge (e.g. "get_labels covers
# publish_labels and get_pr_labels") and cannot be derived from the providers alone, so
# unlike the capability set below it is maintained by hand.
GUARDED_METHODS = {
    "get_pr_labels": "get_labels",
    "publish_labels": "get_labels",
    "publish_inline_comments": "publish_inline_comments",
    "create_inline_comment": "create_inline_comment",
    "get_issue_comments": "get_issue_comments",
    "create_or_update_pr_file": "push_code",
}


def _tool_files():
    return sorted(TOOLS_DIR.glob("*.py"))


def _is_supported_capability_strings(node):
    """Every literal string passed as the first arg to an `is_supported(...)` call
    anywhere under `node` (a module, class or function node)."""
    caps = set()
    for inner in ast.walk(node):
        if (isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "is_supported"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and isinstance(inner.args[0].value, str)):
            caps.add(inner.args[0].value)
    return caps


def _is_git_provider(node):
    """`self.git_provider`, or a bare `git_provider` name in a helper that takes the
    provider as a parameter (the pattern `pr_code_suggestions.py` already uses)."""
    if isinstance(node, ast.Name):
        return node.id == "git_provider"
    return (isinstance(node, ast.Attribute)
            and node.attr == "git_provider"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self")


def _git_provider_call_sites(func_node):
    """Every `self.git_provider.<method>(...)` or `git_provider.<method>(...)` call
    directly under a function node, keyed by method name."""
    calls = {}
    for inner in ast.walk(func_node):
        if (isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and _is_git_provider(inner.func.value)):
            calls.setdefault(inner.func.attr, []).append(inner)
    return calls


def _function_nodes(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _unguarded_call_sites():
    """(file, function, method, lineno) for every capability-gated
    `self.git_provider.<method>(...)` call with no matching `is_supported()` check
    anywhere in its own enclosing function.

    This is membership, not control flow: it does not check that the guard runs
    before the call, sits in the branch that actually reaches it, or that its result
    is used. A guard placed after the call, in an unrelated branch, or with its
    result ignored still satisfies this check. It catches a capability check being
    absent from the function entirely, which is the regression this test guards
    against; it will not catch one being present but misplaced.
    """
    hits = []
    for path in _tool_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for func_node in _function_nodes(tree):
            guarded_caps = _is_supported_capability_strings(func_node)
            calls = _git_provider_call_sites(func_node)
            for method, cap in GUARDED_METHODS.items():
                for call in calls.get(method, []):
                    if cap not in guarded_caps:
                        hits.append((path.name, func_node.name, method, call.lineno))
    return hits


def _provider_declinable_capabilities():
    """Every capability string at least one provider's `is_supported()` special-cases.

    Parsed from `pr_agent/git_providers/*.py` rather than hardcoded, so a new provider
    declining a new capability grows this set (and the typo check below) automatically
    instead of it silently going stale.

    Collects any string literal compared inside `is_supported()`, without checking
    which branch it feeds or whether that branch returns False. A capability named in
    a comparison that always returns True, or in an unrelated comparison, is treated
    as declinable the same as one a provider genuinely rejects.
    """
    caps = set()
    for path in sorted(PROVIDERS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "is_supported":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Compare):
                        for comparator in inner.comparators:
                            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                                caps.add(comparator.value)
                            elif isinstance(comparator, (ast.List, ast.Tuple, ast.Set)):
                                for elt in comparator.elts:
                                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                        caps.add(elt.value)
    return caps


def _is_supported_strings_used_in_tools():
    caps = set()
    for path in _tool_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        caps |= _is_supported_capability_strings(tree)
    return caps


def test_every_capability_gated_provider_call_is_locally_guarded():
    hits = _unguarded_call_sites()
    assert hits == [], (
        "self.git_provider.<method>() reachable without checking is_supported() in the "
        "same function first: "
        + ", ".join(f"{f}:{fn}() calls {m}() at line {ln}" for f, fn, m, ln in hits)
    )


def test_every_is_supported_string_in_tools_is_a_capability_some_provider_declines():
    used = _is_supported_strings_used_in_tools()
    declinable = _provider_declinable_capabilities()
    typos = used - declinable
    assert not typos, (
        f"is_supported() called in pr_agent/tools/ with a string no provider ever "
        f"declines, so the check always passes: {typos}"
    )
