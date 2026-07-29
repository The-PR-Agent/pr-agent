from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .contracts import ReviewResult

REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"api[_-]?key|token|authorization|cookie|password|credential|secret",
    re.IGNORECASE,
)
SENSITIVE_TEXT = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|password|cookie|secret)"
        r"\s*[:=]\s*)(\S+)"
    ),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b[A-Za-z]:\\(?:Users|Documents|Temp)\\[^\s\"']+"),
    re.compile(r"/(?:home|Users|tmp)/[^\s\"']+"),
)
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def save_artifact(result: ReviewResult, directory: str | Path) -> Path:
    target_directory = Path(directory)
    target_directory.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["errors"] = [
        _redact_text(error[:500])
        for error in result.errors[:20]
    ]
    payload = _redact(payload)
    filename = _safe_filename(result.run_id)
    target = target_directory / f"{filename}.json"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}-",
        suffix=".tmp",
        dir=target_directory,
    )
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return target


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED
                if SENSITIVE_KEY.search(str(key))
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    result = value
    for pattern in SENSITIVE_TEXT:
        if pattern.groups >= 2:
            result = pattern.sub(
                lambda match: f"{match.group(1)}{REDACTED}",
                result,
            )
        else:
            result = pattern.sub(REDACTED, result)
    return result


def _safe_filename(run_id: str) -> str:
    if SAFE_FILENAME.fullmatch(run_id):
        return run_id
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
