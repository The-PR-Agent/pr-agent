"""Guard the docs-only workflow against hiding source under root-doc paths."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/build-and-test.yaml"
DOCS_EXCLUSIONS = [
    ":(top,exclude,glob)docs/docs/**/*.md",
    ":(top,exclude,glob)docs/docs/**/*.mdx",
]


def test_docs_only_classifier_excludes_only_documentation_pages() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["classify_changes"]["steps"] if s.get("id") == "docs")
    exclusions = re.findall(r"'(:\([^']*exclude[^']*\)[^']+)'", step["run"])

    # Root README.md etc. can become directories containing Python source.
    assert exclusions == DOCS_EXCLUSIONS, "Only docs/docs Markdown/MDX paths may bypass native tests"
    magic_count = len(re.findall(r":\(|:[!^]", step["run"]))
    assert magic_count == len(DOCS_EXCLUSIONS), "Unexpected Git pathspec magic in docs-only classifier"
