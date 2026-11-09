"""Execute the self-review workflow's shell gates without GitHub credentials or model calls."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/pr-agent-review.yaml"
STEPS = {step["id"]: step for step in yaml.safe_load(WORKFLOW.read_text())["jobs"]["pr_agent_job"]["steps"]
         if "id" in step}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD")


def outputs(path):
    result = {}
    lines = iter(path.read_text().splitlines())
    for line in lines:
        if "<<" in line:
            key, delimiter = line.split("<<", 1)
            value = []
            for part in lines:
                if part == delimiter:
                    break
                value.append(part)
            result[key] = "\n".join(value)
        else:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def run_step(step, env, **extra):
    output = Path(env["RUNNER_TEMP"]) / f"{step}-output"
    output.write_text("")
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", STEPS[step]["run"]],
        cwd=env["GITHUB_WORKSPACE"], env={**env, **extra, "GITHUB_OUTPUT": str(output)},
        capture_output=True, text=True, timeout=90,
    )
    return result, outputs(output)


@pytest.fixture
def replay(tmp_path):
    missing = [tool for tool in ("bash", "git", "jq", "timeout") if not shutil.which(tool)]
    if missing:
        pytest.skip(f"Workflow shell tests require: {', '.join(missing)}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_temp = tmp_path / "temp"
    runner_temp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Only the API transport and Python environment installer are replaced. Git and the workflow shell are real.
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
endpoint = sys.argv[2]
with open(os.environ["API_CALLS"], "a") as log:
    log.write(endpoint + "\\n")
if endpoint.endswith("/permission"):
    print(os.environ.get("TEST_ROLE", "write"))
else:
    print((Path(os.environ["API_FIXTURES"]) / (endpoint.replace("/", "_") + ".json")).read_text())
''')
    gh.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(f"#!{sys.executable}\n" + '''import os, sys
args = sys.argv[sys.argv.index("python") + 1:]
os.execv(sys.executable, [sys.executable, *args])
''')
    uv.chmod(0o755)
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    builder = origin / "scripts/review_context"
    builder.mkdir(parents=True)
    (builder / "instructions.txt").write_text("Trusted reading instructions.\nSecond line.\n")
    (builder / "graph_bundle_ts.py").write_text('''import argparse, os
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument("--out-dir")
parser.add_argument("--pr")
args, _ = parser.parse_known_args()
out = Path(args.out_dir)
out.mkdir()
artifact = out / f"pr{args.pr}.G6.md"
mode = os.environ.get("TEST_BUILD", "success")
if mode == "failure":
    artifact.write_text("partial bundle")
    raise SystemExit(2)
if mode == "symlink":
    artifact.symlink_to(os.environ["OUTSIDE_FILE"])
elif mode == "directory":
    artifact.mkdir()
else:
    artifact.write_text("review bundle")
''')
    base = commit(origin)
    (builder / "graph_bundle_ts.py").write_text("raise RuntimeError('PR head must never execute')\n")
    (builder / "instructions.txt").write_text("Untrusted instructions\n")
    head = commit(origin)
    git(origin, "update-ref", "refs/pull/17/head", head)
    git(workspace, "clone", "-q", str(origin), "base")
    git(workspace / "base", "checkout", "-q", "--detach", base)
    api = tmp_path / "api"
    api.mkdir()
    pr = {"number": 17, "state": "open", "draft": False,
          "base": {"sha": base, "repo": {"full_name": "owner/repo"}}, "head": {"sha": head}}
    comment = {"body": "/review", "user": {"login": "maintainer", "type": "User"},
               "issue_url": "https://api.github.com/repos/owner/repo/issues/17"}
    issue = {"number": 17, "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/17"}}
    for endpoint, data in [("pulls_17", pr), ("issues_comments_23", comment), ("issues_17", issue)]:
        (api / f"repos_owner_repo_{endpoint}.json").write_text(json.dumps(data))
    event = workspace / "event.json"
    event.write_text(json.dumps({"pull_request": pr, "comment": comment, "issue": issue}))
    outside = tmp_path / "outside"
    outside.write_text("must remain untouched")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(runner_temp),
        "GITHUB_EVENT_PATH": str(event), "EVENT_NAME": "pull_request_target", "REPLAY_EVENT_FILE": "",
        "GH_REPO": "owner/repo", "API_FIXTURES": str(api), "API_CALLS": str(tmp_path / "api-calls"),
        "PR": "17", "BASE_SHA": base, "HEAD_SHA": head, "OUTSIDE_FILE": str(outside),
    }
    return env


@pytest.mark.parametrize("event", ["opened", "comment", "replay-pr", "replay-comment"])
def test_event_and_replay_attach_trusted_context(replay, event):
    if event.startswith("replay-"):
        result, data = run_step("replay", replay, PR_NUMBER="17" if event == "replay-pr" else "",
                                COMMENT_ID="23" if event == "replay-comment" else "")
        assert result.returncode == 0, result.stderr
        assert data["event_path"] == "/github/workspace/pr-agent-event.json"
        replay.update(EVENT_NAME=data["event_name"], REPLAY_EVENT_FILE=data["host_event_path"])
    elif event == "comment":
        replay["EVENT_NAME"] = "issue_comment"
    result, auth = run_step("authorize", replay)
    assert result.returncode == 0 and auth["allowed"] == "true"
    result, revision = run_step("context-revision", replay)
    assert result.returncode == 0, result.stderr
    assert revision == {"enabled": "true", "pr": "17", "base_sha": replay["BASE_SHA"],
                        "head_sha": replay["HEAD_SHA"]}
    result, context = run_step("context", replay)
    assert result.returncode == 0, result.stderr
    assert context == {"path": "/github/workspace/review-context/pr17.G6.md",
                       "instructions": "Trusted reading instructions.\nSecond line."}
    assert git(Path(replay["GITHUB_WORKSPACE"]) / "base", "rev-parse", "HEAD") == replay["BASE_SHA"]
    assert Path(replay["REPLAY_EVENT_FILE"] or replay["GITHUB_EVENT_PATH"]).is_file()


@pytest.mark.parametrize("command", ["/describe", "/improve", "/ask question", "/reviewer", "/review; touch /tmp/x"])
def test_other_commands_do_not_build_context(replay, command):
    event = Path(replay["GITHUB_EVENT_PATH"])
    data = json.loads(event.read_text())
    data["comment"]["body"] = command
    event.write_text(json.dumps(data))
    result, revision = run_step("context-revision", replay, EVENT_NAME="issue_comment")
    assert result.returncode == 0 and revision == {}
    assert not Path(replay["API_CALLS"]).exists()


@pytest.mark.parametrize("role", ["read", "triage", "none"])
def test_unauthorized_comments_cannot_reach_context(replay, role):
    result, auth = run_step("authorize", replay, EVENT_NAME="issue_comment", TEST_ROLE=role)
    assert result.returncode == 0 and auth["allowed"] == "false"
    assert STEPS["context-revision"]["if"] == "steps.authorize.outputs.allowed == 'true'"


@pytest.mark.parametrize("field,value", [("head", "x\npath=evil"), ("base", "--upload-pack=evil")])
def test_invalid_revision_cannot_inject_outputs(replay, field, value):
    event = Path(replay["GITHUB_EVENT_PATH"])
    data = json.loads(event.read_text())
    data["pull_request"][field]["sha"] = value
    event.write_text(json.dumps(data))
    result, revision = run_step("context-revision", replay)
    assert result.returncode != 0 and revision == {}


@pytest.mark.parametrize("plant", ["file", "directory", "symlink", "artifact-symlink"])
def test_stale_or_hostile_output_is_removed_before_build(replay, plant):
    out = Path(replay["GITHUB_WORKSPACE"]) / "review-context"
    outside = Path(replay["OUTSIDE_FILE"])
    if plant == "file":
        out.write_text("stale")
    elif plant == "symlink":
        out.symlink_to(outside.parent, target_is_directory=True)
    else:
        out.mkdir()
        if plant == "artifact-symlink":
            (out / "pr17.G6.md").symlink_to(outside)
        else:
            (out / "stale.md").write_text("stale")
    result, context = run_step("context", replay)
    assert result.returncode == 0 and context["path"].endswith("/pr17.G6.md"), result.stderr
    assert (out / "pr17.G6.md").read_text() == "review bundle"
    assert not out.is_symlink() and not (out / "pr17.G6.md").is_symlink()
    assert outside.read_text() == "must remain untouched"
    assert not (out / "stale.md").exists()


@pytest.mark.parametrize("failure", ["failure", "symlink", "directory", "sha-mismatch", "missing-builder"])
def test_build_failure_leaves_artifact_path_empty(replay, failure):
    if failure == "sha-mismatch":
        replay["HEAD_SHA"] = "0" * 40
    elif failure == "missing-builder":
        shutil.rmtree(Path(replay["GITHUB_WORKSPACE"]) / "base/scripts/review_context")
    result, context = run_step("context", replay, TEST_BUILD=failure)
    assert result.returncode != 0
    assert context.get("path", "") == ""


def test_workflow_preserves_trust_and_fallback_boundaries():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["pr_agent_job"]["steps"]
    checkout = STEPS["context-checkout"]
    assert checkout["with"]["ref"] == "${{ steps.context-revision.outputs.base_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["path"] == "base"
    assert checkout["with"]["fetch-depth"] == 0
    assert STEPS["context-uv"]["with"]["enable-cache"] is False
    for name in ("context-revision", "context-checkout", "context-uv", "context"):
        assert STEPS[name]["continue-on-error"] is True
    assert STEPS["context-checkout"]["if"] == "steps.context-revision.outputs.enabled == 'true'"
    assert STEPS["context-uv"]["if"] == "steps.context-checkout.outcome == 'success'"
    assert STEPS["context"]["if"] == "steps.context-uv.outcome == 'success'"
    assert set(STEPS["context"]["env"]) == {"PR", "BASE_SHA", "HEAD_SHA"}
    assert "tree-sitter-language-pack==1.22.1" in STEPS["context"]["run"]
    assert not any(step.get("uses", "").startswith("actions/cache@") for step in steps)
    assert steps.index(STEPS["context"]) < steps.index(STEPS["app-token"])
    action = next(step for step in steps if step["name"] == "Run sponsored PR-Agent")
    assert action["if"] == "steps.authorize.outputs.allowed == 'true'"
    assert action["env"]["ARTIFACT_PATH"] == (
        "${{ steps.context.outcome == 'success' && steps.context.outputs.path || '' }}"
    )
    assert action["env"]["ARTIFACTS__TARGET_TOOLS"] == '["pr_reviewer"]'
    for tool in ("DESCRIBE", "REVIEW", "IMPROVE"):
        assert action["env"][f"GITHUB_ACTION_CONFIG__AUTO_{tool}"] == "true"
