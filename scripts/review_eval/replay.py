"""Run pr-agent's /review once on a local checkout of a reviewed commit, with the PR's own title and description.

Started by run_eval.py in a fresh process per run (Dynaconf settings must not leak between runs), with the current
directory at the reviewed commit and PYTHONPATH at the pr-agent code under test. The local git provider writes the
review to REVIEW_EVAL_RUN_DIR/final.md; status.json records the model calls, token usage and finish reasons.

Env: REVIEW_EVAL_RUN_DIR, REVIEW_EVAL_CASE (JSON file with title, branch, description, tickets),
REVIEW_EVAL_TARGET_BRANCH (a local branch at the PR's merge base).
"""
import asyncio
import json
import os
import pathlib
import sys
import traceback

RUN = pathlib.Path(os.environ["REVIEW_EVAL_RUN_DIR"])
CASE = json.loads(pathlib.Path(os.environ["REVIEW_EVAL_CASE"]).read_text())
os.environ.update({
    "CONFIG__GIT_PROVIDER": "local",
    "CONFIG__PUBLISH_OUTPUT": "true",
    "CONFIG__USE_REPO_SETTINGS_FILE": "false",
    "LOCAL__REVIEW_PATH": str(RUN / "final.md"),
})

from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler  # noqa: E402
from pr_agent.commands import ticket_pr_compliance_check  # noqa: E402
from pr_agent.commands.pr_reviewer import PRReviewer  # noqa: E402
from pr_agent.git_providers.local_git_provider import LocalGitProvider  # noqa: E402

usage, finish = [], []
_record = LiteLLMAIHandler._record_completion_metadata
_chat = LiteLLMAIHandler.chat_completion


def record(response, model=None, display_model=None):
    u = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)

    def field(name):
        return (u.get(name) if isinstance(u, dict) else getattr(u, name, None)) or 0

    usage.append({"input": field("prompt_tokens"), "output": field("completion_tokens")})
    return _record(response, model, display_model)


async def chat(self, model, system, user, temperature=0.2, img_path=None):
    text, reason = await _chat(self, model, system, user, temperature, img_path)
    finish.append(reason)
    return text, reason


async def tickets(git_provider):
    return [dict(t) for t in CASE["tickets"]]


LiteLLMAIHandler._record_completion_metadata = staticmethod(record)
LiteLLMAIHandler.chat_completion = chat
# The local provider would use the commit sha as the title and commit messages as the description; a review sees
# the PR's real intent, so replay it as it was when the PR was opened.
LocalGitProvider.get_pr_title = lambda self: CASE["title"]
LocalGitProvider.get_pr_branch = lambda self: CASE["branch"]
LocalGitProvider.get_pr_description_full = lambda self: CASE["description"]
ticket_pr_compliance_check.extract_tickets = tickets


async def main():
    try:
        await PRReviewer(os.environ["REVIEW_EVAL_TARGET_BRANCH"]).run()
        return None
    except Exception:  # noqa: BLE001  recorded in status.json so the run counts as failed
        text = traceback.format_exc()[-4000:]
        for name, value in os.environ.items():  # status.json is uploaded: never let a provider error echo a key
            if name.endswith(("KEY", "TOKEN")) and len(value) >= 8:
                text = text.replace(value, "[redacted]")
        return text


error = asyncio.run(main())
final = RUN / "final.md"
(RUN / "status.json").write_text(json.dumps({
    "ok": error is None and final.exists() and final.stat().st_size > 0,
    "error": error, "usage": usage, "finish": finish}))
sys.exit(0)
