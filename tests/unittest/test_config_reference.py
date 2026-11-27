import importlib.util
import re
import tomllib
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = ROOT / "pr_agent/settings/configuration.toml"
OUTPUT_PAGE = ROOT / "docs/docs/usage-guide/configuration_reference.md"
GENERATOR = ROOT / "scripts" / "generate_config_reference.py"
MAX_DESCRIPTION_LENGTH = 160
LONG_DESCRIPTION_ALLOWLIST = {
    ("config", "global_settings_repo"),
    ("config", "enable_per_directory_settings"),
    ("config", "per_directory_settings_max_tree_pages"),
    ("config", "extra_config_url"),
    ("config", "num_retries"),
    ("config", "repo_context_max_glob_matches"),
    ("config", "repo_context_sibling_repos"),
    ("config", "repo_context_max_sibling_files"),
    ("config", "repo_context_sibling_namespaces"),
    ("config", "model_token_count_estimate_factor"),
    ("config", "persistent_inline_comments"),
    ("config", "bot_user_indicators"),
    ("config", "additional_reasoning_effort_models"),
    ("config", "no_temperature_models"),
    ("config", "enable_claude_adaptive_thinking"),
    ("config", "claude_extended_thinking_models_override"),
    ("config", "extract_issue_from_branch"),
    ("config", "branch_issue_regex"),
    ("config", "description_issue_regex"),
    ("pr_reviewer", "require_merge_recommendation"),
    ("pr_reviewer", "max_previous_findings_chars"),
    ("pr_reviewer", "inline_key_issues"),
    ("pr_reviewer", "enable_large_pr_chunking"),
    ("github", "agent_login"),
    ("github", "ratelimit_retries"),
    ("github", "polling_repositories"),
    ("github_app", "webhook_delivery_deduplication"),
    ("gitlab", "publish_code_suggestions_as_review"),
    ("gitlab", "auto_resolve_fixed_inline_threads"),
    ("gitlab", "ignore_user_ids"),
    ("gerrit", "command_timeout_seconds"),
    ("jira", "jira_requirements_field"),
    ("jira", "project_keys"),
    ("litellm", "custom_llm_provider"),
    ("litellm", "force_streaming_custom_llm_provider"),
    ("litellm", "base_models"),
    ("litellm", "cache_control_injection_points"),
    ("openrouter", "reasoning_effort"),
    ("openrouter", "reasoning_max_tokens"),
    ("otel", "otlp_timeout"),
    ("otel", "prometheus_multiproc_dir"),
    ("artifacts", "truncate_from"),
    ("azure_devops_server", "agent_identity"),
}


def _toml_keys(text: str) -> Counter:
    return Counter(re.findall(r"^([a-zA-Z0-9_]+)\s*=", text, re.MULTILINE))


def _documented_keys(text: str) -> Counter:
    return Counter(
        row.group(1)
        for line in text.splitlines()
        if line.startswith("| ")
        for row in [re.match(r"\| `([^`]+)` \|", line)]
        if row
    )


def test_config_reference_covers_every_active_key():
    keys = _toml_keys(CONFIG_TOML.read_text(encoding="utf-8"))
    assert sum(keys.values()) == 303

    assert {
        "model",
        "http_request_timeout",
        "use_group_settings_file",
        "enable_auto_approval",
        "reaction_on_start",
        "reaction_on_failure",
        "telegram_bot_token",
        "telegram_chat_id",
        "teams_webhook_url",
    } <= set(keys)
    assert {"force_streaming_custom_llm_provider", "cache_control_injection_points"} <= set(keys)


def test_config_reference_page_lists_every_active_key():
    toml_keys = _toml_keys(CONFIG_TOML.read_text(encoding="utf-8"))
    documented = _documented_keys(OUTPUT_PAGE.read_text(encoding="utf-8"))
    assert documented == toml_keys, (
        "docs/docs/usage-guide/configuration_reference.md is out of date; "
        "run scripts/generate_config_reference.py"
    )


def _generator():
    spec = importlib.util.spec_from_file_location("generate_config_reference", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_reference_page_matches_generator_output():
    """The page is generated, so the committed copy has to equal what the generator produces.

    Comparing only the key names left description text and hand-added prose free to drift:
    #4025 documented the Azure DevOps dismissal behavior by editing the page directly, and
    the next regeneration would have deleted it. Comparing the rendered page in-process
    catches both a stale page and a note the generator has stopped emitting.
    """
    module = _generator()
    rendered = module.render_page(module.load_sections(CONFIG_TOML.read_text(encoding="utf-8")))
    assert rendered == OUTPUT_PAGE.read_text(encoding="utf-8"), (
        "docs/docs/usage-guide/configuration_reference.md does not match the generator; "
        "run scripts/generate_config_reference.py"
    )


def test_config_reference_group_labels_have_table_boundaries():
    """Group labels must be separate blocks, not cells in the preceding table."""
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    lines = module.render_page(sections).splitlines()
    expected = Counter(f"**{key['label']}**" for section in sections for key in section["keys"] if key["label"])
    actual = Counter()

    for index, line in enumerate(lines):
        if line not in expected:
            continue
        actual[line] += 1
        assert index > 0 and lines[index - 1] == "", f"{line} is inside the previous table"
        assert index < 2 or lines[index - 2] != "", f"{line} has redundant blank lines"
        assert lines[index + 1 : index + 4] == ["", *module.TABLE_HEADER.splitlines()], (
            f"{line} must precede its own table"
        )
    assert actual == expected


def test_config_reference_keeps_the_azure_dismissal_note():
    """The note belongs to the generator, so regenerating must not drop the paragraph."""
    module = _generator()
    note = module.NOTES_AFTER_KEY["pr_reviewer"]["final_update_message"]
    assert "Azure DevOps also supplies" in note
    assert "azure_devops.default_comment_status" in note
    assert note in OUTPUT_PAGE.read_text(encoding="utf-8")


def test_generator_rejects_a_note_whose_key_is_gone():
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    for section in sections:
        if section["name"] == "pr_reviewer":
            section["keys"] = [k for k in section["keys"] if k["key"] != "final_update_message"]
    try:
        module.render_page(sections)
    except ValueError as exc:
        assert "final_update_message" in str(exc)
    else:
        raise AssertionError("render_page accepted a NOTES_AFTER_KEY entry with no matching key")


def _docs_anchors(path: Path) -> set:
    """Anchors a docs page actually offers: explicit {#...} markers plus
    GitHub-style slugs of its headings, skipping fenced code blocks."""
    anchors = set()
    counts = {}
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for marker in re.finditer(r"\{#([A-Za-z0-9_-]+)\}", line):
            anchors.add(marker.group(1))
        heading = re.match(r"#{1,6}\s+(.*)", stripped)
        if heading:
            text = re.sub(r"\s*\{#([A-Za-z0-9_-]+)\}\s*$", "", heading.group(1)).strip()
            text = re.sub(r"<[^>]+>", "", text)
            slug = re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", text.lower()).strip())
            if not slug:
                continue
            count = counts.get(slug, 0)
            counts[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_section_docs_links_point_to_existing_files():
    """Every SECTION_DOCS entry must name a real section and resolve to a
    real docs file with a real anchor, so the generated "See ..." line never
    links nowhere. The docs build is a second net, not the only one."""
    module = _generator()
    assert module.SECTION_DOCS, "SECTION_DOCS is empty"
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    names = {section["name"] for section in sections}
    assert set(module.SECTION_DOCS) <= names, (
        f"SECTION_DOCS names unknown sections: {sorted(set(module.SECTION_DOCS) - names)}"
    )
    docs_root = (ROOT / "docs" / "docs").resolve()
    reference_dir = docs_root / "usage-guide"
    for section, (label, link) in module.SECTION_DOCS.items():
        assert label, f"SECTION_DOCS[{section!r}] has an empty link label"
        target, _, anchor = link.partition("#")
        path = (reference_dir / target).resolve()
        assert docs_root in path.parents, f"SECTION_DOCS[{section!r}] escapes docs/: {link}"
        assert path.is_file(), f"SECTION_DOCS[{section!r}] points to a missing file: {link}"
        if anchor:
            assert anchor in _docs_anchors(path), (
                f"SECTION_DOCS[{section!r}] points to a missing anchor: {link}"
            )


def test_section_docs_covers_every_rendered_section():
    """The map is the whole point of the reference links, so a section added
    without a map entry must fail loudly instead of silently losing its link."""
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    names = {section["name"] for section in sections}
    assert set(module.SECTION_DOCS) == names, (
        f"sections without a docs link: {sorted(names - set(module.SECTION_DOCS))}; "
        f"map entries with no section: {sorted(set(module.SECTION_DOCS) - names)}"
    )


def test_render_page_rejects_a_section_docs_entry_with_no_section():
    """The render guard must catch a stale map entry instead of silently
    dropping its link from the regenerated page."""
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    module.SECTION_DOCS = {**module.SECTION_DOCS, "no_such_section": ("Nowhere", "../nowhere.md")}
    try:
        module.render_page(sections)
    except ValueError as exc:
        assert "no_such_section" in str(exc)
    else:
        raise AssertionError("render_page accepted a SECTION_DOCS entry with no rendered section")


@pytest.mark.parametrize(
    "section_name,key,fragments",
    [
        ("config", "repo_context_sibling_repos", (
            "On GitHub and GitLab, this also approves repositories for cross-repository issue and ticket context.",
        )),
        ("pr_reviewer", "require_merge_recommendation", (
            "no_concerns_found (no important blockers or risks identified)",
            "needs_review (seems acceptable but deserves focused reviewer attention)",
            "changes_required (clear issues to fix before merge)",
        )),
        ("openrouter", "reasoning_effort", (
            "For models not covered by PR-Agent's clamps, do not assume",
        )),
    ],
)
def test_generator_preserves_reference_description_details(section_name, key, fragments):
    """Keep details in the generator's TOML source only when no docs page holds them."""
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    rendered = module.render_page(sections)
    section = rendered.split(f"## `[{section_name}]`", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith(f"| `{key}` |"))
    for fragment in fragments:
        assert fragment in row


@pytest.mark.parametrize(
    "section_name,key,page",
    [
        ("config", "repo_context_files", "additional_configurations.md"),
        ("config", "claude_adaptive_thinking_models_override", "changing_a_model.md"),
        ("model_routing", "enable", "changing_a_model.md"),
        ("model_routing", "rules", "changing_a_model.md"),
        ("skills", "enabled", "agent_skills.md"),
    ],
)
def test_shortened_descriptions_point_to_docs_page(section_name, key, page):
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    rendered = module.render_page(sections)
    section = rendered.split(f"## `[{section_name}]`", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith(f"| `{key}` |"))
    assert page in row


def test_config_reference_descriptions_stay_short():
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    too_long = [
        f"[{section['name']}] {key['key']} ({len(key['desc'])} chars)"
        for section in sections
        for key in section["keys"]
        if key["desc"]
        and len(key["desc"]) > MAX_DESCRIPTION_LENGTH
        and (section["name"], key["key"]) not in LONG_DESCRIPTION_ALLOWLIST
    ]
    assert not too_long, (
        f"Descriptions over {MAX_DESCRIPTION_LENGTH} characters need a docs pointer or an allowlist entry: "
        + ", ".join(too_long)
    )


def test_persistent_inline_comments_description_keeps_skip_phrase():
    """Regression for #3454: a mid-sentence short comment line must not become a heading.

    The generated description for persistent_inline_comments has to retain the
    skip/re-post wording; a dropped fragment inverted the feature's meaning.
    """
    page = OUTPUT_PAGE.read_text(encoding="utf-8")
    row = next(
        (line for line in page.splitlines() if line.startswith("| `persistent_inline_comments`")),
        None,
    )
    assert row is not None, "persistent_inline_comments missing from configuration_reference.md"
    lowered = row.lower()
    assert "and skip" in lowered or "then skip" in lowered, (
        f"persistent_inline_comments description lost the skip phrase: {row}"
    )
    assert "embed a provider-compatible marker" in lowered
    assert "**embed a provider-compatible marker, and skip**" not in page


_CONFIG_FENCE = re.compile(r"^( *)(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^( *)(?:[-+*]|[0-9]{1,9}[.)]) {1,4}(?=\S)")
_CONFIG_LANGUAGES = {"toml", "yaml", "yml"}
_YAML_SCALAR_TAGS = {
    f"tag:yaml.org,2002:{name}" for name in ("str", "bool", "int", "float", "null", "timestamp", "binary")
}


def _config_examples(markdown: str):
    """Extract configuration fences in the selected guides, including list continuations."""
    fence = None
    language = ""
    start = indent = container_indent = 0
    list_indents = [0]
    body = []
    for number, line in enumerate(markdown.splitlines(), 1):
        padding = len(line) - len(line.lstrip(" "))
        if fence is not None:
            match = _CONFIG_FENCE.match(line)
            if (
                match and container_indent <= padding <= container_indent + 3
                and match[2][0] == fence[0] and len(match[2]) >= len(fence) and not match[3].strip()
            ):
                if language in _CONFIG_LANGUAGES:
                    yield language, start, "\n".join(body)
                fence = None
                continue
            if padding >= container_indent or not line.strip():
                if language in _CONFIG_LANGUAGES:
                    body.append(line[min(indent, padding):])
                continue
            # A dedent also ends a fenced block inside a list item.
            if language in _CONFIG_LANGUAGES:
                yield language, start, "\n".join(body)
            fence = None
        if not line.strip():
            continue
        while padding < list_indents[-1]:
            list_indents.pop()
        container_indent = list_indents[-1]
        item = _LIST_ITEM.match(line)
        if item and padding <= container_indent + 3:
            container_indent = item.end()
            list_indents.append(container_indent)
            padding = container_indent
            line = " " * padding + line[padding:]
        match = _CONFIG_FENCE.match(line)
        if not match or padding > container_indent + 3:
            continue
        _, marker, info = match.groups()
        if marker[0] == "`" and "`" in info:
            continue
        fence = marker
        start, indent = number, padding
        parts = info.split(maxsplit=1)
        language = parts[0].lower() if parts else ""
        body = []
    if fence is not None and language in _CONFIG_LANGUAGES:
        yield language, start, "\n".join(body)


def _check_yaml_examples(body: str) -> None:
    # Inspect composed nodes, constructing only built-in scalar keys. Custom tags
    # remain opaque, and aliases may refer back to an enclosing mapping.
    loader = yaml.SafeLoader(body)
    try:
        while loader.check_node():
            pending = [loader.get_node()]
            visited = set()
            while pending:
                node = pending.pop()
                if node in visited:
                    continue
                visited.add(node)
                if isinstance(node, yaml.MappingNode):
                    keys = set()
                    for key, value in node.value:
                        if isinstance(key, yaml.ScalarNode):
                            try:
                                resolved = loader.construct_object(key) if key.tag in _YAML_SCALAR_TAGS else key.value
                            except (AttributeError, IndexError, KeyError, OverflowError, TypeError, ValueError) as exc:
                                raise yaml.YAMLError(f"invalid scalar key {key.value!r}: {exc}") from exc
                            identity = (key.tag, resolved)
                            if identity in keys:
                                raise yaml.YAMLError(
                                    f"duplicate mapping key {key.value!r} (line {key.start_mark.line + 1})"
                                )
                            keys.add(identity)
                        pending.extend((key, value))
                elif isinstance(node, yaml.SequenceNode):
                    pending.extend(node.value)
    finally:
        loader.dispose()


def _config_example_errors(examples, path: Path) -> list[str]:
    errors = []
    for language, start, body in examples:
        try:
            if language == "toml":
                tomllib.loads(body)
            else:
                _check_yaml_examples(body)
        except (tomllib.TOMLDecodeError, yaml.YAMLError) as exc:
            errors.append(f"{path}:{start}: invalid {language} example: {exc}")
    return errors


@pytest.mark.parametrize("page", ["configuration_options.md", "additional_configurations.md"])
def test_config_examples_are_valid(page):
    # Start with the configuration guides whose labeled snippets are parseable.
    # Use a text fence for deliberately incomplete syntax illustrations.
    path = Path("docs/docs/usage-guide") / page
    examples = list(_config_examples((ROOT / path).read_text(encoding="utf-8")))
    assert examples, f"No configuration examples found in {path}"
    errors = _config_example_errors(examples, path)
    assert not errors, "\n".join(errors)


def test_config_example_fences():
    markdown = (
        ":::note\n"
        "```toml title=\"settings.toml\"\nkey = \"...\"\n```\n"
        ":::\n\n1. Example:\n\n"
        "   ~~~~yaml\n   key:\n     nested: true\n   ~~~~~\n"
        "\n````markdown\n```toml\nkey = ...\n```\n````\n"
        "```text\nkey = ...\n```\n"
        "~~~yml\nkey: !reference [a, b]\n---\nother: true\n"
    )
    examples = list(_config_examples(markdown))
    assert examples == [
        ("toml", 2, 'key = "..."'),
        ("yaml", 9, "key:\n  nested: true"),
        ("yml", 22, "key: !reference [a, b]\n---\nother: true"),
    ]
    assert not _config_example_errors(examples, Path("example.md"))


@pytest.mark.parametrize(("language", "body"), [("toml", "key = ..."), ("yaml", "key: ["), ("yml", "key: [")])
def test_config_example_errors_report_fence_line(language, body):
    examples = _config_examples(f"# Example\n\n```{language}\n{body}\n```\n")
    errors = _config_example_errors(examples, Path("example.md"))
    assert len(errors) == 1
    assert errors[0].startswith(f"example.md:3: invalid {language} example:")


def test_indented_literal_fences_do_not_hide_config_errors():
    markdown = (
        "    ```text\n\n"
        "```yaml\nkey: |\n    ```\ninvalid: [\n```\n"
        "```toml\nkey = ...\n```\n"
    )
    examples = list(_config_examples(markdown))
    assert examples == [
        ("yaml", 3, "key: |\n    ```\ninvalid: ["),
        ("toml", 8, "key = ..."),
    ]
    assert len(_config_example_errors(examples, Path("example.md"))) == 2


def test_config_examples_in_nested_lists():
    markdown = (
        "1. Example:\n\n"
        "    ```toml\n    key = ...\n    ```\n\n"
        "   - Nested:\n\n"
        "     ~~~yaml\n     key: |\n         ~~~\n     invalid: [\n     ~~~\n\n"
        "Outside the list\n\n    ```text\n\n"
        "```toml\nkey = 1\n```\n"
    )
    examples = list(_config_examples(markdown))
    assert [(lang, body) for lang, _, body in examples] == [
        ("toml", "key = ..."),
        ("yaml", "key: |\n    ~~~\ninvalid: ["),
        ("toml", "key = 1"),
    ]
    assert len(_config_example_errors(examples, Path("example.md"))) == 2


@pytest.mark.parametrize("body", [
    "key: 1\n'key': 2",
    "items:\n  - key: 1\n    key: 2",
    "true: 1\nTrue: 2",
    "0x10: 1\n16: 2",
])
def test_config_examples_reject_duplicate_yaml_keys(body):
    errors = _config_example_errors([("yaml", 1, body)], Path("example.md"))
    assert len(errors) == 1
    assert "duplicate mapping key" in errors[0]


def test_config_examples_accept_yaml_aliases_and_separate_mappings():
    body = "first: &shared {key: 1, alias: *shared}\nsecond: *shared\nthird: {key: !reference [a, b]}\n---\nfirst: 2"
    assert not _config_example_errors([("yaml", 1, body)], Path("example.md"))


@pytest.mark.parametrize("body", ['!!timestamp foo: 1', '!!int "": 1'])
def test_config_examples_report_invalid_explicit_yaml_scalar_keys(body):
    errors = _config_example_errors([("yaml", 3, body), ("toml", 8, "key = ...")], Path("example.md"))
    assert len(errors) == 2
    assert errors[0].startswith("example.md:3: invalid yaml example: invalid scalar key")
    assert errors[1].startswith("example.md:8: invalid toml example:")
