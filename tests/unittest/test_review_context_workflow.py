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
JOBS = yaml.safe_load(WORKFLOW.read_text())["jobs"]
STEPS = {step["id"]: step for job in JOBS.values() for step in job["steps"] if "id" in step}
BUILD_WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/build-and-test.yaml").read_text())
HARDENING_JOB = BUILD_WORKFLOW["jobs"]["review-context-hardening"]
HARDENING_STEPS = {step["id"]: step for step in HARDENING_JOB["steps"] if "id" in step}


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


def run_step(step, env, *, steps=STEPS, **extra):
    output = Path(env["RUNNER_TEMP"]) / f"{step}-output"
    output.write_text("")
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", steps[step]["run"]],
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
    python = runner_temp / "review-context-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
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
if mode == "inject":
    Path(os.environ["GITHUB_ENV"]).write_text("OPENAI_KEY=attacker\\n")
    Path(os.environ["GITHUB_OUTPUT"]).write_text("path=evil\\ninstructions=evil\\nallowed=true\\n")
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
    default = commit(origin)
    (builder / "graph_bundle_ts.py").write_text("raise RuntimeError('PR head must never execute')\n")
    (builder / "instructions.txt").write_text("Untrusted instructions\n")
    base = commit(origin)
    (builder / "instructions.txt").write_text("PR head instructions\n")
    head = commit(origin)
    git(origin, "update-ref", "refs/pull/17/head", head)
    git(workspace, "clone", "-q", str(origin), "tooling")
    git(workspace / "tooling", "checkout", "-q", "--detach", default)
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
        "DEFAULT_SHA": default, "PR": "17", "BASE_SHA": base, "HEAD_SHA": head, "OUTSIDE_FILE": str(outside),
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
    result, tooling = run_step("context-tooling", replay)
    assert result.returncode == 0, result.stderr
    assert tooling == {"sha": replay["DEFAULT_SHA"], "instructions": "Trusted reading instructions.\nSecond line."}
    result, context = run_step("context", replay)
    assert result.returncode == 0 and context == {}, result.stderr
    result, artifact = run_step("context-artifact", replay)
    assert result.returncode == 0, result.stderr
    assert artifact == {"path": "/github/workspace/review-context/bundle.md"}
    assert git(Path(replay["GITHUB_WORKSPACE"]) / "tooling", "rev-parse", "HEAD") == replay["DEFAULT_SHA"]
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
    assert result.returncode == 0 and context == {}, result.stderr
    assert (out / "bundle.md").read_text() == "review bundle"
    assert (out / "pr17.G6.md").read_text() == "review bundle"
    assert not out.is_symlink() and not (out / "pr17.G6.md").is_symlink()
    assert outside.read_text() == "must remain untouched"
    assert not (out / "stale.md").exists()


@pytest.mark.parametrize("failure", ["failure", "symlink", "directory", "sha-mismatch", "missing-builder"])
def test_build_failure_leaves_artifact_path_empty(replay, failure):
    if failure == "sha-mismatch":
        replay["HEAD_SHA"] = "0" * 40
    elif failure == "missing-builder":
        shutil.rmtree(Path(replay["GITHUB_WORKSPACE"]) / "tooling/scripts/review_context")
    result, context = run_step("context", replay, TEST_BUILD=failure)
    assert result.returncode != 0
    assert context.get("path", "") == ""


@pytest.mark.parametrize("plant", ["missing", "empty", "directory", "symlink", "parent-symlink", "oversized"])
def test_downloaded_artifact_is_validated_before_use(replay, plant):
    out = Path(replay["GITHUB_WORKSPACE"]) / "review-context"
    out.mkdir()
    bundle = out / "bundle.md"
    if plant == "empty":
        bundle.touch()
    elif plant == "directory":
        bundle.mkdir()
    elif plant == "symlink":
        bundle.symlink_to(replay["OUTSIDE_FILE"])
    elif plant == "parent-symlink":
        out.rmdir()
        out.symlink_to(Path(replay["OUTSIDE_FILE"]).parent, target_is_directory=True)
    elif plant == "oversized":
        bundle.write_bytes(b"x" * 200001)
    result, artifact = run_step("context-artifact", replay)
    assert result.returncode != 0 and artifact == {}


def test_artifact_content_cannot_set_keyed_environment_or_outputs(replay):
    out = Path(replay["GITHUB_WORKSPACE"]) / "review-context"
    out.mkdir()
    env_file = Path(replay["RUNNER_TEMP"]) / "keyed-env"
    env_file.touch()
    (out / "bundle.md").write_text("GITHUB_TOKEN=attacker\npath=evil\n::set-env name=OPENAI_KEY::evil\n")
    result, artifact = run_step("context-artifact", replay, GITHUB_ENV=str(env_file))
    assert result.returncode == 0
    assert artifact == {"path": "/github/workspace/review-context/bundle.md"}
    assert env_file.read_text() == ""


def test_parser_command_files_do_not_cross_job_boundary(replay, tmp_path):
    parser_env = tmp_path / "parser-env"
    result, injected = run_step("context", replay, TEST_BUILD="inject", GITHUB_ENV=str(parser_env))
    assert result.returncode == 0, result.stderr
    assert injected == {"path": "evil", "instructions": "evil", "allowed": "true"}
    assert parser_env.read_text() == "OPENAI_KEY=attacker\n"
    # Transfer only the uploaded file to the fresh keyed runner, as the workflow does.
    workspace = tmp_path / "keyed-workspace"
    out = workspace / "review-context"
    out.mkdir(parents=True)
    shutil.copyfile(Path(replay["GITHUB_WORKSPACE"]) / "review-context/bundle.md", out / "bundle.md")
    keyed_env = tmp_path / "keyed-env"
    keyed_env.touch()
    result, artifact = run_step("context-artifact", {**replay, "GITHUB_WORKSPACE": str(workspace)},
                                GITHUB_ENV=str(keyed_env))
    assert result.returncode == 0
    assert artifact == {"path": "/github/workspace/review-context/bundle.md"}
    assert keyed_env.read_text() == ""


@pytest.mark.parametrize("step,steps,tooling_path", [
    ("context-dependencies", STEPS, "tooling/scripts/review_context"),
    ("hardening-dependencies", HARDENING_STEPS, "scripts/review_context"),
])
def test_grammar_checksum_failure_prevents_extraction(replay, step, steps, tooling_path):
    bin_dir = Path(replay["PATH"].split(os.pathsep)[0])
    for name, script in {
        "uv": "exit 0",
        "curl": 'printf tampered > parsers-linux-x86_64.tar.zst',
        "tar": 'touch "$RUNNER_TEMP/extracted"',
    }.items():
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\n" + script + "\n")
        tool.chmod(0o755)
    tooling = Path(replay["GITHUB_WORKSPACE"]) / tooling_path
    tooling.mkdir(parents=True, exist_ok=True)
    (tooling / "grammars.sha256").write_text((ROOT / "scripts/review_context/grammars.sha256").read_text())
    result, _ = run_step(step, replay, steps=steps)
    assert result.returncode != 0
    assert "FAILED" in result.stdout
    assert not (Path(replay["RUNNER_TEMP"]) / "extracted").exists()


def test_hardening_runs_on_pull_requests_with_verified_grammars():
    # PyYAML interprets the workflow's unquoted "on" as True.
    assert BUILD_WORKFLOW[True]["pull_request"]["branches"] == ["main"]
    assert HARDENING_JOB["permissions"] == {"contents": "read"}
    assert HARDENING_JOB["runs-on"] == "ubuntu-latest"
    assert "secrets." not in json.dumps(HARDENING_JOB)
    assert "if" not in HARDENING_JOB and "continue-on-error" not in HARDENING_JOB
    for step in HARDENING_JOB["steps"]:
        assert "if" not in step and "continue-on-error" not in step
    checkout = HARDENING_JOB["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}  # exercise the PR's code with no saved token
    deps = HARDENING_STEPS["hardening-dependencies"]["run"]
    assert "uv sync --frozen --python 3.12" in deps
    assert "uv pip install --python .venv/bin/python" in deps
    assert "--require-hashes --only-binary :all:" in deps
    assert "/v1.22.1/parsers-linux-x86_64.tar.zst" in deps
    assert deps.index("sha256sum --check") < deps.index("tar --zstd")
    test = HARDENING_STEPS["hardening-test"]
    assert test["env"] == {"GRAMMAR_CACHE": "${{ runner.temp }}/review-context-grammars", "PYTHONPATH": "."}
    assert test["run"] == "uv run --frozen --python 3.12 pytest scripts/review_context -q"
    steps = HARDENING_JOB["steps"]
    assert steps.index(HARDENING_STEPS["hardening-dependencies"]) < steps.index(test)


def test_workflow_preserves_trust_and_fallback_boundaries():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert workflow["permissions"] == {"contents": "read"}
    prepare, build, keyed = (JOBS[name] for name in ("prepare_review", "review_context", "pr_agent_job"))
    assert build["permissions"] == {"contents": "read"}
    assert "outputs" not in build
    assert "secrets." not in json.dumps(prepare)
    assert "secrets." not in json.dumps(build)
    assert "app-token" not in {step.get("id") for step in build["steps"]}
    assert build["needs"] == "prepare_review"
    assert build["if"] == ("needs.prepare_review.outputs.allowed == 'true' && "
                           "needs.prepare_review.outputs.tooling_sha != ''")
    assert keyed["needs"] == ["prepare_review", "review_context"]
    assert "always()" in keyed["if"] and "!cancelled()" in keyed["if"]
    assert "needs.prepare_review.result == 'success'" in keyed["if"]
    assert "needs.prepare_review.outputs.allowed == 'true'" in keyed["if"]
    assert "needs.review_context.result" not in keyed["if"]  # a build failure still runs diff-only review
    replay_download = next(step for step in keyed["steps"] if step["name"] == "Load manual replay event")
    assert replay_download["with"]["artifact-ids"] == "${{ needs.prepare_review.outputs.event_artifact_id }}"
    assert prepare["outputs"]["event_artifact_id"] == "${{ steps.replay-upload.outputs.artifact-id }}"
    assert STEPS["tooling-checkout"]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert STEPS["context-checkout"]["with"]["ref"] == "${{ needs.prepare_review.outputs.tooling_sha }}"
    assert prepare["outputs"]["tooling_sha"] == "${{ steps.context-tooling.outputs.sha }}"
    for step in (STEPS["tooling-checkout"], STEPS["context-checkout"]):
        assert step["with"]["persist-credentials"] is False
        assert step["with"]["path"] == "tooling"
    assert STEPS["context-checkout"]["with"]["fetch-depth"] == 0
    assert STEPS["context-uv"]["with"]["enable-cache"] is False
    assert set(STEPS["context"]["env"]) == {"PR", "BASE_SHA", "HEAD_SHA"}
    assert "GITHUB_OUTPUT" not in STEPS["context"]["run"]
    assert "GITHUB_ENV" not in STEPS["context"]["run"]
    deps = STEPS["context-dependencies"]["run"]
    assert "--require-hashes --only-binary :all:" in deps
    assert "/v1.22.1/parsers-linux-x86_64.tar.zst" in deps
    assert deps.index("sha256sum --check") < deps.index("tar --zstd")
    upload = STEPS["context-upload"]
    assert upload["with"]["name"] == "review-context"
    assert upload["with"]["path"] == "review-context/bundle.md"
    assert "if" not in upload  # upload only after all build steps succeed
    assert STEPS["context-download"]["with"]["name"] == "review-context"
    assert STEPS["context-download"]["if"] == "needs.review_context.result == 'success'"
    assert STEPS["context-artifact"]["if"] == "steps.context-download.outcome == 'success'"
    for name in ("context-revision", "tooling-checkout", "context-tooling", "context-download", "context-artifact"):
        assert STEPS[name]["continue-on-error"] is True
    for job in JOBS.values():
        assert not any(step.get("uses", "").startswith("actions/cache@") for step in job["steps"])
    assert not any("graph_bundle_ts.py" in step.get("run", "") for step in keyed["steps"])
    action = next(step for step in keyed["steps"] if step["name"] == "Run sponsored PR-Agent")
    assert action["env"]["ARTIFACT_PATH"] == (
        "${{ steps.context-artifact.outcome == 'success' && steps.context-artifact.outputs.path || '' }}"
    )
    assert action["env"]["ARTIFACT_INSTRUCTIONS"] == (
        "${{ steps.context-artifact.outcome == 'success' && needs.prepare_review.outputs.instructions || '' }}"
    )
    assert action["env"]["ARTIFACTS__TARGET_TOOLS"] == '["pr_reviewer"]'
    assert action["env"]["ARTIFACTS__TARGET_COMMANDS"] == '["pr_reviewer"]'
    for tool in ("DESCRIBE", "REVIEW", "IMPROVE"):
        assert action["env"][f"GITHUB_ACTION_CONFIG__AUTO_{tool}"] == "true"
