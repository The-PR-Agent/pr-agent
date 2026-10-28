"""Deterministic blast-radius row for /review.

Rates a change Low, Moderate, High or Critical from three axes: churn from the diff,
structural reach from an optional input file written by a build-graph tool, and
sensitivity. No model call is involved. The risk model and its sources are documented
in docs/docs/tools/review.md.
"""

from __future__ import annotations

import html
import json
import re

from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

BANDS = ("Low", "Moderate", "High", "Critical")
MAX_INPUT_BYTES = 1_000_000
MAX_NAME_CHARS = 200
_LIST_FIELDS = ("dependents", "domains", "sensitive_domains")

# Files whose large diffs are a mechanical consequence of an authored change elsewhere.
# Only the churn axis and the path heuristic skip them.
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

SENSITIVE_TERMS = (
    "auth", "oauth", "authn", "authz", "login", "logout", "session",
    "token", "jwt", "secret", "credential", "password", "passwd",
    "payment", "billing", "invoice", "kms", "iam", "crypto", "privatekey",
    "apikey",
)


def build_blast_radius(files, resolve_path) -> dict | None:
    """Return the row for this review, or None. Never raises."""
    try:
        if not _as_bool_setting(get_settings().pr_reviewer.get("enable_blast_radius", False)):
            return None
        return assess_change(
            files,
            structure=load_structure(get_settings().pr_reviewer.get("blast_radius_input_file", ""), resolve_path),
            path_heuristic=_as_bool_setting(get_settings().pr_reviewer.get("blast_radius_path_heuristic", False)),
            sensitive_terms=_as_terms(get_settings().pr_reviewer.get("blast_radius_sensitive_terms", [])),
            churn_ignore=compile_churn_ignore(get_settings().pr_reviewer.get("blast_radius_churn_ignore_pattern", "")),
        )
    except Exception as e:
        get_logger().warning(f"Blast radius row skipped: {e!r}")
        return None


def assess_change(files, structure=None, path_heuristic=False, sensitive_terms=(), churn_ignore=None) -> dict | None:
    churn = compute_churn(files, churn_ignore)

    if structure:
        risk = compose_risk(structure, churn)
        return {
            "band": risk["band"],
            "level": risk["level"],
            "heuristic": False,
            "factors": factors(structure, churn),
            "driver": driver(structure, churn),
        }

    if not path_heuristic:
        return None

    signal = path_signal(changed_paths(files, churn_ignore), sensitive_terms)
    if not signal and not (churn and churn["files"]):
        return None

    band = path_band(signal, churn)
    return {
        "band": band,
        "level": BANDS.index(band),
        "heuristic": True,
        "factors": path_factors(signal, churn),
        "driver": path_driver(signal, churn),
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
        heuristic = " <em>(heuristic)</em>" if row.get("heuristic") else ""
        parts = [f"💣&nbsp;<strong>Blast radius</strong>{heuristic}: <strong>{band}</strong>", *lines,
                 f"<em>{rationale}</em>"]
        return f"<tr><td>{'<br>'.join(parts)}</td></tr>\n"
    heuristic = " _(heuristic)_" if row.get("heuristic") else ""
    bullets = "".join(f"- {line}\n" for line in [*lines, rationale])
    return f"### 💣 Blast radius{heuristic}: **{band}**\n\n{bullets}\n"


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


# Path heuristic for repositories without an input file. Capped at Moderate: path names
# are too weak a signal to justify High or Critical.


def sensitive_pattern(extra_terms=()):
    terms = [*SENSITIVE_TERMS, *extra_terms]
    joined = "|".join(re.escape(term) for term in terms)
    return re.compile(rf"(?<![A-Za-z])({joined})s?(?![A-Za-z])", re.IGNORECASE)


def path_signal(paths, extra_terms=()):
    if not paths:
        return None
    pattern = sensitive_pattern(extra_terms)
    dirs = {path.rsplit("/", 1)[0] if "/" in path else "" for path in paths}
    tops = {path.split("/", 1)[0] for path in paths if "/" in path}
    hits = sorted({m.group(1).lower() for path in paths for m in [pattern.search(path)] if m})
    return {"areas": len(dirs), "cross_area": len(tops) > 1, "sensitive_hits": hits}


def path_band(signal, churn):
    signal = signal or {}
    cb = churn_band(churn)
    level = 0
    if cb == "large":
        level = 1
    if signal.get("sensitive_hits"):
        level = 1
    if signal.get("cross_area") and cb in ("moderate", "large"):
        level = 1
    if signal.get("areas", 0) > 10:
        level = 1
    return BANDS[min(level, 1)]


def path_factors(signal, churn):
    signal = signal or {}
    out = []
    areas = signal.get("areas")
    if areas:
        cross = ", cross-area" if signal.get("cross_area") else ""
        out.append(_factor("Reach (est.)", f"~{areas} area{_plural(areas)}{cross}, path-based"))
    if churn:
        out.append(_factor("Churn", _churn_text(churn)))
    if signal.get("sensitive_hits"):
        out.append(_factor("Sensitivity (path match)", ", ".join(signal["sensitive_hits"])))
    return out


def path_driver(signal, churn):
    signal = signal or {}
    cb = churn_band(churn)
    if signal.get("sensitive_hits"):
        reason = "sensitive path match"
    elif cb == "large":
        reason = "large churn"
    elif signal.get("cross_area"):
        reason = "cross-area change"
    elif signal.get("areas", 0) > 10:
        reason = "many areas touched"
    else:
        reason = "small, contained change"
    return f"{reason} (heuristic estimate, no dependency graph)"


# Churn from the diff files the reviewer already holds.


def is_mechanical(path, extra=None):
    return bool(IGNORED_CHURN.search(path) or (extra and extra.search(path)))


def compute_churn(files, churn_ignore=None):
    if files is None:
        return None
    count = added = deleted = 0
    for file in files:
        name = getattr(file, "filename", "") or ""
        if is_mechanical(name, churn_ignore):
            continue
        plus, minus = count_patch_lines(getattr(file, "patch", "") or "")
        count += 1
        added += plus
        deleted += minus
    return {"files": count, "added": added, "deleted": deleted}


def changed_paths(files, churn_ignore=None):
    if files is None:
        return None
    return [
        name for file in files
        if (name := getattr(file, "filename", "") or "") and not is_mechanical(name, churn_ignore)
    ]


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


def compile_churn_ignore(pattern):
    pattern = (pattern or "").strip() if isinstance(pattern, str) else ""
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as e:
        get_logger().warning(f"Blast radius: ignoring invalid blast_radius_churn_ignore_pattern: {e}")
        return None


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


def _as_terms(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list | tuple):
        return ()
    return tuple(term.strip() for term in value if isinstance(term, str) and term.strip())


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
