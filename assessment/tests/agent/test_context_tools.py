from __future__ import annotations

import time
from pathlib import Path

import pytest

from assessment.ai_reviewer.context_tools import (
    MAX_READ_BYTES,
    MAX_READ_LINES,
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_HITS,
    MAX_TREE_PATHS,
    ContextToolError,
    ContextTools,
)


def test_read_file_is_bounded_and_repository_relative(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text(
        "\n".join(f"line {number}" for number in range(300)),
        encoding="utf-8",
    )
    tools = ContextTools(tmp_path)

    observation = tools.read_file("source.py", 1, 999)

    assert observation.payload["path"] == "source.py"
    assert len(observation.payload["content"].splitlines()) == MAX_READ_LINES


@pytest.mark.parametrize(
    "path",
    ("../secret.txt", "C:/secret.txt", "C:\\secret.txt", "/etc/passwd"),
)
def test_read_file_rejects_path_escape(tmp_path: Path, path: str) -> None:
    tools = ContextTools(tmp_path)

    with pytest.raises(ContextToolError, match="repository-relative"):
        tools.read_file(path)


def test_search_code_caps_results(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text(
        "\n".join("needle" for _ in range(50)),
        encoding="utf-8",
    )
    tools = ContextTools(tmp_path)

    observation = tools.search_code("needle", "**/*.py", max_hits=1000)

    assert len(observation.payload["matches"]) == MAX_SEARCH_HITS


def test_read_file_rejects_content_beyond_byte_budget(tmp_path: Path) -> None:
    (tmp_path / "huge.py").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    tools = ContextTools(tmp_path)

    with pytest.raises(ContextToolError, match="byte limit"):
        tools.read_file("huge.py")


def test_search_code_stops_at_byte_and_file_access_limits(
    tmp_path: Path,
) -> None:
    for index in range(MAX_SEARCH_FILES + 2):
        (tmp_path / f"file-{index:04}.py").write_text(
            "ordinary source\n",
            encoding="utf-8",
        )
    (tmp_path / "large.py").write_bytes(
        b"x" * MAX_SEARCH_BYTES + b"needle-after-budget\n"
    )
    tools = ContextTools(tmp_path)

    observation = tools.search_code("needle-after-budget")

    assert observation.payload["matches"] == []
    assert observation.payload["bytes_read"] <= MAX_SEARCH_BYTES
    assert observation.payload["files_accessed"] <= MAX_SEARCH_FILES
    assert observation.payload["truncated"] is True


def test_tree_traversal_checks_deadline_and_reports_visit_bound(
    tmp_path: Path,
) -> None:
    for index in range(MAX_TREE_PATHS + 2):
        (tmp_path / f"entry-{index:04}.txt").touch()
    tools = ContextTools(tmp_path)

    observation = tools.list_tree(depth=1)

    assert observation.payload["paths_visited"] <= MAX_TREE_PATHS
    assert observation.payload["truncated"] is True

    expired = ContextTools(tmp_path, deadline_monotonic=time.monotonic() - 1)
    with pytest.raises(ContextToolError, match="deadline"):
        expired.list_tree()
