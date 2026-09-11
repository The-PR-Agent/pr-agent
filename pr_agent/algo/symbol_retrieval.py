"""Grep/identifier-based cross-file symbol retrieval for Dart (R-16 v1)."""

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

RETRIEVED_CONTEXT_INTRO = (
    "The block below is code from the repository, provided for reference only."
)
RETRIEVED_CONTEXT_OPEN = "<retrieved_context>"
RETRIEVED_CONTEXT_CLOSE = "</retrieved_context>"
MARKDOWN_FENCE = "`````"
TRUNCATION_NOTE_TEMPLATE = "... ({count} snippet(s) omitted due to size limit)"
SNIPPET_CONTEXT_LINES = 6

DART_KEYWORDS = frozenset({
    "abstract", "as", "assert", "async", "await", "break", "case", "catch", "class", "const",
    "continue", "covariant", "default", "deferred", "do", "dynamic", "else", "enum", "export",
    "extends", "extension", "external", "factory", "false", "final", "finally", "for", "Function",
    "get", "hide", "if", "implements", "import", "in", "interface", "is", "late", "library",
    "mixin", "new", "null", "of", "on", "operator", "part", "required", "rethrow", "return",
    "set", "show", "static", "super", "switch", "sync", "this", "throw", "true", "try", "typedef",
    "var", "void", "while", "with", "yield",
})

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RETURN_TYPE = (
    r"(?:void|bool|int|double|String|dynamic|var|never|Null|[A-Za-z_][A-Za-z0-9_]*(?:<[^>]+>)?)"
)
_DECLARATION_PATTERNS = (
    re.compile(r"\b(?:class|mixin|extension|enum|typedef)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(
        r"^\s*(?:(?:static|external|final|const|late|covariant|required)\s+)+"
        r"(?:[\w<>,\s.?]+\s+)+([A-Za-z_][A-Za-z0-9_]*)\s*[=;{]"
    ),
    re.compile(
        rf"^\s*(?:(?:static|external|async|sync)\s+)*{_RETURN_TYPE}\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|<)"
    ),
)


@dataclass
class SymbolIndex:
    repo_root: str
    definitions: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    references: dict[str, list[tuple[str, int]]] = field(default_factory=dict)


def extract_changed_identifiers(patch: str) -> set[str]:
    """Return Dart identifiers from added/removed diff lines."""
    identifiers: set[str] = set()
    for line in _patch_changed_lines(patch):
        declared, referenced = _dart_identifiers_from_line(line)
        identifiers.update(declared)
        identifiers.update(referenced)
    return identifiers


def build_repo_symbol_index(
    repo_root: str,
    *,
    extensions: tuple[str, ...] = (".dart",),
    skip_globs: tuple[str, ...] = (
        "**/*.g.dart",
        "**/*.freezed.dart",
        "**/build/**",
        "**/.dart_tool/**",
    ),
) -> SymbolIndex:
    """Walk a checkout once and index Dart symbol definitions and references."""
    index = SymbolIndex(repo_root=repo_root)
    root = Path(repo_root)
    if not root.is_dir():
        return index

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = _relative_path(root, Path(dirpath))
        dirnames[:] = sorted(
            name for name in dirnames
            if not _path_matches_skip(_join_rel(rel_dir, name), skip_globs)
        )

        for filename in sorted(filenames):
            if not any(filename.endswith(ext) for ext in extensions):
                continue
            rel_path = _join_rel(rel_dir, filename)
            if _path_matches_skip(rel_path, skip_globs):
                continue
            _index_file(index, root / rel_path, rel_path)

    return index


def retrieve_context_for_files(
    index: SymbolIndex,
    changed_files: dict[str, str],
    *,
    max_chars: int,
    max_files_per_symbol: int = 3,
) -> str:
    """Retrieve cross-file definition and reference snippets for changed identifiers."""
    if not index.definitions and not index.references:
        return ""

    changed_paths = set(changed_files)
    identifiers: set[str] = set()
    for patch in changed_files.values():
        identifiers.update(extract_changed_identifiers(patch))
    if not identifiers:
        return ""

    snippets: list[str] = []
    seen: set[tuple[str, int, str]] = set()

    for symbol in sorted(identifiers):
        for path, line in index.definitions.get(symbol, []):
            if path in changed_paths:
                continue
            key = (path, line, "definition")
            if key in seen:
                continue
            snippet = _make_snippet(index, path, line)
            if snippet:
                snippets.append(snippet)
                seen.add(key)

        ref_count = 0
        for path, line in index.references.get(symbol, []):
            if path in changed_paths:
                continue
            if ref_count >= max_files_per_symbol:
                break
            key = (path, line, "reference")
            if key in seen:
                continue
            snippet = _make_snippet(index, path, line)
            if snippet:
                snippets.append(snippet)
                seen.add(key)
                ref_count += 1

    return _render_retrieved_context(snippets, max_chars)


def _patch_changed_lines(patch: str) -> list[str]:
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+") or line.startswith("-"):
            lines.append(line[1:])
    return lines




def _code_portion(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("//"):
        return ""
    if "//" in line:
        return line.split("//", 1)[0]
    return line


def _dart_identifiers_from_line(line: str) -> tuple[set[str], set[str]]:
    line = _code_portion(line)
    if not line.strip():
        return set(), set()

    declared: set[str] = set()
    for pattern in _DECLARATION_PATTERNS:
        for match in pattern.finditer(line):
            name = match.group(1)
            if _is_meaningful_identifier(name):
                declared.add(name)

    referenced: set[str] = set()
    for match in _IDENTIFIER_RE.finditer(line):
        name = match.group(0)
        if name in declared:
            continue
        if _is_meaningful_identifier(name):
            referenced.add(name)
    return declared, referenced


def _is_meaningful_identifier(name: str) -> bool:
    return len(name) > 1 and name not in DART_KEYWORDS


def _relative_path(root: Path, path: Path) -> str:
    rel = path.relative_to(root)
    # The repo root itself relativises to ".", which would prefix every root-level file as
    # "./a.dart". Diff paths carry no such prefix, so the "already in the diff" check would miss
    # and the diff's own content would be retrieved back into the prompt.
    return "" if rel == Path(".") else rel.as_posix()


def _join_rel(directory: str, name: str) -> str:
    if directory:
        return f"{directory}/{name}"
    return name


def _path_matches_skip(rel_path: str, skip_globs: tuple[str, ...]) -> bool:
    norm = rel_path.replace("\\", "/")
    posix_path = PurePosixPath(norm)
    parts = norm.split("/")
    for pattern in skip_globs:
        if posix_path.match(pattern) or fnmatch.fnmatch(norm, pattern):
            return True
        trimmed = pattern.removeprefix("**/")
        if trimmed != pattern and (posix_path.match(trimmed) or fnmatch.fnmatch(norm, trimmed)):
            return True
        if pattern.endswith("/*.g.dart") and norm.endswith(".g.dart"):
            return True
        if pattern.endswith(".freezed.dart") and norm.endswith(".freezed.dart"):
            return True
        if "/build/" in f"/{norm}/" or norm.startswith("build/"):
            if "build" in pattern:
                return True
        if "/.dart_tool/" in f"/{norm}/" or norm.startswith(".dart_tool/"):
            if ".dart_tool" in pattern:
                return True
    return False


def _index_file(index: SymbolIndex, abs_path: Path, rel_path: str) -> None:
    try:
        lines = abs_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line_number, line in enumerate(lines, start=1):
        declared, referenced = _dart_identifiers_from_line(line)
        for name in declared:
            index.definitions.setdefault(name, []).append((rel_path, line_number))
        for name in referenced:
            index.references.setdefault(name, []).append((rel_path, line_number))


def _make_snippet(index: SymbolIndex, rel_path: str, line_number: int) -> str:
    abs_path = Path(index.repo_root) / rel_path
    try:
        lines = abs_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    start = max(0, line_number - 1 - SNIPPET_CONTEXT_LINES)
    end = min(len(lines), line_number + SNIPPET_CONTEXT_LINES)
    snippet_lines = lines[start:end]
    body = "\n".join(snippet_lines)
    return f"{rel_path}:{line_number}\n{body}"


def _get_markdown_fence(content: str) -> str:
    fence = MARKDOWN_FENCE
    while fence in content:
        fence += "`"
    return fence


def _neutralise_delimiters(text: str) -> str:
    return (
        text.replace(RETRIEVED_CONTEXT_CLOSE, "&lt;/retrieved_context&gt;")
        .replace(RETRIEVED_CONTEXT_OPEN, "&lt;retrieved_context&gt;")
    )


def _render_retrieved_context(snippets: list[str], max_chars: int) -> str:
    if not snippets:
        return ""

    included: list[str] = []
    dropped = 0
    for snippet in snippets:
        candidate_body = "\n\n".join(included + [snippet])
        candidate = _wrap_retrieved_body(candidate_body)
        if len(candidate) <= max_chars:
            included.append(snippet)
            continue
        dropped += 1

    if not included:
        return ""

    body = "\n\n".join(included)
    if dropped:
        note = TRUNCATION_NOTE_TEMPLATE.format(count=dropped)
        candidate = _wrap_retrieved_body(f"{body}\n\n{note}")
        if len(candidate) <= max_chars:
            body = f"{body}\n\n{note}"
        else:
            dropped += 1

    rendered = _wrap_retrieved_body(body)
    if len(rendered) > max_chars:
        return ""
    return rendered


def _wrap_retrieved_body(body: str) -> str:
    body = _neutralise_delimiters(body)
    fence = _get_markdown_fence(body)
    parts = [
        RETRIEVED_CONTEXT_INTRO,
        RETRIEVED_CONTEXT_OPEN,
        f"{fence}markdown",
        body,
        fence,
        RETRIEVED_CONTEXT_CLOSE,
    ]
    return "\n".join(parts)
