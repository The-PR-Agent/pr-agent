## Overview

The `generate_labels` tool scans the PR code changes and generates custom labels for the PR based on the content and context of the changes.

It can be invoked manually by commenting on any PR:

```
/generate_labels
```

## Example usage

Invoke the tool manually by commenting `/generate_labels` on any PR:

![Generate Labels](https://codium.ai/images/pr_agent/generate_labels_comment.png){width=512}

The tool will analyze the PR and add appropriate labels:

![Generate Labels Result](https://codium.ai/images/pr_agent/generate_labels_result.png){width=512}

## Configuration options

The `generate_labels` tool uses configurations from the `[pr_description]` section for custom labels.

### Enabling custom labels

To use custom labels, you need to enable them in the configuration:

```toml
[config]
enable_custom_labels = true
```

### Defining custom labels

You can define your own custom labels in the `[custom_labels]` section. See the [custom_labels.toml](https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/custom_labels.toml) file for examples.

Example configuration:

```toml
[custom_labels."Bug fix"]
description = "A fix for a bug in the codebase"

[custom_labels."Feature"]
description = "A new feature or enhancement"

[custom_labels."Documentation"]
description = "Documentation changes only"

[custom_labels."Tests"]
description = "Adding or modifying tests"

[custom_labels."Refactoring"]
description = "Code refactoring without functional changes"
```

### How labels are applied

1. The tool analyzes the PR diff and commit messages
2. It uses AI to determine which labels best match the PR content
3. Labels are automatically applied to the PR (if the git provider supports it)
4. If labels cannot be applied directly, they are published as a comment

## Comparison with `/describe` labels

The `/describe` tool also generates labels as part of its output. The key differences are:

| Feature | `/generate_labels` | `/describe` |
|---------|-------------------|-------------|
| Purpose | Dedicated label generation | Full PR description with labels |
| Output | Labels only | Title, summary, walkthrough, and labels |
| Custom labels | ✅ Supported | ✅ Supported |
| Use case | When you only need labels | When you want a complete PR description |

## Tips

- Use custom labels that match your team's workflow and labeling conventions
- Combine with automation to automatically label PRs when they are opened
- Review the generated labels and adjust custom label descriptions if the AI consistently misclassifies PRs
