"""Generate docs/docs/usage-guide/configuration_reference.md from configuration.toml.

The generated page lists every active option in pr_agent/settings/configuration.toml,
grouped by section, rendering each default value and its inline comment so the docs
stay in sync with the authoritative source. Run from the repo root:

    python scripts/generate_config_reference.py [output_path]

The output path defaults to docs/docs/usage-guide/configuration_reference.md.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_TOML = ROOT / "pr_agent/settings/configuration.toml"
DEFAULT_OUTPUT = ROOT / "docs/docs/usage-guide/configuration_reference.md"

SOURCE_URL = "https://github.com/the-pr-agent/pr-agent/blob/main/pr_agent/settings/configuration.toml"

PAGE_HEADER = f"""---
title: "Configuration Reference"
sidebar_position: 4
---

> This page is **auto-generated** and should not be edited by hand.
> Regenerate it from the [TOML source]({SOURCE_URL}) with:
>
> ```bash
> python scripts/generate_config_reference.py
> ```

Every configuration option PR-Agent supports, grouped by section. The [configuration.toml]({SOURCE_URL})
file is the single source of truth for defaults and inline comments; this page renders the same
list for easy searching and linking.

Rows with an empty **Description** are keys whose TOML entry carries no explanatory comment yet.
They are listed deliberately rather than hidden, so the gaps double as the documentation
to-do list.
"""

TABLE_HEADER = "| Key | Default | Description |\n| --- | --- | --- |"

# Comment lines shorter than this without trailing sentence punctuation are treated as
# group labels (e.g. "# models") rather than descriptions of the following key.
LABEL_MAX_LEN = 60

KEY_RE = re.compile(r"^(?P<key>[a-zA-Z0-9_]+)\s*=\s*(?P<rest>.*)$")
SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\](?:\s*#+\s*(?P<tag>.+?)\s*#*)?$")

# Notes that document a key's behavior but read as prose rather than as a table cell, so
# they are emitted between the key's row and whatever follows it. Keyed by section and by
# the key they follow because key names repeat across sections. These live here rather than
# in the page because the page is generated: an edit made only to the page is lost the next
# time someone regenerates it.
_AZURE_DISMISSAL_NOTE = """\
Azure DevOps also supplies verified PR-Agent inline key issues as dismissed when their status is `wontFix`
or `byDesign` and differs from `azure_devops.default_comment_status`. Matching defaults, `fixed`, `closed`,
active, and pending threads do not count as dismissals. PR-Agent resolves its posting identity through Azure DevOps;
`azure_devops_server.agent_identity` can override it. This infers a human decision from the current configuration;
it cannot verify the status-changing actor or historical defaults. Dismissed findings share the existing
`max_previous_findings_chars` budget and may be reported again if the code makes them worse."""

NOTES_AFTER_KEY = {
    "pr_reviewer": {
        "final_update_message": _AZURE_DISMISSAL_NOTE,
    },
}

# Docs pages that explain a configuration section, as (link text, link) pairs.
# Links are relative to docs/docs/usage-guide/, where the generated page lives,
# and carry the anchor of the explaining section. Every rendered section is
# mapped; the render guard below fails on a map entry with no rendered section,
# and the unit tests fail if a section is added without a map entry or if a
# mapped file or anchor stops existing.
SECTION_DOCS = {
    "config": (
        "Configuration options",
        "../usage-guide/configuration_options.md#global-configuration-file",
    ),
    "pr_reviewer": ("Review", "../commands/review.md#configuration-options"),
    "pr_description": ("Describe", "../commands/describe.md#configuration-options"),
    "pr_questions": ("Ask", "../commands/ask.md#configuration-options"),
    "pr_code_suggestions": (
        "Fixes and committable suggestions",
        "../commands/review.md#fixes-and-committable-suggestions",
    ),
    "pr_add_docs": ("Add Docs", "../commands/add_docs.md#configuration-options"),
    "pr_update_changelog": ("Update Changelog", "../commands/update_changelog.md#configuration-options"),
    "pr_config": (
        "Show possible configurations",
        "../usage-guide/additional_configurations.md#show-possible-configurations",
    ),
    "pr_help_docs": ("Help Docs", "../commands/help_docs.md#configuration-options"),
    "github": ("GitHub", "../installation/github.md#run-as-a-github-app"),
    "github_action_config": ("GitHub Actions", "../usage-guide/automations_and_usage.md#github-action"),
    "github_app": ("GitHub App", "../usage-guide/automations_and_usage.md#github-app"),
    "gitlab": ("GitLab", "../usage-guide/automations_and_usage.md#gitlab-webhook"),
    "gitea": ("Gitea", "../usage-guide/automations_and_usage.md#gitea-webhook"),
    "bitbucket": ("Bitbucket", "../usage-guide/automations_and_usage.md#bitbucket-app"),
    "bitbucket_app": ("Bitbucket App", "../usage-guide/automations_and_usage.md#bitbucket-app"),
    "local": ("Local git provider", "../usage-guide/local_git_provider.md#when-to-use-it"),
    "gerrit": ("Supported platforms", "../overview/supported_platforms.md#gerrit-and-codecommit"),
    "bitbucket_server": (
        "Bitbucket Server",
        "../usage-guide/automations_and_usage.md#bitbucket-server-webhook",
    ),
    "jira": ("Ticket context", "../core-abilities/fetching_ticket_context.md#jira-integration"),
    "litellm": ("Changing a model", "../usage-guide/changing_a_model.md#changing-a-model-in-pr-agent"),
    "bedrock": ("Amazon Bedrock", "../usage-guide/changing_a_model.md#amazon-bedrock"),
    "openrouter": ("OpenRouter", "../usage-guide/changing_a_model.md#openrouter"),
    "model_routing": (
        "Model routing",
        "../usage-guide/changing_a_model.md#routing-small-pull-requests-to-a-cheaper-model",
    ),
    "otel": (
        "Additional configurations",
        "../usage-guide/additional_configurations.md#built-in-opentelemetry-command-telemetry",
    ),
    "pr_similar_issue": ("Similar Issues", "../commands/similar_issues.md#selecting-a-vector-database"),
    "pinecone": ("Pinecone", "../commands/similar_issues.md#pinecone-configuration"),
    "lancedb": ("LanceDB", "../commands/similar_issues.md#lancedb-configuration"),
    "qdrant": ("Qdrant", "../commands/similar_issues.md#qdrant-configuration"),
    "skills": ("Agent skills", "../core-abilities/agent_skills.md#configuration"),
    "artifacts": ("CI artifacts", "../installation/github.md#ci-artifact-context"),
    "mosaico": ("Mosaico server", "../installation/mosaico_server.md#what-the-mode-is"),
    "asana": ("Asana", "../core-abilities/fetching_ticket_context.md#asana-integration"),
    "linear": ("Linear", "../core-abilities/fetching_ticket_context.md#linear-integration"),
    "azure_devops": ("Azure DevOps", "../usage-guide/automations_and_usage.md#azure-devops-provider"),
    "azure_devops_server": (
        "Azure DevOps Server",
        "../usage-guide/automations_and_usage.md#azure-devops-webhook",
    ),
    "push_outputs": ("Push outputs", "../usage-guide/push_outputs.md#configuration"),
}


def _is_assignment(text: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_\"']+\s*=", text))


def _classify_comment(text: str) -> str:
    stripped = text.strip().lstrip("#").strip()
    if not stripped:
        return "empty"
    if _is_assignment(stripped):
        return "example"
    if len(stripped) <= LABEL_MAX_LEN and not re.search(r"[.:!?]", stripped):
        return "label"
    return "desc"


def _probe_value(buf: str) -> tuple:
    """Return (parsed_value, value_text, inline_comment) or (None, None, None)."""
    idx = buf.find("#")
    if idx != -1 and buf[:idx].rstrip():
        candidate = buf[:idx].rstrip()
        try:
            return tomllib.loads(f"k = {candidate}")["k"], candidate, buf[idx + 1 :].strip()
        except tomllib.TOMLDecodeError:
            pass
    try:
        return tomllib.loads(f"k = {buf}")["k"], buf, None
    except tomllib.TOMLDecodeError:
        return None, None, None


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if "\n" in value:
            lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
            first = lines[0] if lines else ""
            text = first + (" ..." if len(lines) > 1 else "")
            return '"""' + text + '"""'
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{_render_value(key)} = {_render_value(item)}" for key, item in value.items())
        return "{" + items + "}"
    return str(value)


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def load_sections(text: str) -> list:
    sections = []
    current = None
    pending_comments = []

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        section_match = SECTION_RE.match(line)
        if section_match:
            current = {"name": section_match.group("name"), "tag": (section_match.group("tag") or "").strip()}
            current["keys"] = []
            sections.append(current)
            pending_comments = []
            i += 1
            continue

        if not line:
            i += 1
            continue

        if line.startswith("#"):
            if current is not None:
                kind = _classify_comment(line)
                if kind in ("example", "empty"):
                    pending_comments = []
                else:
                    pending_comments.append(line.lstrip("#").strip())
            i += 1
            continue

        key_match = KEY_RE.match(line)
        if key_match is None or current is None:
            i += 1
            continue

        buf = key_match.group("rest")
        value, value_text, inline_comment = _probe_value(buf)
        i += 1
        while value is None and i < len(lines):
            buf += "\n" + lines[i]
            value, value_text, inline_comment = _probe_value(buf)
            i += 1
        if value is None:
            value_text, inline_comment = buf, None

        desc = inline_comment
        if desc is None:
            desc_parts = [c for c in pending_comments if _classify_comment(c) == "desc"]
            desc = " ".join(desc_parts) or None
        label_parts = [c for c in pending_comments if _classify_comment(c) == "label"]
        label = label_parts[-1] if label_parts else None
        pending_comments = []

        current["keys"].append(
            {
                "key": key_match.group("key"),
                "value": _render_value(value) if value is not None else value_text,
                "desc": desc,
                "label": label,
            }
        )

    return sections


def render_page(sections: list) -> str:
    out = [PAGE_HEADER.rstrip(), ""]
    rendered_notes = set()
    rendered_docs = set()

    for section in sections:
        keys = section["keys"]
        notes = NOTES_AFTER_KEY.get(section["name"], {})
        heading = f"## `[{section['name']}]`"
        anchor = section["name"]
        if section["tag"]:
            heading += f" — {section['tag']}"
            anchor = f"{anchor}-{section['tag'].lstrip('/')}"
        # Pin the heading id. Docusaurus' slugger turns the em dash into a second
        # separator ("pr_reviewer--review"), but pr_agent/servers/help.py and the
        # published site both use the single-hyphen form.
        heading += f" {{#{anchor}}}"
        out.append(heading)
        out.append("")
        if section["name"] in SECTION_DOCS:
            label, link = SECTION_DOCS[section["name"]]
            out.append(f"See [{label}]({link}).")
            out.append("")
            rendered_docs.add(section["name"])
        if not keys:
            out.append("_This section only documents commented-out examples; see the [TOML source]"
                       f"({SOURCE_URL}) for details._")
            out.append("")
            continue

        table_open = False
        pending_label = None
        for key in keys:
            if key["label"]:
                if table_open:
                    table_open = False
                pending_label = "**" + key["label"] + "**"
            if not table_open:
                if pending_label:
                    out.append(pending_label)
                    out.append("")
                    pending_label = None
                out.append(TABLE_HEADER)
                table_open = True
            desc = _escape_cell(key["desc"]) if key["desc"] else ""
            value = _escape_cell(key["value"])
            out.append(f"| `{key['key']}` | {value if value.strip() else '`""`'} | {desc} |")
            if key["key"] in notes:
                if table_open:
                    out.append("")
                    table_open = False
                out.append(notes[key["key"]])
                out.append("")
                rendered_notes.add((section["name"], key["key"]))
        if table_open:
            out.append("")
        out.append("")

    missing = [
        f"[{name}] {key}" for name, keys in NOTES_AFTER_KEY.items() for key in keys if (name, key) not in rendered_notes
    ]
    if missing:
        raise ValueError(
            "NOTES_AFTER_KEY refers to keys the generator did not render: "
            + ", ".join(missing)
            + "; update the note or the key it follows"
        )

    missing_docs = sorted(set(SECTION_DOCS) - rendered_docs)
    if missing_docs:
        raise ValueError(
            "SECTION_DOCS refers to sections the generator did not render: "
            + ", ".join(missing_docs)
            + "; update the map or the section it points to"
        )

    return "\n".join(out).rstrip("\n") + "\n"


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    text = CONFIG_TOML.read_text(encoding="utf-8")
    sections = load_sections(text)

    rendered_keys = sum(len(section["keys"]) for section in sections)
    expected_keys = len(re.findall(r"^[a-zA-Z0-9_]+\s*=", text, re.MULTILINE))
    if rendered_keys != expected_keys:
        print(f"ERROR: rendered {rendered_keys} keys, expected {expected_keys}", file=sys.stderr)
        return 1

    output.write_text(render_page(sections), encoding="utf-8")
    print(f"Wrote {rendered_keys} keys across {len(sections)} sections to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
