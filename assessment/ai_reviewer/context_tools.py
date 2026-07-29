from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

MAX_READ_LINES = 200
MAX_READ_BYTES = 256 * 1024
MAX_SEARCH_HITS = 20
MAX_SEARCH_BYTES = 1024 * 1024
MAX_SEARCH_FILE_BYTES = 256 * 1024
MAX_SEARCH_FILES = 100
MAX_SEARCH_PATHS = 1000
MAX_TREE_DEPTH = 3
MAX_TREE_ENTRIES = 200
MAX_TREE_PATHS = 1000
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
    def __init__(
        self,
        repo_root: Path,
        deadline_monotonic: float | None = None,
    ) -> None:
        root = repo_root.resolve(strict=True)
        if not root.is_dir():
            raise ContextToolError("repo_root must be a directory")
        self.repo_root = root
        self.deadline_monotonic = deadline_monotonic

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
        self._check_deadline()
        path = self._resolve(relative_path, require_file=True)
        try:
            if path.stat().st_size > MAX_READ_BYTES:
                raise ContextToolError("read_file byte limit exceeded")
            selected: list[str] = []
            bytes_read = 0
            with path.open("rb") as stream:
                for line_number, raw_line in enumerate(stream, start=1):
                    self._check_deadline()
                    bytes_read += len(raw_line)
                    if bytes_read > MAX_READ_BYTES:
                        raise ContextToolError(
                            "read_file byte limit exceeded"
                        )
                    if line_number >= start_line:
                        selected.append(
                            raw_line.rstrip(b"\r\n").decode(
                                "utf-8",
                                errors="replace",
                            )
                        )
                    if line_number >= end_line:
                        break
        except OSError as error:
            raise ContextToolError("file could not be read") from error
        return ToolObservation(
            tool="read_file",
            payload={
                "path": path.relative_to(self.repo_root).as_posix(),
                "start_line": start_line,
                "end_line": start_line + max(0, len(selected) - 1),
                "content": "\n".join(selected),
                "bytes_read": bytes_read,
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
        self._check_deadline()
        self._validate_glob(path_glob)
        limit = min(max(1, int(max_hits)), MAX_SEARCH_HITS)
        matches: list[dict[str, Any]] = []
        bytes_read = 0
        files_accessed = 0
        paths_visited = 0
        truncated = False
        for path in self.repo_root.glob(path_glob):
            self._check_deadline()
            if paths_visited >= MAX_SEARCH_PATHS:
                truncated = True
                break
            paths_visited += 1
            if not self._is_safe_text_file(path):
                continue
            if files_accessed >= MAX_SEARCH_FILES:
                truncated = True
                break
            remaining_bytes = MAX_SEARCH_BYTES - bytes_read
            if remaining_bytes <= 0:
                truncated = True
                break
            files_accessed += 1
            relative = path.relative_to(self.repo_root).as_posix()
            found, consumed, file_truncated = self._search_file(
                path,
                relative,
                query,
                limit - len(matches),
                remaining_bytes,
            )
            matches.extend(found)
            bytes_read += consumed
            truncated = truncated or file_truncated
            if len(matches) >= limit or bytes_read >= MAX_SEARCH_BYTES:
                truncated = True
                break
        return ToolObservation(
            tool="search_code",
            payload={
                "query": query,
                "matches": matches,
                "bytes_read": bytes_read,
                "files_accessed": files_accessed,
                "paths_visited": paths_visited,
                "truncated": truncated,
            },
        )

    def list_tree(
        self,
        relative_path: str = ".",
        depth: int = 2,
    ) -> ToolObservation:
        depth = min(max(0, int(depth)), MAX_TREE_DEPTH)
        self._check_deadline()
        root = self._resolve(relative_path, require_file=False)
        if not root.is_dir():
            raise ContextToolError("tree path must be a directory")
        entries: list[str] = []
        paths_visited = 0
        truncated = False
        pending = [(root, 0)] if depth else []
        while pending and not truncated:
            directory, parent_depth = pending.pop()
            try:
                iterator = os.scandir(directory)
            except OSError:
                continue
            with iterator:
                for entry in iterator:
                    self._check_deadline()
                    if paths_visited >= MAX_TREE_PATHS:
                        truncated = True
                        break
                    paths_visited += 1
                    try:
                        if (
                            entry.is_symlink()
                            or entry.name in EXCLUDED_PARTS
                        ):
                            continue
                        is_directory = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    path = Path(entry.path)
                    suffix = "/" if is_directory else ""
                    entries.append(
                        f"{path.relative_to(self.repo_root).as_posix()}"
                        f"{suffix}"
                    )
                    if len(entries) >= MAX_TREE_ENTRIES:
                        truncated = True
                        break
                    if is_directory and parent_depth + 1 < depth:
                        pending.append((path, parent_depth + 1))
        return ToolObservation(
            tool="list_tree",
            payload={
                "path": relative_path,
                "depth": depth,
                "entries": entries,
                "paths_visited": paths_visited,
                "truncated": truncated,
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
        return True

    def _search_file(
        self,
        path: Path,
        relative: str,
        query: str,
        max_hits: int,
        byte_budget: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        limit = min(MAX_SEARCH_FILE_BYTES, byte_budget)
        matches: list[dict[str, Any]] = []
        bytes_read = 0
        try:
            file_size = path.stat().st_size
            with path.open("rb") as stream:
                line_number = 0
                while bytes_read < limit and len(matches) < max_hits:
                    self._check_deadline()
                    raw_line = stream.readline(limit - bytes_read)
                    if not raw_line:
                        break
                    line_number += 1
                    bytes_read += len(raw_line)
                    if b"\x00" in raw_line:
                        return [], bytes_read, file_size > bytes_read
                    line = raw_line.decode("utf-8", errors="replace")
                    if query in line:
                        matches.append(
                            {
                                "path": relative,
                                "line": line_number,
                                "text": line.rstrip("\r\n")[:500],
                            }
                        )
        except OSError:
            return [], bytes_read, False
        return matches, bytes_read, file_size > bytes_read

    def _validate_glob(self, path_glob: str) -> None:
        normalized = path_glob.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            not normalized
            or pure.is_absolute()
            or PureWindowsPath(path_glob).is_absolute()
            or ".." in pure.parts
        ):
            raise ContextToolError("path_glob must be repository-relative")

    def _check_deadline(self) -> None:
        if (
            self.deadline_monotonic is not None
            and time.monotonic() >= self.deadline_monotonic
        ):
            raise ContextToolError("context tool deadline exceeded")

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
            except (OverflowError, TypeError, ValueError) as error:
                raise ContextToolError(
                    f"{key} must be an integer"
                ) from error
    return normalized
