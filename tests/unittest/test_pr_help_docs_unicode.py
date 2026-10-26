from io import StringIO

import pytest

from pr_agent.tools.pr_help_docs import map_documentation_files_to_contents, return_document_headings


@pytest.mark.parametrize(
    ("extension", "content", "expected_heading"),
    [
        pytest.param(".md", "# 安裝說明\n\n請先準備開發環境。\n", "# 安裝說明", id="chinese-markdown"),
        pytest.param(".rst", "設定手順\n========\n\n説明を確認してください。\n", "設定手順", id="japanese-rst"),
        pytest.param(".md", "# Setup\n\nRead the instructions.\n", "# Setup", id="english-markdown"),
        pytest.param(".md", "# 12345\n\n--- 67890 !?\n", "", id="numbers-and-symbols"),
    ],
)
def test_documentation_text_detection(tmp_path, extension, content, expected_heading):
    document = tmp_path / f"guide{extension}"
    document.write_text(content, encoding="utf-8")

    assert return_document_headings(content, extension) == expected_heading
    expected_contents = {f"/{document.name}": content.strip()} if expected_heading else {}
    assert map_documentation_files_to_contents(str(tmp_path), [str(document)]) == expected_contents


@pytest.mark.parametrize(
    ("content", "limit", "expected"),
    [
        (" 123456中文", 5, "1234"),
        ("123456789", 5, None),
        ("Guide", 5, "Guide"),
        ("Guide", 0, ""),
        ("Guide", -1, "Guid"),
    ],
)
def test_documentation_read_modes(monkeypatch, content, limit, expected):
    class BoundedReader(StringIO):
        def read(self, size=-1):
            if limit < 0:
                assert size == -1, "Negative slicing must read the full document once"
                return super().read(size)
            assert size > 0, "Documentation must be read in bounded chunks"
            return super().read(min(size, 3))

    monkeypatch.setattr(
        "pr_agent.tools.pr_help_docs.open", lambda *args, **kwargs: BoundedReader(content), raising=False
    )

    result = map_documentation_files_to_contents("/docs", ["/docs/guide.md"], limit)

    assert result == ({"/guide.md": expected} if expected is not None else {})


def test_documentation_rejects_invalid_utf8_after_retained_prefix(tmp_path):
    invalid_document = tmp_path / "invalid.md"
    invalid_document.write_bytes(b"# Guide\n" + b"1" * (64 * 1024) + b"\xff")
    valid_document = tmp_path / "valid.md"
    valid_document.write_text("# Valid guide\n", encoding="utf-8")

    assert map_documentation_files_to_contents(
        str(tmp_path), [str(invalid_document), str(valid_document)]
    ) == {"/valid.md": "# Valid guide"}
