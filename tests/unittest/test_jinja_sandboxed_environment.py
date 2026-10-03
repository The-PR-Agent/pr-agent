import ast
from pathlib import Path

_SCOPE_TYPES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)


def _scope_nodes(scope):
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _SCOPE_TYPES):
            yield child
        else:
            yield child
            yield from _scope_nodes(child)


def _argument_names(scope):
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return set()
    args = scope.args
    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    if args.vararg:
        parameters.append(args.vararg)
    if args.kwarg:
        parameters.append(args.kwarg)
    return {parameter.arg for parameter in parameters}


def _plain_environment_calls(module):
    calls = []

    def scan_scope(scope, parent_environment_names, parent_jinja2_names, parent_environment_modules):
        nodes = list(_scope_nodes(scope))
        bindings = set()
        import_bindings = set()
        jinja_environment_imports = set()
        jinja2_imports = set()
        environment_module_imports = set()

        for node in nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bindings.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bindings.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bindings.add(node.name)
            elif isinstance(node, ast.MatchAs) and node.name:
                bindings.add(node.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    name = alias.asname or alias.name
                    bindings.add(name)
                    import_bindings.add(name)
                    if node.module in {"jinja2", "jinja2.environment"} and alias.name == "Environment":
                        jinja_environment_imports.add(name)
                    elif node.module == "jinja2" and alias.name == "environment":
                        environment_module_imports.add(name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    bindings.add(name)
                    import_bindings.add(name)
                    if alias.name == "jinja2":
                        jinja2_imports.add(name)
                    elif alias.name == "jinja2.environment":
                        if alias.asname:
                            environment_module_imports.add(name)
                        else:
                            jinja2_imports.add(name)

        bindings.update(_argument_names(scope))
        non_jinja_imports = import_bindings - (jinja_environment_imports | jinja2_imports | environment_module_imports)
        non_import_bindings = (bindings - import_bindings) | non_jinja_imports
        environment_names = (parent_environment_names - bindings) | (jinja_environment_imports - non_import_bindings)
        jinja2_names = (parent_jinja2_names - bindings) | (jinja2_imports - non_import_bindings)
        environment_modules = (parent_environment_modules - bindings) | (
            environment_module_imports - non_import_bindings
        )

        for node in nodes:
            if isinstance(node, ast.Call) and _is_plain_environment_call(
                node.func, environment_names, jinja2_names, environment_modules
            ):
                calls.append(node.lineno)
            elif isinstance(node, _SCOPE_TYPES):
                # A method does not close over names in its class namespace.
                if isinstance(scope, ast.ClassDef) and isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
                ):
                    inherited = (
                        parent_environment_names,
                        parent_jinja2_names,
                        parent_environment_modules,
                    )
                else:
                    inherited = (environment_names, jinja2_names, environment_modules)
                scan_scope(node, *inherited)

    scan_scope(module, set(), set(), set())
    return sorted(calls)


def _is_plain_environment_call(function, environment_names, jinja2_names, environment_modules):
    if isinstance(function, ast.Name):
        return function.id in environment_names
    if not isinstance(function, ast.Attribute) or function.attr != "Environment":
        return False
    value = function.value
    if isinstance(value, ast.Name):
        return value.id in jinja2_names or value.id in environment_modules
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "environment"
        and isinstance(value.value, ast.Name)
        and value.value.id in jinja2_names
    )


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


def test_plain_jinja_environment_import_from_environment_module_is_detected():
    direct_import = ast.parse("from jinja2.environment import Environment\nEnvironment()")
    aliased_import = ast.parse("from jinja2.environment import Environment as PlainEnvironment\nPlainEnvironment()")
    assert _plain_environment_calls(direct_import) == [2]
    assert _plain_environment_calls(aliased_import) == [2]


def test_imports_and_shadowed_parameters_are_scoped():
    module = ast.parse(
        "def uses_jinja():\n"
        "    from jinja2 import Environment\n"
        "    Environment()\n"
        "\n"
        "def accepts_safe_factory(Environment):\n"
        "    Environment()\n"
    )
    assert _plain_environment_calls(module) == [3]
