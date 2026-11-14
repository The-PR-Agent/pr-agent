import importlib.util
import pathlib
import sys

import pytest

_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "review_eval"


def _load(name):
    sys.path.insert(0, str(_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"review_eval_{name}", _DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_DIR))


run_eval = _load("run_eval")
judge = _load("judge")


def test_overrides_accept_review_settings_and_upper_case_them():
    assert run_eval.parse_overrides(["pr_reviewer__num_max_findings=4", "CONFIG__MAX_OUTPUT_TOKENS=16384"]) == {
        "PR_REVIEWER__NUM_MAX_FINDINGS": "4", "CONFIG__MAX_OUTPUT_TOKENS": "16384"}


@pytest.mark.parametrize("item", ["openai__api_base=http://evil", "OPENAI__KEY=x", "config__model=x",
                                  "litellm__extra_headers={}", "CONFIG__HTTPS_PROXY=x", "PR_REVIEWER", "noseparator=1"])
def test_overrides_reject_credentials_endpoints_and_other_sections(item):
    with pytest.raises(SystemExit):
        run_eval.parse_overrides([item])


def test_lookup_never_raises_on_malformed_requests(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert judge.lookup(tmp_path, {"kind": "read", "path": "a.py", "start": 1, "end": 1e999}).startswith("invalid")
    assert judge.lookup(tmp_path, {"kind": "grep", "text": "a\x00b"}).startswith("invalid")
    assert judge.lookup(tmp_path, {"kind": "nope"}) == "invalid lookup"


def test_lookup_stays_inside_the_checkout(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    root = tmp_path / "wt"
    root.mkdir()
    (root / "link.txt").symlink_to(outside)
    for path in ("link.txt", "../secret.txt", str(outside)):
        assert judge.lookup(root, {"kind": "read", "path": path}).endswith("not found")
    (root / "ok.py").write_text("a\nb\n")
    assert judge.lookup(root, {"kind": "read", "path": "ok.py", "start": 2, "end": 2}) == "read ok.py [2-2]\n2: b"
