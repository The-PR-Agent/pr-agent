from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALLOWED_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    action: str
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    title: str
    body: str
    same_repository: bool


def load_event(path: str | Path) -> PullRequestEvent:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub event must be a JSON object")
    action = str(payload.get("action", ""))
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"unsupported pull request action: {action}")
    repository = _required_text(
        "repository.full_name",
        payload["repository"]["full_name"],
    )
    pull_request = payload["pull_request"]
    if not isinstance(pull_request, dict):
        raise ValueError("pull_request must be an object")
    pr_number = int(pull_request["number"])
    if pr_number <= 0:
        raise ValueError("pull request number must be positive")
    head_repository = _required_text(
        "pull_request.head.repo.full_name",
        pull_request["head"]["repo"]["full_name"],
    )
    return PullRequestEvent(
        action=action,
        repository=repository,
        pr_number=pr_number,
        base_sha=_sha("base_sha", pull_request["base"]["sha"]),
        head_sha=_sha("head_sha", pull_request["head"]["sha"]),
        title=str(pull_request.get("title") or ""),
        body=str(pull_request.get("body") or ""),
        same_repository=head_repository == repository,
    )


def _required_text(name: str, value: Any) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def _sha(name: str, value: Any) -> str:
    text = str(value)
    if not SHA_PATTERN.fullmatch(text):
        raise ValueError(
            f"{name} must be a 40-character lowercase hex SHA"
        )
    return text
