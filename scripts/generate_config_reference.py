#!/usr/bin/env python3
"""Generate docs/docs/usage-guide/configuration_reference.md from
pr_agent/settings/configuration.toml.

configuration.toml is the authoritative listing of options; this script keeps a
browsable reference page in sync with it, including the inline comments as
descriptions. Line-based parsing (not tomllib) so comments are preserved.

Usage:
    python scripts/generate_config_reference.py           # rewrite the page
    python scripts/generate_config_reference.py --check   # exit 1 if the page is stale
"""
import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_TOML = REPO_ROOT / "pr_agent" / "settings" / "configuration.toml"
OUTPUT_MD = REPO_ROOT / "docs" / "docs" / "usage-guide" / "configuration_reference.md"

SECTION_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s*(?:#\s*(?P<comment>.*))?$")
# key = value  # optional inline comment  (value may contain '#' inside quotes)
KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_.-]+)\s*=\s*(?P<rest>.+)$")

HEADER = """# Configuration Reference

<!-- This page is generated from pr_agent/settings/configuration.toml by
     scripts/generate_config_reference.py. Do not edit it by hand: change the
     TOML (keys and comments) and re-run the script. -->

All PR-Agent options with their default values, generated from
[`configuration.toml`](https://github.com/qodo-ai/pr-agent/blob/main/pr_agent/settings/configuration.toml)
— the single source of truth for configuration. Override any of these in your
repo's `.pr_agent.toml`, via CLI arguments (`--section.key=value`), or with
environment variables. See the
[usage guide](./configuration_options.md) for how overrides are merged.

"""


def split_value_and_comment(rest: str) -> tuple[str, str]:
    """Split a TOML value from its trailing comment, respecting quotes and brackets."""
    in_single = in_double = False
    depth = 0
    for i, ch in enumerate(rest):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch in "[{" and not (in_single or in_double):
            depth += 1
        elif ch in "]}" and not (in_single or in_double):
            depth -= 1
        elif ch == "#" and not (in_single or in_double) and depth == 0:
            return rest[:i].strip(), rest[i + 1:].strip()
    return rest.strip(), ""


def usable_preceding_comments(comments: list[str]) -> list[str]:
    """Filter preceding-comment lines down to ones that read as descriptions.

    Drops commented-out settings (contain '=') and short group labels like
    '# models' or '# token limits' that describe a block, not the next key.
    """
    kept = []
    for comment in comments:
        if "=" in comment:
            continue
        if len(comment.split()) < 4:
            continue
        kept.append(comment)
    return kept


def parse_configuration(text: str) -> list[dict]:
    """Return [{'name': section, 'comment': str, 'keys': [(key, default, description)]}]."""
    sections: list[dict] = []
    current: dict | None = None
    pending_comments: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            pending_comments.clear()
            continue
        if line.startswith("#"):
            pending_comments.append(line.lstrip("#").strip())
            continue

        section_match = SECTION_RE.match(line)
        if section_match:
            current = {
                "name": section_match.group("name"),
                "comment": " ".join(pending_comments),
                "keys": [],
            }
            sections.append(current)
            pending_comments.clear()
            continue

        key_match = KEY_RE.match(line)
        if key_match and current is not None:
            value, inline_comment = split_value_and_comment(key_match.group("rest"))
            description = inline_comment or " ".join(usable_preceding_comments(pending_comments))
            current["keys"].append((key_match.group("key"), value, description))
            pending_comments.clear()
            continue

        # continuation lines of multi-line values (e.g. multi-line lists) are skipped;
        # the first line already carries the key and enough of the default to be useful
        pending_comments.clear()

    return sections


def escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(sections: list[dict]) -> str:
    parts = [HEADER]
    for section in sections:
        if not section["keys"]:
            continue
        parts.append(f"## `[{section['name']}]`\n")
        if section["comment"]:
            parts.append(f"{section['comment']}\n")
        parts.append("| Key | Default | Description |")
        parts.append("|-----|---------|-------------|")
        for key, value, description in section["keys"]:
            parts.append(
                f"| `{escape_cell(key)}` | `{escape_cell(value)}` | {escape_cell(description)} |"
            )
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="don't write; exit 1 if the generated page is stale")
    args = parser.parse_args()

    sections = parse_configuration(CONFIG_TOML.read_text())
    rendered = render_markdown(sections)

    if args.check:
        existing = OUTPUT_MD.read_text() if OUTPUT_MD.exists() else ""
        if existing != rendered:
            print(f"{OUTPUT_MD.relative_to(REPO_ROOT)} is stale. "
                  f"Run: python scripts/generate_config_reference.py", file=sys.stderr)
            return 1
        return 0

    OUTPUT_MD.write_text(rendered)
    print(f"Wrote {OUTPUT_MD.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
