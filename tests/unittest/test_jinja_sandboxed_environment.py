import ast
from pathlib import Path


def test_prompt_rendering_does_not_instantiate_plain_jinja_environment():
    package_root = Path(__file__).resolve().parents[2] / "pr_agent"
    unsafe_calls = []
    for source_path in sorted(package_root.rglob("*.py")):
        module = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            is_plain_environment = (isinstance(function, ast.Name) and function.id == "Environment") or (
                isinstance(function, ast.Attribute) and function.attr == "Environment"
            )
            if is_plain_environment:
                unsafe_calls.append(f"{source_path.relative_to(package_root.parent)}:{node.lineno}")
    assert not unsafe_calls, (
        "Use jinja2.sandbox.SandboxedEnvironment for prompt templates; "
        "plain Environment calls remain at: " + ", ".join(unsafe_calls)
    )
