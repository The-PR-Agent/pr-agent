from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from assessment.ai_reviewer.agent_loop import review
from assessment.ai_reviewer.contracts import ReviewRequest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--repo-root", default=".")
    arguments = parser.parse_args()

    repo_root = Path(arguments.repo_root).resolve(strict=True)
    fixture = Path(arguments.fixture).resolve(strict=True)
    relative = fixture.relative_to(repo_root).as_posix()
    source = fixture.read_text(encoding="utf-8")
    line_count = max(1, len(source.splitlines()))
    os.environ["AI_REVIEW_MODEL"] = arguments.model
    request = ReviewRequest(
        repository="local/agent-smoke",
        pr_number=1,
        base_sha="a" * 40,
        head_sha="b" * 40,
        title="Agent smoke test",
        body="Review the synthetic fixture for a concrete defect.",
        diff=_synthetic_diff(relative, source),
        changed_lines={relative: frozenset(range(1, line_count + 1))},
        changed_files=(relative,),
    )
    result = review(
        request,
        [],
        repo_root,
        time.monotonic() + 300.0,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.status == "success" and result.findings else 1


def _synthetic_diff(path: str, source: str) -> str:
    lines = source.splitlines()
    added = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{added}\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
