"""Deterministic risk checks for /review.

Flags sensitive paths and risky change shapes in pull requests.
Runs locally in linear time over diff files and patches. No model call is involved.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Sequence
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

import regex

from pr_agent.algo.setting_utils import _as_bool_setting
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

MAX_GLOB_LENGTH = 200
MAX_PATH_LENGTH = 1000
MAX_LINE_CHARS = 500
MAX_CUSTOM_PATTERN_LENGTH = 200
# The static check below cannot rule out every backtracking shape, so each custom search gets a timeout, and each
# custom pattern a total budget per review, which also drops a pattern that is slow on every line.
CUSTOM_PATTERN_TIMEOUT_SECONDS = 0.05
CUSTOM_PATTERN_REVIEW_BUDGET_SECONDS = 1.0
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
        re.compile(r"(?i)\bREVOKE\s+[A-Za-z0-9_,]+(?:\s+[A-Za-z0-9_,]+)*?\s+(?:ON|FROM)\b"),
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

# Function signature removal / rewrite (on '-' lines only). Leading whitespace is allowed so methods
# inside classes and impl blocks count too.
_REMOVED_FUNCTION_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    (
        "removed_function",
        "Removed Python function",
        re.compile(r"^-\s*(?:async\s+)?def\s+[A-Za-z_][A-Za-z0-9_]*\s*\("),
    ),
    (
        "removed_function",
        "Removed JS/TS function",
        re.compile(
            r"^-\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function(?:\s+[A-Za-z_$][A-Za-z0-9_$]*)?\s*\("
        ),
    ),
    (
        "removed_function",
        "Removed Go function",
        re.compile(r"^-\s*func\s+(?:\([^)]+\)\s+)?[A-Za-z_][A-Za-z0-9_]*\s*\("),
    ),
    (
        "removed_function",
        "Removed Rust function",
        re.compile(r"^-\s*(?:pub\s+)?(?:async\s+)?fn\s+[A-Za-z_][A-Za-z0-9_]*\s*[(<]"),
    ),
]

# Removed configuration defaults (on '-' lines in config files)
_REMOVED_CONFIG_DEFAULT_PATTERN = re.compile(r"^-[A-Za-z0-9_.-]+\s*[:=]\s*\S+")

# Prompt file globs
_PROMPT_FILE_GLOBS = ("*prompt*.toml", "*prompt*.yaml", "*prompts*/**")

# Unsafe ReDoS quantifier detector for custom patterns
_UNSAFE_QUANTIFIER_RE = re.compile(r"\([^)]*[*+?]\)[*+?]|\([^)]*\{[0-9,]+\}\)[*+?\{]|\([^)|]*\|[^)]*\)[*+?\{]")


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
    parsed = None
    for pat in globs:
        if not isinstance(pat, str) or not pat or len(pat) > MAX_GLOB_LENGTH:
            continue
        if fnmatchcase(norm, pat):
            return pat
        if parsed is None:
            parsed = PurePosixPath(norm)
        if parsed.match(pat):
            return pat
        # Also check basename for simple filename patterns like *.tf or package.json
        if fnmatchcase(parsed.name, pat):
            return pat
    return None


def is_prompt_file(path: str) -> bool:
    """Whether the file matches prompt templates or configurations."""
    norm = normalize_file_path(path)
    if not norm:
        return False
    return any(fnmatchcase(norm, pat) or fnmatchcase(PurePosixPath(norm).name, pat) for pat in _PROMPT_FILE_GLOBS)


def compile_custom_shapes(patterns: Sequence[str] | None) -> list[regex.Pattern]:
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
            compiled.append(regex.compile(cleaned))
        except regex.error as e:
            get_logger().warning(f"Failed to compile custom risk shape {cleaned!r}: {e}")
    return compiled


def scan_file_diff(
    file_info: FilePatchInfo,
    custom_patterns: list[regex.Pattern] | None = None,
    custom_pattern_seconds: dict | None = None,
) -> list[dict]:
    """Scan diff hunks of a single file for risky shapes.

    custom_pattern_seconds holds the time each custom pattern has spent so far; pass the same dict for
    every file of a review. A pattern that times out or spends its review budget is removed from
    custom_patterns, so the caller's later files skip it too.
    """
    spent = custom_pattern_seconds if custom_pattern_seconds is not None else {}
    findings = []
    filename = _file_value(file_info, "filename", "new_path", default="") or ""

    # Check for deleted file
    if (
        _file_value(file_info, "edit_type") == EDIT_TYPE.DELETED
        or _file_value(file_info, "deleted_file") is True
        or _file_value(file_info, "status") in ("removed", "deleted")
    ):
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

    patch = _file_value(file_info, "patch", "diff") or ""
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
            for pat in list(custom_patterns):
                remaining = CUSTOM_PATTERN_REVIEW_BUDGET_SECONDS - spent.get(pat, 0.0)
                start = time.perf_counter()
                try:
                    matched = pat.search(content_after_prefix,
                                         timeout=min(CUSTOM_PATTERN_TIMEOUT_SECONDS, remaining))
                    timed_out = False
                except TimeoutError:
                    matched, timed_out = None, True
                spent[pat] = spent.get(pat, 0.0) + time.perf_counter() - start
                if timed_out or spent[pat] >= CUSTOM_PATTERN_REVIEW_BUDGET_SECONDS:
                    custom_patterns.remove(pat)
                    get_logger().warning(
                        f"Custom risk shape {pat.pattern!r} exceeded its time budget, skipping it for this review")
                if matched:
                    findings.append({
                        "category": "custom_shape",
                        "description": f"Custom pattern match ({pat.pattern})",
                        "file": filename,
                        "line": content_after_prefix,
                    })
                    break

    return findings


def _extract_file_paths(file_entry: Any) -> list[str]:
    """Extract candidate file paths from a file object, dict, or string."""
    paths = []
    if isinstance(file_entry, str):
        paths.append(file_entry)
    elif isinstance(file_entry, dict):
        for key in ("new_path", "old_path", "filename", "path"):
            val = file_entry.get(key)
            if val and isinstance(val, str):
                paths.append(val)
    else:
        for attr in ("filename", "old_filename", "previous_filename", "path", "b_path", "a_path"):
            val = getattr(file_entry, attr, None)
            if val and isinstance(val, str):
                paths.append(val)
    return paths


def _file_value(file_entry: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = file_entry.get(name) if isinstance(file_entry, dict) else getattr(file_entry, name, None)
        if value is not None:
            return value
    return default


def build_risk_checks(
    diff_files: list[FilePatchInfo] | None,
    unfiltered_files: Sequence[Any] | None = None,
    *,
    include_clean: bool = False,
) -> dict | None:
    """Assess PR risk; return clean results only when requested, and None when unavailable."""
    try:
        settings = get_settings()
        if not _as_bool_setting(settings.pr_reviewer.get("enable_risk_checks", True), default=True):
            return None
        if diff_files is None and not unfiltered_files:
            return None

        configured_paths = settings.pr_reviewer.get("sensitive_paths", DEFAULT_SENSITIVE_PATHS)
        if isinstance(configured_paths, str):
            configured_paths = [configured_paths]
        elif not isinstance(configured_paths, (list, tuple)):
            configured_paths = DEFAULT_SENSITIVE_PATHS

        custom_patterns = compile_custom_shapes(settings.pr_reviewer.get("extra_risky_shapes", []))
        custom_pattern_seconds = {}

        sensitive_matches = []
        risky_shapes = []
        deleted_files = []

        seen_sensitive = set()
        for file in (diff_files or []):
            if isinstance(file, dict) and (file.get("too_large") is True or file.get("collapsed") is True):
                return None
            filename = normalize_file_path(_file_value(file, "filename", "new_path", default=""))
            old_filename = normalize_file_path(
                _file_value(file, "old_filename", "previous_filename", "old_path")
            )

            # Check sensitive paths for current filename
            if filename:
                matched_glob = matches_sensitive_path(filename, configured_paths)
                if matched_glob and filename not in seen_sensitive:
                    seen_sensitive.add(filename)
                    sensitive_matches.append({"file": filename, "pattern": matched_glob})

            # Check sensitive paths for old_filename (renamed/moved files)
            if old_filename and old_filename != filename:
                matched_old = matches_sensitive_path(old_filename, configured_paths)
                if matched_old and old_filename not in seen_sensitive:
                    seen_sensitive.add(old_filename)
                    sensitive_matches.append({"file": old_filename, "pattern": matched_old})

            # Check deleted files
            if (
                _file_value(file, "edit_type") == EDIT_TYPE.DELETED
                or _file_value(file, "deleted_file") is True
                or _file_value(file, "status") in ("removed", "deleted")
            ):
                deleted_name = filename or old_filename
                if deleted_name and deleted_name not in deleted_files:
                    deleted_files.append(deleted_name)

            # Scan diff patch
            if _file_value(file, "patch", "diff") is None:
                return None
            shapes = scan_file_diff(file, custom_patterns, custom_pattern_seconds)
            risky_shapes.extend(shape for shape in shapes if shape["category"] != "deleted_file")

        # Check unfiltered file list (e.g. lock files or files omitted by git provider diff filters)
        if unfiltered_files:
            for item in unfiltered_files:
                for candidate in _extract_file_paths(item):
                    norm = normalize_file_path(candidate)
                    if norm and norm not in seen_sensitive:
                        matched = matches_sensitive_path(norm, configured_paths)
                        if matched:
                            seen_sensitive.add(norm)
                            sensitive_matches.append({"file": norm, "pattern": matched})

        has_findings = bool(sensitive_matches or risky_shapes or deleted_files)
        if not has_findings and not include_clean:
            return None

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

    from pr_agent.algo.markdown_span import _markdown_code_span as plain_code_span

    def _markdown_code_span(text: str) -> str:
        # inside the <table> row the backticks stay literal, so escape for HTML instead
        if not gfm_supported:
            return plain_code_span(text)
        text = text.replace("\r", "\\r").replace("\n", "\\n")
        return f"<code>{html.escape(text)}</code>"

    has_findings = data.get("has_findings", False)
    sensitive_paths = data.get("sensitive_paths") or []
    risky_shapes = data.get("risky_shapes") or []
    deleted_files = data.get("deleted_files") or []

    if not has_findings:
        return ""

    total = len(sensitive_paths) + len(risky_shapes) + len(deleted_files)
    if gfm_supported:
        header = f"⚠️&nbsp;<strong>Risk checks:</strong> {total} finding(s) detected"
    else:
        header = f"### ⚠️ Risk checks: {total} finding(s) detected"

    details = []
    if sensitive_paths:
        shown = [_markdown_code_span(item["file"]) for item in sensitive_paths[:MAX_FINDINGS_PER_CATEGORY]]
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
            f"{_markdown_code_span(item['description'])} in {_markdown_code_span(item['file'])}"
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
        shown = [_markdown_code_span(f) for f in deleted_files[:MAX_FINDINGS_PER_CATEGORY]]
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
