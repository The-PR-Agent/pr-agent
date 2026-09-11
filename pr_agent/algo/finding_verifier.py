"""Check each finding's premise against the files it depends on before publishing it.

The audit's four false positives were all guesses about an invariant in a file outside the diff.
Hedge words are logged as a signal; every finding within budget is verified regardless of wording.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Literal

from jinja2 import Environment, StrictUndefined

from pr_agent.log import get_logger

HEDGE_RE = re.compile(
    r"\b(unless|assuming|depending on (?:how|whether)|if .{0,80}?\b(?:is|does|are|still|not)\b|may (?:still|not))\b",
    re.IGNORECASE,
)
_FILE_REF_RE = re.compile(r"`?([\w./-]+\.(?:dart|py|ts|tsx|js|kt|swift|go|java|rb|rs))`?")
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_DEFAULT_USER_TEMPLATE = "{{ finding }}\n\n{{ context }}"

Status = Literal["confirmed", "refuted", "unverified"]


@dataclass(frozen=True)
class Verdict:
    status: Status
    evidence: str = ""
    reason: str = ""


def referenced_pr_files(issue: dict, pr_files: Iterable[str]) -> list[str]:
    own = str(issue.get("relevant_file", ""))
    text = f"{issue.get('issue_header', '')} {issue.get('issue_content', '')}"
    names = {os.path.basename(m) for m in _FILE_REF_RE.findall(text)}
    return [p for p in pr_files if os.path.basename(p) in names and p != own]


def build_verification_context(
    issue: dict, own_file_text: str, other_files: dict[str, str], max_chars: int = 40000
) -> str:
    sections = [(str(issue.get("relevant_file", "")), own_file_text)] + list(other_files.items())
    budget_each = max(200, max_chars // max(1, len(sections)))
    parts = []
    for path, text in sections:
        body = text if len(text) <= budget_each else text[:budget_each] + "\n... [truncated]"
        parts.append(f"### {path}\n```\n{body}\n```")
    return "\n\n".join(parts)


def parse_verdict(text: str) -> Verdict:
    match = _JSON_RE.search(text or "")
    if not match:
        return Verdict("unverified", reason="unparsable verdict")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Verdict("unverified", reason="unparsable verdict")
    status = str(data.get("status", "")).lower()
    if status not in ("confirmed", "refuted", "unverified"):
        status = "unverified"
    return Verdict(
        status,  # type: ignore[arg-type]
        evidence=str(data.get("evidence", ""))[:500],
        reason=str(data.get("reason", ""))[:500],
    )


async def verify_findings(
    issues: list[dict],
    fetch_file: Callable[[str], Awaitable[str]],
    pr_files: Iterable[str],
    call_model: Callable[..., Awaitable[str]],
    max_findings: int,
    system_prompt: str = "",
    user_template: str = "",
) -> list[tuple[dict, Verdict]]:
    pr_files = list(pr_files)
    results: list[tuple[dict, Verdict]] = []
    env = Environment(undefined=StrictUndefined)
    template = env.from_string(user_template or _DEFAULT_USER_TEMPLATE)
    for index, issue in enumerate(issues):
        if index >= max_findings:
            results.append((issue, Verdict("unverified", reason="verification budget exhausted")))
            continue
        own_path = str(issue.get("relevant_file", ""))
        hedged = bool(HEDGE_RE.search(str(issue.get("issue_content", ""))))
        get_logger().info(
            f"Verifying finding hedged={hedged} header={issue.get('issue_header', '')}"
        )
        own = await fetch_file(own_path)
        referenced = referenced_pr_files(issue, pr_files)
        others = {p: await fetch_file(p) for p in referenced}
        context = build_verification_context(issue, own or "", others)
        user = template.render(
            finding=json.dumps(issue, ensure_ascii=False),
            context=context,
            hedged=hedged,
        )
        files = [p for p in [own_path, *referenced] if p]
        try:
            try:
                raw = await call_model(system_prompt, user, files=files)
            except TypeError:
                raw = await call_model(system_prompt, user)
            verdict = parse_verdict(raw)
        except Exception as exc:  # a verifier failure must never lose a finding
            verdict = Verdict("unverified", reason=f"verifier error: {type(exc).__name__}")
        results.append((issue, verdict))
    return results
