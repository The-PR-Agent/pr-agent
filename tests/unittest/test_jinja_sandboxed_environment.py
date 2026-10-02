import ast
from pathlib import Path


def _plain_environment_calls(module):
    environment_names = set()
    jinja2_names = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ImportFrom) and node.module == "jinja2":
            environment_names.update(alias.asname or alias.name for alias in node.names if alias.name == "Environment")
        elif isinstance(node, ast.Import):
            jinja2_names.update(alias.asname or alias.name for alias in node.names if alias.name == "jinja2")

    calls = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        imported_environment = isinstance(function, ast.Name) and function.id in environment_names
        qualified_environment = (
            isinstance(function, ast.Attribute)
            and function.attr == "Environment"
            and isinstance(function.value, ast.Name)
            and function.value.id in jinja2_names
        )
        if imported_environment or qualified_environment:
            calls.append(node.lineno)
    return sorted(calls)


def test_prompt_rendering_does_not_instantiate_plain_jinja_environment():
    package_root = Path(__file__).resolve().parents[2] / "pr_agent"
    unsafe_calls = []
    for source_path in sorted(package_root.rglob("*.py")):
        module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for line in _plain_environment_calls(module):
            unsafe_calls.append(f"{source_path.relative_to(package_root.parent)}:{line}")
    assert not unsafe_calls, (
        "Use jinja2.sandbox.SandboxedEnvironment for prompt templates; "
        "plain Environment calls remain at: " + ", ".join(unsafe_calls)
    )


def test_plain_jinja_environment_import_aliases_are_detected():
    imported_alias = ast.parse("from jinja2 import Environment as PlainEnvironment\nPlainEnvironment()")
    module_alias = ast.parse("import jinja2 as jinja\njinja.Environment()")
    assert _plain_environment_calls(imported_alias) == [2]
    assert _plain_environment_calls(module_alias) == [2]
