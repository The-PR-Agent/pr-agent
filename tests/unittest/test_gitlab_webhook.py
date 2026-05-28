import importlib
import os

import pytest


@pytest.fixture(scope="module", autouse=True)
def _set_gitlab_url():
    os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")
    yield


def _load_module():
    import pr_agent.servers.gitlab_webhook as gitlab_webhook

    return importlib.reload(gitlab_webhook)


def _base_payload(position):
    return {
        "object_attributes": {
            "position": position,
            "discussion_id": "discussion-1",
        }
    }


@pytest.mark.parametrize(
    "line_range_type,expected_side,start_key,end_key,path_key",
    [
        ("new", "RIGHT", "new_line", "new_line", "new_path"),
        ("old", "LEFT", "old_line", "old_line", "old_path"),
    ],
)
def test_handle_ask_line_line_range(line_range_type, expected_side, start_key, end_key, path_key):
    module = _load_module()
    position = {
        "line_range": {
            "start": {start_key: 10, "type": line_range_type},
            "end": {end_key: 12, "type": line_range_type},
        },
        "new_path": "src/new.py",
        "old_path": "src/old.py",
    }
    payload = _base_payload(position)

    result = module.handle_ask_line("/ask what is this?", payload)

    assert f"--line_start=10" in result
    assert f"--line_end=12" in result
    assert f"--side={expected_side}" in result
    assert f"--file_name={position[path_key]}" in result


@pytest.mark.parametrize(
    "position,expected_side,expected_path",
    [
        ({"new_line": 5, "new_path": "src/new.py"}, "RIGHT", "src/new.py"),
        ({"old_line": 7, "old_path": "src/old.py"}, "LEFT", "src/old.py"),
    ],
)
def test_handle_ask_line_single_line(position, expected_side, expected_path):
    module = _load_module()
    payload = _base_payload(position)

    result = module.handle_ask_line("/ask explain", payload)

    assert "--line_start" in result
    assert "--line_end" in result
    assert f"--side={expected_side}" in result
    assert f"--file_name={expected_path}" in result
