from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

MAX_READ_LINES = 200
MAX_SEARCH_HITS = 20
MAX_TREE_DEPTH = 3
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "vendor",
}


class ContextToolError(ValueError):
    """Raised when a read-only context request violates policy."""


@dataclass(frozen=True, slots=True)
class ToolObservation:
    tool: str
    payload: dict[str, Any]

    @property
    def result_hash(self) -> str:
        digest = hashlib.sha256(repr(self.payload).encode("utf-8"))
        return digest.hexdigest()[:16]


class ContextTools:
    def __init__(self, repo_root: Path) -> None:
        root = repo_root.resolve(strict=True)
        if not root.is_dir():
            raise ContextToolError("repo_root must be a directory")
        self.repo_root = root

    def read_file(
        self,
        relative_path: str,
        start_line: int = 1,
        end_line: int = MAX_READ_LINES,
    ) -> ToolObservation:
        if start_line < 1 or end_line < start_line:
            raise ContextToolError("invalid line range")
        if end_line - start_line + 1 > MAX_READ_LINES:
            end_line = start_line + MAX_READ_LINES - 1
        path = self._resolve(relative_path, require_file=True)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start_line - 1:end_line]
        return ToolObservation(
            tool="read_file",
            payload={
                "path": path.relative_to(self.repo_root).as_posix(),
                "start_line": start_line,
                "end_line": start_line + max(0, len(selected) - 1),
                "content": "\n".join(selected),
            },
        )

    def search_code(
        self,
        query: str,
        path_glob: str = "**/*",
        max_hits: int = MAX_SEARCH_HITS,
    ) -> ToolObservation:
        query = query.strip()
        if not query:
            raise ContextToolError("query must not be empty")
        limit = min(max(1, int(max_hits)), MAX_SEARCH_HITS)
        matches: list[dict[str, Any]] = []
        for path in sorted(self.repo_root.glob(path_glob)):
            if len(matches) >= limit:
                break
            if not self._is_safe_text_file(path):
                continue
            relative = path.relative_to(self.repo_root).as_posix()
            for line_number, line in enumerate(
                path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines(),
                start=1,
            ):
                if query in line:
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= limit:
                        break
        return ToolObservation(
            tool="search_code",
            payload={"query": query, "matches": matches},
        )

    def list_tree(
        self,
        relative_path: str = ".",
        depth: int = 2,
    ) -> ToolObservation:
        depth = min(max(0, int(depth)), MAX_TREE_DEPTH)
        root = self._resolve(relative_path, require_file=False)
        if not root.is_dir():
            raise ContextToolError("tree path must be a directory")
        entries: list[str] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or self._has_excluded_part(path):
                continue
            relative_to_start = path.relative_to(root)
            if len(relative_to_start.parts) > depth:
                continue
            suffix = "/" if path.is_dir() else ""
            entries.append(
                f"{path.relative_to(self.repo_root).as_posix()}{suffix}"
            )
            if len(entries) >= 200:
                break
        return ToolObservation(
            tool="list_tree",
            payload={
                "path": relative_path,
                "depth": depth,
                "entries": entries,
            },
        )

    def execute(self, request: dict[str, Any]) -> ToolObservation:
        tool = request.get("tool")
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ContextToolError("tool arguments must be an object")
        normalized = _normalize_arguments(str(tool), arguments)
        if tool == "read_file":
            return self.read_file(**normalized)
        if tool == "search_code":
            return self.search_code(**normalized)
        if tool == "list_tree":
            return self.list_tree(**normalized)
        raise ContextToolError(f"unsupported context tool: {tool}")

    def _resolve(self, relative_path: str, require_file: bool) -> Path:
        normalized = relative_path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            pure.is_absolute()
            or PureWindowsPath(relative_path).is_absolute()
            or ".." in pure.parts
        ):
            raise ContextToolError("path must be repository-relative")
        candidate = self.repo_root.joinpath(*pure.parts).resolve(strict=True)
        try:
            candidate.relative_to(self.repo_root)
        except ValueError as error:
            raise ContextToolError("path escapes repo_root") from error
        if candidate.is_symlink():
            raise ContextToolError("symbolic links are not readable")
        if require_file and not candidate.is_file():
            raise ContextToolError("path must be a file")
        return candidate

    def _is_safe_text_file(self, path: Path) -> bool:
        if (
            not path.is_file()
            or path.is_symlink()
            or self._has_excluded_part(path)
        ):
            return False
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.repo_root)
        except (OSError, ValueError):
            return False
        try:
            sample = path.read_bytes()[:4096]
        except OSError:
            return False
        return b"\x00" not in sample

    def _has_excluded_part(self, path: Path) -> bool:
        parts = set(path.relative_to(self.repo_root).parts)
        return bool(parts & EXCLUDED_PARTS)


def _normalize_arguments(
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    aliases = {
        "read_file": {
            "path": "relative_path",
            "file_path": "relative_path",
            "start": "start_line",
            "end": "end_line",
        },
        "search_code": {
            "path": "path_glob",
            "glob": "path_glob",
            "max_results": "max_hits",
        },
        "list_tree": {"path": "relative_path", "max_depth": "depth"},
    }
    allowed = {
        "read_file": {"relative_path", "start_line", "end_line"},
        "search_code": {"query", "path_glob", "max_hits"},
        "list_tree": {"relative_path", "depth"},
    }
    if tool not in allowed:
        return dict(arguments)
    normalized: dict[str, Any] = {}
    for key, value in arguments.items():
        canonical = aliases[tool].get(key, key)
        if canonical not in allowed[tool]:
            raise ContextToolError(
                f"unsupported argument for {tool}: {canonical}"
            )
        if canonical in normalized:
            raise ContextToolError(
                f"duplicate argument for {tool}: {canonical}"
            )
        normalized[canonical] = value
    for key in {"start_line", "end_line", "max_hits", "depth"}:
        if key in normalized:
            try:
                normalized[key] = int(normalized[key])
            except (TypeError, ValueError) as error:
                raise ContextToolError(
                    f"{key} must be an integer"
                ) from error
    return normalized
