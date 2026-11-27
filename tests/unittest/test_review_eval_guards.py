import importlib.util
import json
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


@pytest.mark.parametrize("reply", ['{"verdicts": null}', '{"verdicts": [{"label": ["a"], "verdict": "caught"}]}'])
def test_catch_judge_retries_a_malformed_reply(monkeypatch, reply):
    replies = iter([reply, '{"verdicts": [{"label": "a", "verdict": "caught"}, {"label": "b", "verdict": "partial"}]}'])
    monkeypatch.setattr(judge, "complete", lambda *a, **k: (next(replies), {"input": 1, "output": 1}))
    assert judge.judge_catch("openai/m", KNOWN, "review")[0] == {"a": "caught", "b": "partial"}


FOUND = [("t0", "b0"), ("t1", "b1")]


def test_precision_judge_retries_an_incomplete_reply(monkeypatch, tmp_path):
    replies = iter(['{"lookups": []}', '{"verdicts": null}', '{"verdicts": [{"id": 0, "valid": "yes"}]}',
                    '{"verdicts": [{"id": 0, "valid": "Yes"}, {"id": 1, "valid": "no"}]}'])
    monkeypatch.setattr(judge, "complete", lambda *a, **k: (next(replies), {"input": 1, "output": 1}))
    verdicts, usage = judge.judge_precision("openai/m", "ctx", FOUND, tmp_path)
    assert verdicts == ["yes", "no"] and len(usage) == 4


def test_precision_judge_fails_instead_of_caching_unsure(monkeypatch, tmp_path):
    monkeypatch.setattr(judge, "complete", lambda *a, **k: ('{"verdicts": [{"id": 0, "valid": "maybe"}]}',
                                                            {"input": 1, "output": 1}))
    with pytest.raises(RuntimeError):
        judge.judge_precision("openai/m", "ctx", FOUND, tmp_path)


@pytest.mark.parametrize("kind", ["catch", "precision"])
def test_judge_errors_are_recorded_for_a_retry_without_the_provider_message(monkeypatch, tmp_path, kind):
    class ProviderError(Exception):
        pass

    def boom(*a, **k):
        raise ProviderError("request body with secrets")

    monkeypatch.setattr(run_eval.judge, "judge_catch", boom)
    monkeypatch.setattr(run_eval.judge, "judge_precision", boom)
    monkeypatch.setattr(run_eval, "context", lambda case, wt: "ctx")
    (tmp_path / "status.json").write_text('{"ok": true}')
    (tmp_path / "review.json").write_text('{"review": {"key_issues_to_review": [{"relevant_file": "a.py", '
                                          '"issue_header": "H", "issue_content": "c"}]}}')
    case = {"kind": kind, "known_findings": KNOWN}
    res = run_eval.judge_run(case, tmp_path, tmp_path, "openai/m")
    assert res["judge_error"] == "ProviderError" and "secrets" not in (tmp_path / "judged.json").read_text()


def test_child_env_passes_a_key_alias_under_the_name_pr_agent_reads(monkeypatch):
    for name in run_eval.SECRET_ENV["openai"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123456")
    env = run_eval.child_env("openai/m", {})
    assert env["OPENAI__KEY"] == "sk-test-123456" and "OPENAI_API_KEY" not in env


def test_redact_hides_key_values():
    assert run_eval.redact("error sk-test-123456 here", {"OPENAI__KEY": "sk-test-123456"}) == "error [redacted] here"


def test_failed_run_status_carries_a_redacted_tail_without_a_split_key(monkeypatch, tmp_path):
    key = "sk-test-0123456789abcdef"
    stderr = "x" + key + "y" * (run_eval.STDERR_TAIL - 10)  # the key straddles the tail's cut

    def replay(cmd, cwd, env, **k):
        (pathlib.Path(env["REVIEW_EVAL_RUN_DIR"]) / "status.json").write_text('{"ok": false, "error": "e"}')
        return type("P", (), {"stderr": stderr})()

    monkeypatch.setattr(run_eval.subprocess, "run", replay)
    case = {"key": "k", "title": "t", "branch": "b", "description": "d", "tickets": []}
    run = run_eval.review(case, 1, tmp_path, tmp_path, {"OPENAI__KEY": key})
    tail = json.loads((run / "status.json").read_text())["stderr_tail"]
    assert key[-8:] not in tail and len(tail) <= run_eval.STDERR_TAIL


def test_base_branches_are_namespaced_by_out(tmp_path):
    a, b = run_eval.base_branch(tmp_path / "a", "k"), run_eval.base_branch(tmp_path / "b", "k")
    assert a != b and a.startswith(run_eval.BRANCH_PREFIX) and a.endswith("-k")


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
