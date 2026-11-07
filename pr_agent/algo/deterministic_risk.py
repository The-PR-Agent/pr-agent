"""Deterministic risk checks for /review.

Flags sensitive paths and risky change shapes in pull requests.
Runs locally in linear time over diff files and patches. No model call is involved.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

MAX_GLOB_LENGTH = 200
MAX_PATH_LENGTH = 1000
MAX_LINE_CHARS = 500
MAX_CUSTOM_PATTERN_LENGTH = 200
MAX_FINDINGS_PER_CATEGORY = 10

# Default sensitive path globs
DEFAULT_SENSITIVE_PATHS: tuple[str, ...] = (
    # Workflows and CI files
    ".github/workflows/**",
    ".gitlab-ci.yml",
    "Jenkinsfile*",
    ".circleci/**",
    "azure-pipelines*.yml",
    ".travis.yml",
    "bitbucket-pipelines.yml",
    # CODEOWNERS
    "*CODEOWNERS",
    # Repository settings
    ".pr_agent.toml",
    # Dependency manifests and lock files
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements*.txt",
    "Pipfile",
    "Pipfile.lock",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle*",
    "Gemfile",
    "Gemfile.lock",
    "composer.json",
    "composer.lock",
    # Dockerfiles and container configs
    "*Dockerfile*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "*.dockerfile",
    # Terraform and IaC
    "*.tf",
    "*.tfvars",
    "*.hcl",
    # Migrations
    "*migrations*/**",
    "*migration*",
    "db/migrate/**",
    "alembic/**",
)

# Destructive SQL / DDL / Migrations
_DESTRUCTIVE_SQL_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "destructive_sql",
        "Destructive DDL / operation",
        re.compile(r"(?i)\bDROP\s+(?:DATABASE|SCHEMA|TABLE|VIEW|INDEX|COLUMN)\b"),
    ),
    (
        "destructive_sql",
        "Destructive DDL / table modification",
        re.compile(r"(?i)\bALTER\s+(?:TABLE\s+)?[A-Za-z0-9_.]+\s+(?:DROP|RENAME)\b"),
    ),
    (
        "destructive_sql",
        "Privilege revocation",
        re.compile(r"(?i)\bREVOKE\s+[A-Za-z0-9_\s,]+\s+(?:ON|FROM)\b"),
    ),
    (
        "destructive_sql",
        "Table truncation",
        re.compile(r"(?i)\bTRUNCATE\s+(?:TABLE\s+)?[A-Za-z0-9_.]+\b"),
    ),
]

# Auth / Tokens / Secrets / IAM logic
_AUTH_AND_SECRET_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "auth_and_secrets",
        "Credential or secret key",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key|jwt[_-]?secret)\b\s*[:=]"
        ),
    ),
    (
        "auth_and_secrets",
        "Hardcoded password assignment",
        re.compile(r"(?i)\b(?:password|passwd)\b\s*[:=]\s*['\"][^'\"]{3,}['\"]"),
    ),
]

# Cache logic
_CACHE_LOGIC_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "cache_logic",
        "Cache mutation or flush",
        re.compile(
            r"(?i)\b(?:cache|redis|memcached)\.(?:set|setex|delete|del|invalidate|clear|flush|flushdb|flushall)\b"
        ),
    ),
    (
        "cache_logic",
        "Cache decorator",
        re.compile(r"^@(?:lru_)?cache\b"),
    ),
]

# Function signature removal / rewrite (on '-' lines only)
_REMOVED_FUNCTION_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "removed_function",
        "Removed Python function",
        re.compile(r"^-(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\("),
    ),
    (
        "removed_function",
        "Removed JS/TS function",
        re.compile(
            r"^-(?:export\s+)?(?:default\s+)?(?:async\s+)?function(?:\s+[A-Za-z_$][A-Za-z0-9_$]*)?\s*\("
        ),
    ),
    (
        "removed_function",
        "Removed Go function",
        re.compile(r"^-func\s+(?:\([^)]+\)\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\("),
    ),
    (
        "removed_function",
        "Removed Rust function",
        re.compile(r"^-(?:pub\s+)?(?:async\s+)?fn\s+[A-Za-z_][A-Za-z0-9_]*\s*[(<]"),
    ),
]

# Removed configuration defaults (on '-' lines in config files)
_REMOVED_CONFIG_DEFAULT_PATTERN = re.compile(r"^-[A-Za-z0-9_.-]+\s*[:=]\s*\S+")

# Prompt file globs
_PROMPT_FILE_GLOBS = ("*prompt*.toml", "*prompt*.yaml", "*prompts*/**")

# Unsafe ReDoS quantifier detector for custom patterns
_UNSAFE_QUANTIFIER_RE = re.compile(r"\([^)]*[*+?]\)[*+?]|\([^)]*\{[0-9,]+\}\)[*+?\{]")


def normalize_file_path(path: str | None) -> str:
    """Normalize file path to POSIX style without leading slash."""
    if not path or not isinstance(path, str):
        return ""
    norm = path.replace("\\", "/").strip().lstrip("/")
    return norm[:MAX_PATH_LENGTH]


def matches_sensitive_path(path: str, patterns: Sequence[str] | None = None) -> str | None:
    """Check if normalized path matches any sensitive path glob. Returns the matched glob or None."""
    norm = normalize_file_path(path)
    if not norm:
        return None
    globs = patterns if patterns is not None else DEFAULT_SENSITIVE_PATHS
    for pat in globs:
        if not isinstance(pat, str) or not pat or len(pat) > MAX_GLOB_LENGTH:
            continue
        if fnmatchcase(norm, pat) or PurePosixPath(norm).match(pat):
            return pat
        # Also check basename for simple filename patterns like *.tf or package.json
        if fnmatchcase(PurePosixPath(norm).name, pat):
            return pat
    return None


def is_prompt_file(path: str) -> bool:
    """Whether the file matches prompt templates or configurations."""
    norm = normalize_file_path(path)
    if not norm:
        return False
    return any(fnmatchcase(norm, pat) or fnmatchcase(PurePosixPath(norm).name, pat) for pat in _PROMPT_FILE_GLOBS)


def compile_custom_shapes(patterns: Sequence[str] | None) -> list[re.Pattern]:
    """Compile custom regex patterns with length cap and ReDoS safety checks."""
    if not patterns or not isinstance(patterns, (list, tuple)):
        return []
    compiled = []
    for pat in patterns:
        if not isinstance(pat, str) or not pat.strip():
            continue
        cleaned = pat.strip()
        if len(cleaned) > MAX_CUSTOM_PATTERN_LENGTH:
            get_logger().warning(f"Custom risk shape exceeds length cap ({len(cleaned)} > {MAX_CUSTOM_PATTERN_LENGTH})")
            continue
        if _UNSAFE_QUANTIFIER_RE.search(cleaned):
            get_logger().warning(f"Rejecting unsafe nested quantifier in custom risk shape: {cleaned!r}")
            continue
        try:
            compiled.append(re.compile(cleaned))
        except re.error as e:
            get_logger().warning(f"Failed to compile custom risk shape {cleaned!r}: {e}")
    return compiled


def scan_file_diff(
    file_info: FilePatchInfo,
    custom_patterns: list[re.Pattern] | None = None,
) -> list[dict]:
    """Scan diff hunks of a single file for risky shapes."""
    findings = []
    filename = file_info.filename or ""

    # Check for deleted file
    if getattr(file_info, "edit_type", None) == EDIT_TYPE.DELETED:
        findings.append({
            "category": "deleted_file",
            "description": "Deleted file",
            "file": filename,
            "line": "",
        })

    # Check for prompt file modification
    if is_prompt_file(filename):
        findings.append({
            "category": "prompt_file",
            "description": "Prompt file modified",
            "file": filename,
            "line": "",
        })

    patch = file_info.patch or ""
    if not patch:
        return findings

    is_config_file = any(
        filename.endswith(ext) for ext in (".toml", ".yaml", ".yml", ".json", ".ini", ".env")
    )

    for raw_line in patch.splitlines():
        if not raw_line or len(raw_line) < 2:
            continue
        prefix = raw_line[0]
        if prefix not in ("+", "-"):
            continue

        # Cap line length for linear-time evaluation
        line_capped = raw_line[:MAX_LINE_CHARS]
        content_after_prefix = line_capped[1:].strip()

        # Check removed function signatures on '-' lines
        if prefix == "-":
            for cat, desc, pat in _REMOVED_FUNCTION_PATTERNS:
                if pat.search(line_capped):
                    findings.append({
                        "category": cat,
                        "description": desc,
                        "file": filename,
                        "line": content_after_prefix,
                    })
                    break

            # Check removed config defaults
            if is_config_file and _REMOVED_CONFIG_DEFAULT_PATTERN.search(line_capped):
                findings.append({
                    "category": "removed_config_default",
                    "description": "Removed configuration default",
                    "file": filename,
                    "line": content_after_prefix,
                })

        # Check destructive operations across added/removed lines
        for cat, desc, pat in _DESTRUCTIVE_SQL_PATTERNS:
            if pat.search(content_after_prefix):
                findings.append({
                    "category": cat,
                    "description": desc,
                    "file": filename,
                    "line": content_after_prefix,
                })
                break

        # Check auth/secret logic
        for cat, desc, pat in _AUTH_AND_SECRET_PATTERNS:
            if pat.search(content_after_prefix):
                findings.append({
                    "category": cat,
                    "description": desc,
                    "file": filename,
                    "line": content_after_prefix,
                })
                break

        # Check cache logic
        for cat, desc, pat in _CACHE_LOGIC_PATTERNS:
            if pat.search(content_after_prefix):
                findings.append({
                    "category": cat,
                    "description": desc,
                    "file": filename,
                    "line": content_after_prefix,
                })
                break

        # Check custom patterns
        if custom_patterns:
            for pat in custom_patterns:
                if pat.search(content_after_prefix):
                    findings.append({
                        "category": "custom_shape",
                        "description": f"Custom pattern match ({pat.pattern})",
                        "file": filename,
                        "line": content_after_prefix,
                    })
                    break

    return findings


def build_risk_checks(diff_files: list[FilePatchInfo] | None) -> dict | None:
    """Assess deterministic risk for all diff files in the PR. Return findings dict or None."""
    try:
        settings = get_settings()
        if not settings.pr_reviewer.get("enable_risk_checks", True):
            return None
        if diff_files is None:
            return None

        configured_paths = settings.pr_reviewer.get("sensitive_paths", DEFAULT_SENSITIVE_PATHS)
        if isinstance(configured_paths, str):
            configured_paths = [configured_paths]

        custom_patterns = compile_custom_shapes(settings.pr_reviewer.get("extra_risky_shapes", []))

        sensitive_matches = []
        risky_shapes = []
        deleted_files = []

        seen_sensitive = set()
        for file in diff_files:
            filename = normalize_file_path(file.filename)
            if not filename:
                continue

            # Check sensitive paths
            matched_glob = matches_sensitive_path(filename, configured_paths)
            if matched_glob and filename not in seen_sensitive:
                seen_sensitive.add(filename)
                sensitive_matches.append({"file": filename, "pattern": matched_glob})

            # Check deleted files
            if getattr(file, "edit_type", None) == EDIT_TYPE.DELETED:
                deleted_files.append(filename)

            # Scan diff patch
            shapes = scan_file_diff(file, custom_patterns)
            for shape in shapes:
                if shape["category"] != "deleted_file":
                    risky_shapes.append(shape)

        has_findings = bool(sensitive_matches or risky_shapes or deleted_files)
        return {
            "has_findings": has_findings,
            "sensitive_paths": sensitive_matches,
            "risky_shapes": risky_shapes,
            "deleted_files": deleted_files,
        }
    except Exception as e:
        get_logger().warning(f"Deterministic risk check skipped due to error: {e!r}")
        return None


def render_risk_checks(data: dict, gfm_supported: bool) -> str:
    """Render the risk check row for PR review markdown."""
    if not isinstance(data, dict):
        return ""

    has_findings = data.get("has_findings", False)
    sensitive_paths = data.get("sensitive_paths") or []
    risky_shapes = data.get("risky_shapes") or []
    deleted_files = data.get("deleted_files") or []

    clean_msg = "No sensitive paths or risky shapes detected"
    if not has_findings:
        if gfm_supported:
            return f"<tr><td>✅&nbsp;<strong>Risk checks:</strong> {clean_msg}</td></tr>\n"
        return f"### ✅ Risk checks: {clean_msg}\n\n"

    total = len(sensitive_paths) + len(risky_shapes) + len(deleted_files)
    if gfm_supported:
        header = f"⚠️&nbsp;<strong>Risk checks:</strong> {total} finding(s) detected"
    else:
        header = f"### ⚠️ Risk checks: {total} finding(s) detected"

    details = []
    if sensitive_paths:
        shown = [f"`{html.escape(item['file'])}`" for item in sensitive_paths[:MAX_FINDINGS_PER_CATEGORY]]
        more = len(sensitive_paths) - len(shown)
        more_str = f" (+{more} more)" if more > 0 else ""
        if gfm_supported:
            details.append(
                f"<strong>Sensitive paths ({len(sensitive_paths)}):</strong> {', '.join(shown)}{more_str}"
            )
        else:
            details.append(f"- **Sensitive paths ({len(sensitive_paths)}):** {', '.join(shown)}{more_str}")

    if risky_shapes:
        shown = [
            f"`{html.escape(item['description'])}` in `{html.escape(item['file'])}`"
            for item in risky_shapes[:MAX_FINDINGS_PER_CATEGORY]
        ]
        more = len(risky_shapes) - len(shown)
        more_str = f" (+{more} more)" if more > 0 else ""
        if gfm_supported:
            details.append(
                f"<strong>Risky shapes ({len(risky_shapes)}):</strong> {', '.join(shown)}{more_str}"
            )
        else:
            details.append(f"- **Risky shapes ({len(risky_shapes)}):** {', '.join(shown)}{more_str}")

    if deleted_files:
        shown = [f"`{html.escape(f)}`" for f in deleted_files[:MAX_FINDINGS_PER_CATEGORY]]
        more = len(deleted_files) - len(shown)
        more_str = f" (+{more} more)" if more > 0 else ""
        if gfm_supported:
            details.append(
                f"<strong>Deleted files ({len(deleted_files)}):</strong> {', '.join(shown)}{more_str}"
            )
        else:
            details.append(f"- **Deleted files ({len(deleted_files)}):** {', '.join(shown)}{more_str}")

    if gfm_supported:
        content = "<br>\n".join([header, *details])
        return f"<tr><td>{content}</td></tr>\n"

    body = "\n".join(details)
    return f"{header}\n\n{body}\n\n"
