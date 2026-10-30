"""Deterministic structural-reach row for /review.

Rates a change Low, Moderate, High or Critical from the structural reach and sensitivity in an input
file written by a build-graph tool. The row appears only when that file is present. No model call is
involved. The rating rules are documented in docs/docs/tools/review.md.
"""

from __future__ import annotations

import html
import json
import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

BANDS = ("Low", "Moderate", "High", "Critical")
MAX_INPUT_BYTES = 1_000_000
MAX_NAME_CHARS = 200
_LIST_FIELDS = ("dependents", "domains", "sensitive_domains")
REACH_UNITS = {"projects": "project", "modules": "module", "files": "file"}
DEPENDENTS_SHOWN = 5


def build_structural_reach(resolve_path) -> dict | None:
    """Return the row for this review, or None. Never raises."""
    try:
        structure = load_structure(get_settings().pr_reviewer.get("structural_reach_input_file", ""), resolve_path)
        return assess_change(structure)
    except Exception as e:
        get_logger().warning(f"Structural reach row skipped: {e!r}")
        return None


def assess_change(structure) -> dict | None:
    if not structure:
        return None
    if not is_banded(structure):
        return {"factors": factors(structure)} if structure.get("reach") else None
    level = compose_risk(structure)
    row = {"band": BANDS[level], "level": level, "factors": factors(structure), "driver": driver(structure)}
    return row if row["factors"] else None


def render_structural_reach(row: dict, gfm_supported: bool) -> str:
    if not _is_row(row):
        get_logger().warning("Skipping malformed structural reach row", artifact={"row": row})
        return ""
    lines = [
        f"{_escape(factor['label'], gfm_supported)}: {_escape(factor['value'], gfm_supported)}"
        for factor in row["factors"]
    ]
    band = _escape(row["band"], gfm_supported) if row.get("band") else None
    rationale = f"Rating driven by {_escape(row['driver'], gfm_supported)}" if band else None
    if gfm_supported:
        title = "🔗&nbsp;<strong>Structural reach</strong>" + (f": <strong>{band}</strong>" if band else "")
        parts = [title, *lines, *([f"<em>{rationale}</em>"] if rationale else [])]
        return f"<tr><td>{'<br>'.join(parts)}</td></tr>\n"
    bullets = "".join(f"- {line}\n" for line in [*lines, *([rationale] if rationale else [])])
    title = "### 🔗 Structural reach" + (f": **{band}**" if band else "")
    return f"{title}\n\n{bullets}\n"


def reach_band(reach):
    if reach is None:
        return None
    if reach > 25:
        return "wide"
    if reach > 3:
        return "moderate"
    return "narrow"


def reach_unit(structure):
    return structure.get("reach_unit", "projects")


def is_banded(structure):
    # Reach bands are defined for projects only, so a module or file count is shown without one.
    return (reach_unit(structure) == "projects" or "reach" not in structure
            or bool(structure.get("cross_domain") or structure.get("sensitive_domains")))


def banded_reach(structure):
    return reach_band(structure.get("reach")) if reach_unit(structure) == "projects" else None


def compose_risk(structure) -> int:
    wide = banded_reach(structure) == "wide"
    if structure.get("sensitive_domains"):
        return 3 if wide else 2
    return 1 if wide or structure.get("cross_domain") else 0


def factors(structure):
    out = []
    reach = structure.get("reach")
    if reach is not None:
        cross = ", cross-domain" if structure.get("cross_domain") else ""
        out.append(_factor("Reach", f"{reach} {_unit_text(reach, reach_unit(structure))}{cross}"))
    dependents = structure.get("dependents") or []
    if dependents:
        shown = dependents[:DEPENDENTS_SHOWN]
        more = max(reach or 0, len(dependents)) - len(shown)
        out.append(_factor("Dependents", ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")))
    sensitive = structure.get("sensitive_domains") or []
    if sensitive:
        shown = sensitive[:DEPENDENTS_SHOWN]
        more = len(sensitive) - len(shown)
        out.append(_factor("Sensitivity", ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")))
    return out


def driver(structure):
    if structure.get("sensitive_domains"):
        return "sensitive domain (escalates regardless of size)"
    if banded_reach(structure) == "wide":
        return "wide reach"
    if structure.get("cross_domain"):
        return "cross-domain change"
    return "contained reach"


# Structural input file. See the schema in docs/docs/tools/review.md.


def load_structure(path, resolve_path):
    if not path or not isinstance(path, str):
        return None
    resolved = resolve_path(path)
    if resolved is None:
        get_logger().info(f"Structural reach: no input file at '{path}'")
        return None
    try:
        if resolved.stat().st_size > MAX_INPUT_BYTES:
            get_logger().warning(f"Structural reach: input file '{path}' is larger than {MAX_INPUT_BYTES} bytes")
            return None
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        get_logger().warning(f"Structural reach: could not read input file '{path}': {e!r}")
        return None
    structure = parse_structure(raw)
    if structure is None:
        get_logger().warning(f"Structural reach: input file '{path}' does not match the schema")
    return structure


def parse_structure(raw):
    if not isinstance(raw, dict):
        return None
    version = raw.get("version", 1)
    if isinstance(version, bool) or version != 1:
        return None

    out = {}
    for field in _LIST_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if not isinstance(value, list) or not all(
                isinstance(item, str) and 0 < len(item) <= MAX_NAME_CHARS for item in value):
            return None
        out[field] = value

    if "cross_domain" in raw:
        if not isinstance(raw["cross_domain"], bool):
            return None
        out["cross_domain"] = raw["cross_domain"]

    if "reach_unit" in raw:
        if raw["reach_unit"] not in REACH_UNITS:
            return None
        out["reach_unit"] = raw["reach_unit"]

    if "reach" in raw:
        reach = raw["reach"]
        if isinstance(reach, bool) or not isinstance(reach, int) or reach < 0:
            return None
        out["reach"] = reach
    elif "dependents" in out:
        out["reach"] = len(out["dependents"])

    return out or None


def _is_row(row) -> bool:
    return (
        isinstance(row, dict)
        and (
            (row.get("band") in BANDS and isinstance(row.get("driver"), str))
            or (row.get("band") is None and row.get("driver") is None)
        )
        and isinstance(row.get("factors"), list)
        and all(
            isinstance(factor, dict) and isinstance(factor.get("label"), str) and isinstance(factor.get("value"), str)
            for factor in row["factors"]
        )
    )


def _factor(label, value):
    return {"label": label, "value": value}


def _unit_text(count, unit):
    return REACH_UNITS[unit] if count == 1 else unit


def _escape(text, gfm_supported):
    text = " ".join(str(text).splitlines())
    if not gfm_supported:
        text = re.sub(r"([\\`*_\[\]()!#|])", r"\\\1", text)
    return html.escape(text)
