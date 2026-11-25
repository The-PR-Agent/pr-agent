"""Guard the docs-only Docker skip against accidentally hiding source changes."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/build-and-test.yaml"
JOB = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["build-and-test"]
STEPS = {step.get("id", step.get("name")): step for step in JOB["steps"]}


def test_docs_only_classifier_excludes_only_documentation_pages():
    step = STEPS["docs_pages"]
    script = step["run"]
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "HEAD^2" in script and "git diff --no-renames --quiet HEAD^1 HEAD -- ." in script
    assert "echo 'docs_only=true' >> \"$GITHUB_OUTPUT\"" in script
    assert re.findall(r"'(:\(top,exclude,glob\)[^']+)'", script) == [
        ":(top,exclude,glob)docs/docs/**/*.md",
        ":(top,exclude,glob)docs/docs/**/*.mdx",
    ]
    assert len(re.findall(r":\(|:[!^]", script)) == 2


def test_docs_only_preserves_targeted_checks_and_full_non_docs_validation():
    assert STEPS["checkout"]["with"]["fetch-depth"] == "${{ github.event_name == 'pull_request' && 2 || 1 }}"
    for name in ("Check uv.lock", "Test package artifacts"):
        assert "if" not in STEPS[name]

    docs_test = STEPS["Test documentation contracts"]
    assert docs_test["if"] == "steps.docs_pages.outputs.docs_only == 'true'"
    assert docs_test["run"].lstrip().startswith("PYTHONPATH=. uv run pytest -q")
    for name in ("test_docs_help_corpus.py", "test_docs_nav_parity.py",
                 "test_docs_urls.py", "test_config_reference.py", "test_command_descriptions.py",
                 "test_similar_issue_qdrant_collection.py", "test_unreachable_code.py",
                 "test_ci_docs_only_docker.py"):
        assert name in docs_test["run"]

    for name in ("dockerx", "build", "test", "Validate coverage report",
                 "Upload coverage to Codecov", "build-smoke", "smoke"):
        assert STEPS[name]["if"] == "steps.docs_pages.outputs.docs_only != 'true'"


@pytest.mark.parametrize(("case", "docs_only"), [
    ("markdown", True),
    ("mdx", True),
    ("source", False),
    ("mixed", False),
    ("workflow", False),
    ("root_readme", False),
    ("rename", False),
    ("directory_replacement", False),
])
def test_classifier_executes_on_merge_commits(tmp_path, case, docs_only):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.com")
    (tmp_path / "docs/docs").mkdir(parents=True)
    (tmp_path / "docs/docs/page.md").write_text("# Original\n")
    (tmp_path / "src.py").write_text("old = True\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feature")

    if case == "rename":
        git("mv", "src.py", "docs/docs/moved.md")
    elif case == "directory_replacement":
        old = tmp_path / "docs/docs/page.md"
        old.unlink()
        old.mkdir()
        (old / "source.py").write_text("source = True\n")
    else:
        changed = {
            "markdown": ["docs/docs/page.md"],
            "mdx": ["docs/docs/new.mdx"],
            "source": ["pr_agent/code.py"],
            "mixed": ["docs/docs/page.md", "pr_agent/code.py"],
            "workflow": [".github/workflows/ci.yml"],
            "root_readme": ["README.md"],
        }[case]
        for filename in changed:
            path = tmp_path / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed = True\n")

    git("add", "-A")
    git("commit", "-qm", "change")
    git("checkout", "-q", "main")
    git("merge", "--no-ff", "-qm", "merge", "feature")
    output = tmp_path / "github_output"
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", STEPS["docs_pages"]["run"]],
                            cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, "GITHUB_OUTPUT": str(output)})
    assert result.returncode == 0, result.stderr
    assert (output.exists() and "docs_only=true" in output.read_text()) is docs_only
