"""Deterministic blast-radius row for /review.

Rates a change Low, Moderate, High or Critical from structural reach and sensitivity in an
input file written by a build-graph tool, and churn from the diff. The row appears only
when that file is present. No model call is involved. The risk model and its sources are documented
in docs/docs/tools/review.md.
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

# Files whose large diffs are a mechanical consequence of an authored change elsewhere.
# The churn axis skips them.
IGNORED_CHURN = re.compile(
    r"\.(lock|lockb|lockfile)$"
    r"|(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml"
    r"|packages\.lock\.json|Package\.resolved|go\.sum)$"
    r"|(^|/)\.terraform\.lock\.hcl$"
    r"|\.snap$|\.ambr$|(^|/)__(image_)?snapshots__/|(^|/)(vcr_)?cassettes/"
    r"|\.(zod|generated|gen)\.[tj]sx?$|(^|/)(gen|generated)/"
    r"|_pb2(_grpc)?\.pyi?$|\.pb\.go$|_pb\.[tj]s$"
    r"|\.mock\.[tj]sx?$|\.min\.(js|css)$|\.[cm]?js\.map$|\.css\.map$|(^|/)dist/"
    r"|(^|/)(vendor|node_modules)/"
)

def build_blast_radius(files, resolve_path) -> dict | None:
    """Return the row for this review, or None. Never raises."""
    try:
        structure = load_structure(get_settings().pr_reviewer.get("blast_radius_input_file", ""), resolve_path)
        return assess_change(files, structure)
    except Exception as e:
        get_logger().warning(f"Blast radius row skipped: {e!r}")
        return None


def assess_change(files, structure) -> dict | None:
    if not structure:
        return None
    churn = compute_churn(files)
    risk = compose_risk(structure, churn)
    return {
        "band": risk["band"],
        "level": risk["level"],
        "factors": factors(structure, churn),
        "driver": driver(structure, churn),
    }


def render_blast_radius(row: dict, gfm_supported: bool) -> str:
    if not _is_row(row):
        get_logger().warning("Skipping malformed blast radius row", artifact={"row": row})
        return ""
    band = _escape(row["band"], gfm_supported)
    lines = [
        f"{_escape(factor['label'], gfm_supported)}: {_escape(factor['value'], gfm_supported)}"
        for factor in row["factors"]
    ]
    rationale = f"Rating driven by {_escape(row['driver'], gfm_supported)}"
    if gfm_supported:
        parts = [f"💣&nbsp;<strong>Blast radius</strong>: <strong>{band}</strong>", *lines, f"<em>{rationale}</em>"]
        return f"<tr><td>{'<br>'.join(parts)}</td></tr>\n"
    bullets = "".join(f"- {line}\n" for line in [*lines, rationale])
    return f"### 💣 Blast radius: **{band}**\n\n{bullets}\n"


# Risk model: impact (reach, sensitivity) x likelihood (churn).


def reach_band(reach):
    if reach is None:
        return None
    if reach > 25:
        return "wide"
    if reach > 3:
        return "moderate"
    return "narrow"


def churn_band(churn):
    if not churn:
        return None
    lines = churn["added"] + churn["deleted"]
    if lines > 400 or churn["files"] > 30:
        return "large"
    if lines >= 50 or churn["files"] > 5:
        return "moderate"
    return "small"


def compose_risk(structure, churn):
    structure = structure or {}
    reach = structure.get("reach")
    cross = bool(structure.get("cross_domain"))
    sensitive = structure.get("sensitive_domains") or []
    rb = reach_band(reach)
    cb = churn_band(churn)

    reasons = []
    if rb is not None:
        reasons.append(f"{rb} reach ({reach} project{_plural(reach)})")
    if cb is not None:
        reasons.append(f"{cb} churn ({_churn_text(churn)})")

    level = 0
    if rb == "wide" or cb == "large":
        level = 1
    if rb == "wide" and cb == "large":
        level = 2
        reasons.append("wide reach combined with large churn")
    if cross:
        level = max(level, 1)
        reasons.append("cross-domain change")
    if sensitive:
        level = max(level, 2)
        if rb == "wide" or cb == "large":
            level = 3
        reasons.append(f"touches sensitive domain(s): {', '.join(sensitive)}")

    return {"band": BANDS[level], "level": level, "reasons": reasons}


def factors(structure, churn):
    structure = structure or {}
    out = []
    reach = structure.get("reach")
    if reach is not None:
        cross = ", cross-domain" if structure.get("cross_domain") else ""
        out.append(_factor("Reach", f"{reach} project{_plural(reach)}{cross}"))
    if churn:
        out.append(_factor("Churn", _churn_text(churn)))
    sensitive = structure.get("sensitive_domains") or []
    if sensitive:
        out.append(_factor("Sensitivity", ", ".join(sensitive)))
    return out


def driver(structure, churn):
    structure = structure or {}
    if structure.get("sensitive_domains"):
        return "sensitive domain (escalates regardless of size)"
    rb = reach_band(structure.get("reach"))
    cb = churn_band(churn)
    if rb == "wide" and cb == "large":
        return "wide reach and large churn"
    if rb == "wide":
        return "wide reach"
    if cb == "large":
        return "large churn"
    if structure.get("cross_domain"):
        return "cross-domain change"
    return "contained reach and churn"


# Churn from the diff files the reviewer already holds.


def is_mechanical(path):
    return bool(IGNORED_CHURN.search(path))


def compute_churn(files):
    if files is None:
        return None
    count = added = deleted = 0
    for file in files:
        name = getattr(file, "filename", "") or ""
        if is_mechanical(name):
            continue
        plus, minus = count_patch_lines(getattr(file, "patch", "") or "")
        count += 1
        added += plus
        deleted += minus
    return {"files": count, "added": added, "deleted": deleted}


def count_patch_lines(patch: str):
    added = deleted = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk and line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


# Structural input file. See the schema in docs/docs/tools/review.md.


def load_structure(path, resolve_path):
    if not path or not isinstance(path, str):
        return None
    resolved = resolve_path(path)
    if resolved is None:
        get_logger().info(f"Blast radius: no input file at '{path}'")
        return None
    try:
        if resolved.stat().st_size > MAX_INPUT_BYTES:
            get_logger().warning(f"Blast radius: input file '{path}' is larger than {MAX_INPUT_BYTES} bytes")
            return None
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        get_logger().warning(f"Blast radius: could not read input file '{path}': {e!r}")
        return None
    structure = parse_structure(raw)
    if structure is None:
        get_logger().warning(f"Blast radius: input file '{path}' does not match the schema")
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
        and row.get("band") in BANDS
        and isinstance(row.get("driver"), str)
        and isinstance(row.get("factors"), list)
        and all(
            isinstance(factor, dict) and isinstance(factor.get("label"), str) and isinstance(factor.get("value"), str)
            for factor in row["factors"]
        )
    )


def _factor(label, value):
    return {"label": label, "value": value}


def _plural(count):
    return "" if count == 1 else "s"


def _churn_text(churn):
    return f"{churn['files']} file{_plural(churn['files'])}, +{churn['added']}/-{churn['deleted']}"


def _escape(text, gfm_supported):
    text = " ".join(str(text).splitlines())
    if not gfm_supported:
        text = re.sub(r"([\\`*_\[\]()!#|])", r"\\\1", text)
    return html.escape(text)
