import importlib.util
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = ROOT / "pr_agent/settings/configuration.toml"
OUTPUT_PAGE = ROOT / "docs/docs/usage-guide/configuration_reference.md"
GENERATOR = ROOT / "scripts" / "generate_config_reference.py"


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
    assert sum(keys.values()) == 304

    assert {
        "model",
        "http_request_timeout",
        "use_group_settings_file",
        "enable_auto_approval",
        "reaction_on_start",
        "reaction_on_failure",
        "telegram_bot_token",
        "telegram_chat_id",
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
        ("model_routing", "rules", (
            "max_patch_chars",
            "inclusive ceiling",
            "count unknown",
            "not tokens or full file/diff size",
            "append the displaced primary unless already present",
            'model = "gpt-6-luna"',
            'model = "gpt-6.1-sol"',
        )),
    ],
)
def test_generator_preserves_reference_description_details(section_name, key, fragments):
    """Keep page-only description details in the generator's TOML source."""
    module = _generator()
    sections = module.load_sections(CONFIG_TOML.read_text(encoding="utf-8"))
    rendered = module.render_page(sections)
    section = rendered.split(f"## `[{section_name}]`", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith(f"| `{key}` |"))
    for fragment in fragments:
        assert fragment in row


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
