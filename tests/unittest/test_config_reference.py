import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = ROOT / "pr_agent/settings/configuration.toml"
OUTPUT_PAGE = ROOT / "docs/docs/usage-guide/configuration_reference.md"


def _toml_keys(text: str) -> Counter:
    return Counter(re.findall(r"^([a-zA-Z0-9_]+)\s*=", text, re.M))


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
    assert sum(keys.values()) == 298

    assert {
        "model",
        "enable_auto_approval",
        "reaction_on_start",
        "reaction_on_failure",
    } <= set(keys)
    assert {"force_streaming_custom_llm_provider", "cache_control_injection_points"} <= set(keys)


def test_config_reference_page_lists_every_active_key():
    toml_keys = _toml_keys(CONFIG_TOML.read_text(encoding="utf-8"))
    documented = _documented_keys(OUTPUT_PAGE.read_text(encoding="utf-8"))
    assert documented == toml_keys, (
        "docs/docs/usage-guide/configuration_reference.md is out of date; "
        "run scripts/generate_config_reference.py"
    )
