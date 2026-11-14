#!/usr/bin/env python3
"""Review-quality eval: replay /review on frozen PR commits, judge the output, compare with a baseline.

Catch cases carry known findings (real issues a maintainer raised) and measure how many the review raises; precision
cases measure how many posted findings a code-reading judge confirms. Each case runs `--repeats` times because model
output varies run to run. The exit code is 1 when the gate fails (see score.gate), so a pipeline can block on it.

Example (from the repository root, with the model's key in the environment, e.g. ANTHROPIC__KEY):
  uv run python scripts/review_eval/run_eval.py --model anthropic/claude-sonnet-5-5 \\
      --judge-model anthropic/claude-sonnet-5-5 --repeats 2 --out /tmp/review-eval
Runs are cached in --out, so an interrupted eval resumes. Use --write-baseline to record an accepted run.
"""
import argparse
import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import judge  # noqa: E402
import score  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
REPLAY = HERE / "replay.py"
BRANCH_PREFIX = "review-eval-base-"
MAX_DIFF = 60_000
PASS_ENV = ("PATH", "HOME", "TMPDIR", "SSL_CERT_FILE")
SECRET_ENV = {"anthropic": ("ANTHROPIC__KEY",), "openai": ("OPENAI__KEY", "OPENAI_KEY")}
OVERRIDE_SECTIONS = {"CONFIG", "PR_REVIEWER"}
OVERRIDE_DENY = {"KEY", "KEYS", "TOKEN", "SECRET", "PASSWORD", "BASE", "URL", "PROXY", "HEADER", "HEADERS", "TYPE",
                 "ENDPOINT", "MODEL", "MODELS", "CERT", "SSL", "BUNDLE", "HOST"}


def git(*args, cwd=REPO, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout


def has(sha):
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=REPO, capture_output=True).returncode == 0


def prepare(case, out, remote):
    """A detached worktree at the reviewed commit and a local branch at its merge base (the local provider diffs
    against merge-base(target, HEAD))."""
    for sha in (case["reviewed_sha"], case["merge_base"]):
        if not has(sha):
            git("fetch", "-q", remote, sha)
    wt = out / "wt" / case["key"]
    if not wt.exists():
        git("worktree", "add", "-q", "--detach", str(wt), case["reviewed_sha"])
    git("branch", "-f", BRANCH_PREFIX + case["key"], case["merge_base"])
    return wt


def cleanup(out, cases):
    for case in cases:
        wt = out / "wt" / case["key"]
        if wt.exists():
            git("worktree", "remove", "--force", str(wt), check=False)
        git("branch", "-D", BRANCH_PREFIX + case["key"], check=False)


def parse_overrides(items):
    """SECTION__KEY=VALUE review settings. Dynaconf reads env names case-insensitively, so keys are upper-cased and
    checked against an allowlist of sections and a denylist of credential and endpoint words."""
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip().upper()
        section = key.split("__", 1)[0]
        words = set(key.replace("__", "_").split("_"))
        if not sep or "__" not in key or section not in OVERRIDE_SECTIONS or words & OVERRIDE_DENY:
            raise SystemExit(f"--set {key or item!r}: only review settings ({', '.join(sorted(OVERRIDE_SECTIONS))}"
                             "), never keys, tokens or endpoints")
        out[key] = value
    return out


def child_env(model, overrides):
    """Allowlist: a few process vars, the model settings, SECTION__KEY overrides, and the model provider's key."""
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env.update({"PYTHONPATH": str(REPO), "CONFIG__MODEL": model, "CONFIG__MODEL_WEAK": model,
                "CONFIG__MODEL_REASONING": model, "CONFIG__FALLBACK_MODELS": "[]"})
    env.update(overrides)
    provider = model.split("/", 1)[0] if "/" in model else "openai"
    keys = [k for k in SECRET_ENV.get(provider, ()) if os.environ.get(k)]
    if not keys:
        raise SystemExit(f"no API key in the environment for model {model!r} (need one of {SECRET_ENV.get(provider)})")
    env.update({k: os.environ[k] for k in keys})
    return env


def review(case, rep, out, wt, env):
    run = out / "runs" / case["key"] / f"r{rep}"
    status = run / "status.json"
    if status.exists() and json.loads(status.read_text())["ok"]:
        return run
    run.mkdir(parents=True, exist_ok=True)
    (run / "final.md").unlink(missing_ok=True)
    (run / "case.json").write_text(json.dumps({k: case[k] for k in ("title", "branch", "description", "tickets")}))
    env = {**env, "REVIEW_EVAL_RUN_DIR": str(run), "REVIEW_EVAL_CASE": str(run / "case.json"),
           "REVIEW_EVAL_TARGET_BRANCH": BRANCH_PREFIX + case["key"]}
    try:
        p = subprocess.run([sys.executable, str(REPLAY)], cwd=wt, env=env, capture_output=True, text=True,
                           timeout=900)
        (run / "stderr.log").write_text(p.stderr[-20000:])
    except subprocess.TimeoutExpired:
        status.write_text(json.dumps({"ok": False, "error": "timeout", "usage": [], "finish": []}))
    if not status.exists():
        status.write_text(json.dumps({"ok": False, "error": "replay crashed, see stderr.log", "usage": [],
                                      "finish": []}))
    return run


def context(case, wt):
    diff = git("diff", case["merge_base"], case["reviewed_sha"], cwd=wt)
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n... (diff truncated)"
    return f"PR title: {case['title']}\n\nPR description:\n{case['description']}\n\nPR diff:\n{diff}"


def judge_run(case, run, wt, judge_model):
    """Cached judge verdicts for one run: catch verdicts by two judges, or a precision verdict per finding."""
    f = run / "judged.json"
    if f.exists():
        return json.loads(f.read_text())
    status = json.loads((run / "status.json").read_text())
    text = (run / "final.md").read_text(errors="replace") if status["ok"] else ""
    text = text.split(score.FINDINGS_END, 1)[0]
    found = score.findings(text)
    res = {"findings": len(found), "usage": []}
    if case["kind"] == "catch":
        known = case["known_findings"]
        if text:
            a, ua = judge.judge_catch(judge_model, known, text)
            b, ub = judge.judge_catch(judge_model, known, text, reverse=True)
            res["usage"] += ua + ub
        else:
            a = b = {k["label"]: "missed" for k in known}
        res["catch"] = {k["label"]: [a[k["label"]], b[k["label"]]] for k in known}
    elif found:
        res["valid"], u = judge.judge_precision(judge_model, context(case, wt), found, wt)
        res["usage"] += u
    else:
        res["valid"] = []
    f.write_text(json.dumps(res, indent=1))
    return res


def metrics(cases, out, repeats, judge_model):
    runs = failed = capped = posted = valid = 0
    catch_by_rep = [0.0] * repeats
    per_case = {}
    tokens = {"review_in": 0, "review_out": 0, "judge_in": 0, "judge_out": 0}
    for case in cases:
        for rep in range(1, repeats + 1):
            run = out / "runs" / case["key"] / f"r{rep}"
            st = json.loads((run / "status.json").read_text())
            jd = json.loads((run / "judged.json").read_text())
            runs += 1
            failed += not st["ok"]
            capped += "length" in st.get("finish", [])
            tokens["review_in"] += sum(u["input"] for u in st["usage"])
            tokens["review_out"] += sum(u["output"] for u in st["usage"])
            tokens["judge_in"] += sum(u["input"] for u in jd["usage"])
            tokens["judge_out"] += sum(u["output"] for u in jd["usage"])
            if case["kind"] == "catch":
                pts = sum(score.finding_points(*v) for v in jd["catch"].values())
                catch_by_rep[rep - 1] += pts
                per_case.setdefault(case["key"], []).append(pts)
            else:
                posted += jd["findings"]
                valid += sum(v == "yes" for v in jd["valid"])
    n_findings = sum(len(c["known_findings"]) for c in cases if c["kind"] == "catch")
    n_precision = sum(c["kind"] == "precision" for c in cases)
    return {"runs": runs, "failed_runs": failed, "capped_runs": capped, "known_findings": n_findings,
            "catch": sum(catch_by_rep) / repeats, "catch_by_repeat": catch_by_rep,
            "catch_per_case": {k: sum(v) / len(v) for k, v in per_case.items()},
            "posted": posted / repeats, "valid": valid / repeats,
            "findings_per_run": posted / max(n_precision * repeats, 1), "judge_model": judge_model, "tokens": tokens}


def summary(m, failures, notes, args):
    precision = "none posted"
    if m["posted"]:
        precision = f"{m['valid']:.1f} of {m['posted']:.1f} ({m['valid'] / m['posted']:.0%})"
    lines = [f"## Review-quality eval: {'FAIL' if failures else 'pass'}", "",
             f"Model `{args.model}`, judge `{args.judge_model}`, {args.repeats} repeats, {m['runs']} runs.", "",
             "| Metric | Value |", "|---|---|",
             f"| Known findings caught | {m['catch']:.2f} of {m['known_findings']} (per repeat: "
             f"{', '.join(f'{x:.2f}' for x in m['catch_by_repeat'])}) |",
             f"| Valid of posted findings, recent PRs (per repeat) | {precision} |",
             f"| Findings per recent PR | {m['findings_per_run']:.2f} |",
             f"| Failed runs / output-cap hits | {m['failed_runs']} / {m['capped_runs']} |",
             f"| Review tokens in / out | {m['tokens']['review_in']:,} / {m['tokens']['review_out']:,} |",
             f"| Judge tokens in / out | {m['tokens']['judge_in']:,} / {m['tokens']['judge_out']:,} |", ""]
    lines += [f"- FAIL: {f}" for f in failures] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="model under test, with provider prefix")
    ap.add_argument("--judge-model", required=True, help="keep it fixed across runs so scores stay comparable")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--cases", type=pathlib.Path, default=HERE / "cases.json")
    ap.add_argument("--baseline", type=pathlib.Path, default=HERE / "baseline.json")
    ap.add_argument("--write-baseline", type=pathlib.Path)
    ap.add_argument("--limit", type=int, help="first N cases of each kind only (smoke test)")
    ap.add_argument("--jobs", type=int, default=1, help="parallel runs")
    ap.add_argument("--remote", default="origin", help="remote to fetch missing commits from")
    ap.add_argument("--set", action="append", default=[], metavar="SECTION__KEY=VALUE",
                    help="pr-agent setting override for the reviewed runs, repeatable")
    ap.add_argument("--keep-worktrees", action="store_true")
    args = ap.parse_args()

    cases = json.loads(args.cases.read_text())["cases"]
    if args.limit:
        cases = [c for c in cases if c["kind"] == "catch"][:args.limit] + \
                [c for c in cases if c["kind"] == "precision"][:args.limit]
    overrides = parse_overrides(args.set)
    env = child_env(args.model, overrides)
    judge.warm_up(args.judge_model)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        wts = {c["key"]: prepare(c, args.out, args.remote) for c in cases}
        jobs = [(c, r) for c in cases for r in range(1, args.repeats + 1)]
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            def one(job):
                c, r = job
                run = review(c, r, args.out, wts[c["key"]], env)
                judge_run(c, run, wts[c["key"]], args.judge_model)
                print(f"{c['key']} r{r} done", flush=True)
            list(pool.map(one, jobs))
        m = metrics(cases, args.out, args.repeats, args.judge_model)
    finally:
        if not args.keep_worktrees:
            cleanup(args.out, cases)
    baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() and not args.limit else None
    failures, notes = score.gate(m, baseline, args.repeats)
    text = summary(m, failures, notes, args)
    (args.out / "metrics.json").write_text(json.dumps(m, indent=1))
    (args.out / "summary.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text)
    print(text)
    if args.write_baseline:
        args.write_baseline.write_text(json.dumps({**m, "model": args.model, "repeats": args.repeats}, indent=1) + "\n")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
