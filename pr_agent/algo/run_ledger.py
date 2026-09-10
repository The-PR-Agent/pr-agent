"""Append one JSON line per model call so tokens are attributable to a stage and a file set."""
from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from pr_agent.algo.run_details import RunDetails


def write_ledger(details: RunDetails, path: str, *, run_id: str, tool: str) -> int:
    """Append one JSON line per recorded call in `details.calls` to `path`. Returns rows written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with target.open("a", encoding="utf-8") as fh:
        for call in details.calls:
            row = asdict(call)
            row["files"] = list(call.files)
            row["cost_usd"] = str(call.cost_usd) if isinstance(call.cost_usd, Decimal) else None
            row.update(run_id=run_id, tool=tool)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            rows += 1
    return rows
