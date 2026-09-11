import json

from pr_agent.algo.run_details import init_run_details, record_ai_call
from pr_agent.algo.run_ledger import write_ledger
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_reviewer import PRReviewer


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120
    prompt_tokens_details = type("D", (), {"cached_tokens": 60})()


class _UsageWithMismatchedTotal:
    """A provider that reports a `total_tokens` that is not prompt + completion (e.g. it
    counts reasoning/tool tokens neither field reports). `add_token_usage` trusts the
    provider's total in this case, so `CallRecord.total_tokens` must match it exactly,
    not silently fall back to prompt + completion."""

    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 150
    prompt_tokens_details = type("D", (), {"cached_tokens": 0})()


def test_record_ai_call_appends_call_record():
    details = init_run_details()
    record_ai_call(_Usage(), model="m", cost_usd="0.01", stage="review", chunk_index=2,
                   sample_index=0, files=["a.py", "b.py"], latency_ms=812)

    assert len(details.calls) == 1
    call = details.calls[0]
    assert (call.stage, call.chunk_index, call.files) == ("review", 2, ("a.py", "b.py"))
    assert (call.prompt_tokens, call.cached_tokens, call.completion_tokens) == (100, 60, 20)
    assert details.total_tokens == 120


def test_call_record_total_tokens_matches_run_details_when_provider_total_diverges(tmp_path):
    """The token-sum invariant: summing `total_tokens` over every ledger row must equal
    `RunDetails.total_tokens` for the same run, even when a provider's reported total is not
    prompt + completion. Goes through the JSONL round trip, not just the in-memory records,
    so a bug that dropped the field from `write_ledger` (leaving a reader to reconstruct it
    as prompt + completion) would fail this the same way it would fail in production."""
    details = init_run_details()
    record_ai_call(_UsageWithMismatchedTotal(), model="m", stage="review")
    record_ai_call(_UsageWithMismatchedTotal(), model="m", stage="review")

    assert details.calls[0].total_tokens == 150  # the provider's total, not 100 + 20 = 120

    path = tmp_path / "ledger.jsonl"
    write_ledger(details, str(path), run_id="r1", tool="review")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert sum(row["total_tokens"] for row in rows) == details.total_tokens


def test_write_ledger_returns_zero_and_creates_nothing_when_there_are_no_calls(tmp_path):
    details = init_run_details()
    path = tmp_path / "subdir" / "ledger.jsonl"

    rows = write_ledger(details, str(path), run_id="r1", tool="review")

    assert rows == 0
    assert not path.exists()
    assert not path.parent.exists()


def test_write_ledger_emits_one_jsonl_row_per_call(tmp_path):
    details = init_run_details()
    record_ai_call(_Usage(), model="m", stage="review", chunk_index=0)
    record_ai_call(_Usage(), model="m", stage="verify", chunk_index=None)

    path = tmp_path / "ledger.jsonl"
    rows = write_ledger(details, str(path), run_id="r1", tool="review")

    lines = path.read_text().splitlines()
    assert rows == 2 and len(lines) == 2
    first = json.loads(lines[0])
    assert first["run_id"] == "r1" and first["stage"] == "review" and first["prompt_tokens"] == 100
    assert sum(json.loads(line)["total_tokens"] for line in lines) == details.total_tokens


def test_ledger_run_id_prefers_commit_url_then_config_then_minted_fallback():
    """Ledger rows must be attributable even without a hosting platform. Plain-diff and
    local providers return no commit URL, so `_ledger_run_id` falls back to an explicit
    `config.run_ledger_run_id`, and failing that mints one stable id per reviewer."""
    reviewer = PRReviewer.__new__(PRReviewer)

    reviewer._review_run_id = lambda: "https://host/org/repo/commit/abc"
    assert reviewer._ledger_run_id() == "https://host/org/repo/commit/abc"

    reviewer._review_run_id = lambda: ""
    get_settings().set("config.run_ledger_run_id", "  eval-block-rush-1  ")
    assert reviewer._ledger_run_id() == "eval-block-rush-1"

    get_settings().set("config.run_ledger_run_id", "")
    minted = reviewer._ledger_run_id()
    assert minted.startswith("local-") and len(minted) > len("local-")
    assert reviewer._ledger_run_id() == minted

    other = PRReviewer.__new__(PRReviewer)
    other._review_run_id = lambda: ""
    assert other._ledger_run_id() != minted
