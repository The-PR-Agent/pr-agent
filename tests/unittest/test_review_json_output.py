import json
import os
import stat

import pytest

from pr_agent.algo import review_json_output


def test_serialization_failure_preserves_existing_review(tmp_path):
    output = tmp_path / "review.json"
    original = '{"previous": true}\n'
    output.write_text(original, encoding="utf-8")

    with pytest.raises(TypeError):
        review_json_output.write_review_json_output({"first": "serializable", "later": object()}, output)

    assert output.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [output]


def test_replace_failure_preserves_existing_review_and_cleans_temporary_file(monkeypatch, tmp_path):
    output = tmp_path / "review.json"
    output.write_text('{"previous": true}\n', encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        review_json_output.write_review_json_output({"new": True}, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}
    assert list(tmp_path.iterdir()) == [output]


def test_successful_review_write_keeps_format_and_existing_permissions(tmp_path):
    output = tmp_path / "review.json"
    output.write_text("previous", encoding="utf-8")
    output.chmod(0o640)

    review_json_output.write_review_json_output({"summary": "你好"}, output)

    assert output.read_text(encoding="utf-8") == json.dumps({"summary": "你好"}, indent=2) + "\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation may require elevated Windows privileges")
def test_write_through_existing_symlink_preserves_the_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("previous", encoding="utf-8")
    output = tmp_path / "review.json"
    output.symlink_to(target)

    review_json_output.write_review_json_output({"new": True}, output)

    assert output.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_new_output_is_complete_and_no_temporary_file_remains(tmp_path):
    output = tmp_path / "review.json"

    review_json_output.write_review_json_output({"new": [1, 2]}, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"new": [1, 2]}
    assert list(tmp_path.iterdir()) == [output]


def test_write_failure_preserves_existing_review_and_cleans_temporary_file(monkeypatch, tmp_path):
    output = tmp_path / "review.json"
    output.write_text('{"previous": true}\n', encoding="utf-8")

    def fail_dump(review, stream, **kwargs):
        stream.write('{"partial":')
        raise OSError("synthetic disk full")

    monkeypatch.setattr(json, "dump", fail_dump)
    with pytest.raises(OSError, match="synthetic disk full"):
        review_json_output.write_review_json_output({"new": True}, output)

    assert json.loads(output.read_text(encoding="utf-8")) == {"previous": True}
    assert list(tmp_path.iterdir()) == [output]
