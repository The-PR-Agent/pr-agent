from __future__ import annotations

from pathlib import Path

import pytest

from assessment.ai_reviewer.context_tools import (
    MAX_READ_LINES,
    MAX_SEARCH_HITS,
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
