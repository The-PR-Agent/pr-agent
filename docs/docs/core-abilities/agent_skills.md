# Agent Skills

`Supported Tools: Review, Improve, Describe`

## Overview

Agent Skills let you distribute curated, reusable review guidance to PR-Agent using the [agent-skills (`SKILL.md`) format](https://github.com/The-PR-Agent/pr-agent/issues/2384). A skill is a directory containing a `SKILL.md` file with YAML frontmatter (`name` + `description`) followed by a markdown body:

```markdown
---
name: terraform-standards
description: Use when reviewing Terraform code — checks state safety and risky deletions.
---

# Terraform Review Guidance

- Flag any resource deletion that is not explicitly called out in the PR description.
- Require `prevent_destroy` on stateful resources.
- ...
```

When enabled, PR-Agent discovers every `SKILL.md` under the configured paths, parses it, and injects the skill's `name`, `description`, and body into the `/review`, `/improve`, and `/describe` prompts alongside `extra_instructions`. The model applies the guidance it judges relevant to the PR, using each skill's `description` as the signal for when the skill applies.

The value proposition is **org-wide, host-level skill libraries**: install one curated set of skills on your PR-Agent deployment and reuse it across many repositories, without checking guidance into each repo.

## Configuration

Skills are **disabled by default** and configured in `configuration.toml` (or any host-level config source):

```toml
[skills]
enabled = false
paths = []                # directories scanned recursively for "*/SKILL.md"; supports ~ and $VAR
max_skills_tokens = 8000  # token budget for the combined skills block
```

- `enabled` — turn the feature on.
- `paths` — a list of directories (scanned recursively for `*/SKILL.md`) or direct paths to a `SKILL.md` file. `~` and `$VAR`/`${VAR}` are expanded.
- `max_skills_tokens` — caps the combined size of the injected skills block. Skills past the cap are dropped from the end with a warning; if the first skill alone exceeds the budget it is clipped and marked `[truncated]`.

!!! warning "`skills.paths` is host-level only"
    `skills.paths` **cannot be set from a repository's `.pr_agent.toml`** and is configurable only where the deployment is administered. Because it reads files from the PR-Agent host's filesystem, allowing a repository to set it would let a malicious repo point PR-Agent at sensitive host files (e.g. `~/.ssh/*`) and exfiltrate their contents into the model prompt. A repo-supplied `skills.paths` is ignored with a warning.

    A repository *may* set the safe per-repo preferences `skills.enabled` and `skills.max_skills_tokens` in its own `.pr_agent.toml` — e.g. to opt in to (or size) the host's admin-curated skill library for that repo. It can never redirect the filesystem scan.

## Bundled resources

The agent-skills standard supports bundled files alongside `SKILL.md`. PR-Agent inlines the **text** ones:

- All `*.md` files in the skill directory tree (including a `references/` subdirectory) are appended after the `SKILL.md` body. Individual resource files larger than 256&nbsp;KB are skipped with a warning.
- `scripts/` and `assets/` subdirectories are **skipped**: PR-Agent runs single-shot model calls with no tool-use loop, so it cannot execute scripts or load binary assets on demand.
- A nested directory that contains its own `SKILL.md` is treated as a separate skill and not inlined into its parent.

In short, PR-Agent supports **text-only** agent skills.

## Limitations

PR-Agent dispatches single-shot model calls, so the agent-skills standard's *progressive disclosure* model (the model reads `SKILL.md` only after selecting it by `description`, and reads `references/*.md` only on demand) is not implementable on the current architecture — an architecture change to support this is planned for the future. Until then, every enabled skill's text is loaded into every PR's prompt, bounded by `max_skills_tokens`. Skills that depend on script execution or binary assets will not work.
