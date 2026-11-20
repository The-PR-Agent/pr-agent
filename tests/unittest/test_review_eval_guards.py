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


KNOWN = [{"label": "a", "finding": "f"}, {"label": "b", "finding": "g"}]


def test_catch_judge_retries_an_incomplete_reply(monkeypatch):
    replies = iter(['{"verdicts": [{"label": "a", "verdict": "caught"}]}',
                    '{"verdicts": [{"label": "a", "verdict": "Caught"}, {"label": "b", "verdict": "missed"}]}'])
    monkeypatch.setattr(judge, "complete", lambda *a, **k: (next(replies), {"input": 1, "output": 1}))
    verdicts, usage = judge.judge_catch("openai/m", KNOWN, "review")
    assert verdicts == {"a": "caught", "b": "missed"} and len(usage) == 2


def test_catch_judge_fails_instead_of_caching_missed(monkeypatch):
    monkeypatch.setattr(judge, "complete", lambda *a, **k: ("not json", {"input": 1, "output": 1}))
    with pytest.raises(RuntimeError):
        judge.judge_catch("openai/m", KNOWN, "review")


def test_cache_refuses_other_review_inputs_and_drops_verdicts_of_another_judge(tmp_path):
    verdict = tmp_path / "runs" / "k" / "r1" / "judged.json"
    verdict.parent.mkdir(parents=True)
    verdict.write_text("{}")
    run_eval.check_cache(tmp_path, "rev1", "judge1")
    run_eval.check_cache(tmp_path, "rev1", "judge1")
    assert verdict.exists()
    run_eval.check_cache(tmp_path, "rev1", "judge2")
    assert not verdict.exists()
    with pytest.raises(SystemExit):
        run_eval.check_cache(tmp_path, "rev2", "judge2")


overlay = _load("overlay_pr")


@pytest.mark.parametrize("path,ok", [("pr_agent/settings/pr_reviewer_prompts.toml", True),
                                     ("pr_agent/settings/code_suggestions/pr_code_suggestions_prompts.toml", True),
                                     ("pr_agent/settings/configuration.toml", False),
                                     ("pr_agent/settings/../settings/pr_reviewer_prompts.toml", False),
                                     ("pr_agent/settings/a/b/x_prompts.toml", False),
                                     ("pr_agent/commands/pr_reviewer.py", False)])
def test_overlay_only_takes_prompt_files(path, ok):
    assert overlay.is_prompt_file(path) is ok


def test_overlay_refuses_new_tables_keys_and_non_strings():
    old = {"pr_review_prompt": {"system": "a", "user": "b"}}
    assert overlay.same_shape({"pr_review_prompt": {"system": "changed"}}, old)
    assert not overlay.same_shape({"pr_review_prompt": {"system": "a"}, "openai": {"api_base": "http://x"}}, old)
    assert not overlay.same_shape({"pr_review_prompt": {"system": "a", "api_base": "http://x"}}, old)
    assert not overlay.same_shape({"pr_review_prompt": {"system": 1}}, old)


@pytest.mark.parametrize("value", ["@format {env[OPENAI__KEY]}", "  @jinja {{ env.OPENAI__KEY }}", "@get openai.key"])
def test_overlay_refuses_dynaconf_tokens(value):
    assert not overlay.same_shape({"pr_review_prompt": {"system": value}}, {"pr_review_prompt": {"system": "a"}})
