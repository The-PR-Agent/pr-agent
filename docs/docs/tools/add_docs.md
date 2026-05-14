## Overview

The `add_docs` tool scans the PR code changes and suggests documentation for any code components that are missing documentation, such as functions, classes, and methods.

It can be invoked manually by commenting on any PR:

```
/add_docs
```

## Example usage

Invoke the tool manually by commenting `/add_docs` on any PR:

![Add Docs](https://codium.ai/images/pr_agent/add_docs_comment.png){width=512}

The tool will generate documentation suggestions as inline code suggestions:

![Add Docs Result](https://codium.ai/images/pr_agent/add_docs_result.png){width=512}

### Language-specific documentation styles

The tool automatically detects the programming language and generates documentation in the appropriate format:

| Language | Documentation Format |
|----------|---------------------|
| Python | Docstrings (Sphinx, Google, Numpy styles) |
| Java | Javadocs |
| JavaScript/TypeScript | JSdocs |
| C++ | Doxygen |
| Other | Generic documentation |

## Configuration options

Under the section `[pr_add_docs]`, the following options are available:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `extra_instructions` | string | `""` | Additional instructions for the AI model |
| `docs_style` | string | `"Sphinx"` | Documentation style for Python. Options: `"Sphinx"`, `"Google Style with Args, Returns, Attributes...etc"`, `"Numpy Style"`, `"PEP257"`, `"reStructuredText"` |
| `file` | string | `""` | Specific file to document (useful when multiple components have the same name) |
| `class_name` | string | `""` | Specific class name to target (useful when methods have the same name in the same file) |

### Example configuration

To customize the documentation style, add the following to your configuration file:

```toml
[pr_add_docs]
docs_style = "Google Style with Args, Returns, Attributes...etc"
extra_instructions = "Focus on documenting public methods and include usage examples"
```

### Command line options

You can pass configuration options directly in the command:

```
/add_docs --pr_add_docs.docs_style="Numpy Style"
```

## How it works

1. The tool analyzes the PR diff to identify code components (functions, classes, methods) that lack documentation
2. It uses AI to generate appropriate documentation based on the code context and language
3. Documentation suggestions are published as inline code suggestions that can be applied with a single click
